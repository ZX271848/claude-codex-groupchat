"""Probe: emulate insertion for agy. A message arrives mid-turn; at the next finished tool step the process is ended,
then the same conversation resumes with the message. Does Gemini keep the finished steps, take the message, and go on
with the original task without redoing it?"""
import json, os, subprocess, sys, tempfile, threading, time
sys.stdout.reconfigure(encoding="utf-8")
AGY = os.path.join(os.environ["LOCALAPPDATA"], "agy", "bin", "agy.exe")
cwd = tempfile.mkdtemp(prefix="agy-probe4-")
t0 = time.time()
state = {"conv": None, "pending": None, "results": [], "cut": False, "echoes": []}


def run(extra, first, on_tool_done=None):
    p = subprocess.Popen([AGY, "--input-format", "stream-json", "--output-format", "stream-json", "--model", "gemini-3.8-flash-low",
                          "--dangerously-skip-permissions", *extra], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, creationflags=0x08000000)
    ended = threading.Event()

    def out():
        for raw in p.stdout:
            e = json.loads(raw)
            if e["event"] == "init":
                state["conv"] = e["conversation_id"]
            elif e["event"] == "step_update":
                s = e["step_update"]
                if s.get("step_type") == "tool" and s["state"] == "DONE":
                    cmd = ((s.get("tool_info") or {}).get("parameters") or {}).get("CommandLine", "")
                    print(f"  [{time.time()-t0:5.1f}] #{s['step_index']} tool DONE {s.get('tool_name')} {cmd!r}")
                    state["echoes"].append(cmd)
                    if on_tool_done and on_tool_done(p):
                        return
                elif s["state"] == "DONE":
                    print(f"  [{time.time()-t0:5.1f}] #{s['step_index']} {s['step_type']} DONE")
            elif e["event"] == "result":
                state["results"].append(e["result"])
                print(f"  [{time.time()-t0:5.1f}] RESULT {e['result']['status']} {e['result'].get('response','')[-200:]!r}")
                ended.set()
        ended.set()
    threading.Thread(target=out, daemon=True).start()
    p.stdin.write((json.dumps({"event": "user", "message": {"content": first}}, ensure_ascii=False) + "\n").encode()); p.stdin.flush()
    return p, ended


def cut(p):
    if state["pending"] and not state["cut"]:
        state["cut"] = True
        print(f"  [{time.time()-t0:5.1f}] -- cut after this tool step, message pending --")
        p.kill()
        return True
    return False


print("[1] original task")
p, ended = run([], "依次运行 6 次命令 echo，参数 1 到 6，每次之前说一句；全部做完后最后说一句总结。", cut)
time.sleep(6)
state["pending"] = "（插话）另外新建 b.txt 写入 hi。"
print(f">>> [{time.time()-t0:5.1f}] message arrives while it works")
ended.wait(120); p.wait(30)
done_before = list(state["echoes"])
print("echoes before cut:", done_before)

print("\n[2] resume with the message")
note = ("（客户端）你上一轮在两次工具调用之间被用户插话打断：已经做完的步骤都在对话里，没写完的话丢掉了。"
        "先处理下面这条插话，然后接着做原来的事，已经做过的步骤不要重做。\n\n" + state["pending"])
p, ended = run(["--conversation", state["conv"]], note)
ended.wait(180)
p.stdin.close(); p.wait(30)
print("\nechoes after resume:", state["echoes"][len(done_before):])
print("files:", sorted(os.listdir(cwd)), "| results:", len(state["results"]))
