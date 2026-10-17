"""Single room state machine: classify once, deliver to persistent workers, record.

All scheduling mutations run on one queue. Transport reader threads enqueue events;
they never synchronously call another model. Display is independent of delivery.
"""
import json
import os
import queue
import threading
import time
import uuid

import common as C
from messages import split_output
from routing import PAIR, chain_of, explicit_target, members_of
from history import context_event

MODELS = ('gpt', 'claude')   # the original pair; a room may list more members in room.json
NAMES = {'gpt': 'GPT', 'claude': 'Claude', 'gemini': 'Gemini'}
SILENT = ('', '—', '-', '——', '…')
JOIN_CHARS = 55000  # recent chat a member joining an existing room reads verbatim; fits one 60k delivery chunk


class Conversation:
    def __init__(self, app, room, factory, format_event, images_of, audit, start=True):
        self.app, self.room, self.rid = app, room, os.path.basename(room['dir'])
        self.format_event, self.images_of, self.audit = format_event, images_of, audit
        self.members = members_of(room)
        self.lanes = {f'{m}:work': factory(self, m) for m in self.members}
        self.live, self.act, self.presented = {}, {}, {}
        self.phase, self.handling = '', False
        self.inbox = queue.Queue()
        self.mode = C.load_json(self.path('mode.json'), {}).get('mode', 'normal')
        if self.mode not in ('normal', 'collaboration'):
            self.mode = 'normal'
        self.mode_epoch = 0
        self.jobs = C.load_json(self.path('dispatches.json'), [])
        self.active = {m: None for m in self.members}
        # Native monitor notifications can start a turn without a client job.
        # User insertions during that turn still need delivery/ordering receipts.
        self.ambient_jobs = {m: [] for m in self.members}
        self.waiting = {m: [] for m in self.members}
        self.control_seen = {m: set() for m in self.members}
        self.delivered = {m: set(C.load_json(self.path(f'delivered-{m}.json'), {}).get('seqs', [])) for m in self.members}
        for job in self.jobs:
            if job['status'] in ('running', 'queued', 'classifying'):
                job['status'] = 'interrupted'  # never replay an uncertain dispatch on restart
                job['interruption'] = 'client_restart'
        self.persist()
        if start:
            threading.Thread(target=self.worker, daemon=True).start()

    def path(self, name):
        return os.path.join(self.room['dir'], name)

    def lane(self, model, role='work'):
        return self.lanes[f'{model}:work']

    @property
    def busy(self):
        return self.handling or bool(self.phase) or any(l.busy for l in self.lanes.values())

    def persist(self):
        C.save_json(self.path('dispatches.json'), self.jobs)

    def enqueue(self, kind, **data):
        self.inbox.put({'kind': kind, **data})

    def worker(self):
        while True:
            event = self.inbox.get()
            self.handling = True
            try:
                self.process(event)
            except Exception as exc:
                self.audit(self.room, 'client_error', event_kind=event.get('kind'), error=str(exc))
                self.notice('客户端处理失败；消息已保留。' + str(exc))
            finally:
                self.handling = False
                self.phase = ''

    def process(self, event):
        kind = event['kind']
        if kind == 'user':
            self.handle_user(event)
        elif kind == 'complete':
            self.complete(event)
        elif kind == 'intermediate_complete':
            self.receive_output(event['model'], event['text'], final=True)
        elif kind == 'external_complete':
            model = event['model']
            jobs, self.ambient_jobs[model] = self.ambient_jobs[model], []
            self.finish_jobs(model, event, jobs)
        elif kind == 'external_start':
            self.control_seen[event['model']].clear()
        elif kind in ('note', 'partial'):
            if not self.lane(event['model']).quiet:
                self.receive_output(event['model'], event['text'], final=False, partial=kind == 'partial')
        elif kind == 'mode':
            self.set_mode(event['mode'])
        elif kind == 'retry':
            self.retry(event['id'])
        elif kind == 'stop':
            self.stop_model(event['model'])
        elif kind == 'notice':  # a transport's remark that belongs after what is already queued (e.g. an answer)
            self.notice(event['text'])

    def notice(self, text):
        return C.append(self.room, 'controller', text, 'client', kind='status')

    def environment(self, model, job):
        if self.members == PAIR:
            return (f'（客户端环境 · coordinator-v1）你是 {model.upper()} 的持续工作会话，有工具和此前工作记忆。'
                    '没有独立讨论前台。静默分类器只选接收者。\n'
                    f'当前模式：{self.mode}。'
                    + ('协同开启：可主动 @ 对方；共享笔记是背景，非用户新要求。\n' if self.mode == 'collaboration'
                       else '普通模式：用户消息只递送选中者；另一方未读消息等其下次调用，不自动协作。\n')
                    + f"本次来源：{job['origin']}；需处理的消息编号：{job['seqs']}。"
                    + ('这是双方回应的第二位，请读 GPT 本次回答后回应。' if job.get('second') else '')
                    + '上面的未读历史保留来源，不是全部都要重新执行。用户的新要求优先；继续保留未完成的原任务。')
        names = '、'.join(NAMES[m] for m in self.members)
        prior = job.get('prior') or []
        return (f'（客户端环境 · coordinator-v1）你是 {model.upper()} 的持续工作会话，有工具和此前工作记忆。'
                f'群聊成员：{names}。没有独立讨论前台。静默分类器只选接收者。\n'
                f'当前模式：{self.mode}。'
                + ('协同开启：可主动 @ 其他成员（@GPT / @Claude / @Gemini）；共享笔记是背景，非用户新要求。\n'
                   if self.mode == 'collaboration'
                   else '普通模式：用户消息只递送选中者；其他成员的未读消息等其下次调用，不自动协作。\n')
                + f"本次来源：{job['origin']}；需处理的消息编号：{job['seqs']}。"
                + (f"这是依次回应的第 {len(prior) + 1} 位，请读 {'、'.join(NAMES[m] for m in prior)} 本次回答后回应。"
                   if job.get('second') and prior else '这是依次回应的后一位，请读前面成员本次回答后回应。' if job.get('second') else '')
                + '上面的未读历史保留来源，不是全部都要重新执行。用户的新要求优先；继续保留未完成的原任务。')

    def route(self, job, target):
        """Point a job at the first member of its chain; the others answer after it, in order."""
        chain = chain_of(target, self.members) or [self.members[0]]
        job.update(model=chain[0], after=chain[1] if len(chain) > 1 else None,
                   **({'then': chain[2:]} if len(chain) > 2 else {}))

    def handle_user(self, incoming):
        event = C.append(self.room, 'user', incoming.get('text', ''), 'client',
                         to=[], images=incoming.get('images', []))
        target = explicit_target(event['text'], incoming.get('route', 'auto'), self.members)
        job = self.new_job(None, [event['seq']], 'user', status='classifying')
        try:
            if target is None:
                self.phase = 'Opus 5.5 low 正在分类'
                availability = {
                    m: 'busy' if (self.lane(m).busy or self.active[m] or self.waiting[m]) else 'idle'
                    for m in self.members
                }
                target = self.app.classifier.choose(self.room, C.read_after(self.room, 0),
                                                    availability=availability)
            self.audit(self.room, 'route', seq=event['seq'], recipient=target,
                       source='explicit' if explicit_target(event['text'], incoming.get('route'), self.members) else 'classifier')
            self.route(job, target)
            job['status'] = 'queued'
            self.persist()
            self.deliver(job)
        except Exception as exc:
            job.update(status='failed', error=str(exc))
            self.persist()
            self.notice('消息尚未递送：' + str(exc))

    def new_job(self, model, seqs, origin, status='queued', **extra):
        job = {'id': uuid.uuid4().hex, 'model': model, 'seqs': list(seqs), 'origin': origin,
               'status': status, **extra}
        self.jobs.append(job)
        self.persist()
        return job

    def events(self, seqs):
        wanted = set(seqs)
        return [e for e in C.read_after(self.room, 0) if e['seq'] in wanted]

    def join(self, model):
        """A member joining an existing chat (Gemini, added after GPT and Claude) reads it from a recent point, not the
        whole archive; its first message names channel.jsonl for earlier lines. The pair always reads all it missed."""
        if model in PAIR or os.path.exists(C.cursor_path(self.room, f'lane-{model}-work')):
            return
        rows = C.read_after(self.room, 0)
        size, start = 0, (rows[-1]['seq'] + 1 if rows else 1)
        for event in reversed(rows):
            cleaned = context_event(event, 'work', model)
            if cleaned is None:
                continue
            size += len(self.format_event(cleaned))
            if size > JOIN_CHARS:
                break
            start = event['seq']
        C.set_cursor(self.room, f'lane-{model}-work', start - 1)
        if rows and start > rows[0]['seq']:
            C.save_json(self.path(f'{model}-join.json'), {'from_seq': start, 'at': time.time()})

    def unread(self, model):
        cursor = C.get_cursor(self.room, f'lane-{model}-work')
        rows = C.read_after(self.room, cursor)
        eligible = []
        for event in rows:
            if event['seq'] in self.delivered[model]:
                continue
            if event.get('from') == model and event.get('role') in ('work', 'working') and event.get('kind') != 'tool_summary':
                continue
            cleaned = context_event(event, 'work', model)
            if cleaned is not None:
                eligible.append(cleaned)
        return eligible, rows[-1]['seq'] if rows else cursor

    def mark(self, model, rows, how, cursor=None):
        seqs = [e['seq'] for e in rows]
        self.delivered[model].update(seqs)
        if cursor is not None:
            C.set_cursor(self.room, f'lane-{model}-work', cursor)
            self.delivered[model] = {n for n in self.delivered[model] if n > cursor}
        C.save_json(self.path(f'delivered-{model}.json'), {'seqs': sorted(self.delivered[model])})
        C.log_delivery(self.room, f'{model}:work', seqs, how)

    def prompt(self, model, job, rows):
        return '\n\n'.join(self.format_event(e) for e in rows) + '\n\n' + self.environment(model, job)

    def deliver(self, job):
        model = job['model']
        if job.get('collaboration') and self.mode != 'collaboration':
            job['status'] = 'deferred'; self.persist(); return
        lane = self.lane(model)
        if lane.busy or self.active[model]:
            # User messages default to insertion, regardless of topic. No unrelated backlog.
            if lane.busy and not lane.quiet:
                rows = self.events(job['seqs'])
                if lane.steer(self.prompt(model, job, rows), [p for e in rows for p in self.images_of(e)]):
                    self.mark(model, rows, 'coordinator-steer')
                    job['status'] = 'running'
                    active = self.active[model]
                    (active['attached'] if active else self.ambient_jobs[model]).append(job)
                    self.persist()
                    return
            job['status'] = 'queued'; self.waiting[model].append(job); self.persist(); return
        self.join(model)
        rows, cursor = self.unread(model)
        # Retry or reordered peer answer still carries its explicit request even if already read.
        present = {e['seq'] for e in rows}
        rows += [e for e in self.events(job['seqs']) if e['seq'] not in present]
        chunks, part, size = [], [], 0
        for event in rows:
            n = len(self.format_event(event))
            if part and size + n > 60000:
                chunks.append(part); part, size = [], 0
            part.append(event); size += n
        chunks.append(part)
        job['status'] = 'running'
        self.active[model] = {'job': job, 'attached': [], 'chunks': chunks, 'index': 0, 'cursor': cursor}
        self.control_seen[model].clear()
        self.persist()
        self.send_chunk(model)

    def send_chunk(self, model):
        active, lane = self.active[model], self.lane(model)
        index, chunks = active['index'], active['chunks']
        final = index == len(chunks) - 1
        rows, job = chunks[index], active['job']
        prompt = self.prompt(model, job, rows) if final else '\n\n'.join(self.format_event(e) for e in rows) + '\n（客户端）未读历史分段加载，暂不执行，等待最后一段。只回 —。'
        done = lambda text, stopped: self.enqueue('complete', model=model, text=text, stopped=stopped,
                                                   job_id=job['id'], index=index, error=lane.error)
        ok = lane.ask(prompt, quiet=not final, on_done=done,
                      images=[p for e in rows for p in self.images_of(e)][-10:])
        if ok:
            self.mark(model, rows, 'coordinator-request', cursor=active['cursor'] if final else None)
            if final and self.waiting[model]:
                # Messages arriving during history loading can now be inserted.
                waiting, self.waiting[model] = self.waiting[model], []
                for pending in waiting:
                    self.deliver(pending)
        else:
            # Some transports invoke the callback on failure, others do not. ID/index
            # checks in complete() make these duplicate failure notifications harmless.
            self.enqueue('complete', model=model, text='', stopped=False, error=lane.error or '请求未发出',
                         job_id=job['id'], index=index)

    def complete(self, result):
        model, active = result['model'], self.active[result['model']]
        if not active or active['job']['id'] != result['job_id'] or active['index'] != result['index']:
            return
        failed = result.get('error') or result.get('stopped')
        if not failed and active['index'] < len(active['chunks']) - 1:
            active['index'] += 1
            self.send_chunk(model)
            return
        jobs = [active['job']] + active['attached']
        self.active[model] = None
        self.finish_jobs(model, result, jobs)

    def finish_jobs(self, model, result, jobs):
        failed = result.get('error') or result.get('stopped')
        if failed:
            for job in jobs:
                job.update(status='interrupted' if result.get('stopped') else 'failed', error=str(failed))
                if result.get('stopped'):
                    job['interruption'] = 'user_stop'
            self.notice(f'{model.upper()} 本次回应已停止，消息记录保留。' if result.get('stopped') else
                        f'{model.upper()} 本次回应未完成，消息已保留，可重试。')
        else:
            answer = self.receive_output(model, result.get('text', ''), final=True,
                                         share=not any(j.get('after') or j.get('reply_to') for j in jobs))
            for job in jobs:
                job['status'] = 'completed'
                if job.get('after'):
                    prior = (job.get('prior') or []) + [model]
                    rest = job.get('then') or []
                    origin = ('user · both（GPT 已完成）' if self.members == PAIR or (prior == ['gpt'] and job['after'] == 'claude' and not rest)
                              else f"user · 依次回应（{'、'.join(NAMES[m] for m in prior)} 已完成）")
                    follow = self.new_job(job['after'], job['seqs'] + ([answer['seq']] if answer else []), origin, second=True,
                                          **({'prior': prior} if self.members != PAIR else {}),
                                          **({'after': rest[0]} if rest else {}),
                                          **({'then': rest[1:]} if len(rest) > 1 else {}))
                    self.deliver(follow)
                if job.get('reply_to') and answer and self.mode == 'collaboration':
                    follow = self.new_job(job['reply_to'], [answer['seq']], f'{model} 的协作答复', collaboration=True)
                    self.deliver(follow)
        self.persist()
        while self.waiting[model] and not self.active[model] and not self.lane(model).busy:
            self.deliver(self.waiting[model].pop(0))

    def receive_output(self, model, text, final=False, partial=False, share=True):
        public, requests, progress = split_output(text)
        for item in progress:
            marker = ('progress', item)
            if item and marker not in self.control_seen[model]:
                self.control_seen[model].add(marker)
                C.append(self.room, model, item, 'client', role='work', kind='progress')
        for target, request in requests:
            # Plain @ is only complete at a message boundary, not every partial token.
            if partial and '<collaborate' not in text:
                continue
            marker = (target, request)
            if not request or target == model or target not in self.members or marker in self.control_seen[model]:
                continue
            self.control_seen[model].add(marker)
            event = C.append(self.room, model, request, 'client', role='collaboration', kind='collaboration',
                             visibility='internal', to=[target], source_model=model)
            job = self.new_job(target, [event['seq']], f'{model} 明确 @ 请求', reply_to=model, collaboration=True)
            if self.mode == 'collaboration': self.deliver(job)
            else: job['status'] = 'deferred'; self.persist()
        if partial or public in SILENT:
            return None
        event = C.append(self.room, model, public, 'client', role='work' if final else 'working',
                         **({} if final else {'visibility': 'internal'}))
        for other in self.members:
            if other == model:
                continue
            if share and self.mode == 'collaboration' and self.lane(other).busy and not self.lane(other).quiet:
                # Background information can steer an active collaborator, never wake an idle one.
                job = {'origin': f'{model} 协同背景（无需逐条回应）', 'seqs': [event['seq']]}
                if self.lane(other).steer(self.prompt(other, job, [event])):
                    self.mark(other, [event], 'coordinator-collaboration')
        return event

    def note(self, model, text):
        self.enqueue('note', model=model, text=text)

    def partial(self, model, text):
        if '</progress>' in text or '</collaborate>' in text:
            self.enqueue('partial', model=model, text=text)

    def set_mode(self, mode):
        if mode not in ('normal', 'collaboration'):
            raise ValueError('无效协同模式')
        if mode == self.mode: return
        self.mode = mode; self.mode_epoch += 1
        C.save_json(self.path('mode.json'), {'mode': mode})
        event = C.append(self.room, 'user', '开启协同模式' if mode == 'collaboration' else '退出协同模式',
                         'client', kind='mode')
        # Mode is a user control instruction, so notify every already-running worker.
        for model in self.members:
            lane = self.lane(model)
            if lane.busy and not lane.quiet:
                job = {'origin': '用户切换模式', 'seqs': [event['seq']]}
                if lane.steer(self.prompt(model, job, [event])):
                    self.mark(model, [event], 'coordinator-mode')

    def retry(self, job_id):
        job = next((j for j in self.jobs if j['id'] == job_id), None)
        if not job or job['status'] not in ('failed', 'interrupted'):
            return
        if not job['model']:
            try:
                target = self.app.classifier.choose(self.room, [e for e in C.read_after(self.room, 0) if e['seq'] <= max(job['seqs'])])
                self.route(job, target)
            except Exception as exc:
                job['error'] = str(exc); self.persist(); return
        job.pop('error', None); job.pop('interruption', None); self.deliver(job)

    def stop_model(self, model):
        for job in self.waiting[model]:
            job.update(status='interrupted', interruption='user_stop')
        self.waiting[model].clear(); self.persist()
        return self.lane(model).stop_turn()

    def stop(self, key):
        model = key.split(':')[0]
        if model in self.members: self.enqueue('stop', model=model)
        return True

    def shutdown(self):
        for lane in self.lanes.values():
            if hasattr(lane, 'stop'): lane.stop()
