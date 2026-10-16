"""Separate model-authored public progress, collaboration requests and ordinary text."""
import re
from history import code_ranges

CONTROL = re.compile(r'<collaborate\s+target=["\x27](gpt|claude|gemini)["\x27]\s*>(.*?)</collaborate>'
                     r'|<progress>(.*?)</progress>', re.I | re.S)
MENTION = re.compile(r'^\s*@(gpt|codex|claude|gemini)\b[\s:：,，]*(.*)$', re.I | re.S)


def split_output(text):
    spans = code_ranges(text)
    matches = [m for m in CONTROL.finditer(text) if not any(a <= m.start() < b for a, b in spans)
               and (m.start() == 0 or text[m.start()-1] != '\\')]
    requests, progress = [], []
    for match in matches:
        if match.group(1):
            requests.append((match.group(1).lower(), match.group(2).strip()))
        else:
            progress.append(match.group(3).strip())
    public = text
    for match in reversed(matches):
        public = public[:match.start()] + public[match.end():]
    if mention := MENTION.match(public):
        target = 'gpt' if mention.group(1).lower() == 'codex' else mention.group(1).lower()
        requests.append((target, mention.group(2).strip()))
        public = ''
    return public.strip(), requests, progress
