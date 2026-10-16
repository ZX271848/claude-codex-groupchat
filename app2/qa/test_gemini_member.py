"""Offline tests for a third member (Gemini via agy). Never invokes a model.
  python -m unittest qa.test_gemini_member"""
import json
import os
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qa.test_coordinator import A, C, FakeLane, TEMP  # same module state and fakes as the pair tests
from conversation import Conversation
from routing import Classifier, chain_of, explicit_target, members_of, recipients_for

# The event sequence agy actually produced in qa/probe_agy2.py (turn A), trimmed to what the lane reads.
AGY_TURN = [
    {"step_index": 0, "state": "DONE", "step_type": "user_input"},
    {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "第一步：我将列出"},
    {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "当前工作目录。"},
    {"step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "\n"},
    {"step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
     "tool_info": {"name": "run_command", "parameters": {"CommandLine": "Get-ChildItem"}}},
    {"step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
     "tool_info": {"name": "run_command", "parameters": {"CommandLine": "Get-ChildItem"}}},
    {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "当前目录为空。\n\n第二步：新建 a.txt。"},
    {"step_index": 3, "state": "DONE", "step_type": "agent_response", "text_delta": "\n"},
    {"step_index": 4, "state": "ACTIVE", "step_type": "tool", "tool_name": "write_to_file",
     "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": "C:\\x\\a.txt"}}},
    {"step_index": 4, "state": "DONE", "step_type": "tool", "tool_name": "write_to_file",
     "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": "C:\\x\\a.txt"}}},
    {"step_index": 5, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "总结：已建好 a.txt。"},
    {"step_index": 5, "state": "DONE", "step_type": "agent_response", "text_delta": "\n"},
]


class GeminiRoomTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.room = {'dir': self.tmp.name, 'root': self.tmp.name, 'name': 'trio'}
        C.save_json(os.path.join(self.tmp.name, 'room.json'), {'name': 'trio', 'members': ['gpt', 'claude', 'gemini']})
        self.class_calls, self.target = [], 'gemini'

        def choose(room, events, availability=None):
            self.class_calls.append(availability)
            return self.target
        self.app = SimpleNamespace(classifier=SimpleNamespace(choose=choose))
        self.s = Conversation(self.app, self.room, FakeLane, A.line_text, lambda e: e.get('images', []),
                              lambda *a, **k: None, start=False)

    def tearDown(self):
        self.tmp.cleanup()

    def drain(self):
        for _ in range(60):
            try:
                event = self.s.inbox.get_nowait()
            except queue.Empty:
                return
            self.s.process(event)
        self.fail('event loop did not settle')

    def user(self, text, route='auto'):
        self.s.process({'kind': 'user', 'text': text, 'route': route, 'images': []})

    def finish(self, model, text=''):
        self.s.lane(model).finish(text)
        self.drain()

    def test_members_and_targets(self):
        self.assertEqual(self.s.members, ('gpt', 'claude', 'gemini'))
        self.assertEqual(set(self.s.lanes), {'gpt:work', 'claude:work', 'gemini:work'})
        trio = ('gpt', 'claude', 'gemini')
        self.assertEqual(explicit_target('@Gemini 写一版', 'auto', trio), 'gemini')
        self.assertEqual(explicit_target('@claude @gemini', 'auto', trio), 'claude+gemini')
        self.assertEqual(explicit_target('@gpt @claude @gemini', 'auto', trio), 'all')
        self.assertEqual(explicit_target('@gpt @claude', 'auto', trio), 'both')
        self.assertEqual(chain_of('all', trio), ['gpt', 'claude', 'gemini'])
        self.assertEqual(chain_of('gemini+gpt', trio), ['gpt', 'gemini'])
        self.assertIn('gemini', recipients_for(trio))

    def test_pair_rooms_ignore_gemini(self):
        pair = members_of({'dir': os.path.join(self.tmp.name, 'nowhere')})
        self.assertEqual(pair, ('gpt', 'claude'))
        self.assertIsNone(explicit_target('@gemini 你好'))
        self.assertEqual(explicit_target('@gemini 你好', 'gpt'), 'gpt')
        self.assertEqual(explicit_target('hi', 'all'), 'both')
        self.assertNotIn('gemini', recipients_for(pair))

    def test_explicit_gemini_skips_classifier(self):
        self.user('@Gemini 把这段提示写一版')
        self.assertEqual(self.class_calls, [])
        self.assertEqual(len(self.s.lane('gemini').asks), 1)
        prompt = self.s.lane('gemini').asks[0][0]
        self.assertIn('你是 GEMINI 的持续工作会话', prompt)
        self.assertIn('群聊成员：GPT、Claude、Gemini', prompt)

    def test_classifier_sees_all_three(self):
        self.user('随便问一个新问题')
        self.assertEqual(self.class_calls[-1], {'gpt': 'idle', 'claude': 'idle', 'gemini': 'idle'})
        self.assertEqual(len(self.s.lane('gemini').asks), 1)

    def test_all_answers_in_order_each_reading_the_ones_before(self):
        self.user('你们各自说说看法', 'all')
        self.assertEqual(len(self.s.lane('gpt').asks), 1)
        self.assertEqual(self.s.lane('claude').asks, [])
        self.finish('gpt', 'GPT 的看法')
        claude_prompt = self.s.lane('claude').asks[0][0]
        self.assertIn('GPT 的看法', claude_prompt)
        self.assertIn('第 2 位，请读 GPT 本次回答', claude_prompt)
        self.assertEqual(self.s.lane('gemini').asks, [])
        self.finish('claude', 'Claude 的看法')
        gemini_prompt = self.s.lane('gemini').asks[0][0]
        self.assertIn('GPT 的看法', gemini_prompt)
        self.assertIn('Claude 的看法', gemini_prompt)
        self.assertIn('第 3 位，请读 GPT、Claude 本次回答', gemini_prompt)
        self.finish('gemini', 'Gemini 的看法')
        self.assertEqual([j['status'] for j in self.s.jobs], ['completed'] * 3)

    def test_busy_gemini_queues_instead_of_inserting(self):
        self.user('先做第一件事', 'gemini')
        lane = self.s.lane('gemini')
        lane.insert = False  # GeminiLane.steer declines: agy has no mid-turn insertion
        self.user('再补一句', 'gemini')
        self.assertEqual(len(lane.asks), 1)
        self.assertEqual(len(self.s.waiting['gemini']), 1)
        self.finish('gemini', '第一件做完了')
        self.assertEqual(len(lane.asks), 2)
        self.assertIn('再补一句', lane.asks[1][0])

    def test_collaboration_request_to_gemini(self):
        self.s.set_mode('collaboration')
        self.user('Claude 你来', 'claude')
        self.finish('claude', '@Gemini 帮我按这个要求写一版提示')
        self.assertEqual(len(self.s.lane('gemini').asks), 1)
        self.assertIn('帮我按这个要求写一版提示', self.s.lane('gemini').asks[0][0])
        self.finish('gemini', '写好了')
        self.assertEqual(len(self.s.lane('claude').asks), 2)  # the reply returns to Claude


