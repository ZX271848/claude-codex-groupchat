"""把第一版客户端的一个群聊接到前后台版继续。

  python import_v1.py "<第一版的群聊目录名>" [--dry]        # 第一版 rooms/ 下的群聊目录名

- Claude 后台：分叉那边的 Claude 会话——复制到上一轮结束为止的消息链，换新会话 id、uuid 不变（和 --fork-session
  写出来的一样）。那边如果正在回答，这一轮不带过来。
- GPT 后台：Codex thread/fork，开发者说明换成 work.md。
- 群聊记录：那边到分叉点为止的原话照原编号抄过来（模型的发言标为 work：它们就是两个后台说过的话），后面接一条分隔说明。
  两个后台的游标停在各自已经看过的地方，下次开口时补看没看过的；两个前台是新会话，从这些原话和工作记录读起。
第一版的文件和会话都只读，不改。
"""
import glob, importlib.util, json, os, re, sys, time, uuid
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("app2", os.path.join(HERE, "app.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
C = A.C
V1_ROOMS = os.path.join(A.BRIDGE, "rooms")
CHAIN = {"user", "assistant", "attachment", "system"}


def claude_fork(sid, busy, root, dry):
    """Copy the session's message chain up to the last finished turn into a new session file. Returns (new id, cut prompt text)."""
    src = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", sid + ".jsonl"))[0]
    lines = open(src, encoding="utf-8").read().splitlines()
    es = [json.loads(l) for l in lines]
    cut, prompt = len(es), None
    if busy:  # the turn running now stays behind: cut at the prompt that opened it
        last_stop = max((i for i, e in enumerate(es) if e.get("type") == "system" and e.get("subtype") == "stop_hook_summary"), default=-1)
        opener = next((i for i in range(last_stop + 1, len(es)) if es[i].get("type") == "user" and es[i].get("promptSource")), None)
        if opener is not None:
            cut = opener
            c = es[opener]["message"]["content"]
            prompt = c if isinstance(c, str) else "".join(b.get("text", "") for b in c if b.get("type") == "text")
    new = str(uuid.uuid4())
    kept = [dict(e, sessionId=new) for e in es[:cut] if e.get("type") in CHAIN and e.get("uuid")]
    # the chain must be whole from the leaf back to its root / compaction boundary, or --resume would load a broken history
    by = {e["uuid"]: e for e in kept}
    node, depth = kept[-1], 0
    while node.get("parentUuid"):
        node = by.get(node["parentUuid"])
        assert node is not None, "消息链断了，停止分叉"
        depth += 1
    if cut < len(es):
        assert es[cut]["parentUuid"] == kept[-1]["uuid"], "截断点不是上一轮的结尾"
    title = next((e for e in reversed(es[:cut]) if e.get("type") == "custom-title"), None)
    print(f"Claude: {sid} 共 {len(es)} 行 -> 保留到第 {cut} 行，消息链 {len(kept)} 条（叶子往回 {depth} 步到起点）")
    if not dry:
        # into the project folder of the room's root (the one --resume looks in first); the source may live elsewhere
        proj = os.path.join(os.path.expanduser("~"), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", root))
        dst = os.path.join(proj if os.path.isdir(proj) else os.path.dirname(src), new + ".jsonl")
        with open(dst, "w", encoding="utf-8") as f:
            for e in kept:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            if title:
                f.write(json.dumps(dict(title, sessionId=new, customTitle=(title.get("customTitle") or "") + " · 后台（接自第一版）"),
                                   ensure_ascii=False) + "\n")
        print("  写入", dst)
    return new, prompt


def gpt_fork(tid, root, name, dry):
    if dry:
        return None
    eng = A.GPTEngine(None, "work")
    eng.start()
    if eng.error:
        raise RuntimeError(eng.error)
    try:
        new = eng.call("thread/fork", {"threadId": tid, "cwd": root, "approvalPolicy": "never",
                                       "developerInstructions": A.prompt_text("work.md"), "excludeTurns": True})["thread"]["id"]
        eng.call("thread/name/set", {"threadId": new, "name": f"{name} · 群聊后台（接自第一版）"})
    finally:
        eng.p.kill()
    print(f"GPT: {tid} -> {new}")
    return new


def main():
    name, dry = sys.argv[1], "--dry" in sys.argv
    v1 = os.path.join(V1_ROOMS, name)
    r1 = C.load_json(os.path.join(v1, "room.json"), {})
    chan = [json.loads(l) for l in open(os.path.join(v1, "channel.jsonl"), encoding="utf-8")]
    status = C.load_json(os.path.join(v1, "status.json"), {})
    sid = C.load_json(os.path.join(v1, "claude-latest.json"), {})["session"]
    gtid = C.load_json(os.path.join(v1, "app-gpt-thread.json"), {})["thread"]
    if (status.get("gpt") or {}).get("state") == "busy":
        sys.exit("GPT 在第一版里正在回答，等它说完再接")

    new_sid, prompt = claude_fork(sid, (status.get("claude") or {}).get("state") == "busy", r1["root"], dry)
    fork_seq = chan[-1]["seq"]
    if prompt is not None:  # the line that opened the turn left behind, and everything after it, stay in the first client
        fork_seq = max(e["seq"] for e in chan if e["from"] == "user" and e["text"].strip() == prompt.strip()) - 1
    lines = [e for e in chan if e["seq"] <= fork_seq]
    last_claude = max((e for e in lines if e["from"] == "claude"), key=lambda e: e["seq"])
    claude_cur = max(e["seq"] for e in lines if e["from"] != "claude" and e["ts"] <= last_claude["ts"])
    gpt_cur = min(C.get_cursor({"dir": v1}, gtid), fork_seq)
    print(f"群聊记录：#1–#{fork_seq}（共 {len(lines)} 条）；Claude 后台已看到 #{claude_cur}，GPT 后台已看到 #{gpt_cur}；两个前台从头读")
    if dry:
        return

    new_tid = gpt_fork(gtid, r1["root"], r1["name"], dry)
    rid = A.new_room_dir(r1.get("project_id") or "_", r1.get("project") or r1["name"], r1["root"], f"{r1['name']} · 接自第一版")
    room = C.room_by_dir(os.path.join(C.ROOMS_DIR, rid))
    with open(C.channel_path(room), "w", encoding="utf-8") as f:
        for e in lines:
            e = dict(e, v1=True)
            if e["from"] in ("claude", "gpt") and e.get("kind") != "tool_summary":
                e["role"] = "work"
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    C.append(room, "client", f"以上是第一版客户端「{r1['name']}」群聊到 #{fork_seq} 为止的记录，从这里起在前后台版继续。"
                             "两边的后台是那边工作会话的分叉；两个前台从这些原话和工作记录开始。", "client", kind="divider")
    C.set_cursor(room, "lane-claude-work", claude_cur)
    C.set_cursor(room, "lane-gpt-work", gpt_cur)
    C.save_json(os.path.join(room["dir"], "claude-work.json"), {"session": new_sid, "forked_from": sid})
    C.save_json(os.path.join(room["dir"], "gpt-work.json"), {"thread": new_tid, "forked_from": gtid})
    prefs = C.load_json(A.PREFS, {})
    prefs["room_id"] = rid
    C.save_json(A.PREFS, prefs)
    print("新群聊：", room["dir"])


if __name__ == "__main__":
    main()
