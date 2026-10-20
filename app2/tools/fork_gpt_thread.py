"""Fork the group chat's GPT work thread (held by the Codex app) into a new thread the client can resume.

Mirrors import_v1.gpt_fork of the group-chat client: same app-server handshake, same fork parameters.
usage: python fork_gpt_thread.py ROOM_DIR [--apply]
Without --apply: fork + verify only, gpt-work.json is left untouched.
"""
import glob, json, os, queue, shutil, subprocess, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backends
sys.stdout.reconfigure(encoding="utf-8")
ROOM = sys.argv[1]
APPLY = "--apply" in sys.argv
HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".claude", "groupchat2")
PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "work.md")
EXE = backends.find("codex")
if not EXE:
    raise RuntimeError(backends.missing_note("codex"))


class Engine:
    def __init__(self, name):
        env = dict(os.environ, GROUPCHAT_OFF="1", CODEX_BRIDGE_DIR=STATE, PATH=backends.desktop_path())
        for k in ("GROUPCHAT_HOSTED", "GROUPCHAT_ROOM", "GROUPCHAT_CHANNEL"):
            env.pop(k, None)
        self.p = subprocess.Popen([EXE, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0, env=env)
        self.nid, self.pending, self.lock = 0, {}, threading.Lock()
        threading.Thread(target=self.read, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": name, "version": "1.0-coordinator"},
                                 "capabilities": {"experimentalApi": True}})
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


room = json.load(open(os.path.join(ROOM, "room.json"), encoding="utf-8"))
idfile = os.path.join(ROOM, "gpt-work.json")
meta = json.load(open(idfile, encoding="utf-8"))
old = meta["thread"]
print("room", room["name"], "| root", room["root"], "| current thread", old)
instructions = open(PROMPT, encoding="utf-8").read()
eng = Engine("groupchat2-work")
try:
    result = eng.call("thread/fork", {"threadId": old, "cwd": room["root"], "approvalPolicy": "never",
                                      "developerInstructions": instructions, "excludeTurns": True})
    new = result["thread"]["id"]
    print("forked ->", new)
    print("fork result keys:", sorted(result.keys()), "| thread keys:", sorted(result["thread"].keys())[:30])
    for key in ("model", "reasoningEffort", "sandbox", "approvalPolicy", "cwd"):
        if key in result:
            print("  ", key, "=", json.dumps(result[key], ensure_ascii=False)[:200])
    eng.call("thread/name/set", {"threadId": new, "name": f"{room['name']} · 持续会话（分叉自 Codex 接班）"})
finally:
    eng.close()
time.sleep(2)
# Verify in a fresh app-server that the fork can be resumed (the client does exactly this).
check = Engine("groupchat2-work")
try:
    resumed = check.call("thread/resume", {"threadId": new, "excludeTurns": True, "developerInstructions": instructions})
    print("resume ok:", resumed["thread"]["id"] == new, "| model", resumed.get("model"), "| effort", resumed.get("reasoningEffort"))
    try:
        turns = check.call("thread/turns/list", {"threadId": new, "limit": 3})
        print("turns/list keys:", sorted(turns.keys()))
    except Exception as ex:
        print("turns/list not available:", str(ex)[:120])
finally:
    check.close()
if APPLY:
    backup = idfile + ".before-fork-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(idfile, backup)
    tmp = idfile + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({**meta, "thread": new, "forked_from": old}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, idfile)
    print("gpt-work.json ->", json.load(open(idfile, encoding="utf-8")), "| backup", backup)
else:
    print("dry: gpt-work.json untouched; new thread id", new)
