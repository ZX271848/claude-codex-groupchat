"""Read-only projection of current and legacy records; never dispatches work."""
import json
import re

MODELS = ('claude', 'gpt', 'gemini')
HANDOFF = re.compile(r'^\s*交给工作\s*[:：]\s*(claude|gpt)\s*\n?', re.I)
BLOCK = re.compile(r'<handoff\s+target=["\x27](claude|gpt)["\x27]\s*>(.*?)</handoff>', re.I | re.S)


def code_ranges(text):
    """Markdown examples are text, never dispatch instructions (also while streaming)."""
    spans, pos = [], 0
    token = re.compile(r'(?m)^[ \t]{0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)|`+')
    while match := token.search(text, pos):
        if match.group(1):
            marker = match.group(1)
            close = re.search(r'(?m)^[ \t]{0,3}' + re.escape(marker[0])
                              + '{' + str(len(marker)) + r',}[ \t]*(?:\n|$)', text[match.end():])
            end = match.end() + close.end() if close else len(text)
        else:
            marker = match.group()
            close = re.search(r'(?<!`)' + re.escape(marker) + r'(?!`)', text[match.end():])
            end = match.end() + close.end() if close else len(text)
        spans.append((match.start(), end))
        pos = end
    return spans


def blocks(text):
    spans = code_ranges(text)
    return [m for m in BLOCK.finditer(text)
            if not any(a <= m.start() < b for a, b in spans)
            and (m.start() == 0 or text[m.start() - 1] != '\\')]


def strip_blocks(text):
    for match in reversed(blocks(text)):
        text = text[:match.start()] + text[match.end():]
    return text


def split_handoff(text):
    """Legacy dispatch-only replies remain internal in their entirety."""
    match = HANDOFF.match(text or '')
    return (match.group(1).lower(), text[match.end():].strip()) if match else (None, text or '')


def parse_front(text):
    target, request = split_handoff(text)
    if target:
        return [{'target': target, 'request': request}], ''
    jobs = [{'target': m.group(1).lower(), 'request': m.group(2).strip()} for m in blocks(text)]
    # Final replies preserve incomplete tags; only complete, unquoted blocks are control.
    return jobs, strip_blocks(text).strip()


def mechanical(text):
    """Recognize legacy control replies, not discussion which happens to mention them."""
    if re.fullmatch(r'\s*suggest\s*:\s*(?:claude|gpt)(?:-(?:front|work))?\s*', text, re.I):
        return True
    try:
        obj = json.loads(text)
        return (isinstance(obj, dict) and {'task_id', 'task', 'implementation'} <= obj.keys()
                and obj.keys() <= {'task_id', 'task', 'implementation', 'decision'})
    except (ValueError, TypeError):
        return False


def context_event(event, role, model):
    """A separate projection from UI visibility; original records remain untouched."""
    if event.get('from') == 'user':
        return event  # even if the user quotes JSON or routing syntax
    text = event.get('text') or ''
    if event.get('kind') in ('suggest', 'task_association', 'dispatch_confirmation') or mechanical(text):
        return None
    if event.get('kind') == 'routing':
        # Historical dispatches go only to their intended worker, never to fronts.
        return event if role == 'work' and model in event.get('to', []) else None
    if event.get('role') == 'front' or HANDOFF.match(text):
        jobs, public = parse_front(text)
        own_jobs = [j['request'] for j in jobs if role == 'work' and j['target'] == model]
        text = '\n\n'.join([public] + own_jobs).strip()
        if not text:
            return None
        return {**event, 'text': text}
    # Working notes and tool results remain available to the persistent workers
    # on their next invocation, even though they have no chat bubbles.
    return event


def public_line(event):
    return (event.get('visibility') != 'internal' and event.get('role') != 'working'
            and event.get('kind') not in ('tool_summary', 'routing')
            and not (event.get('from') in MODELS and (HANDOFF.match(event.get('text') or '')
                                                     or mechanical(event.get('text') or ''))))


def public_event(event):
    """Project an archived record into chat without exposing old dispatch blocks."""
    if not public_line(event):
        return None
    if event.get('role') == 'front' and event.get('from') in MODELS:
        _, text = parse_front(event.get('text') or '')
        return {**event, 'text': text} if text or event.get('images') else None
    return event

