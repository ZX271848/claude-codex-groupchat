"""Where each member's program is, found without setup: the user's own choice first, then the places the desktop apps
and the standalone installers put them, then PATH. Nothing here logs in or changes the programs' own settings.

A path the search misses can be set in ~/.claude/groupchat2/paths.json  ({"claude": "...", "codex": "...", "agy": "..."})
or in an environment variable (GROUPCHAT_CLAUDE_PATH, GROUPCHAT_CODEX_PATH / CODEX_CLI_PATH, GROUPCHAT_AGY_PATH).
See docs/SETUP.md."""
import glob
import json
import os
import shutil
import sys
from pathlib import Path

HOME = os.path.expanduser("~")
LOCAL = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))
ROAMING = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
STATE = os.environ.get("GROUPCHAT2_STATE") or os.path.join(HOME, ".claude", "groupchat2")
PATHS = os.path.join(STATE, "paths.json")
GUIDE = "docs/SETUP.md"

SEARCH = {
    "claude": {"env": ("GROUPCHAT_CLAUDE_PATH",), "names": ("claude.exe", "claude.cmd", "claude"),
               "globs": [os.path.join(LOCAL, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"),
                         os.path.join(ROAMING, "Claude", "claude-code", "*", "claude.exe"),
                         os.path.join(HOME, ".local", "bin", "claude.exe"),
                         os.path.join(ROAMING, "npm", "claude.cmd")]},
    "codex": {"env": ("GROUPCHAT_CODEX_PATH", "CODEX_CLI_PATH"), "names": ("codex.exe", "codex.cmd", "codex"),
              "globs": [os.path.join(LOCAL, "OpenAI", "Codex", "bin", "*", "codex.exe"),
                        os.path.join(ROAMING, "npm", "codex.cmd")]},
    "agy": {"env": ("GROUPCHAT_AGY_PATH",), "names": ("agy.exe", "agy"),
            "globs": [os.path.join(LOCAL, "agy", "bin", "agy.exe")]},
}


def desktop_path():
    """Finder does not inherit a terminal's PATH. Include common user installations."""
    entries = os.environ.get("PATH", "").split(os.pathsep)
    if os.name != "nt":
        entries += [str(Path(__file__).resolve().parents[1] / ".tools/node_modules/.bin"),
                    os.path.join(HOME, ".local/bin"), os.path.join(HOME, ".npm-global/bin"),
                    os.path.join(HOME, ".volta/bin"), os.path.join(HOME, ".fnm/aliases/default/bin"),
                    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        entries += sorted(glob.glob(os.path.join(HOME, ".nvm/versions/node/*/bin")),
                          key=os.path.getmtime, reverse=True)
    return os.pathsep.join(dict.fromkeys(p for p in entries if p))


if os.name != "nt":
    for name in SEARCH:
        SEARCH[name]["names"] = (name,)
        SEARCH[name]["globs"] = []
    SEARCH["claude"]["globs"] = [os.path.join(HOME, ".local/bin/claude")]
    SEARCH["agy"]["globs"] = [os.path.join(HOME, ".agy/bin/agy"), os.path.join(HOME, ".local/bin/agy")]
    if sys.platform == "darwin":
        SEARCH["claude"]["globs"] += [os.path.join(HOME, "Library/Application Support/Claude/claude-code/*/claude")]
        for applications in ("/Applications", os.path.join(HOME, "Applications")):
            SEARCH["codex"]["globs"] += [
                os.path.join(applications, "Codex.app/Contents/Resources/codex"),
                os.path.join(applications, "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            ]

LABEL = {"claude": "Claude Code", "codex": "Codex", "agy": "Antigravity CLI (agy)"}
_cache = {}


def configured(name):
    for var in SEARCH[name]["env"]:
        if os.environ.get(var):
            return os.environ[var]
    try:
        with open(PATHS, encoding="utf-8") as stream:
            return json.load(stream).get(name)
    except (OSError, ValueError):
        return None


def find(name, fresh=False):
    """Full path of the program, or None. A configured path is used as given if it exists."""
    if not fresh and executable(_cache.get(name)):
        return _cache[name]
    path = configured(name)
    path = os.path.expandvars(os.path.expanduser(path)) if isinstance(path, str) else None
    if not executable(path):
        hits = [p for pattern in SEARCH[name]["globs"] for p in glob.glob(pattern) if executable(p)]
        path = max(hits, key=os.path.getmtime) if hits else None   # desktop apps keep one folder per version
    if not path:
        path = next((p for n in SEARCH[name]["names"] if (p := shutil.which(n, path=desktop_path()))), None)
    _cache[name] = path
    return path


def executable(path):
    return bool(path and os.path.isfile(path) and (os.name == "nt" or os.access(path, os.X_OK)))


def missing_note(name):
    return f"找不到 {LABEL[name]}：装好并登录后重开客户端；装在别处的，在 {PATHS} 里写上路径（见 {GUIDE}）"


def report():
    return {name: find(name, fresh=True) for name in SEARCH}


if __name__ == "__main__":  # python app2\backends.py : what the client finds on this machine
    import sys
    sys.stdout.reconfigure(errors="replace")  # GBK consoles included
    for name, path in report().items():
        print(f"{'√' if path else '×'} {LABEL[name]:<24} {path or missing_note(name)}")
