"""Local, passive tool receipts. Never runs tools/tests or asks a model to summarize.

Only selected observations/actions cross the channel. Raw selected events stay in
the room for optional inspection; ordinary successful reads/searches are omitted.
"""
import json
import os
import re
import uuid

import common as C


def short(value, limit=180):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(x) for x in value)
    if isinstance(value, dict):
        # Do not turn images, reasoning, or arbitrary structured payloads into text.
        return text_content(value.get("text") or value.get("content") or "")
    return ""


TEST_COMMAND = re.compile(r"\b(pytest|unittest|vitest|jest|playwright\s+test|cargo\s+test|go\s+test|dotnet\s+test|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test)\b", re.I)
TEST_OUTPUT = re.compile(r"\b\d+\s+(?:passed|failed|skipped|tests?\b)|\bRan\s+\d+\s+tests?\b|\bTests?:\s+\d|\btest result:", re.I)
OBSERVATION = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:key observations?|关键观察)\s*[:：](?:\*\*)?\s*(.*)$", re.I)


def nested_results(value, depth=0):
    """Structured exec results printed by code-mode; no inference from source code."""
    if depth > 5:
        return
    if isinstance(value, dict):
        if "exit_code" in value and isinstance(value.get("output"), str):
            yield value
        else:
            for child in value.values():
                yield from nested_results(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from nested_results(child, depth + 1)
    elif isinstance(value, str):
        for line in value.splitlines():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, (dict, list)):
                yield from nested_results(parsed, depth + 1)


class WorkRecord:
    def __init__(self, room, who, session=None, turn=None):
        self.room, self.who, self.session, self.turn = dict(room), who, session, turn
        self.path = os.path.join(room["dir"], "work-records", uuid.uuid4().hex + ".jsonl")
        self.pending = {}
        self.seen = set()
        self.actions, self.observations = [], []
        self.finished = False

    def keep(self, event, actions=(), observations=()):
        if not actions and not observations:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        for target, values in [(self.actions, actions), (self.observations, observations)]:
            for value in values:
                if value not in target:
                    target.append(value)

    def observation_text(self, text):
        # Optional existing author observations, never an extra model request or a
        # required report. Keep attribution; do not turn them into verified facts.
        lines = []
        for line in text.splitlines():
            match = OBSERVATION.match(line)
            if match and match.group(1).strip():
                lines.append("模型原话：" + short(match.group(1), 240))
        self.keep({"type": "author_observation", "text": text}, observations=lines)

    def command(self, event, command, output, failed=False, exit_code=None, status=None, result_excerpt=False):
        observations = []
        testing = bool(TEST_COMMAND.search(command or ""))
        if testing or result_excerpt:
            for line in output.splitlines():
                if TEST_OUTPUT.search(line):
                    observations.append("工具输出摘录：" + short(line, 220))
            observations = observations[-3:]
        if not testing and not failed and not observations:
            return
        result = "工具报告失败" if failed else "工具返回"
        if exit_code is not None:
            result += f"，exit={exit_code}"
        elif status:
            result += "，status=" + str(status)
        self.keep(event, [f"{short(command)} — {result}"], observations)

    def claude(self, event):
        if event.get("session_id"):
            self.session = event["session_id"]
        message = event.get("message") or {}
        blocks = message.get("content") or []
        if not isinstance(blocks, list):
            return
        for block in blocks:
            kind = block.get("type")
            if event.get("type") == "assistant" and kind == "tool_use":
                self.pending[block["id"]] = block
            elif event.get("type") == "assistant" and kind == "text":
                self.observation_text(block.get("text", ""))
            elif kind == "tool_result":
                key = block.get("tool_use_id")
                if key in self.seen:
                    continue
                self.seen.add(key)
                call = self.pending.pop(key, {})
                name, args = call.get("name", "unknown"), call.get("input") or {}
                output = text_content(block.get("content"))
                failed = bool(block.get("is_error"))
                raw = {"call": call, "result": block}
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    path = args.get("file_path") or args.get("notebook_path") or "（未提供路径）"
                    self.keep(raw, [f"{name} {short(path)} — 工具报告{'失败' if failed else '成功'}"])
                elif name in ("Bash", "PowerShell"):
                    self.command(raw, args.get("command", ""), output, failed)
                elif failed:
                    self.keep(raw, [f"{name} — 工具报告失败"], ["工具输出摘录：" + short(output, 220)])

    def gpt(self, item):
        key = item.get("id")
        if key and key in self.seen:
            return
        if key:
            self.seen.add(key)
        kind, status = item.get("type"), item.get("status")
        if kind == "agentMessage":
            self.observation_text(item.get("text", ""))
        elif kind == "fileChange":
            actions = []
            for change in item.get("changes", []):
                action = change.get("kind") or {}
                action = action.get("type", "修改") if isinstance(action, dict) else action
                actions.append(f"{action} {short(change.get('path'))} — 工具状态 {status}")
            self.keep(item, actions)
        elif kind == "commandExecution":
            code = item.get("exitCode")
            self.command(item, item.get("command", ""), item.get("aggregatedOutput") or "",
                         status in ("failed", "declined") or code not in (None, 0), code, status)
        elif kind in ("mcpToolCall", "dynamicToolCall"):
            result = item.get("result") or {}
            failed = status in ("failed", "declined") or item.get("success") is False or bool(item.get("error"))
            if isinstance(result, dict):
                failed = failed or bool(result.get("isError"))
            if failed:
                output = text_content(item.get("contentItems") or result)
                self.keep(item, [f"{item.get('tool')} — 工具报告失败"],
                          ["工具输出摘录：" + short(output, 220)] if output else [])
            # A completed wrapper is not proof its nested tools succeeded. Read
            # only explicit nested exit codes; otherwise leave the payload local.
            for nested in nested_results(item.get("contentItems") or result):
                code = nested["exit_code"]
                if code is not None:
                    self.command(item, f"{item.get('tool')} 内的命令（完整调用见记录）", nested["output"],
                                 code != 0, code, result_excerpt=True)

    def finish(self, status="completed"):
        if self.finished:
            return
        # Calls without results are not successful edits. Keep them only for
        # interrupted/error turns, where partial work matters to the next reader.
        if status != "completed":
            pending = [f"{c.get('name')} — 已发起，未收到结果" for c in self.pending.values()]
            self.keep({"type": "turn_end", "status": status}, pending)
        if not self.actions and not self.observations:
            self.finished = True
            return
        lines = [f"本地 controller · {C.AI_NAME[self.who]} 工具记录（轮次：{status}）"]
        for title, values in [("Key observations", self.observations), ("Tool use", self.actions)]:
            if values:
                lines.append(title + ":")
                lines.extend("- " + value for value in values[:8])
                if len(values) > 8:
                    lines.append(f"- 另有 {len(values)-8} 条，见记录。")
        lines.append("记录：" + self.path)
        # Same session excludes this receipt from its author's next prompt. No @
        # prefix, user routing, or model wakeup is generated here.
        C.append(self.room, self.who, "\n".join(lines), "controller", session=self.session,
                 kind="tool_summary", turn=self.turn, artifact=self.path)
        self.finished = True
