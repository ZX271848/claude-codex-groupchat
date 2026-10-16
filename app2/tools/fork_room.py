"""Fork a group chat (room) into a new room: the chat record, Claude's work session and GPT's work thread, all three.

usage: python fork_room.py ROOM_FOLDER_NAME [--title "新名字"] [--dry]
  ROOM_FOLDER_NAME: folder under ~/.claude/groupchat2/rooms (e.g. myproject-20260930-232829)

One-off tool, outside the client: it only reads the source room and its sessions, and writes a new room folder,
a new Claude session file and a new Codex thread. The same steps as import_v1.py (which carried first-client rooms
over), applied to a room of this client:
- Claude: the work session's message chain is copied up to its last finished turn into a new session id (uuids
  kept, like --fork-session). A turn running right now stays behind, and so do the chat lines it was answering.
- GPT: Codex thread/fork of the work thread (refused while a GPT dispatch is still running), then checked by
  resuming it in a fresh app-server, as the client will.
- Chat: lines up to the fork point copied verbatim with their numbers, then a divider line; both work cursors
  stop where each model had read, so each catches up on what it has not seen when next called.
The new room appears in the client's room list (reopen the list or the client if it does not show at once).
"""
import glob, json, os, queue, re, shutil, subprocess, sys, threading, time, uuid
sys.stdout.reconfigure(encoding="utf-8")
HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".claude", "groupchat2")
ROOMS = os.path.join(STATE, "rooms")
WORK_PROMPT = os.path.join(HOME, ".claude", "codex-bridge", "app2", "prompts", "work.md")
CHAIN = {"user", "assistant", "attachment", "system"}
args = sys.argv[1:]
SRC = os.path.join(ROOMS, args[0])
DRY = "--dry" in args
TITLE = args[args.index("--title") + 1] if "--title" in args else None


