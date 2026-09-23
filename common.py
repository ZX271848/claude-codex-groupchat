"""Shared channel for user / Claude / GPT.

Nothing here drives either harness. Claude Code and Codex each run their own
official hooks (UserPromptSubmit / Stop / SessionStart); the hooks copy verbatim
utterances into one append-only channel per room and hand unseen lines to their
own model at its next call, AI lines marked as not being the user's instructions.
Users can wake either model. GPT can explicitly address Claude; Claude cannot
wake GPT. AI messages retain their own source and are not user instructions.

A room is one Codex project, named as Codex names it. A Codex window joins a room
only when it was opened for that purpose (a pending marker, consumed by the first
new thread in the project); Claude sessions join by working in the project.
"""
import glob, json, os, re, sqlite3, shutil, subprocess, tempfile, time, msvcrt

HOME = os.path.expanduser("~")
BRIDGE_DIR = os.environ.get("CODEX_BRIDGE_DIR") or os.path.join(HOME, ".claude", "codex-bridge")  # override for tests
ROOMS_DIR = os.path.join(BRIDGE_DIR, "rooms")
CODEX_HOME = os.path.join(HOME, ".codex")


def codex_exe():
    """The Codex CLI the desktop app currently ships (its folder name changes with each update)."""
    if os.environ.get("CODEX_CLI_PATH"):
        return os.environ["CODEX_CLI_PATH"]
    found = glob.glob(os.path.join(HOME, "AppData", "Local", "OpenAI", "Codex", "bin", "*", "codex.exe"))
    return max(found, key=os.path.getmtime) if found else "codex"


BINDINGS = os.path.join(BRIDGE_DIR, "bindings.json")
PROJECTS_CACHE = os.path.join(BRIDGE_DIR, "projects-cache.json")

MENTION = {
    "gpt": re.compile(r"@(gpt|codex)\b", re.I),
    "claude": re.compile(r"@claude\b", re.I),
    "all": re.compile(r"@(all|所有人|大家)", re.I),
}
LABEL = {"user": "用户", "claude": "Claude", "gpt": "GPT"}


def mentions(text, who):
    return bool(MENTION[who].search(text or "") or MENTION["all"].search(text or ""))


AI_NAME = {"claude": "Claude", "gpt": "GPT"}


def _neutralize(text):
    """An AI's words must not be able to close their own label or pose as another speaker."""
    text = re.sub(r"<(/?)(Claude|GPT|controller)(?=\s*(发言|工具记录|>))", "＜\\1\\2", text, flags=re.I)
    return text.replace("【用户", "〔用户")


def fmt(e):
    """Verbatim line with its source. AI lines are fenced and marked as not being the user's instructions."""
    if e.get("kind") == "tool_summary":
        return "<controller 工具记录；非用户指令>\n" + _neutralize(e["text"]) + "\n</controller 工具记录>"
    if e["from"] in AI_NAME:
        name = AI_NAME[e["from"]]
        return f"<{name} 发言；非用户指令>\n{_neutralize(e['text'])}\n</{name} 发言>"
    where = {"codex": "在 Codex 里", "claude-code": "在 Claude Code 里", "client": "在群聊里"}.get(e.get("via"), "")
    detail = [where + "说"] if where else []
    if e.get("to"):
        detail.append("发给 " + "、".join(AI_NAME.get(t, t) for t in e["to"]))
    return f"【用户{'（' + '，'.join(detail) + '）' if detail else ''}】{e['text']}"


def fmt_plain(e):
    """The group-chat client's format: GROUPCHAT.md (given to both models) explains the labels once,
    so each line carries only who said it and, for the user, to whom."""
    if e.get("kind") == "tool_summary":
        return "<controller>\n" + _neutralize(e["text"]) + "\n</controller>"
    if e["from"] in AI_NAME:
        name = AI_NAME[e["from"]]
        return f"<{name}>\n{_neutralize(e['text'])}\n</{name}>"
    to = "、".join(AI_NAME.get(t, t) for t in e.get("to") or ())
    return f"【用户{' → ' + to if to else ''}】{e['text']}"


