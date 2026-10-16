"""Add a member (now: gemini) to a group chat, with the point from which it reads the chat.

  python tools/add_member.py <room folder or room name> [--from-seq N]

The member reads the chat verbatim from N on (default: the last ~55k characters of chat, one delivery); earlier lines stay in
channel.jsonl and its first message names that file. Takes effect when the client (re)opens the room."""
import argparse, json, os, sys, time

STATE = os.environ.get("GROUPCHAT2_STATE") or os.path.join(os.path.expanduser("~"), ".claude", "groupchat2")
os.environ["CODEX_BRIDGE_DIR"] = STATE
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import common as C
from history import context_event

ap = argparse.ArgumentParser()
ap.add_argument("room")
ap.add_argument("--member", default="gemini", choices=["gemini"])
ap.add_argument("--from-seq", type=int)
ap.add_argument("--chars", type=int, default=55000, help="history budget when --from-seq is not given")
a = ap.parse_args()

rid = next((r for r in os.listdir(C.ROOMS_DIR) if r == a.room
            or C.load_json(os.path.join(C.ROOMS_DIR, r, "room.json"), {}).get("name") == a.room), None)
if not rid:
    sys.exit(f"找不到群聊：{a.room}")
d = os.path.join(C.ROOMS_DIR, rid)
room, info = C.room_by_dir(d), C.load_json(os.path.join(d, "room.json"), {})
if a.member in (info.get("members") or []):
    sys.exit(f"{a.member} 已经在 {info.get('name')} 里")
rows = C.read_after(room, 0)
start = a.from_seq
if start is None:  # newest lines first until the budget is used
    size, start = 0, (rows[-1]["seq"] + 1 if rows else 1)
    for e in reversed(rows):
        c = context_event(e, "work", a.member)
        if c is None:
            continue
        size += len(json.dumps(c, ensure_ascii=False))
        if size > a.chars:
            break
        start = e["seq"]
info["members"] = sorted(set(info.get("members") or ["gpt", "claude"]) | {a.member}, key=["gpt", "claude", "gemini"].index)
C.save_json(os.path.join(d, "room.json"), info)
C.set_cursor(room, f"lane-{a.member}-work", start - 1)
C.save_json(os.path.join(d, f"{a.member}-join.json"), {"from_seq": start, "at": time.time()})
print(f"{info.get('name')}: 成员 {info['members']}；{a.member} 从 #{start} 开始读（共 {rows[-1]['seq'] if rows else 0} 条）。重开客户端或切换到这个群聊后生效。")
