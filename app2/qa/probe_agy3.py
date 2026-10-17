"""Probe: with "queuedMessages": "send-immediately" in agy's settings, does a message written mid-turn in headless
stream-json mode reach the running turn (one result) or still wait for the next turn (two results)?
The settings file is created for the probe and removed afterwards if it did not exist before."""
import json, os, subprocess, sys, tempfile, threading, time
sys.stdout.reconfigure(encoding="utf-8")
AGY = os.path.join(os.environ["LOCALAPPDATA"], "agy", "bin", "agy.exe")
SETTINGS = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "settings.json")
existed = os.path.exists(SETTINGS)
assert not existed, "settings.json exists: merge by hand instead of overwriting"
with open(SETTINGS, "w", encoding="utf-8") as f:
    json.dump({"queuedMessages": "send-immediately"}, f)
try:
    cwd = tempfile.mkdtemp(prefix="agy-probe3-")
    p = subprocess.Popen([AGY, "--input-format", "stream-json", "--output-format", "stream-json", "--model", "gemini-3.8-flash-low",
                          "--dangerously-skip-permissions"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, creationflags=0x08000000)
    t0, results, done = time.time(), [], threading.Event()

    def out():
        for raw in p.stdout:
            e = json.loads(raw)
            if e["event"] == "step_update":
                s = e["step_update"]
                if s["state"] == "DONE" or s.get("step_type") == "user_input":
                    print(f"  [{time.time()-t0:5.1f}] #{s['step_index']} {s['step_type']} {s['state']} {s.get('tool_name') or ''}")
            elif e["event"] == "result":
                results.append(e["result"]); print(f"  [{time.time()-t0:5.1f}] RESULT {e['result']['status']} {e['result'].get('response','')[-160:]!r}")
                if len(results) >= 2 or "b.txt" in os.listdir(cwd):
                    done.set()
    threading.Thread(target=out, daemon=True).start()

    def send(t):
        print(f">>> [{time.time()-t0:5.1f}] {t}")
        p.stdin.write((json.dumps({"event": "user", "message": {"content": t}}, ensure_ascii=False) + "\n").encode()); p.stdin.flush()
    send("依次运行 6 次命令 echo，参数 1 到 6，每次之前说一句；全部做完后最后说一句总结。")
    time.sleep(7)
    send("（插话）另外新建 b.txt 写入 hi，然后接着做原来的事。")
    done.wait(180); time.sleep(3)
    print("results:", len(results), "| files:", sorted(os.listdir(cwd)))
    p.stdin.close(); p.wait(30)
finally:
    os.remove(SETTINGS)
    print("settings.json removed:", not os.path.exists(SETTINGS))