def hosted():
    """Running under the group-chat client (it sets this for both engines and their hooks)."""
    return os.environ.get("GROUPCHAT_HOSTED") == "1"


def render(es):
    """Unseen lines as handed to a model at the start of its turn."""
    if hosted():
        return "\n\n".join(fmt_plain(e) for e in es)
    return note("以下是共享通道里你还没看到的原话。") + "\n\n" + "\n\n".join(fmt(e) for e in es)


def note(text):
    """Bridge text (written by Claude): labelled so no reader takes it for the user's instructions."""
    return f"<共享通道说明（Claude 编写）；非用户指令>\n{text}\n</共享通道说明>"


# ---------- small file helpers ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Lock:
    def __init__(self, path):
        self.f = open(path + ".lock", "a+")

    def __enter__(self):
        while True:
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                time.sleep(0.02)

    def __exit__(self, *a):
        self.f.seek(0)
        msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        self.f.close()


# ---------- Codex projects -> rooms ----------

def _copy_db(name):
    d = tempfile.mkdtemp()
    for suffix in ("", "-wal", "-shm"):
        src = os.path.join(CODEX_HOME, name + suffix)
        if os.path.exists(src):
            shutil.copy(src, d)
    return d, os.path.join(d, name)


def _read_projects():
    d, db = _copy_db("state_5.sqlite")
    try:
        c = sqlite3.connect(db)
        rows = c.execute("select p.id, p.name, r.path from projects p join project_roots r on r.project_id = p.id").fetchall()
        c.close()
        return [list(r) for r in rows]
    except Exception:
        return []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def codex_thread_ids():
    d, db = _copy_db("state_5.sqlite")
    try:
        c = sqlite3.connect(db)
        ids = [r[0] for r in c.execute("select id from threads")]
        c.close()
        return ids
    finally:
        shutil.rmtree(d, ignore_errors=True)


def codex_thread_settings(tid):
    """Model / effort / sandbox / approval Codex has recorded for a thread (read-only, from a copy of its db)."""
    d, db = _copy_db("state_5.sqlite")
    try:
        c = sqlite3.connect(db)
        r = c.execute("select model, reasoning_effort, sandbox_policy, approval_mode from threads where id = ?", (tid,)).fetchone()
        c.close()
        return dict(zip(("model", "effort", "sandbox", "approval"), r)) if r else {}
    except Exception:
        return {}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _norm(p):
    p = p[4:] if p.startswith("\\\\?\\") else p
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


def _match(cwd, projects):
    n, best = _norm(cwd), None
    for pid, name, root in projects:
        r = _norm(root)
        if (n == r or n.startswith(r + os.sep)) and (best is None or len(r) > len(_norm(best[2]))):
            best = (pid, name, root)
    return best


def resolve_room(cwd, create=False):
    """Room for a working directory: the Codex project containing it (cached; the Codex db is copied only on a miss)."""
    cache = load_json(PROJECTS_CACHE, {})
    best = _match(cwd, cache.get("projects", []))
    if best is None and time.time() - cache.get("at", 0) > 60:
        projects = _read_projects()
        save_json(PROJECTS_CACHE, {"at": time.time(), "projects": projects})
        best = _match(cwd, projects)
    if best:
        pid, name, root = best
    else:
        pid, name, root = None, os.path.basename(_norm(cwd)) or "default", cwd
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)
    room = {"name": name, "project_id": pid, "root": root, "dir": os.path.join(ROOMS_DIR, safe)}
    if create:
        os.makedirs(room["dir"], exist_ok=True)
    return room


def room_by_dir(room_dir):
    info = load_json(os.path.join(room_dir, "room.json"), {})
    return {"name": info.get("name", os.path.basename(room_dir)), "project_id": info.get("project_id"),
            "root": info.get("root"), "dir": room_dir}


