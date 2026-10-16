"""群聊客户端：一个静默分类器，两个持续工作会话。见 PROTOCOL.md。"""
import os, sys

STATE = os.environ.get("GROUPCHAT2_STATE") or os.path.join(os.path.expanduser("~"), ".claude", "groupchat2")  # override for tests
os.environ["CODEX_BRIDGE_DIR"] = STATE  # before common is imported: every room / cursor lives here
HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
APP1 = os.path.join(BRIDGE, "app")
sys.path.insert(0, BRIDGE)
sys.path.insert(0, APP1)

import base64, glob, json, queue, re, subprocess, threading, time, uuid
from history import public_event
from conversation import Conversation
from routing import Classifier
import backends
import common as C
import webview
from work_record import WorkRecord, short
import app as A1  # shared helpers of the first client (paths, quota parsing, conversation list)

PROMPTS = os.path.join(HERE, "prompts")
PREFS = os.path.join(STATE, "app-prefs.json")
ICON = os.path.join(HERE, "groupchat2.ico")  # blue, so it is told apart from the green first client
NO_WINDOW = 0x08000000
MODELS = ("claude", "gpt")
GEMINI_EFFORTS = ("low", "medium", "high")  # agy model slugs end in the effort: gemini-4-argon-high
PHILOSOPHY = [os.path.join(os.path.expanduser("~"), ".codex", "AGENTS.md"),
              os.path.join(os.path.expanduser("~"), ".codex", "principles", "world-model-alignment.md")]
ROLES = ("work",)


def routing_log(room, kind, **data):
    """Audit-only storage: not a channel message, never read by lane.unseen()."""
    path = os.path.join(room['dir'], 'routing-log.jsonl')
    with C.Lock(path):
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': time.time(), 'kind': kind, **data}, ensure_ascii=False) + '\n')


def prompt_text(name):
    with open(os.path.join(PROMPTS, name), encoding="utf-8") as stream:
        return stream.read()


def log(where, ex):
    C.log_error("app2 " + where, ex)


def child_env(**extra):
    env = dict(os.environ, GROUPCHAT_OFF="1", CODEX_BRIDGE_DIR=STATE, **extra)
    for k in ("GROUPCHAT_HOSTED", "GROUPCHAT_ROOM", "GROUPCHAT_CHANNEL"):
        env.pop(k, None)
    return env


def rid_of(room):
    return os.path.basename(room["dir"])


IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MD_IMG = re.compile(r"!\[[^\]\n]*\]\(<?([^)>\n]+?)>?\)")
CLAUDE_IMG_MAX = 3_700_000  # bytes before base64: the API takes up to 5 MB per image


def images_of(e):
    """Local image files a channel line carries: the user's attachments, or pictures a model put in its text."""
    paths = list(e.get("images") or ())
    if e.get("from") != "user":
        paths += [u for u in MD_IMG.findall(e.get("text") or "") if not re.match(r"(?i)(https?|data):", u)]
    return [p for p in paths if os.path.splitext(p)[1].lower() in IMG_EXT and os.path.isfile(p)]


TEXT_EXT = {".md", ".markdown", ".txt", ".log", ".py", ".pyw", ".ipynb", ".json", ".jsonl", ".toml", ".yaml", ".yml", ".ini",
            ".cfg", ".conf", ".env", ".csv", ".tsv", ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".js", ".mjs", ".cjs", ".ts", ".tsx",
            ".jsx", ".css", ".scss", ".xml", ".tex", ".bib", ".rs", ".go", ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".sql",
            ".r", ".lua", ".rb", ".php", ".vue", ".svelte", ".gitignore", ""}
REVEAL_EXT = {".exe", ".com", ".scr", ".msi", ".msix", ".appx", ".lnk", ".url", ".reg", ".hta", ".cpl", ".vbs", ".vbe",
              ".jse", ".wsf", ".wsh", ".jar", ".msc", ".dll", ".sys"}


def resolve_path(raw, room):
    """A path as written in a message (absolute, ~, /c/…, relative to the project, with :line or #L…) -> an existing path."""
    import urllib.parse
    s = urllib.parse.unquote((raw or "").strip().strip("\"'<>"))
    s = re.sub(r"^file:/{2,3}", "", s)
    s = re.sub(r"^/([a-zA-Z])/", r"\1:/", s)
    s = os.path.expandvars(os.path.expanduser(s))
    tries = [s, re.sub(r"#L?\d+.*$", "", s), re.sub(r"^(.+?):\d+(:\d+)?$", r"\1", s)]
    for t in tries:
        for base in ([""] if os.path.isabs(t) else [room.get("root") or "", room["dir"]]):
            p = os.path.normpath(os.path.join(base, t)) if base else os.path.normpath(t)
            if (base or os.path.isabs(p)) and os.path.exists(p):
                return p
    return None


def editor():
    import shutil
    code = shutil.which("code")
    exe = code and os.path.join(os.path.dirname(os.path.dirname(code)), "Code.exe")
    return exe if exe and os.path.isfile(exe) else "notepad.exe"


def open_path(p):
    ext = os.path.splitext(p)[1].lower()
    if os.path.isdir(p):
        os.startfile(p)
    elif ext in REVEAL_EXT:
        subprocess.Popen(["explorer", "/select,", p])
    elif ext in TEXT_EXT:
        subprocess.Popen([editor(), p], creationflags=NO_WINDOW)
    else:
        try:
            os.startfile(p)
        except OSError:  # nothing registered for this kind of file
            subprocess.Popen(["explorer", "/select,", p])


def line_text(e):
    """A channel line as the models read it; attached pictures are named by path, the pictures themselves go alongside."""
    if e.get('from') not in ('user', 'gpt', 'claude') or e.get('kind') in ('divider', 'tool_summary'):
        t = '<controller>\n' + C._neutralize(e.get('text', '')) + '\n</controller>'
    else:
        t = C.fmt_plain(e)
    # The delivery envelope refers to these IDs, so they must be visible here.
    t = f"[消息 {e.get('seq', '?')}]\n" + t
    if e.get("images"):
        t += "\n（附图：" + "；".join(e["images"]) + "）"
    return t


