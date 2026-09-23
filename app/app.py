"""群聊客户端（桌面窗口）：在一个窗口里和 GPT、Claude 说话，两边都流式显示。

  pythonw app.py [--cwd <project dir>]

- Claude：客户端托管 Claude Code（stream-json），续上房间里最近的 Claude 会话。它在忙时，发给它的话插进当前这一轮
  （Claude Code 在下一次工具调用之间接收）。
- GPT：客户端起一个 Codex app-server（同一个 codex.exe、同一套配置和 hook），在从房间原 GPT 窗口分叉出的线程上
  回答；它在忙时，发给它的话用 turn/steer 插进当前这一轮。
- 路由（一次只叫醒一个）：写了 @gpt/@claude 或手动选了对象就按指定的；否则都空闲给 Claude，Claude 忙给 GPT，
  GPT 忙给 Claude，都忙插给 GPT。
- 共享通道照旧：用户的话由客户端记进通道，两边的最终回复由各自的 Stop hook 记进通道，另一方在下次开口前看到。
界面是 pywebview 窗口（Edge 内核），和 Python 之间直接调用，不开网络端口。
"""
import argparse, glob, json, os, queue, subprocess, sys, threading, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
sys.path.insert(0, BRIDGE)
import common as C
import webview
from work_record import WorkRecord

DEFAULT_CWD = os.environ.get("GROUPCHAT_CWD") or os.getcwd()  # only used when there is no conversation yet
PREFS = os.path.join(BRIDGE, "app-prefs.json")
GUIDE = os.path.join(HERE, "GROUPCHAT.md")  # the client's own instructions, given to both models only here


def guide_text():
    return open(GUIDE, encoding="utf-8").read()
NO_WINDOW = 0x08000000
CLAUDE_MODELS = [("claude-opus-5-5", "Opus 5.5"), ("claude-fable-5-1", "Fable 5.1"), ("claude-opus-5", "Opus 5"),
                 ("claude-sonnet-5", "Sonnet 5"), ("claude-haiku-4-5", "Haiku 4.5")]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def log(where, ex):
    C.log_error("app " + where, ex)


def record_event(work, method, *args):
    """A receipt failure must never stop streaming or leave a model busy forever."""
    try:
        getattr(work, method)(*args)
    except Exception as ex:
        log("work record " + method, ex)


def claude_exe():
    local, roaming = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
    found = glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"))
    found += glob.glob(os.path.join(roaming, "Claude", "claude-code", "*", "claude.exe"))
    return max(found, key=os.path.getmtime) if found else None


def pct_left(used, scale=100):
    """Remaining share in percent; `scale` is 100 when `used` is a percentage, 1 when it is a fraction."""
    if used is None:
        return None
    return max(0, min(100, round(100 - float(used) * (100 / scale))))


def when(ts):
    if not ts:
        return ""
    try:
        if isinstance(ts, str):
            from datetime import datetime
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        else:
            t = float(ts) / (1000 if float(ts) > 1e11 else 1)
        return time.strftime("%m-%d %H:%M", time.localtime(t))
    except Exception:
        return ""


