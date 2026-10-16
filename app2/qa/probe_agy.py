"""Probe agy's headless stream-json protocol: two turns in one process, every raw event printed (shortened).
  python probe_agy.py [model]"""
import json, os, subprocess, sys, tempfile, threading, time
sys.stdout.reconfigure(encoding="utf-8")
AGY = os.path.join(os.environ["LOCALAPPDATA"], "agy", "bin", "agy.exe")
model = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.8-flash-low"
cwd = tempfile.mkdtemp(prefix="agy-probe-")
p = subprocess.Popen([AGY, "--input-format", "stream-json", "--output-format", "stream-json", "--model", model],
                     cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
done = threading.Event()
t0 = time.time()


def err():
    for l in p.stderr:
        print(f"  [stderr {time.time()-t0:5.1f}] {l.decode('utf-8', 'replace').rstrip()[:200]}")


def out():
    for raw in p.stdout:
        try:
            e = json.loads(raw)
        except Exception:
            print("  [non-json]", raw[:200]); continue
        short = json.dumps(e, ensure_ascii=False)
        print(f"  [{time.time()-t0:5.1f}] {short[:420]}")
        if e.get("event") == "result":
            done.set()


threading.Thread(target=err, daemon=True).start()
threading.Thread(target=out, daemon=True).start()
for prompt in ["只回一个词：苹果", "刚才你回的是什么？只回那个词。"]:
    done.clear()
    print(f"\n>>> {prompt}")
    p.stdin.write((json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n").encode("utf-8"))
    p.stdin.flush()
    done.wait(180)
p.stdin.close()
p.wait(30)
print("exit", p.returncode)
