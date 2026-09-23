"""Open a room and hand-join sessions.

  python room.py new-window --cwd <project dir> [--seed <claude transcript.jsonl>] [--minutes 30]
      Activates the project's room and waits for a new Codex window: the first thread
      created in that project within the time limit is bound to the room, and its first
      hook call gives GPT the room note plus the seed conversation.
  python room.py join --cwd <project dir> --session <claude session id>
      Puts an already-running Claude Code session into the room (new sessions join by themselves).
  python room.py status --cwd <project dir>
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

PENDING_INDEX = os.path.join(C.BRIDGE_DIR, "pending-index.json")

ap = argparse.ArgumentParser()
ap.add_argument("cmd", choices=["new-window", "join", "status"])
ap.add_argument("--cwd", required=True)
ap.add_argument("--seed")
ap.add_argument("--session")
ap.add_argument("--minutes", type=int, default=30)
ap.add_argument("--room", help="a separate room in the same project (folder name); default: the project's room")
ap.add_argument("--copy", nargs=3, metavar=("FROM_ROOM", "FIRST", "LAST"),
                help="start the new room's channel with lines FIRST..LAST copied verbatim from another room")
a = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8")

room = C.resolve_room(a.cwd, create=True)
if a.room:
    room = dict(room, name=a.room, dir=os.path.join(C.ROOMS_DIR, a.room))
    os.makedirs(room["dir"], exist_ok=True)
if a.cmd == "new-window":
    if room["project_id"] is None:
        sys.exit(f"{a.cwd} is not inside a Codex project")
    C.save_json(os.path.join(room["dir"], "room.json"),
                {"name": room["name"], "project_id": room["project_id"], "root": room["root"]})
    if a.copy and C.last_seq(room) == 0:
        src = C.room_by_dir(os.path.join(C.ROOMS_DIR, a.copy[0]))
        for e in C.read_after(src, int(a.copy[1]) - 1):
            if e["seq"] <= int(a.copy[2]):
                extra = {k: v for k, v in e.items() if k not in ("seq", "ts", "from", "via", "text")}
                C.append(room, e["from"], e["text"], e["via"], copied_from=f"{a.copy[0]}#{e['seq']}", **extra)
    exp = time.time() + a.minutes * 60
    C.save_json(os.path.join(room["dir"], "pending-codex.json"),
                {"created": time.time(), "expires": exp, "known": C.codex_thread_ids(), "seed": a.seed})
    with C.Lock(PENDING_INDEX):
        idx = C.load_json(PENDING_INDEX, {})
        idx[room["dir"]] = exp
        C.save_json(PENDING_INDEX, idx)
    print(f"room {room['name']} is waiting for a new Codex window (until {time.strftime('%H:%M', time.localtime(exp))})")
elif a.cmd == "join":
    if not C.room_active(room):
        sys.exit(f"room {room['name']} is not active")
    C.bind("claude", a.session, room)
    C.set_cursor(room, a.session, C.last_seq(room))
    print(f"session {a.session} joined {room['name']}")
else:
    b = C.load_json(C.BINDINGS, {})
    print(json.dumps({"room": room["name"], "active": C.room_active(room),
                      "codex": [k for k, v in b.get("codex", {}).items() if v == room["dir"]],
                      "claude": [k for k, v in b.get("claude", {}).items() if v == room["dir"]],
                      "pending": C.load_json(os.path.join(room["dir"], "pending-codex.json"), None) is not None,
                      "lines": C.last_seq(room)}, ensure_ascii=False, indent=1))