# ================================================================ lanes: one model session in one role
class Lane:
    """What every lane shares: identity, delivery cursor, busy state, the callback when a turn ends."""

    def __init__(self, room_sess, model, role):
        self.rs, self.app = room_sess, room_sess.app
        self.room, self.rid = room_sess.room, rid_of(room_sess.room)
        self.model, self.role = model, role
        self.key = f"{model}:{role}"
        self.busy = False
        self.quiet = False          # loading unread history; no public reply yet
        self.on_done = None
        self.seen = set()           # channel lines already handed over outside the cursor (notes steered in)
        self.stopping = False
        self.error = None
    def finish(self, text, stopped=False):
        self.busy = False
        self.app.live_end(self.rid, self.key, stopped)
        cb, self.on_done = self.on_done, None
        if cb:
            try:
                cb(text or "", stopped)
            except Exception as ex:
                log("on_done " + self.key, ex)
        else:
            # Native background-task notifications can start another response
            # after the last client request finished. Never drop that result.
            self.rs.enqueue('external_complete', model=self.model, text=text or '',
                            stopped=stopped, error=self.error)

    def state(self):
        return "busy" if self.busy else "idle"


class ClaudeLane(Lane):
    def __init__(self, room_sess, role):
        super().__init__(room_sess, "claude", role)
        self.p = None
        self.started_with = None
        self.pending = {}
        self.wlock = threading.Lock()
        self.cur = ""               # text of the text block being written
        self.work = None

    @property
    def ready(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        exe = backends.find("claude")
        if not exe:
            self.error = backends.missing_note("claude")
            return False
        cfg = self.app.claude_cfg
        self.started_with = (cfg["model"], cfg["effort"])
        idfile = os.path.join(self.room["dir"], f"claude-{self.role}.json")
        meta = C.load_json(idfile, {})
        sid = meta.get('session')
        cmd = [exe, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--model", cfg["model"], "--effort", cfg["effort"]]
        cmd += ["--permission-mode", cfg["perm"], "--system-prompt-snapshot", "off",
                "--append-system-prompt-file", os.path.join(PROMPTS, "work.md")]
        if sid:
            cmd += ["--resume", sid]
        else:
            sid = str(uuid.uuid4())
            C.save_json(idfile, {**meta, 'session': sid})
            cmd += ["--session-id", sid, "--name", f"{self.room['name']} · 持续会话"]
        self.p = subprocess.Popen(cmd, cwd=self.room["root"], env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.error, self.busy = None, False
        threading.Thread(target=self.read, args=(self.p,), daemon=True).start()
        threading.Thread(target=self.read_err, args=(self.p,), daemon=True).start()
        return True

    def stop(self):
        if self.ready:
            try:
                self.p.stdin.close()
                self.p.wait(timeout=10)
            except Exception:
                self.p.kill()
        self.p = None

    def write(self, obj):
        with self.wlock:
            self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()

    @staticmethod
    def content(text, images):
        if not images:
            return text
        blocks, skipped = [{"type": "text", "text": text}], []
        for path in images:
            if os.path.getsize(path) > CLAUDE_IMG_MAX:
                skipped.append(path)
                continue
            data = base64.b64encode(open(path, "rb").read()).decode()
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": IMG_EXT[os.path.splitext(path)[1].lower()], "data": data}})
        if skipped:
            blocks[0]["text"] += "\n（这些图片太大，没有随消息附上，需要时用工具读取：" + "；".join(skipped) + "）"
        return blocks

    def ask(self, text, quiet=False, on_done=None, images=()):
        cfg = self.app.claude_cfg
        if self.ready and not self.busy and self.started_with != (cfg["model"], cfg["effort"]):
            self.stop()
        if not self.ready and not self.start():
            if on_done:
                on_done("", False)
            return False
        self.quiet, self.on_done, self.cur, self.stopping = quiet, on_done, "", False
        self.error = None
        self.busy = True
        try:
            self.write({"type": "user", "message": {"role": "user", "content": self.content(text, images)}, "parent_tool_use_id": None})
        except Exception as exc:
            self.error = 'Claude 消息未送达：' + str(exc)[:160]
            self.finish('', False)
            return False
        return True

    def steer(self, text, images=()):
        """Taken in at the next tool-call boundary of the running turn."""
        if self.ready and self.busy and not self.quiet:
            try:
                self.write({"type": "user", "message": {"role": "user", "content": self.content(text, images)}, "parent_tool_use_id": None})
                return True
            except Exception as exc:
                log('claude steer', exc)
        return False

    def stop_turn(self):
        if self.busy and self.ready:
            self.stopping = True
            rid = "req_" + uuid.uuid4().hex[:10]
            self.write({"type": "control_request", "request_id": rid, "request": {"subtype": "interrupt"}})
            return True
        return False

    def read_err(self, p):
        tail = []
        for raw in p.stderr:
            tail = (tail + [raw.decode("utf-8", "replace").strip()])[-5:]
        if p.poll() not in (None, 0):
            self.error = " / ".join(x for x in tail if x)[:200] or f"Claude 进程退出（{p.returncode}）"

    def read(self, p):
        app = self.app
        for raw in p.stdout:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type")
            try:
                if self.role == "work" and t in ("assistant", "user") and isinstance(ev.get("message"), dict):
                    if self.work is None:
                        self.work = WorkRecord(self.room, "claude", ev.get("session_id"))
                    A1.record_event(self.work, "claude", ev)
                if t == "stream_event":
                    e = ev.get("event") or {}
                    et = e.get("type")
                    if et == 'message_start':
                        if not self.busy and self.on_done is None:
                            # Native notifications can start a response without
                            # a client request. They do not add a result counter.
                            self.error = None
                            self.cur = ''
                            self.quiet = False
                            self.rs.enqueue('external_start', model=self.model)
                        self.busy = True
                    elif et == "content_block_start":
                        cb = e.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            if self.role == "work" and self.cur.strip():   # text before a tool call: a working note
                                self.rs.note("claude", self.cur.strip())
                            self.cur = ""
                            if not self.quiet:
                                app.activity(self.rid, self.key, f"正在使用工具：{cb.get('name')}")
                        elif cb.get("type") == "text":
                            if self.cur.strip() and not self.quiet:
                                app.delta(self.rid, self.key, "\n\n")
                            if not self.quiet:
                                app.activity(self.rid, self.key, "")
                    elif et == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                        piece = e["delta"].get("text", "")
                        self.cur += piece
                        self.rs.partial("claude", self.cur)
                        if not self.quiet:
                            app.delta(self.rid, self.key, piece)
                elif t == "result":
                    if self.work is not None:
                        A1.record_event(self.work, "finish", "interrupted" if self.stopping else "error" if ev.get("is_error") else "completed")
                        self.work = None
                    final = ev.get("result") if isinstance(ev.get("result"), str) else self.cur
                    if ev.get("is_error"):
                        self.error = "Claude 请求失败：" + str(ev.get("result") or ev.get("subtype") or "unknown")[:200]
                    stopped = self.stopping
                    self.stopping = False
                    # A send is not a turn: inserted messages may be folded into
                    # the current turn, and several queued sends may coalesce.
                    # Only the producer knows whether another user turn follows.
                    queued = ev.get('queued_turn_count', 0)
                    queued = queued if type(queued) is int and queued > 0 else 0
                    try:
                        routing_log(self.room, 'claude_result', queued_turn_count=queued,
                                    result_index=ev.get('result_index'),
                                    terminal_reason=ev.get('terminal_reason'),
                                    stopped=stopped, failed=bool(self.error))
                    except Exception:
                        pass  # an audit-file failure must never prevent completion
                    if queued and not stopped and not self.error:
                        self.rs.enqueue('intermediate_complete', model=self.model, text=final)
                        self.cur = ''
                    else:
                        self.finish(final, stopped)
                elif t == "rate_limit_event":
                    week = ((ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("seven_day") or {}
                    if week.get("utilization") is not None:
                        app.claude_quota = {"remaining": A1.pct_left(week["utilization"], 1), "resets": A1.when(week.get("resetsAt"))}
                elif t == "control_request":
                    self.write({"type": "control_response", "response": {"subtype": "error", "request_id": ev.get("request_id"),
                                                                         "error": "群聊客户端不处理这个请求"}})
            except Exception as ex:
                log("claude read " + self.key, ex)
        if self.busy:
            self.error = self.error or "Claude 连接中断"
            self.finish(self.cur, False)


class GPTEngine:
    """One Codex app-server for the existing GPT work threads."""

    def __init__(self, app, role):
        self.app, self.role, self.p = app, role, None
        self.error, self.models = None, []
        self.nid, self.pending, self.wlock = 0, {}, threading.Lock()
        self.lanes = {}             # thread id -> GPTLane
        self.loaded = set()
        self.quota = None

    @property
    def ready(self):
        return self.p is not None and self.p.poll() is None and bool(self.models)

    def start(self):
        try:
            exe = backends.find("codex")
            if not exe:
                raise RuntimeError(backends.missing_note("codex"))
            args = [exe, "app-server"]
            self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      creationflags=NO_WINDOW, env=child_env())
            threading.Thread(target=self.read, daemon=True).start()
            self.call("initialize", {"clientInfo": {"name": f"groupchat2-{self.role}", "version": "1.0-coordinator"},
                                     "capabilities": {"experimentalApi": True}})
            self.notify("initialized")
            self.models = [m for m in self.call("model/list", {}).get("data", []) if not m.get("hidden")]
            if self.role == "work":
                try:
                    self.take_limits(self.call("account/rateLimits/read", {}, timeout=30).get("rateLimits") or {})
                except Exception as ex:
                    log("gpt limits", ex)
            self.error = None
        except Exception as ex:
            self.error = f"GPT {self.role} 启动失败：{ex}"[:200]
            log("gpt start " + self.role, ex)

    def write(self, obj):
        with self.wlock:
            self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()

    def call(self, method, params, timeout=120):
        with self.wlock:
            self.nid += 1
            nid = self.nid
        q = queue.Queue()
        self.pending[nid] = q
        self.write({"jsonrpc": "2.0", "id": nid, "method": method, "params": params})
        r = q.get(timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message', r['error'])}")
        return r["result"]

    def notify(self, method, params=None):
        self.write({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def take_limits(self, snap):
        wins = [w for w in (snap.get("primary"), snap.get("secondary")) if w]
        if wins:
            week = max(wins, key=lambda w: w.get("windowDurationMins") or 0)
            self.quota = {"remaining": A1.pct_left(week.get("usedPercent")), "resets": A1.when(week.get("resetsAt"))}

    def read(self):
        for raw in self.p.stdout:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            try:
                if "id" in m and "method" not in m:
                    q = self.pending.pop(m["id"], None)
                    if q:
                        q.put(m)
                    continue
                if "id" in m:
                    self.write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "群聊客户端不处理 " + m["method"]}})
                    continue
                meth, p = m.get("method"), m.get("params") or {}
                if meth == "account/rateLimits/updated":
                    self.take_limits(p.get("rateLimits") or {})
                    continue
                lane = self.lanes.get(p.get("threadId"))
                if lane:
                    lane.on_event(meth, p)
            except Exception as ex:
                log("gpt read " + self.role, ex)
        for lane in list(self.lanes.values()):
            if lane.busy:
                lane.error = "GPT 连接中断"
                lane.finish(lane.last, False)
        self.error = self.error or f"GPT {self.role} 引擎已退出"


class GPTLane(Lane):
    def __init__(self, room_sess, role):
        super().__init__(room_sess, "gpt", role)
        self.engine = room_sess.app.gpt[role]
        self.tid, self.turn = None, None
        self.last, self.final = "", None
        self.work = None

    @property
    def ready(self):
        return self.engine.ready

    def open(self):
        eng, room = self.engine, self.room
        idfile = os.path.join(room["dir"], f"gpt-{self.role}.json")
        meta = C.load_json(idfile, {})
        tid = meta.get('thread')
        if tid and tid not in eng.loaded:
            try:
                params = {"threadId": tid, "excludeTurns": True}
                params['developerInstructions'] = prompt_text('work.md')
                eng.call("thread/resume", params)
            except RuntimeError as ex:
                if "missing source rollout" not in str(ex):
                    raise
                tid = None
        if not tid:
            params = {"cwd": room["root"], "approvalPolicy": "never"}
            if room.get("project_id"):
                params["projectId"] = room["project_id"]
            params["developerInstructions"] = prompt_text("work.md")
            tid = eng.call("thread/start", params)["thread"]["id"]
            eng.call("thread/name/set", {"threadId": tid, "name": f"{room['name']} · 持续会话"})
            C.save_json(idfile, {**meta, 'thread': tid})
        self.tid = tid
        eng.loaded.add(tid)
        eng.lanes[tid] = self

    @staticmethod
    def items(text, images):
        return [{"type": "text", "text": text}] + [{"type": "localImage", "path": p} for p in images]

    def ask(self, text, quiet=False, on_done=None, images=()):
        self.quiet, self.on_done, self.stopping = quiet, on_done, False
        if not self.engine.ready:
            self.error = self.engine.error or "GPT 还没连上"
            if on_done:
                on_done("", False)
            return False
        self.busy = True
        if not self.tid or self.tid not in self.engine.lanes:
            try:
                self.open()
            except Exception as ex:
                self.error = str(ex)[:200]
                self.finish("", False)
                return False
        self.quiet, self.on_done, self.stopping = quiet, on_done, False
        self.last, self.final = "", None
        self.error = None
        self.busy = True
        params = {"threadId": self.tid, "input": self.items(text, images)}
        if self.app.gpt_cfg.get("model"):
            params["model"] = self.app.gpt_cfg["model"]
        if self.app.gpt_cfg.get("effort"):
            params["effort"] = self.app.gpt_cfg["effort"]
        try:
            self.turn = self.engine.call("turn/start", params)["turn"]["id"]
        except Exception as ex:
            self.error = str(ex)[:200]
            self.finish("", False)
            return False
        return True

    def steer(self, text, images=()):
        if self.busy and self.turn:
            try:
                self.engine.call("turn/steer", {"threadId": self.tid, "expectedTurnId": self.turn, "input": self.items(text, images)})
                return True
            except Exception as ex:
                log("gpt steer", ex)
        return False

    def stop_turn(self):
        if self.busy and self.turn:
            self.stopping = True
            self.engine.call("turn/interrupt", {"threadId": self.tid, "turnId": self.turn})
            return True
        return False

    def on_event(self, meth, p):
        app = self.app
        if meth == "turn/started":
            if not self.busy and self.on_done is None:
                self.last, self.final, self.error, self.quiet = '', None, None, False
                self.rs.enqueue('external_start', model=self.model)
            self.busy, self.turn = True, (p.get("turn") or {}).get("id", self.turn)
            if self.role == "work":
                self.work = WorkRecord(self.room, "gpt", self.tid, self.turn)
        elif meth == "item/started":
            kind = (p.get("item") or {}).get("type")
            if kind == "agentMessage":
                self.last = ""
                if not self.quiet:
                    app.activity(self.rid, self.key, "")
            elif kind in ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "dynamicToolCall") and not self.quiet:
                label = {"commandExecution": "运行命令", "fileChange": "修改文件", "mcpToolCall": "调用工具",
                         "webSearch": "搜索网页", "dynamicToolCall": "调用工具"}[kind]
                app.activity(self.rid, self.key, f"正在{label}")
        elif meth == "item/agentMessage/delta":
            piece = p.get("delta", "")
            self.last += piece
            self.rs.partial("gpt", self.last)
            if not self.quiet:
                app.delta(self.rid, self.key, piece)
        elif meth == "item/completed":
            it = p.get("item") or {}
            if self.work is not None:
                A1.record_event(self.work, "gpt", it)
            if it.get("type") == "agentMessage":
                text = (it.get("text") or self.last).strip()
                if it.get("phase") == "final_answer":
                    self.final = text
                elif self.role == "work" and text:   # commentary between tool calls: a working note
                    self.rs.note("gpt", text)
            elif it.get("type") == "imageGeneration" and it.get("savedPath"):
                cap = (it.get("revisedPrompt") or "生成的图片").replace("\n", " ")[:80]
                C.append(self.room, "gpt", f"![{cap}]({it['savedPath']})", "codex", role=self.role, kind="image")
        elif meth == "turn/completed":
            if self.work is not None:
                A1.record_event(self.work, "finish", (p.get("turn") or {}).get("status") or "completed")
                self.work = None
            if (p.get("turn") or {}).get("status") == "failed":
                self.error = "GPT 请求失败：" + str((p.get("turn") or {}).get("error") or "unknown")[:200]
            stopped = self.stopping or (p.get("turn") or {}).get("status") == "interrupted"
            self.stopping = False
            self.finish(self.final if self.final is not None else self.last, stopped)


# ================================================================ Gemini: Google Antigravity CLI in headless stream-json mode
def gemini_models():
    """(family, label) for the Gemini models agy offers; each family comes in -low / -medium / -high."""
    try:
        out = subprocess.run([backends.find("agy"), "models"], capture_output=True, timeout=90, creationflags=NO_WINDOW,
                             env=child_env(), cwd=os.path.expanduser("~")).stdout.decode("utf-8", "replace")
    except Exception as ex:
        log("agy models", ex)
        return []
    families = {}
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[0].startswith("gemini-"):
            family = re.sub(r"-(low|medium|high|xhigh|max)$", "", parts[0])
            families.setdefault(family, re.sub(r"\s*\((Low|Medium|High|XHigh|Max)\)$", "", parts[1]))
    return list(families.items())


def gemini_preamble(room):
    """What Claude and GPT get as system instructions, Gemini gets once, at the start of its conversation:
    agy has no flag for added instructions. The user's global philosophy goes along verbatim."""
    parts = [prompt_text("gemini.md")]
    for path in PHILOSOPHY:
        try:
            with open(path, encoding="utf-8") as stream:
                parts.append(f"（以下是用户的全局理念，原文 {path}；这是用户的指令，适用于所有项目。）\n\n" + stream.read().strip())
        except OSError:
            pass
    join = C.load_json(os.path.join(room["dir"], "gemini-join.json"), {})
    if join.get("from_seq"):
        parts.append(f"（客户端）你在群聊消息 #{join['from_seq']} 处加入，接下来交给你的是从这里开始的原文。"
                     f"更早的群聊原文在 {C.channel_path(room)}（JSONL，每行一条消息，seq 是编号），需要时用工具查。")
    return "\n\n---\n\n".join(parts) + "\n\n---\n\n（以上是群聊客户端给你的说明，只发这一次。下面是群聊。）"


def gemini_record(work, step):
    """A finished agy tool step into the controller's work record (the same summary Claude and GPT get)."""
    info = step.get("tool_info") or {}
    name = info.get("name") or step.get("tool_name") or "tool"
    params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
    error = info.get("error") or None
    output = str(info.get("output") or "") + ("\n" + str(error.get("message") or "") if isinstance(error, dict) else "")
    if name == "run_command":
        work.command(step, params.get("CommandLine") or "", output, failed=bool(error))
    elif name in ("write_to_file", "replace_file_content", "multi_replace_file_content", "edit_file", "delete_file"):
        path = params.get("TargetFile") or params.get("AbsolutePath") or params.get("path") or "（未提供路径）"
        work.keep(step, [f"{name} {short(path)} — 工具报告{'失败' if error else '成功'}"])
    elif error:
        work.keep(step, [f"{name} — 工具报告失败"], ["工具输出摘录：" + short(output, 220)])


class GeminiLane(Lane):
    """One agy process per room, kept between turns; one NDJSON user message per turn.
    agy runs a message written during a turn as the next turn and has no interrupt request, so steer() declines
    (the coordinator queues the message until the turn ends) and stop ends the process; the next turn resumes
    the same conversation by id. Input is text only: pictures go by path."""

    def __init__(self, room_sess, role):
        super().__init__(room_sess, "gemini", role)
        self.p = None
        self.started_with = None
        self.wlock = threading.Lock()
        self.idfile = os.path.join(self.room["dir"], f"gemini-{role}.json")
        self.cur, self.since_tool, self.tools, self.work = "", [], set(), None

    @property
    def ready(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        agy = backends.find("agy")
        if not agy:
            self.error = backends.missing_note("agy")
            return False
        cfg = self.app.gemini_cfg
        self.started_with = f"{cfg['model']}-{cfg['effort']}"
        conv = C.load_json(self.idfile, {}).get("conversation")
        cmd = [agy, "--input-format", "stream-json", "--output-format", "stream-json", "--model", self.started_with,
               "--disable-slash-commands"]
        if cfg.get("perm", "skip") == "skip":
            cmd.append("--dangerously-skip-permissions")
        if conv:
            cmd += ["--conversation", conv]
        self.p = subprocess.Popen(cmd, cwd=self.room["root"], env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.error, self.busy, self.stopping = None, False, False
        threading.Thread(target=self.read, args=(self.p,), daemon=True).start()
        threading.Thread(target=self.read_err, args=(self.p,), daemon=True).start()
        return True

    def stop(self):
        p = self.p
        if p is not None and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=10)
            except Exception:
                p.kill()
        self.p = None

    def write(self, obj):
        with self.wlock:
            self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()

    def ask(self, text, quiet=False, on_done=None, images=()):
        cfg = self.app.gemini_cfg
        if self.ready and not self.busy and self.started_with != f"{cfg['model']}-{cfg['effort']}":
            self.stop()
        if not self.ready and not self.start():
            if on_done:
                on_done("", False)
            return False
        if not C.load_json(self.idfile, {}).get("introduced"):
            text = gemini_preamble(self.room) + "\n\n" + text
        missing = [path for path in images if path not in text]
        if missing:
            text += "\n（附图路径，需要看图时用工具打开：" + "；".join(missing) + "）"
        self.quiet, self.on_done, self.stopping, self.error = quiet, on_done, False, None
        self.cur, self.since_tool, self.tools = "", [], set()
        self.busy = True
        try:
            self.write({"event": "user", "message": {"content": text}})
        except Exception as exc:
            self.error = "Gemini 消息未送达：" + str(exc)[:160]
            self.finish("", False)
            return False
        return True

    def steer(self, text, images=()):
        return False  # agy would run it as a separate turn after this one; the coordinator queues it instead

    def stop_turn(self):
        if self.busy and self.ready:
            self.stopping = True
            try:
                self.p.kill()  # the reader sees the end of output and finishes the turn as stopped
            except Exception as ex:
                log("gemini stop", ex)
            return True
        return False

    def read_err(self, p):
        tail = []
        for raw in p.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line and not re.match(r"^(ERROR: logging before google\.Init: )?I\d{4} ", line):  # glog info lines
                tail = (tail + [line])[-5:]
        if p.poll() not in (None, 0) and not self.stopping and tail:
            self.error = self.error or (" / ".join(tail))[:200]

    def text_so_far(self):
        return "\n\n".join(self.since_tool + ([self.cur.strip()] if self.cur.strip() else []))

    def read(self, p):
        for raw in p.stdout:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            try:
                kind = ev.get("event")
                if kind == "init":
                    conv, meta = ev.get("conversation_id"), C.load_json(self.idfile, {})
                    if conv and (meta.get("conversation") != conv or not meta.get("introduced")):
                        C.save_json(self.idfile, {**meta, "conversation": conv, "introduced": True,
                                                  "model": (ev.get("init") or {}).get("model")})
                elif kind == "step_update":
                    self.step(ev.get("step_update") or {})
                elif kind == "result":
                    self.result(ev.get("result") or {})
            except Exception as ex:
                log("gemini read " + self.key, ex)
        if self.busy and (self.p is p or self.p is None):
            stopped = self.stopping
            if not stopped:
                self.error = self.error or "Gemini 进程退出"
            self.close_work("interrupted" if stopped else "error")
            self.stopping = False
            self.finish(self.text_so_far(), stopped)
        if self.p is p:
            self.p = None

    def step(self, s):
        kind, state = s.get("step_type"), s.get("state")
        if kind == "agent_response":
            if s.get("text_delta"):
                self.cur += s["text_delta"]
                self.rs.partial("gemini", self.text_so_far())
            if state == "DONE":
                if self.cur.strip():
                    self.since_tool.append(self.cur.strip())
                self.cur = ""
        elif kind == "tool":
            index = s.get("step_index")
            if index not in self.tools:  # a tool starts: what was written before it is a working note
                self.tools.add(index)
                if self.since_tool:
                    self.rs.note("gemini", "\n\n".join(self.since_tool))
                self.since_tool = []
                if not self.quiet:
                    self.app.activity(self.rid, self.key, f"正在使用工具：{s.get('tool_name') or (s.get('tool_info') or {}).get('name')}")
            if state == "DONE" and self.role == "work":
                if self.work is None:
                    self.work = WorkRecord(self.room, "gemini", s.get("conversation_id"))
                try:
                    gemini_record(self.work, s)
                except Exception as ex:
                    log("gemini record", ex)

    def close_work(self, status):
        if self.work is not None:
            A1.record_event(self.work, "finish", status)
            self.work = None

    def result(self, r):
        status = r.get("status") or ""
        stopped = self.stopping
        if status != "SUCCESS" and not stopped:
            self.error = "Gemini 请求失败：" + str(r.get("error") or status)[:200]
        final = self.text_so_far()
        self.close_work("interrupted" if stopped else "completed" if status == "SUCCESS" else "error")
        self.cur, self.since_tool, self.tools, self.stopping = "", [], set(), False
        self.finish(final, stopped)


class RoomSession(Conversation):
    def __init__(self, app, room):
        lanes = {'claude': ClaudeLane, 'gpt': GPTLane, 'gemini': GeminiLane}
        super().__init__(app, room, lambda rs, m: lanes[m](rs, 'work'), line_text, images_of, routing_log)


# ================================================================ the app
class App:
    def __init__(self):
        os.makedirs(C.ROOMS_DIR, exist_ok=True)
        self.prefs = C.load_json(PREFS, {})
        rid = self.prefs.get("room_id")
        self.room = None
        if rid and not C.load_json(os.path.join(C.ROOMS_DIR, rid, "room.json"), {"archived": True}).get("archived"):
            self.room = C.room_by_dir(os.path.join(C.ROOMS_DIR, rid))
        self.route = self.prefs.get("route", "auto")
        self.claude_cfg = {"model": self.prefs.get("claude_model", "claude-opus-5-5"),
                           "effort": self.prefs.get("claude_effort", "medium"),
                           "perm": self.prefs.get("claude_permission", "bypassPermissions")}
        self.gpt_cfg = {"model": self.prefs.get("gpt_model"), "effort": self.prefs.get("gpt_effort")}
        self.gemini_cfg = {"model": self.prefs.get("gemini_model", "gemini-4-argon"),
                           "effort": self.prefs.get("gemini_effort", "high"),
                           "perm": self.prefs.get("gemini_permission", "skip")}
        self.gemini_families = []
        self.claude_quota = None
        self.window = None
        self.last = 0
        self.buf = {}
        self.blk = threading.Lock()
        self.plock = threading.Lock()
        self.gpt = {"work": GPTEngine(self, "work")}
        self.classifier = Classifier(lambda: backends.find("claude"), os.path.join(PROMPTS, 'coordinator-memory.md'), routing_log, child_env)
        self.sessions = {}

    @property
    def rid(self):
        return rid_of(self.room)

    def sess(self, rid=None):
        rid = rid or self.rid
        if rid not in self.sessions:
            room = self.room if rid == self.rid else C.room_by_dir(os.path.join(C.ROOMS_DIR, rid))
            self.sessions[rid] = RoomSession(self, room)
        return self.sessions[rid]

    def take_claude_usage(self, obj):
        w = (obj.get("rate_limits") or {}).get("seven_day") or {}
        if w.get("utilization") is not None:
            self.claude_quota = {"remaining": A1.pct_left(w["utilization"], 100), "resets": A1.when(w.get("resets_at"))}

    # --- to the page (only the conversation on screen; the rest is kept per conversation)
    def js(self, fn, *args):
        if self.window:
            try:
                self.window.evaluate_js(f"gc.{fn}(" + ",".join(json.dumps(a, ensure_ascii=False) for a in args) + ")")
            except Exception as ex:
                log("js", ex)

    def delta(self, rid, key, text):
        # Generic text may be a technical note before a tool call. Only explicit
        # progress blocks publish mid-turn; the final answer publishes at completion.
        return

    def activity(self, rid, key, label):
        self.sess(rid).act[key] = label  # sidebar only; no technical chatter bubble

    def live_end(self, rid, key, stopped=False, reset=False):
        s = self.sess(rid)
        with self.blk:
            s.live.pop(key, None)
            s.presented.pop(key, None)
            s.act.pop(key, None)
            if rid == self.rid:
                self.buf.pop(key, None)
        if rid == self.rid:
            self.flush()
            self.js("liveEnd", key, bool(stopped))

    def flush(self):
        with self.blk:
            buf, self.buf = self.buf, {}
        for key, text in buf.items():
            lane = self.sess().lanes[key]
            if text and lane.busy and not lane.quiet and not key.endswith(':work'):
                self.js("delta", key, text, False)

    def push_lines(self):
        with self.plock:
            es = C.read_after(self.room, self.last)
            if es:
                self.last = es[-1]["seq"]
                visible = [p for e in es if (p := public_event(e)) is not None]
                if visible:
                    self.js("lines", visible)

    def pump(self):
        n = 0
        while True:
            time.sleep(0.08)
            self.flush()
            n += 1
            if n % 5 == 0 and self.room:
                self.push_lines()
            if n % 12 == 0 and self.room:
                self.js("status", self.status_payload())

    def status_payload(self):
        s, models = self.sess(), {}
        for model in s.members:
            lane = s.lane(model)
            if model == 'gemini':
                models[model] = {'ready': bool(backends.find('agy')) and not lane.error, 'busy': lane.busy,
                                 'label': '工作中' if lane.busy else '空闲', 'error': lane.error,
                                 'queued': len(s.waiting[model]), 'lanes': {'work': lane.state()},
                                 'quota': {'note': '反重力没有提供额度查询'},
                                 'access': '自动批准所有工具' if self.gemini_cfg.get('perm') == 'skip' else '需确认的工具会被拒绝'}
                continue
            error = lane.error or (self.gpt['work'].error if model == 'gpt' else None)
            ready = (lane.ready or lane.p is None) if model == 'claude' else self.gpt['work'].ready
            models[model] = {'ready': bool(ready) and not error, 'busy': lane.busy,
                             'label': '工作中' if lane.busy else '空闲', 'error': error,
                             'queued': len(s.waiting[model]), 'lanes': {'work': lane.state()},
                             'quota': self.claude_quota if model == 'claude' else self.gpt['work'].quota,
                             'access': A1.CLAUDE_ACCESS.get(self.claude_cfg['perm'], self.claude_cfg['perm'])
                             if model == 'claude' else A1.GPTEngine.access.fget(self.gpt['work'])}
        retry = [{'id': j['id'], 'model': j.get('model'), 'error': j.get('error', '上次调用中断'),
                  'status': j['status'], 'interruption': j.get('interruption'),
                  'seqs': j['seqs']} for j in s.jobs if j['status'] in ('failed', 'interrupted')]
        return {'models': models, 'conn': s.phase, 'mode': s.mode, 'retry': retry,
                'busyRooms': [rid for rid, x in self.sessions.items() if x.busy]}

    def save_prefs(self):
        self.prefs.update({"route": self.route, "claude_model": self.claude_cfg["model"], "claude_effort": self.claude_cfg["effort"],
                           "gpt_model": self.gpt_cfg.get("model"), "gpt_effort": self.gpt_cfg.get("effort"),
                           "gemini_model": self.gemini_cfg["model"], "gemini_effort": self.gemini_cfg["effort"],
                           "room_id": self.rid if self.room else None})
        C.save_json(PREFS, self.prefs)

    def claude_options(self):
        return {"models": [{"id": m, "label": l} for m, l in A1.CLAUDE_MODELS], "model": self.claude_cfg["model"],
                "efforts": [{"id": e, "label": e} for e in A1.CLAUDE_EFFORTS], "effort": self.claude_cfg["effort"]}

    def gemini_options(self):
        families = self.gemini_families or [(self.gemini_cfg["model"], self.gemini_cfg["model"])]
        return {"models": [{"id": f, "label": l} for f, l in families], "model": self.gemini_cfg["model"],
                "efforts": [{"id": e, "label": e} for e in GEMINI_EFFORTS], "effort": self.gemini_cfg["effort"]}

    def gpt_options(self):
        eng = self.gpt["work"]
        if not self.gpt_cfg.get("model"):
            try:
                import tomllib
                cfg = tomllib.load(open(os.path.join(C.CODEX_HOME, "config.toml"), "rb"))
            except Exception:
                cfg = {}
            self.gpt_cfg["model"] = cfg.get("model") or (eng.models[0]["id"] if eng.models else None)
            self.gpt_cfg["effort"] = self.gpt_cfg.get("effort") or cfg.get("model_reasoning_effort")
        info = next((m for m in eng.models if m.get("id") == self.gpt_cfg["model"]), {})
        efforts = [e.get("reasoningEffort") for e in info.get("supportedReasoningEfforts") or [] if e.get("reasoningEffort")]
        if self.gpt_cfg.get("effort") and self.gpt_cfg["effort"] not in efforts:
            efforts.append(self.gpt_cfg["effort"])
        return {"models": [{"id": m["id"], "label": m.get("displayName") or m["id"]} for m in eng.models] or
                          [{"id": self.gpt_cfg["model"] or "", "label": self.gpt_cfg["model"] or "—"}],
                "model": self.gpt_cfg["model"], "efforts": [{"id": e, "label": e} for e in efforts], "effort": self.gpt_cfg.get("effort")}


def new_room_dir(proj_id, proj_name, root, title):
    rid = re.sub(r'[<>:"/\\|?*]', "_", f"{proj_name}-{time.strftime('%Y%m%d-%H%M%S')}")
    d = os.path.join(C.ROOMS_DIR, rid)
    os.makedirs(d, exist_ok=True)
    C.save_json(os.path.join(d, "room.json"), {"name": title, "title": title, "project": proj_name,
                                               "project_id": None if proj_id == "_" else proj_id, "root": root, "created": time.time()})
    return rid


class Api:
    def __init__(self, app):
        self._app = app

    def init(self):
        a = self._app
        s = a.sess()
        lines = C.read_after(a.room, 0)
        with a.blk:
            a.last = lines[-1]["seq"] if lines else 0
            a.buf = {}
            live = {k: {"text": s.presented.get(k, ""), "activity": s.act.get(k, "")}
                    for k, l in s.lanes.items() if l.busy and not l.quiet and s.presented.get(k)}
        lines = [p for e in lines if (p := public_event(e)) is not None]
        return {"tree": A1.conversations(), "room": a.rid, "title": a.room["name"], "lines": lines[-300:], "live": live,
                "route": a.route, "members": list(s.members),
                "setup": {name: {"found": bool(path), "note": None if path else backends.missing_note(name)}
                          for name, path in backends.report().items()},
                "options": {"claude": a.claude_options(), "gpt": a.gpt_options(), "gemini": a.gemini_options()},
                "status": a.status_payload()}

    def tree(self):
        return A1.conversations()

    def switch_room(self, rid):
        a = self._app
        if rid != a.rid:
            with a.blk:
                a.room = a.sessions[rid].room if rid in a.sessions else C.room_by_dir(os.path.join(C.ROOMS_DIR, rid))
                a.buf = {}
            a.save_prefs()
            if a.window:
                a.window.set_title(f"群聊 · Coordinator · {a.room['name']}")
        return self.init()

    def new_room(self, pid):
        proj = next((p for p in A1.conversations() if p["id"] == pid), None)
        if not proj or not proj.get("root"):
            return {"error": "找不到这个项目"}
        return self.switch_room(new_room_dir(pid, proj["name"], proj["root"], "新对话 " + time.strftime("%m-%d %H:%M")))

    def archive_room(self, rid, archived=True):
        a = self._app
        path = os.path.join(C.ROOMS_DIR, rid, "room.json")
        info = C.load_json(path, None)
        if info is None:
            return {"error": "找不到这个对话"}
        s = a.sessions.get(rid)
        if archived and s and s.busy:
            return {"error": "这个群聊里有模型正在回答，等它说完再归档"}
        info.update(archived=bool(archived), archived_at=time.time() if archived else None)
        C.save_json(path, info)
        if archived and s:
            s.shutdown()
            a.sessions.pop(rid, None)
        for role in ROLES:
            tid = C.load_json(os.path.join(C.ROOMS_DIR, rid, f"gpt-{role}.json"), {}).get("thread")
            eng = a.gpt[role]
            if tid and eng.ready:
                try:
                    eng.call("thread/archive" if archived else "thread/unarchive", {"threadId": tid})
                    if archived:
                        eng.loaded.discard(tid)
                        eng.lanes.pop(tid, None)
                except Exception as ex:
                    log("gpt archive", ex)
        if archived and rid == a.rid:
            rest = [r for p in A1.conversations() for r in p["rooms"] if not r["archived"] and r["id"] != rid]
            if rest:
                return self.switch_room(max(rest, key=lambda r: r["at"])["id"])
            return self.new_room(info.get("project_id") or "_")
        return {"tree": A1.conversations(), "room": a.rid, "title": a.room["name"], "keep": True}

    def rename_room(self, rid, title):
        title = (title or "").strip()[:60]
        path = os.path.join(C.ROOMS_DIR, rid, "room.json")
        info = C.load_json(path, None)
        if not title or info is None:
            return {"error": "改名失败"}
        info.update(name=title, title=title)
        C.save_json(path, info)
        a = self._app
        if rid in a.sessions:
            a.sessions[rid].room["name"] = title
        if rid == a.rid:
            a.room["name"] = title
            if a.window:
                a.window.set_title(f"群聊 · Coordinator · {title}")
        for role in ROLES:
            tid = C.load_json(os.path.join(C.ROOMS_DIR, rid, f"gpt-{role}.json"), {}).get("thread")
            if tid and a.gpt[role].ready:
                try:
                    a.gpt[role].call("thread/name/set", {"threadId": tid, "name": f"{title} · 持续会话"})
                except Exception as ex:
                    log("gpt rename", ex)
        return {"tree": A1.conversations(), "title": title}

    def set_route(self, route):
        if route not in ('auto', 'claude', 'gpt', 'both', 'gemini', 'all'):
            return {'error': '无效接收者'}
        self._app.route = route
        self._app.save_prefs()

    def set_model(self, who, model, effort):
        a = self._app
        if who == "claude":
            a.claude_cfg.update(model=model, effort=effort)
            opts = a.claude_options()
        elif who == "gemini":
            a.gemini_cfg.update(model=model, effort=effort if effort in GEMINI_EFFORTS else "high")
            opts = a.gemini_options()
        else:
            a.gpt_cfg.update(model=model, effort=effort)
            opts = a.gpt_options()
        a.save_prefs()
        a.js("options", {who: opts})

    def stop(self, key):
        try:
            ok = self._app.sess().stop(key)
        except Exception as ex:
            log("stop", ex)
            return {"error": f"停止失败：{ex}"[:200]}
        return {"ok": ok}

    def image(self, path):
        return A1.Api.image(self, path)

    def attach(self, data_url, name=""):
        """A picture the user pasted, dropped or picked: saved under the conversation, sent later by path."""
        m = re.match(r"data:(image/[\w.+-]+);base64,(.*)$", data_url or "", re.S)
        ext = m and {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(m.group(1))
        if not ext:
            return {"error": "只能附图片（png / jpg / gif / webp）"}
        folder = os.path.join(self._app.room["dir"], "attachments")
        os.makedirs(folder, exist_ok=True)
        stem = re.sub(r"[^\w一-鿿-]+", "_", os.path.splitext(name or "")[0])[:40] or "图片"
        path = os.path.join(folder, time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4] + "-" + stem + ext)
        with open(path, "wb") as f:
            f.write(base64.b64decode(m.group(2)))
        return {"path": path}

    def open_file(self, path):
        """Open a path shown in the chat. Documents open with their usual program, text and code in the editor,
        folders in Explorer; programs and scripts are never run from here, only shown in their folder."""
        found = resolve_path(path, self._app.room)
        if not found:
            return {"error": f"找不到文件：{path}"}
        try:
            open_path(found)
            return {"ok": True}
        except Exception as ex:
            log("open " + found, ex)
            return {"error": f"打不开：{ex}"[:200]}

    def open_url(self, url):
        if re.match(r"(?i)(https?:|mailto:)", url or ""):
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        return self.open_file(url)

    def set_mode(self, mode):
        if mode not in ('normal', 'collaboration'):
            return {'error': '无效模式'}
        self._app.sess().enqueue('mode', mode=mode)
        return {'queued': True}

    def retry(self, job_id):
        self._app.sess().enqueue('retry', id=job_id)
        return {'queued': True}

    def send(self, text, route, images=None):
        imgs = [p for p in (images or []) if os.path.isfile(p)]
        self._app.sess().enqueue('user', text=text, route=route, images=imgs)
        return {"queued": True}


def first_room(app):
    rooms = [r for p in A1.conversations() for r in p["rooms"] if not r["archived"]]
    if rooms:
        app.room = C.room_by_dir(os.path.join(C.ROOMS_DIR, max(rooms, key=lambda r: r["at"])["id"]))
        return
    projects = [p for p in A1.conversations() if p.get("root") and p["id"] != "_"]
    here = C.resolve_room(A1.DEFAULT_CWD)
    pid, pname, root = ((here["project_id"], here["name"], here["root"]) if here["project_id"]
                        else (projects[0]["id"], projects[0]["name"], projects[0]["root"]) if projects
                        else ("_", os.path.basename(A1.DEFAULT_CWD) or "默认", A1.DEFAULT_CWD))
    app.room = C.room_by_dir(os.path.join(C.ROOMS_DIR, new_room_dir(pid, pname, root, "第一个群聊")))


def main():
    import tray
    tray.CLASS = "GroupChat2TrayWindow"  # its own tray and single-instance identity: runs beside the first client
    if tray.bring_to_front():
        return
    app = App()
    if app.room is None:
        first_room(app)
    for eng in app.gpt.values():
        threading.Thread(target=eng.start, daemon=True).start()
    threading.Thread(target=A1.probe_claude_usage, args=(app,), daemon=True).start()

    def load_gemini():
        if not backends.find("agy"):
            return
        app.gemini_families = gemini_models()
        if app.gemini_families:
            app.js("options", {"gemini": app.gemini_options()})
    threading.Thread(target=load_gemini, daemon=True).start()
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("claude-codex.groupchat2")
    except Exception:
        pass
    html = open(os.path.join(HERE, "ui.html"), encoding="utf-8").read()
    app.window = webview.create_window(f"群聊 · Coordinator · {app.room['name']}", html=html, js_api=Api(app),
                                       width=1440, height=920, min_size=(900, 600), text_select=True)
    quitting = {"now": False}

    def on_closing():
        if quitting["now"]:
            return True
        app.window.hide()
        return False

    def show():
        app.window.show()
        app.window.restore()

    def quit_all():
        quitting["now"] = True
        app.window.destroy()

    app.window.events.closing += on_closing
    tray.Tray(ICON, "群聊 · Coordinator", show, quit_all).start()
    threading.Thread(target=app.pump, daemon=True).start()
    webview.start(gui="edgechromium", private_mode=False, icon=ICON)  # window and taskbar icon (the process is pythonw)
    for s in list(app.sessions.values()):
        s.shutdown()
    app.classifier.close()
    for eng in app.gpt.values():
        if eng.p:
            eng.p.kill()


if __name__ == "__main__":
    main()