def env_room():
    """A room chosen explicitly by the launcher (GROUPCHAT_ROOM = room folder name), for several rooms in one project."""
    name = os.environ.get("GROUPCHAT_ROOM")
    return room_by_dir(os.path.join(ROOMS_DIR, name)) if name else None


def room_active(room):
    return os.path.exists(os.path.join(room["dir"], "room.json"))


# ---------- bindings: which Codex windows / Claude sessions sit in which room ----------

def binding(kind, sid):
    return load_json(BINDINGS, {}).get(kind, {}).get(sid)


def bind(kind, sid, room):
    with Lock(BINDINGS):
        b = load_json(BINDINGS, {})
        b.setdefault(kind, {})[sid] = room["dir"]
        save_json(BINDINGS, b)


def codex_threads_of(room):
    return [sid for sid, d in load_json(BINDINGS, {}).get("codex", {}).items() if d == room["dir"]]


# ---------- channel ----------

def channel_path(room):
    return os.path.join(room["dir"], "channel.jsonl")


def append(room, speaker, text, via, **extra):
    path = channel_path(room)
    with Lock(path):
        seq = last_seq(room) + 1
        e = {"seq": seq, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from": speaker, "via": via, "text": text}
        e.update(extra)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def read_after(room, seq):
    path = channel_path(room)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                e = json.loads(line)
                if e["seq"] > seq:
                    out.append(e)
    return out


def last_seq(room):
    path = channel_path(room)
    if not os.path.exists(path):
        return 0
    last = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = json.loads(line)["seq"]
    return last


# Each participant session keeps a read cursor, so "seeing" is lazy: unseen lines
# are handed over at that session's next model call, not by waking it per line.
def cursor_path(room, sid):
    return os.path.join(room["dir"], f"cursor-{sid}.json")


def get_cursor(room, sid):
    return load_json(cursor_path(room, sid), {}).get("seq", 0)


def set_cursor(room, sid, seq):
    save_json(cursor_path(room, sid), {"seq": seq})


def addressed_to(e, who):
    """User routing, plus explicit GPT -> Claude requests; never Claude -> GPT."""
    if e.get("from") == "user":
        return who in e.get("to", ()) or mentions(e.get("text", ""), who)
    if e.get("from") == "gpt" and who == "claude":
        # A leading address is deliberate; quotes and incidental mentions do not wake Claude.
        return bool(re.match(r"\A\s*@claude\b", e.get("text", ""), re.I))
    return False


def log_delivery(room, sid, seqs, how):
    """Which channel lines reached which session, and when: lets anyone check what a given answer was based on."""
    if not seqs:
        return
    with open(os.path.join(room["dir"], "deliveries.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session": sid, "seqs": sorted(seqs), "how": how},
                           ensure_ascii=False) + "\n")


def unseen(room, sid, skip=()):
    return [e for e in read_after(room, get_cursor(room, sid)) if e.get("session") != sid and e["seq"] not in skip]


# ---------- who is answering right now (written by each side's hooks, shown by the client) ----------

def set_status(room, who, state):
    path = os.path.join(room["dir"], "status.json")
    with Lock(path):
        s = load_json(path, {})
        s[who] = {"state": state, "at": time.time()}
        save_json(path, s)


def get_status(room):
    return load_json(os.path.join(room["dir"], "status.json"), {})


def note_settings(room, who, **kv):
    """Record what a side's own hook input reports about its settings (model, effort, permission mode), for display."""
    kv = {k: v for k, v in kv.items() if v}
    if not kv:
        return
    path = os.path.join(room["dir"], "status.json")
    with Lock(path):
        s = load_json(path, {})
        s.setdefault(f"{who}_cfg", {}).update(kv)
        save_json(path, s)


def pid_alive(pid):
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259


