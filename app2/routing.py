"""One silent classifier, with room routing memory and worker availability."""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid

from history import public_event
from routing_memory import atomic_json, load_memory, save_edits, validate_edits

RECIPIENTS = ('gpt', 'claude', 'both')
ORDER = ('gpt', 'claude', 'gemini')        # speaking order when several members answer one message
PAIR = ('gpt', 'claude')                    # every room has these two; others join per room (room.json "members")
MODEL = 'claude-opus-5-5'
EFFORT = 'low'
AUTOCOMPACT = '100k'


def default_members():
    """Members of a room that does not list its own: everyone whose program is installed (Gemini needs agy)."""
    import backends
    return PAIR + (('gemini',) if backends.find('agy') else ())


DEFAULT_MEMBERS = default_members  # tests replace it with a fixed tuple


def members_of(room):
    """Members of a room in speaking order. A room may list its own in room.json; otherwise the default."""
    try:
        with open(os.path.join(room['dir'], 'room.json'), encoding='utf-8') as stream:
            listed = json.load(stream).get('members')
    except (OSError, ValueError):
        listed = None
    if not listed:
        listed = DEFAULT_MEMBERS() if callable(DEFAULT_MEMBERS) else DEFAULT_MEMBERS
    return tuple(m for m in ORDER if m in set(listed) | set(PAIR))


def recipients_for(members):
    return RECIPIENTS if tuple(members) == PAIR else RECIPIENTS + tuple(m for m in members if m not in PAIR) + ('all',)


def chain_of(target, members=PAIR):
    """A routing target as the ordered list of members who answer, each reading the answers before it."""
    if target == 'both':
        return ['gpt', 'claude']
    if target == 'all':
        return [m for m in ORDER if m in members]
    names = set(str(target).split('+'))
    return [m for m in ORDER if m in names and m in members]


def explicit_target(text, selected='auto', members=PAIR):
    names = set(re.findall(r'(?i)@(gpt|codex|claude|gemini)\b', text))
    names = {'gpt' if n.lower() == 'codex' else n.lower() for n in names} & set(members)
    if names == set(PAIR):
        return 'both'
    if len(names) > 1:
        return 'all' if names == set(members) else '+'.join(m for m in ORDER if m in names)
    if names:
        return names.pop()
    if selected == 'all' and tuple(members) == PAIR:
        return 'both'
    return selected if selected in recipients_for(members) else None


def chat_input(events, memory=None, availability=None, after_seq=0, members=PAIR):
    # Only actual chat. Even a public system status is not classifier context.
    public = (public_event(e) for e in events)
    rows = [{'speaker': e['from'], 'text': e.get('text', ''),
             **({'seq': e['seq']} if 'seq' in e else {}),
             **({'attachments': len(e['images'])} if e.get('images') else {})}
            for e in public if e and e.get('from') in ('user', *members)
            and e.get('kind') not in ('collaboration', 'mode')
            and (not after_seq or e.get('seq', 0) > after_seq)]
    payload = {'chat': rows, 'history_delivery': 'incremental' if after_seq else 'initial'}
    if memory is not None:
        payload['routing_memory'] = memory['rules']
    if availability is not None:
        payload['availability'] = {model: availability.get(model, 'unknown')
                                   for model in members}
    return json.dumps(payload, ensure_ascii=False)


def parse_decision(raw, recipients=RECIPIENTS):
    raw = raw.strip()
    # Compatibility with an in-flight legacy prompt; never invent memory edits.
    if raw.lower() in recipients:
        return raw.lower(), [], []
    decision = json.loads(raw)
    if (not isinstance(decision, dict)
            or set(decision) - {'recipient', 'remember', 'forget'}
            or decision.get('recipient') not in recipients):
        raise ValueError('分类器未返回有效接收者')
    remember, forget = decision.get('remember', []), decision.get('forget', [])
    validate_edits(remember, forget)
    return decision['recipient'], remember, forget


class RouterProcess:
    """One tool-less stream; no worker hooks or background model tasks."""
    def __init__(self, command, cwd, env):
        self.results = queue.Queue()
        self.p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=str(cwd), env=env,
                                  creationflags=0x08000000 if os.name == 'nt' else 0)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            for line in self.p.stdout:
                event = json.loads(line)
                if event.get('type') == 'result':
                    self.results.put(event)
        except Exception:
            pass
        finally:
            self.results.put(None)

    def request(self, payload):
        if self.p.poll() is not None:
            raise RuntimeError('路由会话已退出；消息保留，可重试')
        message = {'type':'user', 'message':{'role':'user', 'content':payload}}
        self.p.stdin.write((json.dumps(message, ensure_ascii=False)+'\n').encode('utf-8'))
        self.p.stdin.flush()
        try:
            result = self.results.get(timeout=120)
        except queue.Empty:
            raise RuntimeError('路由调用超时；消息保留，可重试') from None
        if result is None:
            raise RuntimeError('路由连接中断；消息保留，可重试')
        return result

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=5)
        for stream in (self.p.stdin, self.p.stdout):
            if stream:
                stream.close()


