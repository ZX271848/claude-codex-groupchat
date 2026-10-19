"""Offline protocol/transport tests. Never touches a live room or invokes a model."""
from pathlib import Path
import importlib.util
import json
import os
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix='coordinator-tests-')
os.environ['GROUPCHAT2_STATE'] = TEMP.name
os.environ['CODEX_BRIDGE_DIR'] = TEMP.name
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('coordinator_app', ROOT/'app.py')
A = importlib.util.module_from_spec(spec); spec.loader.exec_module(A)
from conversation import Conversation
from routing import chat_input, explicit_target, Classifier
import routing
routing.DEFAULT_MEMBERS = routing.PAIR  # these tests describe the original pair; rooms with Gemini: test_gemini_member.py
from messages import split_output
from history import context_event, parse_front, public_line, public_event
C = A.C


class FakeLane:
    def __init__(self, rs, model):
        self.rs, self.model = rs, model
        self.key, self.role = model+':work', 'work'
        self.busy = self.quiet = False
        self.error = None
        self.asks, self.steers = [], []
        self.accept, self.insert = True, True
        self.ready, self.p = True, None
    def state(self): return 'busy' if self.busy else 'idle'
    def ask(self, text, quiet=False, on_done=None, images=()):
        if not self.accept:
            self.error = 'offline'; return False
        self.error = None
        self.busy, self.quiet, self.callback = True, quiet, on_done
        self.asks.append((text, images)); return True
    def steer(self, text, images=()):
        if not self.insert: return False
        self.steers.append((text, images)); return True
    def finish(self, text='', error=None, stopped=False):
        self.busy = False; self.error = error
        callback, self.callback = self.callback, None
        if callback: callback(text, stopped)
    def stop_turn(self):
        if self.busy: self.finish(stopped=True)
        return True


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.room = {'dir': self.tmp.name, 'root': self.tmp.name, 'name':'test'}
        self.class_calls = []
        self.class_availability = []
        self.target = 'gpt'
        def choose(room, events, availability=None):
            self.class_availability.append(availability)
            self.class_calls.append(events); return self.target
        self.app = SimpleNamespace(classifier=SimpleNamespace(choose=choose))
        self.audit = []
        self.s = Conversation(self.app, self.room, FakeLane, A.line_text, lambda e:e.get('images', []),
                              lambda *a, **k:self.audit.append((a,k)), start=False)
    def tearDown(self): self.tmp.cleanup()
    def drain(self):
        for _ in range(40):
            try: event = self.s.inbox.get_nowait()
            except queue.Empty:return
            self.s.process(event)
        self.fail('event loop did not settle')
    def user(self,text,route='auto',images=()):
        self.s.process({'kind':'user','text':text,'route':route,'images':list(images)})
    def finish(self,model,text='',**kwargs):
        self.s.lane(model).finish(text,**kwargs);self.drain()
    def rows(self): return C.read_after(self.room,0)

    def test_explicit_target_bypasses_classifier(self):
        self.user('@Claude 看实验')
        self.assertEqual(self.class_calls,[])
        self.assertEqual(len(self.s.lane('claude').asks),1)
        self.assertEqual(self.s.lane('gpt').asks,[])
        self.assertEqual(explicit_target('@GPT hi', 'claude'),'gpt')
        self.assertEqual(explicit_target('@GPT @Claude'), 'both')

    def test_one_classifier_call_direct_to_worker(self):
        self.user('继续刚才的修改')
        self.assertEqual(len(self.class_calls),1)
        self.assertEqual(set(self.s.lanes),{'gpt:work','claude:work'})
        self.assertEqual(len(self.s.lane('gpt').asks),1)

    def test_busy_worker_status_reaches_router_without_forcing_recipient(self):
        self.user('deploy the training change', 'claude')
        self.target='gpt';self.user('What is mid-training?')
        self.assertEqual(self.class_availability[-1], {'gpt':'idle', 'claude':'busy'})
        self.assertEqual(len(self.s.lane('gpt').asks),1)
        self.assertEqual(self.s.lane('claude').steers,[])
        self.target='claude';self.user('Keep the optimizer state')
        self.assertEqual(len(self.s.lane('claude').steers),1)

    def test_native_worker_busy_is_visible_without_dispatch_job(self):
        self.s.lane('claude').busy=True
        self.user('a new independent question')
        self.assertEqual(self.class_availability[-1]['claude'],'busy')

    def test_both_waits_for_gpt_and_includes_answer(self):
        self.target='both';self.user('你们怎么看？')
        self.assertEqual(self.s.mode,'normal')
        self.assertEqual(self.s.lane('claude').asks,[])
        self.finish('gpt','GPT 的具体回答')
        self.assertIn('GPT 的具体回答',self.s.lane('claude').asks[0][0])
        self.assertIn('你们怎么看',self.s.lane('claude').asks[0][0])
        self.finish('claude','Claude 补充')
        self.assertEqual(len(self.s.lane('gpt').asks),1)
        self.assertEqual(len(self.class_calls),1)

    def test_both_busy_gpt_inserts_then_still_orders_claude(self):
        self.user('原任务','gpt');self.user('双方回答','both')
        self.assertEqual(len(self.s.lane('gpt').steers),1)
        self.assertEqual(self.s.lane('claude').asks,[])
        self.finish('gpt','按新消息回答')
        self.assertEqual(len(self.s.lane('claude').asks),1)

    def test_user_insertion_into_native_turn_has_a_receipt_and_ordered_followup(self):
        lane=self.s.lane('gpt');lane.busy=True
        self.user('双方看看最新结果','both')
        self.assertEqual(len(lane.steers),1)
        self.assertEqual(self.s.jobs[0]['status'],'running')
        self.assertIsNone(self.s.active['gpt'])
        self.assertEqual(len(self.s.ambient_jobs['gpt']),1)
        self.s.process({'kind':'intermediate_complete','model':'gpt','text':'原来的监控报告'})
        self.assertEqual(self.s.lane('claude').asks,[])
        lane.busy=False
        self.s.process({'kind':'external_complete','model':'gpt','text':'这次问题的回答'})
        self.assertEqual(self.s.jobs[0]['status'],'completed')
        self.assertEqual(self.s.ambient_jobs['gpt'],[])
        self.assertIn('这次问题的回答',self.s.lane('claude').asks[-1][0])

    def test_native_turn_failure_marks_inserted_job_retryable(self):
        self.s.lane('claude').busy=True
        self.user('新增要求','claude')
        self.s.lane('claude').busy=False
        self.s.process({'kind':'external_complete','model':'claude','text':'','error':'lost connection'})
        self.assertEqual(self.s.jobs[0]['status'],'failed')
        self.s.retry(self.s.jobs[0]['id'])
        self.assertIn('新增要求',self.s.lane('claude').asks[-1][0])

    def test_native_turn_folds_user_insertions_and_settles_dispatches(self):
        self.app.live_end=lambda *a:None
        lane=A.ClaudeLane(self.s,'work')
        self.s.lanes['claude:work']=lane
        lane.p=SimpleNamespace(poll=lambda:None)
        sent=[];lane.write=sent.append
        def stdout():
            yield json.dumps({'type':'stream_event','event':{'type':'message_start'}}).encode()
            self.user('p06跑完了，先算胜率','claude')
            self.user('补充一个读法','claude')
            self.assertEqual(len(sent),2)
            yield json.dumps({'type':'result','result':'两个要求已在本轮一并回答',
                              'queued_turn_count':0}).encode()
        lane.read(SimpleNamespace(stdout=stdout()));self.drain()
        self.assertFalse(lane.busy)
        self.assertEqual([j['status'] for j in self.s.jobs],['completed','completed'])
        self.assertEqual(self.s.ambient_jobs['claude'],[])

    def test_failed_first_response_never_starts_second(self):
        self.user('你们看一下','both')
        self.finish('gpt',error='connection lost')
        self.assertEqual(self.s.lane('claude').asks,[])
        self.assertEqual(self.s.jobs[0]['status'],'failed')
        self.s.retry(self.s.jobs[0]['id']);self.finish('gpt','已恢复')
        self.assertEqual(len(self.s.lane('claude').asks),1)

    def test_user_insertion_does_not_flush_unrelated_history(self):
        self.user('研究A','gpt')
        C.append(self.room,'claude','无关笔记','client',role='working',visibility='internal')
        self.user('补充要求','gpt')
        self.assertIn('补充要求',self.s.lane('gpt').steers[-1][0])
        self.assertNotIn('无关笔记',self.s.lane('gpt').steers[-1][0])
        self.assertEqual(self.s.lane('claude').asks+self.s.lane('claude').steers,[])
        self.finish('gpt','完成')
        self.user('下一条','gpt')
        text=self.s.lane('gpt').asks[-1][0]
        self.assertIn('无关笔记',text)
        self.assertNotIn('补充要求',text)

    def test_unselected_worker_reads_deferred_history_next_time(self):
        self.user('给 GPT 的问题','gpt');self.finish('gpt','GPT 已完成')
        self.assertEqual(C.get_cursor(self.room,'lane-claude-work'),0)
        self.user('现在问 Claude','claude')
        prompt=self.s.lane('claude').asks[-1][0]
        self.assertIn('给 GPT 的问题',prompt);self.assertIn('GPT 已完成',prompt)

    def test_notes_never_wake_idle_peer(self):
        self.s.set_mode('collaboration')
        self.user('只让 GPT 开工','gpt');self.s.note('gpt','技术笔记');self.drain()
        self.assertEqual(self.s.lane('claude').asks,[])
        self.assertFalse(public_line(self.rows()[-1]))

    def test_collaboration_notes_reach_busy_peer_and_stop_on_exit(self):
        self.s.set_mode('collaboration');self.user('A','gpt');self.user('B','claude')
        self.s.note('gpt','API ready');self.drain()
        self.assertIn('API ready',self.s.lane('claude').steers[-1][0])
        self.s.set_mode('normal');before=len(self.s.lane('claude').steers)
        self.s.note('gpt','new unrelated note');self.drain()
        self.assertEqual(len(self.s.lane('claude').steers),before)

    def test_explicit_peer_request_and_reply_return(self):
        self.s.set_mode('collaboration');self.user('G 工作','gpt')
        self.finish('gpt','@Claude 请检查接口')
        self.assertIn('请检查接口',self.s.lane('claude').asks[-1][0])
        self.finish('claude','检查结果：通过')
        self.assertEqual(len(self.s.lane('gpt').asks),2)
        self.assertIn('检查结果：通过',self.s.lane('gpt').asks[-1][0])
        self.finish('gpt','已采用')
        self.assertEqual(len(self.s.lane('claude').asks),1) # no wakeup loop

    def test_normal_mode_peer_at_is_deferred(self):
        self.user('检查一下','gpt');self.finish('gpt','@Claude 查查')
        self.assertEqual(self.s.lane('claude').asks,[])
        self.assertEqual(self.s.jobs[-1]['status'],'deferred')

    def test_exit_blocks_queued_peer_request(self):
        self.s.set_mode('collaboration');self.user('C 正在工作','claude')
        self.s.lane('claude').quiet=True
        self.user('G 工作','gpt');self.finish('gpt','@Claude 检查')
        self.assertEqual(len(self.s.waiting['claude']),1)
        self.s.set_mode('normal');self.finish('claude','C 工作完成')
        self.assertEqual(len(self.s.lane('claude').asks),1)
        self.assertEqual(self.s.jobs[-1]['status'],'deferred')

    def test_deferred_peer_request_does_not_block_a_queued_user(self):
        self.s.set_mode('collaboration');self.user('原任务','claude')
        self.s.lane('claude').quiet=True
        self.user('G 工作','gpt');self.finish('gpt','@Claude 检查')
        peer=self.s.jobs[-1]
        self.user('用户下一条','claude')
        self.s.set_mode('normal');self.finish('claude','原任务结束')
        self.assertEqual(peer['status'],'deferred')
        self.assertIn('用户下一条',self.s.lane('claude').asks[-1][0])

    def test_user_queued_during_history_loading_inserts_when_loading_finishes(self):
        C.append(self.room,'user','a'*40000,'client');C.append(self.room,'claude','b'*40000,'client',role='work')
        self.user('任务','gpt');self.user('插入要求','gpt')
        self.assertEqual(len(self.s.waiting['gpt']),1)
        self.finish('gpt','—')
        self.assertIn('插入要求',self.s.lane('gpt').steers[-1][0])
        self.assertEqual(self.s.waiting['gpt'],[])

    def test_progress_is_public_immediate_deduplicated_and_not_broadcast(self):
        self.s.set_mode('collaboration');self.user('跑实验','claude')
        text='<progress>第 2 轮，费用 1 元</progress>'
        self.s.partial('claude',text);self.drain()
        self.s.note('claude',text+'Technical');self.drain()
        self.finish('claude',text+'最终结果')
        rows=self.rows();self.assertEqual(sum(e.get('kind')=='progress' for e in rows),1)
        visible=[e['text'] for e in rows if public_line(e)]
        self.assertIn('第 2 轮，费用 1 元',visible);self.assertIn('最终结果',visible)
        self.assertNotIn('Technical',visible)
        self.assertEqual(self.s.lane('gpt').asks,[])

    def test_history_loading_never_republishes_progress(self):
        self.s.lane('gpt').quiet=True
        self.s.partial('gpt','<progress>历史进展</progress>');self.drain()
        self.assertEqual(self.rows(),[])

    def test_desktop_api_initializes_without_fronts_or_model_calls(self):
        app=A.App();app.room=self.room;app.route='both'
        app.sessions[self.s.rid]=self.s;self.s.app=app
        app.claude_options=lambda:{};app.gpt_options=lambda:{}
        C.append(self.room,'claude','公开结论<handoff target="gpt">旧交接</handoff>','client',role='front')
        C.append(self.room,'gpt','内部笔记','client',role='working')
        with patch.object(A.A1,'conversations',return_value=[]), \
             patch.object(A.A1.GPTEngine,'access',new=property(lambda _: 'test access')):
            state=A.Api(app).init()
        self.assertEqual(state['route'],'both')
        self.assertEqual(state['status']['mode'],'normal')
        self.assertEqual([x['text'] for x in state['lines']],['公开结论'])
        self.assertEqual(state['status']['models']['claude']['lanes'],{'work':'idle'})
        self.assertEqual(state['live'],{})
        self.assertIsNone(app.gpt['work'].p)
        A.Api(app).set_mode('collaboration');self.drain()
        self.assertEqual(self.s.mode,'collaboration')
        A.Api(app).send('直接请求','gpt');self.drain()
        self.assertIn('直接请求',self.s.lane('gpt').asks[-1][0])

    def test_failure_does_not_advance_cursor_and_can_retry(self):
        lane=self.s.lane('gpt');lane.accept=False
        self.user('不要丢掉','gpt');self.drain()
        self.assertEqual(C.get_cursor(self.room,'lane-gpt-work'),0)
        self.assertEqual(self.s.jobs[0]['status'],'failed')
        lane.accept=True;self.s.retry(self.s.jobs[0]['id'])
        self.assertIn('不要丢掉',lane.asks[-1][0])
        self.assertGreater(C.get_cursor(self.room,'lane-gpt-work'),0)

    def test_stopped_request_keeps_error_for_audit_but_is_not_connection_failure(self):
        self.user('需要处理的消息','claude')
        self.finish('claude',error='error_during_execution',stopped=True)
        job=self.s.jobs[0]
        self.assertEqual(job['status'],'interrupted')
        self.assertEqual(job['interruption'],'user_stop')
        self.assertEqual(job['error'],'error_during_execution')
        self.assertIn('已停止',self.rows()[-1]['text'])
        self.s.retry(job['id'])
        self.assertNotIn('error',job)
        self.assertNotIn('interruption',job)
        self.assertEqual(job['status'],'running')

    def test_historical_interruption_does_not_report_current_worker_as_failed(self):
        self.user('先前的工作','claude')
        self.finish('claude',error='error_during_execution',stopped=True)
        self.user('继续一下','claude')
        app=A.App();app.room=self.room
        app.sessions[self.s.rid]=self.s;self.s.app=app
        with patch.object(A.A1.GPTEngine,'access',new=property(lambda _: 'test access')):
            status=app.status_payload()
        self.assertTrue(status['models']['claude']['busy'])
        self.assertTrue(status['models']['claude']['ready'])
        self.assertIsNone(status['models']['claude']['error'])
        self.assertEqual(len(status['retry']),1)
        self.assertEqual(status['retry'][0]['status'],'interrupted')
        self.assertEqual(status['retry'][0]['interruption'],'user_stop')
        self.assertEqual(status['retry'][0]['seqs'],self.s.jobs[0]['seqs'])

    def test_classifier_failure_is_visible_retryable_and_no_default_model(self):
        def fail(*a, **kw):raise RuntimeError('invalid output')
        self.app.classifier.choose=fail;self.user('问题')
        self.assertEqual(self.s.jobs[0]['status'],'failed')
        self.assertEqual(self.s.lane('gpt').asks+self.s.lane('claude').asks,[])

    def test_large_history_loads_without_duplicate_or_rerunning_old_tasks(self):
        C.append(self.room,'user','a'*40000,'client');C.append(self.room,'claude','b'*40000,'client',role='work')
        self.user('现在这个问题','gpt')
        self.assertTrue(self.s.lane('gpt').quiet)
        self.finish('gpt','—')
        self.assertFalse(self.s.lane('gpt').quiet)
        self.assertIn('现在这个问题',self.s.lane('gpt').asks[-1][0])
        self.assertNotIn('a'*40000,self.s.lane('gpt').asks[-1][0])

    def test_restart_preserves_ids_and_requires_retry_for_uncertain_work(self):
        root=Path(self.room['dir'])
        C.save_json(root/'gpt-work.json',{'thread':'native-thread'})
        C.save_json(root/'claude-work.json',{'session':'native-session'})
        self.user('任务','gpt')
        recovered=Conversation(self.app,self.room,FakeLane,A.line_text,lambda e:[],lambda *a,**k:None,start=False)
        self.assertEqual(recovered.jobs[0]['status'],'interrupted')
        self.assertEqual(recovered.lane('gpt').asks,[])
        self.assertEqual(C.load_json(root/'gpt-work.json',{})['thread'],'native-thread')
        self.assertEqual(C.load_json(root/'claude-work.json',{})['session'],'native-session')

    def test_context_filters_old_controls_not_user_quotes(self):
        for text in ['suggest: claude','{"task_id":"x","task":"h","implementation":false}']:
            C.append(self.room,'claude',text,'client',role='front',kind='routing')
        C.append(self.room,'claude','内部派发','client',kind='routing',role='front',to=['claude'])
        self.user('我在讨论 suggest: claude','gpt')
        prompt=self.s.lane('gpt').asks[-1][0]
        self.assertIn('我在讨论 suggest: claude',prompt)
        self.assertNotIn('task_id',prompt);self.assertNotIn('内部派发',prompt)