class DefaultMembersAndJoinTest(unittest.TestCase):
    """Rooms without their own list get everyone whose program is installed; a late member starts at a recent point."""

    def setUp(self):
        import routing
        self.routing, self.saved = routing, routing.DEFAULT_MEMBERS
        routing.DEFAULT_MEMBERS = routing.default_members
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.room = {'dir': self.tmp.name, 'root': self.tmp.name, 'name': 'old room'}

    def tearDown(self):
        self.routing.DEFAULT_MEMBERS = self.saved
        self.tmp.cleanup()

    def test_default_follows_installed_programs(self):
        from unittest.mock import patch
        import backends
        with patch.object(backends, 'find', lambda name, fresh=False: 'agy.exe' if name == 'agy' else None):
            self.assertEqual(members_of(self.room), ('gpt', 'claude', 'gemini'))
        with patch.object(backends, 'find', lambda name, fresh=False: None):
            self.assertEqual(members_of(self.room), ('gpt', 'claude'))
        C.save_json(os.path.join(self.tmp.name, 'room.json'), {'members': ['gpt', 'claude']})
        with patch.object(backends, 'find', lambda name, fresh=False: 'agy.exe'):
            self.assertEqual(members_of(self.room), ('gpt', 'claude'))  # a room's own list wins

    def test_late_member_reads_from_a_recent_point(self):
        import conversation
        for i in range(400):  # an existing chat of ~120k characters
            C.append(self.room, 'user' if i % 2 == 0 else 'claude', f'第 {i} 条 ' + '内容' * 150, 'client',
                     **({} if i % 2 == 0 else {'role': 'work'}))
        C.set_cursor(self.room, 'lane-claude-work', 400)
        C.set_cursor(self.room, 'lane-gpt-work', 400)
        C.save_json(os.path.join(self.tmp.name, 'room.json'), {'members': ['gpt', 'claude', 'gemini']})
        app = SimpleNamespace(classifier=SimpleNamespace(choose=lambda *a, **k: 'gemini'))
        s = Conversation(app, self.room, FakeLane, A.line_text, lambda e: [], lambda *a, **k: None, start=False)
        s.process({'kind': 'user', 'text': '@Gemini 看看', 'route': 'auto', 'images': []})
        join = C.load_json(os.path.join(self.tmp.name, 'gemini-join.json'), {})
        self.assertTrue(100 < join.get('from_seq', 0) < 400, join)  # ~330 characters a line: about 210 recent lines
        prompt = s.lane('gemini').asks[0][0]
        self.assertLess(len(prompt), conversation.JOIN_CHARS + 5000)
        self.assertIn('@Gemini 看看', prompt)
        self.assertNotIn('[消息 1]\n', prompt)

    def test_new_room_member_reads_everything_without_a_join_note(self):
        C.append(self.room, 'user', '你好', 'client')
        C.save_json(os.path.join(self.tmp.name, 'room.json'), {'members': ['gpt', 'claude', 'gemini']})
        app = SimpleNamespace(classifier=SimpleNamespace(choose=lambda *a, **k: 'gemini'))
        s = Conversation(app, self.room, FakeLane, A.line_text, lambda e: [], lambda *a, **k: None, start=False)
        s.process({'kind': 'user', 'text': '@Gemini 在吗', 'route': 'auto', 'images': []})
        self.assertIn('你好', s.lane('gemini').asks[0][0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'gemini-join.json')))


