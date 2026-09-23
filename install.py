"""One-time setup on a new machine (Windows).

  python install.py            install: dependency, hooks for both sides, desktop shortcut
  python install.py --check    only report what is found and what is missing

What it does, nothing more:
  1. pip-installs pywebview (the window) into this Python.
  2. Adds three hooks (SessionStart / UserPromptSubmit / Stop) to ~/.claude/settings.json and
     ~/.codex/hooks.json, pointing at this folder. Existing hooks are kept; running it twice adds nothing twice.
  3. Puts a "群聊" shortcut on the desktop.
Codex runs a new hook only after you trust it: open Codex (CLI: type /hooks) and trust the three groupchat hooks.
"""
import ctypes, glob, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "prompt", "Stop": "stop"}


def short(path):
    """8.3 form of a path, so hook commands need no quoting whatever shell runs them."""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
    return (buf.value if n else path).replace("\\", "/")


def find(label, patterns):
    hits = [p for pat in patterns for p in glob.glob(pat)]
    path = max(hits, key=os.path.getmtime) if hits else None
    print(f"  {'✓' if path else '✗'} {label}: {path or '没找到'}")
    return path


def merge_hooks(path, script, extra=None):
    cfg = {}
    if os.path.exists(path):
        cfg = json.load(open(path, encoding="utf-8"))
        shutil.copyfile(path, path + ".bak-groupchat")
    hooks = cfg.setdefault("hooks", {})
    added = 0
    for event, arg in EVENTS.items():
        groups = hooks.setdefault(event, [])
        if any(os.path.basename(script) in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            continue
        entry = {"type": "command", "command": f"{short(sys.executable)} {short(script)} {arg}", "timeout": 30}
        entry.update(extra or {})
        groups.append({"hooks": [entry]})
        added += 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  ✓ {path}：新增 {added} 个 hook" + ("（已有的保持不变）" if added < 3 else ""))


def shortcut():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    ps = ("$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\群聊.lnk\"); "
          f"$s.TargetPath='{pyw}'; $s.Arguments='\"{os.path.join(HERE, 'app', 'app.py')}\"'; "
          f"$s.WorkingDirectory='{os.path.join(HERE, 'app')}'; $s.IconLocation='{os.path.join(HERE, 'app', 'groupchat.ico')},0'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("  ✓ 桌面快捷方式：群聊")


def main():
    check = "--check" in sys.argv
    if sys.version_info < (3, 11):
        sys.exit("需要 Python 3.11 或更新版本")
    local, roaming = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
    print("查找两边的程序：")
    claude = find("Claude Code（Claude 桌面端自带）", [
        os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(roaming, "Claude", "claude-code", "*", "claude.exe"), os.path.join(HOME, ".local", "bin", "claude.exe")])
    codex = find("Codex（Codex 桌面端自带）", [os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe")])
    try:
        import webview  # noqa: F401
        print("  ✓ pywebview 已安装")
        need_webview = False
    except ImportError:
        print("  ✗ pywebview 未安装")
        need_webview = True
    if check:
        return
    if not (claude and codex):
        sys.exit("先装好 Claude 桌面端（并登录一次 Claude Code）和 Codex 桌面端，再运行本脚本。")
    print("安装：")
    if need_webview:
        subprocess.run([sys.executable, "-m", "pip", "install", "pywebview"], check=True)
    merge_hooks(os.path.join(HOME, ".claude", "settings.json"), os.path.join(HERE, "hook.py"))
    merge_hooks(os.path.join(HOME, ".codex", "hooks.json"), os.path.join(HERE, "codex_hook.py"), {"additionalContextLimit": 0})
    shortcut()
    print("\n最后一步：打开 Codex，在 hooks 设置里（CLI 输入 /hooks）信任 groupchat 的三个 hook。然后双击桌面的「群聊」。")


if __name__ == "__main__":
    main()