# ---------------------------------------------------------------- Claude (Claude Code, stream-json)
class ClaudeHost:
    def __init__(self, app, prefs):
        self.app, self.p = app, None
        self.model = prefs.get("claude_model", "claude-opus-5-5")
        self.effort = prefs.get("claude_effort", "medium")
        self.perm = prefs.get("claude_permission", "bypassPermissions")
        self.busy = False
        self.queue = []            # [(text, seq)] waiting for the current turn to end
        self.error = None
        self.quota = None
        self.pending = {}
        self.wlock = threading.Lock()
        self.text_blocks = 0
        self.stopping = False

    @property
    def ready(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        exe = claude_exe()
        if not exe:
            self.error = "找不到 claude.exe"
            return False
        room = self.app.room
        sid = C.load_json(os.path.join(room["dir"], "claude-latest.json"), {}).get("session")
        cmd = [exe, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--model", self.model, "--effort", self.effort, "--permission-mode", self.perm,
               "--append-system-prompt-file", GUIDE]
        if sid:
            cmd += ["--resume", sid]
        else:  # a new conversation: a new Claude session named after it
            cmd += ["--name", room["name"]]
        env = dict(os.environ, GROUPCHAT_HOSTED="1", GROUPCHAT_ROOM=os.path.basename(room["dir"]))  # hooks: plain format
        env.pop("GROUPCHAT_CHANNEL", None)
        self.p = subprocess.Popen(cmd, cwd=room["root"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.error, self.busy = None, False
        threading.Thread(target=self.read, args=(self.p,), daemon=True).start()
        threading.Thread(target=self.read_err, args=(self.p,), daemon=True).start()
        threading.Thread(target=self.ask_usage, daemon=True).start()
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

    def control(self, request, timeout=20):
        rid = "req_" + uuid.uuid4().hex[:12]
        q = queue.Queue()
        self.pending[rid] = q
        self.write({"type": "control_request", "request_id": rid, "request": request})
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.pending.pop(rid, None)

    def ask_usage(self):
        time.sleep(2)
        while self.ready:
            r = self.control({"subtype": "get_usage"})
            if r and r.get("subtype") == "success":
                self.take_usage(r.get("response") or {})
            time.sleep(180)

    def take_usage(self, obj):
        """get_usage reply: rate_limits.seven_day = {utilization: percent, resets_at: ISO time}."""
        w = (obj.get("rate_limits") or {}).get("seven_day") or {}
        if w.get("utilization") is not None:
            self.quota = {"remaining": pct_left(w["utilization"], 100), "resets": when(w.get("resets_at"))}

    def probe_usage(self):
        """Read the weekly quota before any session is started: a throwaway CLI answering one control request (no model call)."""
        exe = claude_exe()
        if not exe:
            return
        try:
            p = subprocess.Popen([exe, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
                                 cwd=os.path.expanduser("~"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=dict(os.environ, GROUPCHAT_OFF="1"), creationflags=NO_WINDOW)
            p.stdin.write(b'{"type":"control_request","request_id":"usage","request":{"subtype":"get_usage"}}\n')
            p.stdin.flush()
            end = time.time() + 30
            for raw in p.stdout:
                ev = json.loads(raw)
                if ev.get("type") == "control_response":
                    self.take_usage((ev.get("response") or {}).get("response") or {})
                    break
                if time.time() > end:
                    break
            p.kill()
        except Exception as ex:
            log("claude usage", ex)

    def send(self, text, seqs):
        if not self.ready and not self.start():
            return False
        C.mark_sent(self.app.room, "claude", text, seqs)
        self.write({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None})
        self.busy, self.text_blocks = True, 0
        return True

    def stop_turn(self):
        """Interrupt the current turn (what Esc does in Claude Code). Messages queued for Claude still follow."""
        if self.busy and self.ready:
            self.stopping = True
            threading.Thread(target=self.control, args=({"subtype": "interrupt"},), daemon=True).start()
            return True
        return False

    def steer(self, text, seq):
        """Mid-turn message: Claude Code takes it in at the next tool-call boundary of the current turn
        (a turn that is only writing text sees it when that text is done)."""
        C.mark_sent(self.app.room, "claude", text, [seq])
        self.write({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None})

    def enqueue(self, text, seq):
        self.queue.append((text, seq))

    def flush_queue(self):
        if self.queue and not self.busy:
            items, self.queue = self.queue, []
            self.send("\n\n".join(t for t, _ in items), [s for _, s in items])

    def read_err(self, p):
        tail = []
        for raw in p.stderr:
            tail = (tail + [raw.decode("utf-8", "replace").strip()])[-5:]
        if p.poll() not in (None, 0):
            self.error = " / ".join(x for x in tail if x)[:200] or f"Claude 进程退出（{p.returncode}）"

    def read(self, p):
        app = self.app
        work = None
        for raw in p.stdout:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type")
            try:
                if t in ("assistant", "user") and isinstance(ev.get("message"), dict):
                    if work is None:
                        work = WorkRecord(app.room, "claude", ev.get("session_id"))
                    record_event(work, "claude", ev)
                if t == "stream_event":
                    e = ev.get("event") or {}
                    et = e.get("type")
                    if et == "content_block_start":
                        cb = e.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            app.activity("claude", f"正在使用工具：{cb.get('name')}")
                        elif cb.get("type") == "text":
                            if self.text_blocks:
                                app.delta("claude", "\n\n")
                            self.text_blocks += 1
                            app.activity("claude", "")
                    elif et == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                        app.delta("claude", e["delta"].get("text", ""))
                elif t == "result":
                    if work is not None:
                        work.session = ev.get("session_id") or work.session
                        record_event(work, "finish", "interrupted" if self.stopping else "error" if ev.get("is_error") else "completed")
                        work = None
                    self.busy = False
                    app.live_end("claude", stopped=self.stopping)
                    self.stopping = False
                    self.flush_queue()
                elif t == "rate_limit_event":
                    week = ((ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("seven_day") or {}
                    if week.get("utilization") is not None:  # a fraction here
                        self.quota = {"remaining": pct_left(week["utilization"], 1), "resets": when(week.get("resetsAt"))}
                elif t == "control_response":
                    r = ev.get("response") or {}
                    q = self.pending.get(r.get("request_id"))
                    if q:
                        q.put(r)
                elif t == "control_request":  # the CLI asking the host something we do not handle
                    self.write({"type": "control_response", "response": {"subtype": "error", "request_id": ev.get("request_id"),
                                                                         "error": "群聊客户端不处理这个请求"}})
            except Exception as ex:
                log("claude read", ex)
        if work is not None:
            try:
                work.finish("process-ended")
            except Exception as ex:
                log("claude work record", ex)
        self.busy = False
        app.live_end("claude")

    def options(self):
        return {"models": [{"id": m, "label": l} for m, l in CLAUDE_MODELS], "model": self.model,
                "efforts": [{"id": e, "label": e} for e in CLAUDE_EFFORTS], "effort": self.effort}

    def apply(self, model, effort):
        """Model/effort take effect by restarting the session (same session id) once Claude is idle."""
        changed = (model, effort) != (self.model, self.effort)
        self.model, self.effort = model, effort
        if changed and self.ready:
            def later():
                while self.busy:
                    time.sleep(0.5)
                self.stop()
                self.start()
            threading.Thread(target=later, daemon=True).start()

    def status(self):
        return {"ready": self.ready or (self.p is None and not self.error), "busy": self.busy, "queued": len(self.queue),
                "quota": self.quota, "error": self.error,
                "access": {"bypassPermissions": "跳过所有确认", "auto": "自动判断", "default": "每次询问",
                           "acceptEdits": "自动接受编辑"}.get(self.perm, self.perm)}


# ---------------------------------------------------------------- GPT (Codex app-server)
class GPTHost:
    def __init__(self, app, prefs):
        self.app, self.p = app, None
        self.model = prefs.get("gpt_model")
        self.effort = prefs.get("gpt_effort")
        self.busy, self.turn = False, None
        self.tid = None
        self.error = None
        self.quota = None
        self.models = []
        self.nid = 0
        self.pending = {}
        self.wlock = threading.Lock()
        self.items = 0
        self.loaded = set()  # threads already open in this engine (no resume needed)
        self.stopping = False

    @property
    def ready(self):
        return self.p is not None and self.p.poll() is None and bool(self.models)  # engine up (the thread may come on first use)

    def start(self):
        try:
            self.p = subprocess.Popen([C.codex_exe(), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                                      env=dict(os.environ, GROUPCHAT_HOSTED="1"))  # its hooks use the client's plain format
            threading.Thread(target=self.read, daemon=True).start()
            self.call("initialize", {"clientInfo": {"name": "groupchat-app", "version": "0.2"}, "capabilities": {"experimentalApi": True}})
            self.notify("initialized")
            self.load_models()
            self.open_thread()
            self.read_limits()
            self.error = None
        except Exception as ex:
            self.error = f"GPT 启动失败：{ex}"[:200]
            log("gpt start", ex)

    def write(self, obj):
        with self.wlock:
            self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()

    def call(self, method, params, timeout=120):
        self.nid += 1
        q = queue.Queue()
        self.pending[self.nid] = q
        self.write({"jsonrpc": "2.0", "id": self.nid, "method": method, "params": params})
        r = q.get(timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message', r['error'])}")
        return r["result"]

    def notify(self, method, params=None):
        self.write({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def load_models(self):
        r = self.call("model/list", {})
        self.models = [m for m in r.get("data", []) if not m.get("hidden")]
        cfg = {}
        try:
            import tomllib
            cfg = tomllib.load(open(os.path.join(C.CODEX_HOME, "config.toml"), "rb"))
        except Exception:
            pass
        self.model = self.model or cfg.get("model") or (self.models[0]["id"] if self.models else None)
        self.effort = self.effort or cfg.get("model_reasoning_effort") or self.model_info().get("defaultReasoningEffort")

    def model_info(self):
        return next((m for m in self.models if m.get("id") == self.model), {})

    def open_thread(self, create=False):
        """Our own thread, forked once from the room's GPT window so GPT keeps its context; resumed afterwards.
        A conversation with no GPT history gets its thread only when GPT is first addressed (create=True),
        so looking at a conversation never leaves an empty thread behind in Codex."""
        room = self.app.room
        mine = C.load_json(os.path.join(room["dir"], "app-gpt-thread.json"), {}).get("thread")
        if mine and mine not in self.loaded:
            try:
                self.call("thread/resume", {"threadId": mine, "excludeTurns": True, "developerInstructions": guide_text()})
            except RuntimeError as ex:
                if "missing source rollout" not in str(ex):
                    raise
                mine = None  # opened but never used, so nothing was saved: start it again
        if mine:
            self.tid = mine
        else:
            src = (C.codex_threads_of(room) or [None])[0]
            if src:
                self.tid = self.call("thread/fork", {"threadId": src, "developerInstructions": guide_text()})["thread"]["id"]
                self.call("thread/name/set", {"threadId": self.tid, "name": f"群聊客户端 · {room['name']}"})
            elif not create:
                self.tid = None
                return
            else:  # a conversation started in the client: a fresh thread that knows it is a group chat
                params = {"cwd": room["root"], "developerInstructions": guide_text()}
                if room.get("project_id"):
                    params["projectId"] = room["project_id"]
                self.tid = self.call("thread/start", params)["thread"]["id"]
                self.call("thread/name/set", {"threadId": self.tid, "name": room["name"]})
            C.save_json(os.path.join(room["dir"], "app-gpt-thread.json"), {"thread": self.tid, "from": src})
        self.loaded.add(self.tid)
        with C.Lock(C.BINDINGS):  # this thread is now the room's GPT window
            b = C.load_json(C.BINDINGS, {})
            codex = {k: v for k, v in b.get("codex", {}).items() if v != room["dir"]}
            codex[self.tid] = room["dir"]
            b["codex"] = codex
            C.save_json(C.BINDINGS, b)
        C.set_cursor(room, self.tid, max(C.get_cursor(room, self.tid), 0))

    def read_limits(self):
        try:
            r = self.call("account/rateLimits/read", {}, timeout=30)
            self.take_limits(r.get("rateLimits") or {})
        except Exception as ex:
            self.quota = {"note": "读取失败"}
            log("gpt limits", ex)

    def take_limits(self, snap):
        wins = [w for w in (snap.get("primary"), snap.get("secondary")) if w]
        if not wins:
            return
        week = max(wins, key=lambda w: w.get("windowDurationMins") or 0)
        note = "" if (week.get("windowDurationMins") or 0) >= 7 * 24 * 60 - 60 else f"（{(week.get('windowDurationMins') or 0) // 60} 小时窗口）"
        self.quota = {"remaining": pct_left(week.get("usedPercent")), "resets": when(week.get("resetsAt")), "note": note}

    def send(self, text, seq):
        if not self.tid:
            self.open_thread(create=True)
        C.mark_sent(self.app.room, "gpt", text, [seq], thread=self.tid)
        params = {"threadId": self.tid, "input": [{"type": "text", "text": text}]}
        if self.model:
            params["model"] = self.model
        if self.effort:
            params["effort"] = self.effort
        self.busy, self.items = True, 0
        self.turn = self.call("turn/start", params)["turn"]["id"]

    def stop_turn(self):
        if self.busy and self.tid and self.turn:
            self.stopping = True
            self.call("turn/interrupt", {"threadId": self.tid, "turnId": self.turn})
            return True
        return False

    def steer(self, text, seq):
        # Codex runs the prompt hook for a steer too: mark the line as already in the channel so it is not recorded twice
        C.mark_sent(self.app.room, "gpt", text, [seq], thread=self.tid)
        self.call("turn/steer", {"threadId": self.tid, "expectedTurnId": self.turn, "input": [{"type": "text", "text": text}]})

    def read(self):
        app = self.app
        work = None
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
                if "id" in m:  # server -> client request (approvals etc.): config says never ask, so decline
                    self.write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "群聊客户端不处理 " + m["method"]}})
                    continue
                meth, p = m.get("method"), m.get("params") or {}
                if p.get("threadId") not in (None, self.tid):
                    continue
                if meth == "turn/started":
                    self.busy, self.turn = True, (p.get("turn") or {}).get("id", self.turn)
                    work = WorkRecord(app.room, "gpt", self.tid, self.turn)
                elif meth == "item/started":
                    it = p.get("item") or {}
                    kind = it.get("type")
                    if kind == "agentMessage":
                        if self.items:
                            app.delta("gpt", "\n\n")
                        self.items += 1
                        app.activity("gpt", "")
                    elif kind in ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "dynamicToolCall"):
                        label = {"commandExecution": "运行命令", "fileChange": "修改文件", "mcpToolCall": "调用工具",
                                 "webSearch": "搜索网页", "dynamicToolCall": "调用工具"}[kind]
                        app.activity("gpt", f"正在{label}")
                elif meth == "item/agentMessage/delta":
                    app.delta("gpt", p.get("delta", ""))
                elif meth == "item/completed":
                    it = p.get("item") or {}
                    if work is not None:
                        record_event(work, "gpt", it)
                    if it.get("type") == "imageGeneration" and it.get("savedPath"):  # GPT made a picture
                        cap = (it.get("revisedPrompt") or "生成的图片").replace("\n", " ")[:80]
                        C.append(app.room, "gpt", f"![{cap}]({it['savedPath']})", "codex", session=self.tid, kind="image")
                elif meth == "turn/completed":
                    if work is not None:
                        record_event(work, "finish", (p.get("turn") or {}).get("status") or "completed")
                        work = None
                    self.busy = False
                    app.live_end("gpt", stopped=self.stopping or (p.get("turn") or {}).get("status") == "interrupted")
                    self.stopping = False
                elif meth == "account/rateLimits/updated":
                    self.take_limits(p.get("rateLimits") or {})
            except Exception as ex:
                log("gpt read", ex)
        if work is not None:
            try:
                work.finish("process-ended")
            except Exception as ex:
                log("gpt work record", ex)
        self.busy = False
        self.error = self.error or "GPT 引擎已退出"

    def options(self):
        info = self.model_info()
        efforts = [e.get("reasoningEffort") for e in info.get("supportedReasoningEfforts") or [] if e.get("reasoningEffort")]
        if self.effort and self.effort not in efforts:
            efforts.append(self.effort)
        return {"models": [{"id": m["id"], "label": m.get("displayName") or m["id"]} for m in self.models] or [{"id": self.model or "", "label": self.model or "—"}],
                "model": self.model, "efforts": [{"id": e, "label": e} for e in efforts], "effort": self.effort}

    def apply(self, model, effort):
        self.model, self.effort = model, effort  # used from the next turn on

    def status(self):
        return {"ready": self.ready, "busy": self.busy, "queued": 0, "quota": self.quota, "error": self.error,
                "access": self.access}

    @property
    def access(self):
        try:
            import tomllib
            cfg = tomllib.load(open(os.path.join(C.CODEX_HOME, "config.toml"), "rb"))
        except Exception:
            return ""
        sandbox = {"danger-full-access": "完全访问", "workspace-write": "工作区可写", "read-only": "只读"}.get(cfg.get("sandbox_mode"), cfg.get("sandbox_mode", ""))
        return sandbox + ("" if cfg.get("approval_policy") in (None, "never") else f" · 审批 {cfg.get('approval_policy')}")


# ---------------------------------------------------------------- the app
def room_title(info, rid):
    return info.get("title") or info.get("name") or rid


def conversations():
    """Codex projects, each with its group-chat conversations (rooms), most recently active first."""
    projects = {pid: {"id": pid, "name": name, "root": root, "rooms": []} for pid, name, root in C._read_projects()}
    for f in glob.glob(os.path.join(C.ROOMS_DIR, "*", "room.json")):
        d = os.path.dirname(f)
        info = C.load_json(f, {})
        ch = os.path.join(d, "channel.jsonl")
        at = os.path.getmtime(ch) if os.path.exists(ch) else os.path.getmtime(f)
        proj = projects.setdefault(info.get("project_id") or "_", {"id": info.get("project_id") or "_", "name": info.get("project") or "其他",
                                                                   "root": info.get("root"), "rooms": []})
        proj["rooms"].append({"id": os.path.basename(d), "title": room_title(info, os.path.basename(d)), "at": at,
                              "when": time.strftime("%m-%d %H:%M", time.localtime(at)), "archived": bool(info.get("archived"))})
    out = list(projects.values())
    for p in out:
        p["rooms"].sort(key=lambda r: -r["at"])
    out.sort(key=lambda p: -(p["rooms"][0]["at"] if p["rooms"] else 0))
    return out


class App:
    def __init__(self, cwd):
        self.prefs = C.load_json(PREFS, {})
        rid = self.prefs.get("room_id")
        if rid and not C.load_json(os.path.join(C.ROOMS_DIR, rid, "room.json"), {"archived": True}).get("archived"):
            self.room = C.room_by_dir(os.path.join(C.ROOMS_DIR, rid))
        else:
            self.room = C.resolve_room(cwd)
        self.route = self.prefs.get("route", "auto")
        self.window = None
        self.last = 0
        self.buf = {}
        self.blk = threading.Lock()
        self.claude = ClaudeHost(self, self.prefs)
        self.gpt = GPTHost(self, self.prefs)

    # --- pushes to the page
    def js(self, fn, *args):
        if self.window:
            try:
                self.window.evaluate_js(f"gc.{fn}(" + ",".join(json.dumps(a, ensure_ascii=False) for a in args) + ")")
            except Exception as ex:
                log("js", ex)

    def delta(self, who, text):
        with self.blk:
            self.buf[who] = self.buf.get(who, "") + text

    def activity(self, who, label):
        self.flush()
        self.js("activity", who, label)

    def live_end(self, who, stopped=False):
        self.flush()
        self.js("liveEnd", who, bool(stopped))

    def flush(self):
        with self.blk:
            buf, self.buf = self.buf, {}
        for who, text in buf.items():
            if text:
                self.js("delta", who, text, False)

    def pump(self):
        n = 0
        while True:
            time.sleep(0.08)
            self.flush()
            n += 1
            if n % 5 == 0:
                es = C.read_after(self.room, self.last)
                if es:
                    self.last = es[-1]["seq"]
                    self.js("lines", es)
            if n % 12 == 0:
                self.js("status", self.status_payload())

    def status_payload(self):
        return {"models": {"claude": self.claude.status(), "gpt": self.gpt.status()}, "conn": ""}

    def save_prefs(self):
        self.prefs.update({"route": self.route, "claude_model": self.claude.model, "claude_effort": self.claude.effort,
                           "gpt_model": self.gpt.model, "gpt_effort": self.gpt.effort})
        C.save_json(PREFS, self.prefs)


class Api:
    """Called from the page (window.pywebview.api.*)."""

    def __init__(self, app):
        # pywebview recursively exposes public attributes; keep the application
        # (including native window and subprocess objects) outside the JS API.
        self._app = app

    def init(self):
        a = self._app
        lines = C.read_after(a.room, 0)
        a.last = lines[-1]["seq"] if lines else 0
        return {"tree": conversations(), "room": os.path.basename(a.room["dir"]), "title": a.room["name"], "lines": lines[-200:],
                "options": {"claude": a.claude.options(), "gpt": a.gpt.options()}, "status": a.status_payload()}

    def tree(self):
        return conversations()

    def switch_room(self, rid):
        a = self._app
        if rid == os.path.basename(a.room["dir"]):
            return self.init()
        if a.claude.busy or a.gpt.busy:
            return {"error": "有模型正在回答，等它说完再切换"}
        a.claude.stop()
        a.claude.queue = []
        a.room = C.room_by_dir(os.path.join(C.ROOMS_DIR, rid))
        a.prefs["room_id"] = rid
        a.save_prefs()
        if a.gpt.p and a.gpt.p.poll() is None:
            try:
                a.gpt.tid = None
                a.gpt.open_thread()
                a.gpt.error = None
            except Exception as ex:
                a.gpt.error = f"GPT 打开对话失败：{ex}"[:200]
                log("gpt switch", ex)
        if a.window:
            a.window.set_title(f"群聊客户端 · {a.room['name']}")
        return self.init()

    def new_room(self, pid):
        proj = next((p for p in conversations() if p["id"] == pid), None)
        if not proj or not proj.get("root"):
            return {"error": "找不到这个项目"}
        title = "新对话 " + time.strftime("%m-%d %H:%M")
        rid = C.re.sub(r'[<>:"/\\|?*]', "_", f"{proj['name']}-{time.strftime('%Y%m%d-%H%M%S')}")
        d = os.path.join(C.ROOMS_DIR, rid)
        os.makedirs(d, exist_ok=True)
        C.save_json(os.path.join(d, "room.json"), {"name": title, "title": title, "project": proj["name"],
                                                   "project_id": None if pid == "_" else pid, "root": proj["root"], "created": time.time()})
        return self.switch_room(rid)

    def archive_room(self, rid, archived=True):
        """Hide a conversation from the list (or bring it back). Its GPT thread is archived in Codex too; nothing is deleted."""
        a = self._app
        path = os.path.join(C.ROOMS_DIR, rid, "room.json")
        info = C.load_json(path, None)
        if info is None:
            return {"error": "找不到这个对话"}
        current = rid == os.path.basename(a.room["dir"])
        if archived and current and (a.claude.busy or a.gpt.busy):
            return {"error": "有模型正在回答，等它说完再归档"}
        info.update(archived=bool(archived), archived_at=time.time() if archived else None)
        C.save_json(path, info)
        tid = C.load_json(os.path.join(C.ROOMS_DIR, rid, "app-gpt-thread.json"), {}).get("thread")
        if tid and a.gpt.p and a.gpt.p.poll() is None:
            try:
                a.gpt.call("thread/archive" if archived else "thread/unarchive", {"threadId": tid})
                if archived:
                    a.gpt.loaded.discard(tid)
            except Exception as ex:  # e.g. a thread GPT never spoke in has nothing to archive
                log("gpt archive", ex)
        if archived and current:  # move to the most recent conversation that is still open
            rest = [r for p in conversations() for r in p["rooms"] if not r["archived"] and r["id"] != rid]
            if rest:
                return self.switch_room(max(rest, key=lambda r: r["at"])["id"])
            pid = info.get("project_id") or "_"
            return self.new_room(pid)
        return {"tree": conversations(), "room": os.path.basename(a.room["dir"]), "title": a.room["name"], "keep": True}

    def rename_room(self, rid, title):
        title = (title or "").strip()[:60]
        path = os.path.join(C.ROOMS_DIR, rid, "room.json")
        info = C.load_json(path, None)
        if not title or info is None:
            return {"error": "改名失败"}
        info.update(name=title, title=title)
        C.save_json(path, info)
        a = self._app
        if rid == os.path.basename(a.room["dir"]):
            a.room["name"] = title
            if a.window:
                a.window.set_title(f"群聊客户端 · {title}")
            if a.gpt.tid:
                try:
                    a.gpt.call("thread/name/set", {"threadId": a.gpt.tid, "name": title})
                except Exception as ex:
                    log("gpt rename", ex)
        return {"tree": conversations(), "title": title}

    def set_route(self, route):
        self._app.route = route
        self._app.save_prefs()

    def set_model(self, who, model, effort):
        host = self._app.claude if who == "claude" else self._app.gpt
        host.apply(model, effort)
        self._app.save_prefs()
        self._app.js("options", {who: host.options()})

    def stop(self, who):
        host = self._app.claude if who == "claude" else self._app.gpt
        try:
            ok = host.stop_turn()
        except Exception as ex:
            log("stop", ex)
            return {"error": f"停止失败：{ex}"[:200]}
        self._app.js("status", self._app.status_payload())
        return {"ok": ok}

    def image(self, path):
        """A local image as a data URI (the page has no file access of its own)."""
        import base64, mimetypes
        path = path.replace("file:///", "").replace("%20", " ")
        mime = mimetypes.guess_type(path)[0] or ""
        if not mime.startswith("image/") or not os.path.isfile(path) or os.path.getsize(path) > 25 * 2**20:
            return None
        return f"data:{mime};base64," + base64.b64encode(open(path, "rb").read()).decode()

    def open_file(self, path):
        path = path.replace("file:///", "")
        if os.path.exists(path):
            os.startfile(path)

    def send(self, text, route):
        a = self._app
        c, g = a.claude, a.gpt
        low = text.lower()
        target = "gpt" if ("@gpt" in low or "@codex" in low) else "claude" if "@claude" in low else None
        if not target:
            if route in ("claude", "gpt"):
                target = route
            elif c.busy and not g.busy:
                target = "gpt"
            elif g.busy and not c.busy:
                target = "claude"
            elif c.busy and g.busy:
                target = "gpt"
            else:
                target = "claude"
        if target == "gpt":
            if not g.ready:
                return {"error": g.error or "GPT 还没连上"}
            mode = "steer" if g.busy else "start"
        else:
            mode = "steer" if c.busy else "start"
        e = C.append(a.room, "user", text, "client", session="client", to=[target], mode=mode)
        try:
            if target == "gpt":
                g.steer(text, e["seq"]) if mode == "steer" else g.send(text, e["seq"])
            elif mode == "steer":
                c.steer(text, e["seq"])
            elif not c.send(text, [e["seq"]]):
                return {"error": c.error}
        except Exception as ex:
            log("send", ex)
            return {"error": f"发送失败：{ex}"[:200]}
        a.js("status", a.status_payload())
        return {"target": target, "mode": mode}


def first_open(app):
    """No usable conversation for the start folder: open the most recent one, or make the first one
    (in the Codex project holding the folder, else the first Codex project, else a plain folder room)."""
    rooms = [r for p in conversations() for r in p["rooms"] if not r["archived"]]
    if rooms:
        app.room = C.room_by_dir(os.path.join(C.ROOMS_DIR, max(rooms, key=lambda r: r["at"])["id"]))
        return
    here = C.resolve_room(DEFAULT_CWD)
    projects = [p for p in conversations() if p.get("root") and p["id"] != "_"]
    pid, pname, root = ((here["project_id"], here["name"], here["root"]) if here["project_id"]
                        else (projects[0]["id"], projects[0]["name"], projects[0]["root"]) if projects
                        else (None, os.path.basename(DEFAULT_CWD) or "默认", DEFAULT_CWD))
    title = "第一个群聊"
    rid = C.re.sub(r'[<>:"/\\|?*]', "_", f"{pname}-{time.strftime('%Y%m%d-%H%M%S')}")
    d = os.path.join(C.ROOMS_DIR, rid)
    os.makedirs(d, exist_ok=True)
    C.save_json(os.path.join(d, "room.json"), {"name": title, "title": title, "project": pname, "project_id": pid,
                                               "root": root, "created": time.time()})
    app.room = C.room_by_dir(d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", default=DEFAULT_CWD)
    a = ap.parse_args()
    import tray
    if tray.bring_to_front():  # already running (maybe hidden in the tray): show that one instead
        return
    app = App(a.cwd)
    if not C.room_active(app.room) or C.load_json(os.path.join(app.room["dir"], "room.json"), {}).get("archived"):
        first_open(app)
    threading.Thread(target=app.gpt.start, daemon=True).start()
    threading.Thread(target=app.claude.probe_usage, daemon=True).start()
    try:  # its own taskbar identity, so the window shows the group-chat icon instead of Python's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("claude-codex.groupchat")
    except Exception:
        pass
    # html= (not a file path): pywebview would otherwise serve the page from a local HTTP server
    html = open(os.path.join(HERE, "ui.html"), encoding="utf-8").read()
    app.window = webview.create_window(f"群聊客户端 · {app.room['name']}", html=html, js_api=Api(app),
                                       width=1440, height=920, min_size=(900, 600), text_select=True)
    quitting = {"now": False}

    def on_closing():  # the close button hides to the tray; the models keep running
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
    tray.Tray(os.path.join(HERE, "groupchat.ico"), "群聊：GPT + Claude", show, quit_all).start()
    threading.Thread(target=app.pump, daemon=True).start()
    webview.start(gui="edgechromium", private_mode=False)
    app.claude.stop()
    if app.gpt.p:
        app.gpt.p.kill()


if __name__ == "__main__":
    main()
