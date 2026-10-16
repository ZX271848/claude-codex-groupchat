"""Live end-to-end for the Gemini member: real agy, cheap model, throwaway state dir and project. Only Gemini is called.
  python qa/test_gemini_live.py"""
import os, sys, tempfile, time
sys.stdout.reconfigure(encoding="utf-8")
STATE = tempfile.mkdtemp(prefix="gc2-gemini-state-")
os.environ["GROUPCHAT2_STATE"] = STATE
PROJ = tempfile.mkdtemp(prefix="gc2-gemini-proj-")
import importlib.util
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
spec =importlib.util.spec_from_file_location("app2", os.path.join(HERE, "app.py"))
A = importlib.util.module_from_spec(spec); spec.loader.exec_module(A)
C = A.C

app = A.App()
rid = A.new_room_dir("_", "gc2-gemini", PROJ, "三人测试")
room_json = os.path.join(C.ROOMS_DIR, rid, "room.json")
C.save_json(room_json, {**C.load_json(room_json, {}), "members": ["gpt", "claude", "gemini"]})
app.room = C.room_by_dir(os.path.join(C.ROOMS_DIR, rid))
app.gemini_cfg.update(model="gemini-3.8-flash", effort="low")
api = A.Api(app)
s = app.sess()
print("members:", s.members, "| lanes:", sorted(s.lanes))
last = 0


def wait_idle(limit=240):
    time.sleep(1.5)
    t0 = time.time()
    while (s.busy or not s.inbox.empty() or any(s.waiting[m] for m in s.members)) and time.time() - t0 < limit:
        time.sleep(0.5)


def show():
    global last
    for e in C.read_after(app.room, last):
        who = e["from"] + (f"·{e['role']}" if e.get("role") else "") + (f"·{e['kind']}" if e.get("kind") else "")
        print(f"  #{e['seq']} {who:<26} {e['text'][:150]!r}")
        last = e["seq"]


print("\n[1] 它知道自己在哪个群聊、群里有谁")
api.send("@gemini 一句话：你是谁，群里还有谁？", "auto")
wait_idle(); show()

print("\n[2] 工具调用：工作笔记、最终回复、工具记录")
api.send("@gemini 先说一句你要做什么，然后新建 notes.txt 写入一行 ok，再读回来确认，最后一句话告诉我结果。", "auto")
wait_idle(); show()
print("  notes.txt:", open(os.path.join(PROJ, "notes.txt"), encoding="utf-8").read().strip() if os.path.exists(os.path.join(PROJ, "notes.txt")) else "没有")

print("\n[3] 它工作时发来的消息排在这一轮之后")
api.send("@gemini 依次运行 5 次命令 echo，参数分别是 1 到 5，每次之前说一句，最后说完成。", "auto")
time.sleep(6)
api.send("@gemini 另外新建 b.txt 写入 hi。", "auto")
time.sleep(1)
print("  queued while busy:", len(s.waiting["gemini"]), "| busy:", s.lane("gemini").busy)
wait_idle(); show()
print("  b.txt:", os.path.exists(os.path.join(PROJ, "b.txt")))

print("\n[4] 停止，然后续上同一个会话")
api.send("@gemini 依次运行 10 次命令 echo，参数 1 到 10，每次之前说一句。", "auto")
time.sleep(7)
print("  stop:", api.stop("gemini"))
wait_idle(); show()
conv_before = C.load_json(os.path.join(app.room["dir"], "gemini-work.json"), {}).get("conversation")
api.send("@gemini 只列文件名：这个对话里你建过哪些文件？", "auto")
wait_idle(); show()
conv_after = C.load_json(os.path.join(app.room["dir"], "gemini-work.json"), {}).get("conversation")
print("  same conversation after stop:", conv_before == conv_after, conv_after)

print("\nstatus:", {k: {x: v[x] for x in ("ready", "busy", "error", "access")} for k, v in app.status_payload()["models"].items()})
print("jobs:", [(j["model"], j["status"]) for j in s.jobs])
errs = os.path.join(STATE, "hook-errors.log")
print("errors:", open(errs, encoding="utf-8").read()[-1500:] if os.path.exists(errs) else "none")
s.shutdown()