class PairRoomGeminiRequestTest(unittest.TestCase):
    def test_request_to_a_non_member_is_ignored(self):
        tmp = tempfile.TemporaryDirectory(dir=TEMP.name)
        room = {'dir': tmp.name, 'root': tmp.name, 'name': 'pair'}
        app = SimpleNamespace(classifier=SimpleNamespace(choose=lambda *a, **k: 'claude'))
        s = Conversation(app, room, FakeLane, A.line_text, lambda e: [], lambda *a, **k: None, start=False)
        s.set_mode('collaboration')
        s.process({'kind': 'user', 'text': 'hi', 'route': 'claude', 'images': []})
        s.lane('claude').finish('@Gemini 你也看看')
        while not s.inbox.empty():
            s.process(s.inbox.get_nowait())
        self.assertNotIn('gemini:work', s.lanes)
        self.assertFalse(any(j.get('model') == 'gemini' for j in s.jobs))
        tmp.cleanup()


class GeminiLaneParsingTest(unittest.TestCase):
    """The real GeminiLane on the event sequence agy produced: notes before tools, the last text as the answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.notes, self.partials, self.done = [], [], []
        rs = SimpleNamespace(room={'dir': self.tmp.name, 'root': self.tmp.name, 'name': 'g'}, app=None,
                             note=lambda m, t: self.notes.append(t), partial=lambda m, t: self.partials.append(t),
                             enqueue=lambda *a, **k: None)
        rs.app = SimpleNamespace(gemini_cfg={'model': 'gemini-3.8-flash', 'effort': 'low', 'perm': 'skip'},
                                 live_end=lambda *a, **k: None, activity=lambda *a, **k: None)
        self.lane = A.GeminiLane(rs, 'work')
        self.lane.busy, self.lane.on_done = True, lambda text, stopped: self.done.append((text, stopped))

    def tearDown(self):
        self.tmp.cleanup()

    def test_turn(self):
        for step in AGY_TURN:
            self.lane.step(step)
        self.lane.result({'status': 'SUCCESS', 'response': 'ignored: it repeats every note'})
        self.assertEqual(self.notes, ['第一步：我将列出当前工作目录。', '当前目录为空。\n\n第二步：新建 a.txt。'])
        self.assertEqual(self.done, [('总结：已建好 a.txt。', False)])
        self.assertFalse(self.lane.busy)
        summary = [e for e in C.read_after(self.lane.room, 0) if e.get('kind') == 'tool_summary']
        self.assertEqual(len(summary), 1)
        self.assertIn('Gemini 工具记录', summary[0]['text'])
        self.assertIn('write_to_file', summary[0]['text'])

    def test_failure_is_reported(self):
        self.lane.result({'status': 'ERROR', 'error': 'quota exhausted'})
        self.assertEqual(self.done, [('', False)])
        self.assertIn('quota exhausted', self.lane.error)

    def test_preamble_carries_rules_philosophy_and_join_point(self):
        C.save_json(os.path.join(self.tmp.name, 'gemini-join.json'), {'from_seq': 42})
        rules = os.path.join(self.tmp.name, 'AGENTS.md')  # stands in for the user's ~/.codex/AGENTS.md
        with open(rules, 'w', encoding='utf-8') as stream:
            stream.write('# 用户自己的全局指令')
        saved, A.PHILOSOPHY = A.PHILOSOPHY, [rules, os.path.join(self.tmp.name, 'missing.md')]
        try:
            text = A.gemini_preamble(self.lane.room)
        finally:
            A.PHILOSOPHY = saved
        self.assertIn('Gemini 的持续工作会话', text)
        self.assertIn('用户自己的全局指令', text)
        self.assertIn('#42', text)
        self.assertIn('channel.jsonl', text)

    def test_trio_classifier_prompt_exists(self):
        c = Classifier(lambda: 'claude.exe', Path(A.PROMPTS) / 'coordinator-memory.md', None, None)
        path = c.prompt_for(('gpt', 'claude', 'gemini'))
        self.assertTrue(path.exists(), path)
        self.assertIn('gemini', path.read_text(encoding='utf-8'))
        self.assertEqual(c.prompt_for(('gpt', 'claude')).name, 'coordinator-memory.md')


if __name__ == '__main__':
    unittest.main()