class IsolationTest(unittest.TestCase):
    def test_environment_message_ids_and_controller_sources_are_unambiguous(self):
        text=A.line_text({'seq':42,'from':'controller','kind':'status','text':'连接失败'})
        self.assertIn('[消息 42]',text)
        self.assertIn('<controller>',text)
        self.assertNotIn('【用户】',text)

    def test_classifier_context_only_public_chat(self):
        rows=[{'from':'user','text':'问题'}, {'from':'gpt','role':'work','text':'公开回答'},
              {'from':'claude','role':'working','text':'秘密工作'},
              {'from':'claude','kind':'routing','text':'握手'},
              {'from':'controller','text':'任务登记'},
              {'from':'gpt','kind':'collaboration','visibility':'internal','text':'协同状态'},
              {'from':'user','kind':'mode','text':'开启协同模式'},
              {'from':'gpt','role':'front','text':'实质讨论\n<handoff target="claude">旧内部交接</handoff>'}]
        text=chat_input(rows)
        for item in ['问题','公开回答','实质讨论']:self.assertIn(item,text)
        for item in ['秘密工作','握手','任务登记','协同状态','开启协同模式','旧内部交接']:self.assertNotIn(item,text)

    def test_ui_public_projection_preserves_body_without_historical_handoffs(self):
        original={'from':'claude','role':'front','text':'正文\n<handoff target="gpt">交接细节</handoff>'}
        self.assertEqual(public_event(original)['text'],'正文')
        self.assertIn('交接细节',original['text']) # saved history is untouched

    def test_classifier_transport_is_isolated_and_fixed(self):
        output={'result':'gpt','usage':{},'total_cost_usd':0}
        records=[]
        c=Classifier(lambda:'claude.exe',ROOT/'prompts/coordinator-memory.md',lambda *a,**k:records.append(k),lambda:{'GROUPCHAT_OFF':'1'})
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory, patch('routing.RouterProcess') as run:
            run.return_value.request.return_value=output
            self.assertEqual(c.choose({'dir':directory},[{'seq':1,'from':'user','text':'hi'}]),'gpt')
        cmd=run.call_args.args[0]
        for flag in ['--safe-mode','--strict-mcp-config','--session-id','--autocompact']:self.assertIn(flag,cmd)
        self.assertNotIn('--no-session-persistence',cmd)
        self.assertEqual(cmd[cmd.index('--autocompact')+1],'100k')
        self.assertEqual(cmd[cmd.index('--tools')+1],'')
        self.assertEqual(cmd[cmd.index('--model')+1],'claude-opus-5-5')
        self.assertEqual(cmd[cmd.index('--effort')+1],'low')
        self.assertNotIn('--resume',cmd)
        self.assertNotIn('stderr',records[0])

    def test_control_examples_in_code_and_incomplete_tags_preserved(self):
        for text in ['正文 `<handoff>` 不应截断','未完 <handoff target="gpt">文字',
                     '`<collaborate target="claude">例子</collaborate>`',
                     '```xml\n<progress>例子</progress>\n```',
                     r'\<progress>转义例子</progress>',
                     '不完整 <progress>仍为正文']:
            public,req,progress=split_output(text)
            self.assertEqual((public,req,progress),(text,[],[]))
            if 'handoff' in text:self.assertEqual(parse_front(text),([],text))

    def test_current_environment_prompt_and_world_model_imports(self):
        text=(ROOT/'prompts/work.md').read_text(encoding='utf-8')
        for term in ['持续工作会话','普通模式','协同模式','<progress>','@GPT','没有另一份无工具']:
            self.assertIn(term,text)
        self.assertIn('--system-prompt-snapshot', (ROOT/'app.py').read_text(encoding='utf-8'))
        self.assertNotIn('self.front_turn(', (ROOT/'app.py').read_text(encoding='utf-8'))


