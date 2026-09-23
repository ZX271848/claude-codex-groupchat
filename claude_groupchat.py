"""Start Claude Code (CLI) in a project with the group-chat channel, in the current console.

  python claude_groupchat.py [--cwd <project dir>] [--resume <session id>] [--model M] [--effort E] [--permission-mode MODE]

Finds the claude.exe the Claude desktop app ships (its real path lives under the app's MSIX package folder),
makes sure the project has the `groupchat` MCP server registered at local scope (channels only accept servers
from user/project/local config), and starts the session with GROUPCHAT_CHANNEL=1 so that server acts as the channel.
"""
import argparse, glob, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
DEFAULT_CWD = os.getcwd()


def claude_exe():
    local, roaming = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
    found = glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"))
    found += glob.glob(os.path.join(roaming, "Claude", "claude-code", "*", "claude.exe"))
    if not found:
        sys.exit("找不到 Claude 桌面端自带的 claude.exe")
    return max(found, key=os.path.getmtime)


def ensure_registered(exe, cwd):
    r = subprocess.run([exe, "mcp", "get", "groupchat"], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0 and "channel_server.py" in (r.stdout or ""):
        return
    subprocess.run([exe, "mcp", "add", "--scope", "local", "groupchat", "--", PYTHON, os.path.join(HERE, "channel_server.py")],
                   cwd=cwd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", default=DEFAULT_CWD)
    ap.add_argument("--resume")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort")
    ap.add_argument("--permission-mode", default="bypassPermissions",
                    choices=["auto", "default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"],
                    help="Claude permission mode (default: bypassPermissions, as requested by the user)")
    ap.add_argument("--room", help="join this room (folder name under rooms/) instead of the project's default room")
    a = ap.parse_args()
    exe = claude_exe()
    ensure_registered(exe, a.cwd)
    cmd = [exe, "--dangerously-load-development-channels", "server:groupchat", "--model", a.model, "--permission-mode", a.permission_mode]
    if a.effort:
        cmd += ["--effort", a.effort]
    if a.resume:
        cmd += ["--resume", a.resume]
    env = dict(os.environ, GROUPCHAT_CHANNEL="1")
    if a.room:
        env["GROUPCHAT_ROOM"] = a.room
    sys.exit(subprocess.call(cmd, cwd=a.cwd, env=env))


if __name__ == "__main__":
    main()