def load(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------- Codex app-server (same handshake as the client / fork_gpt_thread.py) ----------
class Engine:
    def __init__(self, name):
        exe = os.environ.get("CODEX_CLI_PATH") or max(glob.glob(os.path.join(HOME, "AppData", "Local", "OpenAI", "Codex", "bin", "*", "codex.exe")), key=os.path.getmtime)
        env = dict(os.environ, GROUPCHAT_OFF="1", CODEX_BRIDGE_DIR=STATE)
        for k in ("GROUPCHAT_HOSTED", "GROUPCHAT_ROOM", "GROUPCHAT_CHANNEL"):
            env.pop(k, None)
        self.p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  creationflags=0x08000000, env=env)
        self.nid, self.pending, self.lock = 0, {}, threading.Lock()
        threading.Thread(target=self.read, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": name, "version": "1.0-coordinator"}, "capabilities": {"experimentalApi": True}})
        self.write({"jsonrpc": "2.0", "method": "initialized"})

    def write(self, obj):
        with self.lock:
            self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()

    def call(self, method, params, timeout=300):
        with self.lock:
            self.nid += 1
            nid = self.nid
        q = queue.Queue()
        self.pending[nid] = q
        self.write({"jsonrpc": "2.0", "id": nid, "method": method, "params": params})
        r = q.get(timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message', r['error'])}")
        return r["result"]

    def read(self):
        for raw in self.p.stdout:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if "id" in m and "method" not in m:
                q = self.pending.pop(m["id"], None)
                if q:
                    q.put(m)
            elif "id" in m:
                self.write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not handled"}})

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


def claude_fork(sid, root, title):
    """Copy the session's chain up to its last finished turn into a new session file. Returns (new id, seqs of the turn left behind)."""
    src = glob.glob(os.path.join(HOME, ".claude", "projects", "*", sid + ".jsonl"))[0]
    with open(src, encoding="utf-8") as f:
        es = [json.loads(l) for l in f if l.strip()]
    last_stop = max((i for i, e in enumerate(es) if e.get("type") == "system" and e.get("subtype") == "stop_hook_summary"), default=-1)
    opener = next((i for i in range(last_stop + 1, len(es)) if es[i].get("type") == "user" and es[i].get("promptSource")), None)
    cut, left = len(es), []
    if opener is not None:          # a turn is running: cut at the prompt that opened it
        cut = opener
        c = es[opener]["message"]["content"]
        text = c if isinstance(c, str) else "".join(b.get("text", "") for b in c if b.get("type") == "text")
        left = [int(n) for n in re.findall(r"\[消息 (\d+)\]", text)]
    kept = [dict(e, sessionId=None) for e in es[:cut] if e.get("type") in CHAIN and e.get("uuid")]
    by = {e["uuid"]: e for e in kept}
    node, depth = kept[-1], 0
    while node.get("parentUuid"):   # the chain must be whole from the leaf back to its root / compaction boundary
        node = by.get(node["parentUuid"])
        assert node is not None, "消息链断了，停止分叉"
        depth += 1
    if cut < len(es):
        assert es[cut]["parentUuid"] == kept[-1]["uuid"], "截断点不是上一轮的结尾"
    print(f"Claude: {sid} {len(es)} 行 -> 保留到第 {cut} 行，消息链 {len(kept)} 条（叶子往回 {depth} 步到起点）；"
          f"{'正在进行的一轮留在原处，它在回答的消息 ' + str(left) if opener is not None else '没有进行中的一轮'}")
    new = str(uuid.uuid4())
    if DRY:
        return new, left, None
    proj = os.path.join(HOME, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", root))
    dst = os.path.join(proj if os.path.isdir(proj) else os.path.dirname(src), new + ".jsonl")
    last_title = next((e for e in reversed(es[:cut]) if e.get("type") == "custom-title"), None)
    with open(dst, "w", encoding="utf-8") as f:
        for e in kept:
            e["sessionId"] = new
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        f.write(json.dumps(dict(last_title or {"type": "custom-title"}, sessionId=new, customTitle=title), ensure_ascii=False) + "\n")
    print("  写入", dst, f"{os.path.getsize(dst) / 1e6:.0f} MB")
    return new, left, dst


def gpt_fork(tid, root, title):
    instructions = open(WORK_PROMPT, encoding="utf-8").read()
    eng = Engine("groupchat2-work")
    try:
        new = eng.call("thread/fork", {"threadId": tid, "cwd": root, "approvalPolicy": "never",
                                       "developerInstructions": instructions, "excludeTurns": True})["thread"]["id"]
        eng.call("thread/name/set", {"threadId": new, "name": title})
    finally:
        eng.close()
    time.sleep(2)
    check = Engine("groupchat2-work")      # the client resumes it exactly like this
    try:
        ok = check.call("thread/resume", {"threadId": new, "excludeTurns": True, "developerInstructions": instructions})["thread"]["id"] == new
    finally:
        check.close()
    print(f"GPT: {tid} -> {new}；新线程在全新 app-server 里续接：{'正常' if ok else '失败'}")
    return new


def main():
    info = load(os.path.join(SRC, "room.json"))
    assert info, f"找不到群聊 {SRC}"
    title = TITLE or f"{info['name']} · 分叉 {time.strftime('%m-%d %H:%M')}"
    with open(os.path.join(SRC, "channel.jsonl"), encoding="utf-8") as f:
        chan = [json.loads(l) for l in f if l.strip()]
    running_gpt = [d for d in load(os.path.join(SRC, "dispatches.json"), []) if d.get("model") == "gpt" and d.get("status") not in ("completed", "failed", "interrupted")]
    if running_gpt:
        sys.exit(f"GPT 还有没结束的投递 {[d['id'] for d in running_gpt]}，等它说完再分叉")
    sid = load(os.path.join(SRC, "claude-work.json"))["session"]
    gtid = load(os.path.join(SRC, "gpt-work.json"))["thread"]
    new_sid, left, dst = claude_fork(sid, info["root"], title + " · Claude")
    fork_seq = (min(left) - 1) if left else chan[-1]["seq"]
    lines = [e for e in chan if e["seq"] <= fork_seq]
    claude_cur = min(load(os.path.join(SRC, "cursor-lane-claude-work.json"), {}).get("seq", 0), fork_seq)
    gpt_cur = min(load(os.path.join(SRC, "cursor-lane-gpt-work.json"), {}).get("seq", 0), fork_seq)
    print(f"群聊记录：#1–#{fork_seq}（{len(lines)} 条）；Claude 已看到 #{claude_cur}，GPT 已看到 #{gpt_cur}")
    if DRY:
        print("dry：什么都没写")
        return
    new_tid = gpt_fork(gtid, info["root"], title + " · GPT")
    rid = re.sub(r'[<>:"/\\|?*]', "_", f"{info.get('project') or info['name']}-{time.strftime('%Y%m%d-%H%M%S')}")
    d = os.path.join(ROOMS, rid)
    os.makedirs(d)
    save(os.path.join(d, "room.json"), {"name": title, "title": title, "project": info.get("project"), "project_id": info.get("project_id"),
                                       "root": info["root"], "created": time.time(), "forked_from": {"room": args[0], "seq": fork_seq}})
    with open(os.path.join(d, "channel.jsonl"), "w", encoding="utf-8") as f:
        for e in lines:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        f.write(json.dumps({"seq": fork_seq + 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from": "client", "via": "client",
                            "text": f"以上是群聊「{info['name']}」到 #{fork_seq} 为止的记录。这里是它的分叉：Claude 和 GPT 是那边工作会话的分叉，"
                                    "从这里起两边各自继续，互不影响。", "kind": "divider"}, ensure_ascii=False) + "\n")
    save(os.path.join(d, "cursor-lane-claude-work.json"), {"seq": claude_cur})
    save(os.path.join(d, "cursor-lane-gpt-work.json"), {"seq": gpt_cur})
    for m in ("claude", "gpt"):
        save(os.path.join(d, f"delivered-{m}.json"), {"seqs": []})
    save(os.path.join(d, "dispatches.json"), [])
    save(os.path.join(d, "claude-work.json"), {"session": new_sid, "forked_from": sid})
    save(os.path.join(d, "gpt-work.json"), {"thread": new_tid, "forked_from": gtid})
    if os.path.isdir(os.path.join(SRC, "attachments")):
        shutil.copytree(os.path.join(SRC, "attachments"), os.path.join(d, "attachments"))
    print("新群聊：", d, "|", title)


if __name__ == "__main__":
    main()
