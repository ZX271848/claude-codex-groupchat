"""One-time setup on Windows or macOS.

  python install.py            install the group-chat client: pywebview and a desktop shortcut "群聊"
  python install.py --check    only report what is found and what is missing
  python install.py --legacy   also set up the first client (app/), which relays through hooks in both harnesses

The current client (app2/) hosts Claude Code, Codex and, if present, the Antigravity CLI itself and needs no hooks.
What it does, nothing more:
  1. pip-installs pywebview (the window) into this Python.
  2. Puts a "群聊" shortcut on the desktop.
  3. Only with --legacy: adds three hooks (SessionStart / UserPromptSubmit / Stop) to ~/.claude/settings.json and
     ~/.codex/hooks.json for the first client, keeping existing hooks, and a "群聊（第一版）" shortcut.
"""
import ctypes, glob, json, os, plistlib, shlex, shutil, subprocess, sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "prompt", "Stop": "stop"}


def short(path):
    """8.3 form of a path, so hook commands need no quoting whatever shell runs them."""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
    return (buf.value if n else path).replace("\\", "/")


def find(label, patterns, optional=""):
    hits = [p for pat in patterns for p in glob.glob(pat)]
    path = max(hits, key=os.path.getmtime) if hits else None
    print(f"  {'√' if path else ('-' if optional else '×')} {label}: {path or ('没找到' + optional)}")
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
    print(f"  √ {path}：新增 {added} 个 hook" + ("（已有的保持不变）" if added < 3 else ""))


def shortcut(name, folder, icon):
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    target = os.path.join(HERE, folder)
    ps = (f"$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\{name}.lnk\"); "
          f"$s.TargetPath='{pyw}'; $s.Arguments='\"{os.path.join(target, 'app.py')}\"'; "
          f"$s.WorkingDirectory='{target}'; $s.IconLocation='{os.path.join(target, icon)},0'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print(f"  √ 桌面快捷方式：{name}")


def mac_shortcut(name="群聊", destination=None, python=None):
    """A local .app launcher; its Python environment and source stay in this checkout."""
    bundle = Path(destination) if destination else Path(HOME) / "Applications" / f"{name}.app"
    identifier = "local.claude-codex-groupchat"
    info = bundle / "Contents/Info.plist"
    if bundle.exists():
        if not info.exists() or plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") != identifier:
            raise RuntimeError(f"{bundle} 已存在且不是本项目的启动器，请先换名或移走它")
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(exist_ok=True)
    python = python or sys.executable
    launcher = contents / "MacOS/GroupChat"
    # Quote every stored path, including spaces and apostrophes. Do not source a shell profile.
    launcher.write_text("#!/bin/sh\n"
                        "export PATH=\"$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH\"\n"
                        "log_dir=\"$HOME/Library/Logs/ClaudeCodexGroupChat\"\n"
                        "mkdir -p \"$log_dir\"\n"
                        f"cd {shlex.quote(HERE)} || exit 1\n"
                        f"exec {shlex.quote(python)} {shlex.quote(os.path.join(HERE, 'app2/app.py'))} \"$@\" >>\"$log_dir/client.log\" 2>&1\n",
                        encoding="utf-8")
    launcher.chmod(0o755)
    metadata = {"CFBundleName": name, "CFBundleDisplayName": name,
                "CFBundleIdentifier": identifier, "CFBundleVersion": "1.0",
                "CFBundleShortVersionString": "1.0", "CFBundlePackageType": "APPL",
                "CFBundleExecutable": "GroupChat", "NSHighResolutionCapable": True}
    icon = Path(HERE) / "app2/groupchat2.icns"
    if icon.exists():
        shutil.copyfile(icon, contents / "Resources/groupchat2.icns")
        metadata["CFBundleIconFile"] = "groupchat2.icns"
    info.write_bytes(plistlib.dumps(metadata))
    print(f"  √ Mac 应用：{bundle}")
    return bundle


def mac_environment():
    """Avoid modifying a Homebrew/system Python (PEP 668)."""
    python = Path(HERE) / ".venv/bin/python"
    if os.path.abspath(sys.prefix) == os.path.join(HERE, ".venv"):
        return
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
    subprocess.run([str(python), str(Path(HERE) / "install.py"), *sys.argv[1:]], check=True)
    raise SystemExit(0)


def main():
    sys.stdout.reconfigure(errors="replace")  # Chinese Windows consoles are GBK: never fail on a symbol
    check, legacy = "--check" in sys.argv, "--legacy" in sys.argv
    if sys.version_info < (3, 11):
        sys.exit("需要 Python 3.11 或更新版本")
    if sys.platform == "darwin" and legacy:
        sys.exit("macOS 支持新版 app2；第一版 --legacy 仍仅支持 Windows。")
    if sys.platform == "darwin" and not check:
        mac_environment()
    from app2 import backends
    print("查找各方的程序：")
    programs = backends.report()
    for name, path in programs.items():
        print(f"  {'√' if path else '×'} {backends.LABEL[name]}：{path or ('未安装（可选）' if name == 'agy' else '未安装')}")
    try:
        import webview  # noqa: F401
        print("  √ pywebview 已安装")
        need_webview = False
    except ImportError:
        print("  × pywebview 未安装")
        need_webview = True
    if check:
        return
    if not (programs['claude'] and programs['codex']):
        print("  提示：Claude Code / Codex 尚未齐备，安装后按 docs/SETUP.md 登录即可。")
    print("安装：")
    if need_webview:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", os.path.join(HERE, "requirements.txt")], check=True)
    if sys.platform == "darwin":
        bundle = mac_shortcut()
        desktop = Path(HOME) / "Desktop/群聊.app"
        if desktop.parent.is_dir() and not os.path.lexists(desktop):
            desktop.symlink_to(bundle, target_is_directory=True)
        print("\n双击「群聊.app」打开。关闭窗口或 ⌘Q 会退出；日志：~/Library/Logs/ClaudeCodexGroupChat/client.log")
        return
    if os.name != "nt":
        sys.exit("桌面安装器支持 macOS 和 Windows。")
    shortcut("群聊", "app2", "groupchat2.ico")
    if legacy:
        merge_hooks(os.path.join(HOME, ".claude", "settings.json"), os.path.join(HERE, "hook.py"))
        merge_hooks(os.path.join(HOME, ".codex", "hooks.json"), os.path.join(HERE, "codex_hook.py"), {"additionalContextLimit": 0})
        shortcut("群聊（第一版）", "app", "groupchat.ico")
        print("\n第一版还需要一步：打开 Codex，在 hooks 设置里（CLI 输入 /hooks）信任 groupchat 的三个 hook。")
    print("\n双击桌面的「群聊」打开。")


if __name__ == "__main__":
    main()
