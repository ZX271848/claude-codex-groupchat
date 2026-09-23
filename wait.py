"""Blocks until a user or an explicit GPT address requests Claude, then exits.

  python wait.py --session <claude session id>

Run it with Bash run_in_background: its exit is what wakes an idle Claude session.
It carries no channel content itself: the lines are handed over by the hooks at the
turn boundary (UserPromptSubmit of the woken turn, or Stop if Claude was mid-turn),
so nothing lands in the middle of a turn.
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

ap = argparse.ArgumentParser()
ap.add_argument("--session", required=True)
a = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8")
rdir = C.binding("claude", a.session)
if not rdir:
    sys.exit(f"session {a.session} is not in any room")
room = C.room_by_dir(rdir)
marker = os.path.join(room["dir"], f"waiter-{a.session}.json")  # lets the client show that Claude is listening
C.save_json(marker, {"pid": os.getpid(), "session": a.session, "since": time.time()})
try:
    while True:
        hits = [e["seq"] for e in C.unseen(room, a.session) if C.addressed_to(e, "claude")]
        if hits:
            print(C.note(f"用户或 GPT 在共享通道里点名了你（第 {'、'.join(map(str, hits))} 条）。原话保留来源，会在这一轮开始时由 hook 递给你。"))
            break
        time.sleep(1)
finally:
    try:
        os.remove(marker)
    except OSError:
        pass