class Classifier:
    def __init__(self, exe, prompt_path, audit, env):
        self.exe, self.prompt_path, self.audit, self.env = exe, prompt_path, audit, env
        self.processes = {}

    def close(self):
        for _, channel in list(self.processes.values()):
            channel.close()
        self.processes.clear()

    def prompt_for(self, members):
        """Rooms with more members use their own prompt file next to the default one."""
        if tuple(members) == PAIR:
            return Path(self.prompt_path)
        base = Path(self.prompt_path)
        return base.with_name(base.stem + '-' + '-'.join(m for m in members if m not in PAIR) + base.suffix)

    def choose(self, room, events, availability=None, members=None):
        members = tuple(members or members_of(room))
        recipients = recipients_for(members)
        prompt_path = self.prompt_for(members)
        state_path = Path(room['dir']) / 'routing-session.json'
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        if not state or state.get('pending'):
            # An uncertain prior call may have entered the native transcript.
            # Rebuild from public chat instead of resuming a half-accepted turn.
            state = {'version': 1, 'session_id': str(uuid.uuid4()), 'cursor': 0, 'started': False}
        if state.get('version') != 1:
            raise ValueError('路由会话格式无效')
        uuid.UUID(state['session_id'])
        memory = load_memory(room)
        payload = chat_input(events, memory, availability, after_seq=state['cursor'], members=members)
        command = [self.exe(), '-p', '--model', MODEL, '--effort', EFFORT,
                   '--safe-mode', '--tools', '', '--strict-mcp-config', '--disable-slash-commands',
                   '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
                   '--autocompact', AUTOCOMPACT, '--input-format', 'stream-json',
                   '--output-format', 'stream-json', '--verbose',
                   '--system-prompt-file', str(prompt_path)]
        command += ['--resume' if state['started'] else '--session-id', state['session_id']]
        if not command[0]:
            raise RuntimeError('找不到用于分类的 Claude CLI')
        started = time.time()
        previous_cost = state.get('total_cost_usd', 0)
        result, selected = {}, None
        room_key = str(state_path.resolve())
        try:
            # Stable, room-specific cwd and session preserve the cacheable prefix.
            # Safe mode still excludes project instructions, hooks and tools.
            cwd = Path(room['dir']) / 'router-context'
            cwd.mkdir(exist_ok=True)
            state['pending'] = True
            atomic_json(state_path, state)
            entry = self.processes.get(room_key)
            if entry is None or entry[0] != (state['session_id'], str(prompt_path)):  # a member joining restarts it on its prompt
                if entry is not None:
                    entry[1].close()
                entry = ((state['session_id'], str(prompt_path)), RouterProcess(command, cwd, self.env()))
                self.processes[room_key] = entry
            result = entry[1].request(payload)
            if result.get('is_error'):
                raise RuntimeError('分类器未返回有效接收者；消息保留，可重试或手动指定')
            selected, remember, forget = parse_decision(result.get('result') or '', recipients)
            user_event = next((e for e in reversed(events) if e.get('from') == 'user'), {})
            save_edits(room, memory, user_event, remember, forget)
            state.update(started=True, pending=False,
                         total_cost_usd=result.get('total_cost_usd', previous_cost),
                         cursor=max((e.get('seq', 0) for e in events), default=state['cursor']))
            atomic_json(state_path, state)
            return selected
        except Exception:
            entry = self.processes.pop(room_key, None)
            if entry is not None:
                entry[1].close()
            raise
        finally:
            # Only routing metadata and usage; never credentials or stderr.
            self.audit(room, 'classification', model=MODEL, effort=EFFORT,
                       chat=payload, recipient=selected if selected in recipients else None,
                       session_id=state['session_id'], autocompact=AUTOCOMPACT,
                       seconds=round(time.time()-started, 3), usage=result.get('usage'),
                       cost_usd=(max(0, result['total_cost_usd']-previous_cost)
                                 if result.get('total_cost_usd') is not None else None),
                       session_cost_usd=result.get('total_cost_usd'))
