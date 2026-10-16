"""Probe agy for the group chat: tool steps, a message written mid-turn, and resume after the process is killed.
Temp project dir, auto-approved tools, cheap model."""
import json, os, subprocess, sys, tempfile, threading, time
sys.stdout.reconfigure(encoding="utf-8")
AGY = os.path.join(os.environ["LOCALAPPDATA"], "agy", "bin", "agy.exe")
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.8-flash-low"
cwd = tempfile.mkdtemp(prefix="agy-probe2-")
t0 = time.time()


def start(extra=()):
    p = subprocess.Popen([AGY, "--input-format", "stream-json", "--output-format", "stream-json", "--model", MODEL,
                          "--dangerously-skip-permissions", *extra],
                         cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
    st = {"results": 0, "conv": None, "ev": threading.Event()}

    def out():
        for raw in p.stdout:
            try:
                e = json.loads(raw)
            except Exception:
                print("  [non-json]", raw[:160]); continue
            ev = e.get("event")
            if ev == "init":
                st["conv"] = e["conversation_id"]; print(f"  [{time.time()-t0:5.1f}] init conv={st['conv']}"); continue
            if ev == "step_update":
                s = e["step_update"]
                ti = s.get("tool_info") or {}
                extra_s = ""
                if ti:
                    extra_s = f" tool={ti.get('name')} params={json.dumps(ti.get('parameters'), ensure_ascii=False)[:100]} out={str(ti.get('output'))[:80]!r} err={ti.get('error')}"
                td = s.get("text_delta")
                if td is not None and s.get("state") == "ACTIVE" and not ti:
                    print(f"  [{time.time()-t0:5.1f}] #{s['step_index']} {s['step_type']} ACTIVE delta={td[:60]!r}")
                else:
                    print(f"  [{time.time()-t0:5.1f}] #{s['step_index']} {s['step_type']} {s['state']} tool_name={s.get('tool_name')}{extra_s} delta={str(td)[:60]!r}")
                continue
            if ev == "result":
                r = e["result"]; st["results"] += 1
                print(f"  [{time.time()-t0:5.1f}] RESULT {r['status']} turns={r.get('num_turns')} response={r.get('response','')[:200]!r} err={r.get('error')}")
                st["ev"].set(); continue
            print(f"  [{time.time()-t0:5.1f}] OTHER {json.dumps(e, ensure_ascii=False)[:300]}")

    def err():
        for l in p.stderr:
            s = l.decode("utf-8", "replace").rstrip()
            if "W" in s[:2] or "E" in s[:2] or "rror" in s:
                print(f"  [stderr] {s[:200]}")
    threading.Thread(target=out, daemon=True).start()
    threading.Thread(target=err, daemon=True).start()
    return p, st


def send(p, text):
    print(f"\n>>> [{time.time()-t0:5.1f}] {text}")
    p.stdin.write((json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False) + "\n").encode("utf-8"))
    p.stdin.flush()


print("[A] tool turn + a message written mid-turn")
p, st = start()
send(p, "分三步做，每步之前先用一句话说你要做什么：1) 列出当前目录；2) 新建 a.txt 写入 hi；3) 读回 a.txt 确认。最后一句话总结。")
time.sleep(6)
send(p, "（插话）另外再新建 b.txt，写入 ok。")
deadline = time.time() + 240
while st["results"] < 2 and time.time() < deadline:
    st["ev"].wait(5); st["ev"].clear()
print("  files:", sorted(os.listdir(cwd)))
conv = st["conv"]

print("\n[B] kill mid-turn, then resume by id")
send(p, "数到 5，每个数字单独一行，每行之前先运行一次命令 echo 那个数字。")
time.sleep(5)
p.kill(); p.wait()
print(f"  killed at {time.time()-t0:5.1f}")
p, st = start(["--conversation", conv])
send(p, "你记得我们这个对话里建了哪些文件吗？只列文件名。")
st["ev"].wait(120)
p.stdin.close(); p.wait(30)
print("exit", p.returncode)