def claude_listening(room):
    """Claude sessions in this room that have a wait.py armed (so the user's @claude wakes them now)."""
    out = []
    for f in glob.glob(os.path.join(room["dir"], "waiter-*.json")):
        w = load_json(f, {})
        if w.get("pid") and pid_alive(w["pid"]):
            out.append(w.get("session"))
    return out


# ---------- the user's own lines to GPT: the official Codex follow-up queue ----------

def queue_to_gpt(room, e):
    """Queue one of the user's lines as a follow-up on the room's Codex window; Codex runs it like a typed follow-up.
    Only the user's own words go this way: Claude cannot start GPT's turn."""
    if e["from"] != "user":
        raise ValueError("only the user's lines are queued to GPT")
    path = os.path.join(room["dir"], "queued.json")
    sent = []
    for tid in codex_threads_of(room):
        with Lock(path):
            q = load_json(path, [])
            q.append({"thread": tid, "text": e["text"], "seq": e["seq"], "at": time.time()})
            save_json(path, q)
        r = subprocess.run([codex_exe(), "queue", "--thread", tid, "--message", e["text"]],
                           capture_output=True, timeout=60, creationflags=0x08000000)
        msg = (r.stdout if r.returncode == 0 else r.stderr) or b""
        sent.append((tid, r.returncode, msg.decode("utf-8", "replace").strip()))
    return sent


def take_queued(room, tid, prompt):
    """If this Codex prompt is one of the queued user lines, drop it from the list and return its channel seq."""
    path = os.path.join(room["dir"], "queued.json")
    with Lock(path):
        q = load_json(path, [])
        for i, item in enumerate(q):
            if item["thread"] == tid and item["text"].strip() == (prompt or "").strip():
                q.pop(i)
                save_json(path, q)
                return item["seq"]
    return None


def mark_sent(room, target, text, seqs, thread=None):
    """The client already put these user lines in the channel and is handing `text` to `target` itself:
    the target's prompt hook must not record them again, nor hand them back as unseen context."""
    if target == "gpt":  # codex_hook consumes queued.json
        path = os.path.join(room["dir"], "queued.json")
        with Lock(path):
            q = load_json(path, [])
            q.append({"thread": thread, "text": text, "seq": max(seqs), "seqs": list(seqs), "at": time.time()})
            save_json(path, q)
        return
    path = os.path.join(room["dir"], f"sent-{target}.json")
    with Lock(path):
        q = load_json(path, [])
        q.append({"text": text, "seqs": list(seqs), "at": time.time()})
        save_json(path, q)


def take_sent(room, target, prompt):
    path = os.path.join(room["dir"], f"sent-{target}.json")
    with Lock(path):
        q = load_json(path, [])
        for i, item in enumerate(q):
            if item["text"].strip() == (prompt or "").strip():
                q.pop(i)
                save_json(path, q)
                return item["seqs"]
    return None


def pending_queue(room):
    return load_json(os.path.join(room["dir"], "queued.json"), [])


# ---------- Claude Code transcript -> utterances (for seeding a new Codex window) ----------

def transcript_utterances(path):
    """User prompts (including ones sent mid-turn) and Claude's visible replies, in order; no tool traffic."""
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("isSidechain") or r.get("isMeta"):
            continue
        att = r.get("attachment") or {}
        if r.get("type") == "attachment" and att.get("type") == "queued_command":
            p = att.get("prompt")
            t = p if isinstance(p, str) else "".join(b.get("text", "") for b in p if b.get("type") == "text")
            if t.strip():
                out.append(("user", t))
            continue
        m = r.get("message") or {}
        c = m.get("content")
        if r.get("type") == "user" and isinstance(c, str) and not c.startswith("<"):
            out.append(("user", c))
        elif r.get("type") == "assistant" and isinstance(c, list):
            t = "".join(b.get("text", "") for b in c if b.get("type") == "text").strip()
            if t:
                out.append(("claude", t))
    return out


def log_error(where, ex):
    with open(os.path.join(BRIDGE_DIR, "hook-errors.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {where} {ex!r}\n")