class TransportTest(unittest.TestCase):
    def test_claude_inserted_response_not_lost_after_first_result(self):
        events=[]; finals=[]
        app=SimpleNamespace(live_end=lambda *a:None)
        session=SimpleNamespace(app=app,room={'dir':TEMP.name,'root':TEMP.name,'name':'test'},
                                enqueue=lambda kind,**kw:events.append((kind,kw)))
        lane=A.ClaudeLane(session,'work')
        lane.busy=True
        lane.on_done=lambda text,stopped:finals.append(text)
        p=SimpleNamespace(stdout=[json.dumps({'type':'result','result':'first','queued_turn_count':1}).encode(),
                                  json.dumps({'type':'result','result':'inserted answer','queued_turn_count':0}).encode()])
        lane.read(p)
        self.assertEqual(events[0][0],'intermediate_complete')
        self.assertEqual(events[0][1]['text'],'first')
        self.assertEqual(finals,['inserted answer'])
        self.assertFalse(lane.busy)

    def test_native_async_completion_is_not_dropped(self):
        events=[]
        app=SimpleNamespace(live_end=lambda *a:None)
        rs=SimpleNamespace(app=app,room={'dir':TEMP.name,'root':TEMP.name,'name':'test'},
                           enqueue=lambda kind,**kw:events.append((kind,kw)))
        lane=A.Lane(rs,'claude','work');lane.finish('监控更新')
        self.assertEqual(events,[('external_complete',{'model':'claude','text':'监控更新','stopped':False,'error':None})])

    def test_native_claude_turn_with_another_queued_turn(self):
        events=[]
        app=SimpleNamespace(live_end=lambda *a:None)
        rs=SimpleNamespace(app=app,room={'dir':TEMP.name,'root':TEMP.name,'name':'test'},
                           enqueue=lambda kind,**kw:events.append((kind,kw)))
        lane=A.ClaudeLane(rs,'work')
        def stdout():
            yield json.dumps({'type':'stream_event','event':{'type':'message_start'}}).encode()
            self.assertTrue(lane.busy)
            yield json.dumps({'type':'result','result':'monitor','queued_turn_count':1}).encode()
            self.assertTrue(lane.busy)
            yield json.dumps({'type':'result','result':'answer','queued_turn_count':0}).encode()
        lane.read(SimpleNamespace(stdout=stdout()))
        self.assertEqual([e[0] for e in events],['external_start','intermediate_complete','external_complete'])
        self.assertFalse(lane.busy)

    def test_many_queued_sends_can_coalesce_into_one_followup_result(self):
        events=[];finals=[]
        rs=SimpleNamespace(app=SimpleNamespace(live_end=lambda *a:None),
                           room={'dir':TEMP.name,'root':TEMP.name,'name':'test'},
                           enqueue=lambda kind,**kw:events.append((kind,kw)))
        lane=A.ClaudeLane(rs,'work');lane.busy=True
        lane.on_done=lambda text,stopped:finals.append(text)
        lane.read(SimpleNamespace(stdout=[json.dumps(e).encode() for e in [
            {'type':'result','result':'first','queued_turn_count':3},
            {'type':'result','result':'three requests answered together','queued_turn_count':0},
        ]]))
        self.assertEqual(len(events),1)
        self.assertEqual(finals,['three requests answered together'])
        self.assertFalse(lane.busy)

    def test_result_without_queue_metadata_is_a_completed_turn(self):
        finals=[]
        rs=SimpleNamespace(app=SimpleNamespace(live_end=lambda *a:None),
                           room={'dir':TEMP.name,'root':TEMP.name,'name':'test'})
        lane=A.ClaudeLane(rs,'work');lane.busy=True
        lane.on_done=lambda text,stopped:finals.append(text)
        lane.read(SimpleNamespace(stdout=[json.dumps({'type':'result','result':'done'}).encode()]))
        self.assertEqual(finals,['done']);self.assertFalse(lane.busy)

    def test_result_audit_failure_cannot_leave_lane_busy(self):
        finals=[]
        rs=SimpleNamespace(app=SimpleNamespace(live_end=lambda *a:None),
                           room={'dir':TEMP.name,'root':TEMP.name,'name':'test'})
        lane=A.ClaudeLane(rs,'work');lane.busy=True
        lane.on_done=lambda text,stopped:finals.append(text)
        with patch.object(A,'routing_log',side_effect=OSError('locked')):
            lane.read(SimpleNamespace(stdout=[json.dumps({'type':'result','result':'done','queued_turn_count':0}).encode()]))
        self.assertEqual(finals,['done']);self.assertFalse(lane.busy)

    def test_failed_result_never_waits_for_discarded_backlog(self):
        finals=[]
        rs=SimpleNamespace(app=SimpleNamespace(live_end=lambda *a:None),
                           room={'dir':TEMP.name,'root':TEMP.name,'name':'test'})
        lane=A.ClaudeLane(rs,'work');lane.busy=True
        lane.on_done=lambda text,stopped:finals.append(text)
        lane.read(SimpleNamespace(stdout=[json.dumps({'type':'result','is_error':True,
                    'result':'failed','queued_turn_count':2}).encode()]))
        self.assertEqual(finals,['failed']);self.assertFalse(lane.busy)

    def test_gpt_resume_uses_original_worker_and_current_instructions(self):
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory, patch.object(A.sys, 'platform', 'darwin'):
            room={'dir':directory,'root':directory,'name':'test'}
            C.save_json(Path(directory)/'gpt-work.json',{'thread':'original-worker'})
            C.save_json(Path(directory)/'gpt-front.json',{'thread':'unused-front'})
            calls=[]
            engine=SimpleNamespace(loaded=set(),lanes={},call=lambda method,params:calls.append((method,params)))
            rs=SimpleNamespace(room=room,app=SimpleNamespace(gpt={'work':engine}))
            lane=A.GPTLane(rs,'work');lane.open()
            self.assertEqual(lane.tid,'original-worker')
            self.assertEqual(calls[0][0],'thread/resume')
            self.assertIn('持续工作会话',calls[0][1]['developerInstructions'])
            self.assertNotIn('baseInstructions',calls[0][1])
            self.assertEqual(calls[0][1]['sandbox'], 'workspace-write')
            self.assertEqual(calls[0][1]['cwd'], directory)

    def test_gpt_new_worker_can_write_its_project_without_full_access(self):
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory, patch.object(A.sys, 'platform', 'darwin'):
            room={'dir':directory,'root':directory,'name':'test'}
            calls=[]
            def call(method, params):
                calls.append((method, params))
                return {'thread': {'id': 'new-worker'}} if method == 'thread/start' else {}
            engine=SimpleNamespace(loaded=set(),lanes={},call=call)
            rs=SimpleNamespace(room=room,app=SimpleNamespace(gpt={'work':engine}))
            lane=A.GPTLane(rs,'work');lane.open()
            self.assertEqual(lane.tid, 'new-worker')
            self.assertEqual(calls[0][1]['sandbox'], 'workspace-write')
            self.assertEqual(calls[0][1]['cwd'], directory)
            self.assertEqual(C.load_json(Path(directory)/'gpt-work.json', {})['thread'], 'new-worker')

    def test_windows_gpt_workers_preserve_configured_sandbox(self):
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory(dir=TEMP.name) as directory, \
                 patch.object(A.sys, 'platform', 'win32'):
                room={'dir':directory,'root':directory,'name':'test'}
                if resume:
                    C.save_json(Path(directory)/'gpt-work.json', {'thread':'saved-worker'})
                calls=[]
                def call(method, params):
                    calls.append((method, params))
                    return {'thread': {'id': 'new-worker'}} if method == 'thread/start' else {}
                engine=SimpleNamespace(loaded=set(),lanes={},call=call)
                rs=SimpleNamespace(room=room,app=SimpleNamespace(gpt={'work':engine}))
                A.GPTLane(rs,'work').open()
                self.assertEqual(calls[0][0], 'thread/resume' if resume else 'thread/start')
                self.assertNotIn('sandbox', calls[0][1])
                if resume:
                    self.assertNotIn('approvalPolicy', calls[0][1])

    def test_claude_resume_refreshes_saved_system_and_preserves_worker(self):
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory:
            room={'dir':directory,'root':directory,'name':'test'}
            C.save_json(Path(directory)/'claude-work.json',{'session':'original-worker'})
            cfg={'model':'claude-opus-5-5','effort':'high','perm':'bypassPermissions'}
            rs=SimpleNamespace(room=room,app=SimpleNamespace(claude_cfg=cfg))
            lane=A.ClaudeLane(rs,'work')
            with patch.object(A.A1,'claude_exe',return_value='claude.exe'),patch.object(A.subprocess,'Popen') as run,patch.object(A.threading,'Thread'):
                lane.start()
            command=run.call_args.args[0]
            self.assertEqual(command[command.index('--resume')+1],'original-worker')
            self.assertEqual(command[command.index('--system-prompt-snapshot')+1],'off')
            self.assertEqual(command[command.index('--effort')+1],'high')


if __name__=='__main__':
    try:unittest.main()
    finally:TEMP.cleanup()
