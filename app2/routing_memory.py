"""Room-scoped routing preferences that survive native context compaction."""
import json
import os
from pathlib import Path
import re
import tempfile
import time

VERSION = 1
KEY = re.compile(r'[a-z][a-z0-9_]{0,63}')
MAX_RULES = 64


def load_memory(room):
    path = Path(room['dir']) / 'routing-memory.json'
    if not path.exists():
        return {'version': VERSION, 'rules': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != VERSION or not isinstance(data.get('rules'), dict):
        raise ValueError('路由记忆格式无效；没有清空或覆盖原记忆')
    for key, value in data['rules'].items():
        if not KEY.fullmatch(key) or not isinstance(value, dict) or not isinstance(value.get('text'), str):
            raise ValueError('路由记忆条目无效；没有清空或覆盖原记忆')
    return data


def validate_edits(remember, forget):
    if not isinstance(remember, list) or not isinstance(forget, list):
        raise ValueError('路由记忆修改必须是列表')
    seen = set()
    for item in remember:
        if not isinstance(item, dict) or set(item) != {'key', 'text'}:
            raise ValueError('路由记忆修改字段无效')
        key, text = item['key'], item['text']
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in seen:
            raise ValueError('路由记忆键无效或重复')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1200:
            raise ValueError('路由记忆规则为空或过长')
        seen.add(key)
    for key in forget:
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in seen:
            raise ValueError('路由记忆删除键无效、重复或冲突')
        seen.add(key)


def atomic_json(path, data):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.routing-', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_edits(room, memory, user_event, remember, forget):
    validate_edits(remember, forget)
    if not remember and not forget:
        return memory
    if user_event.get('from') != 'user' or type(user_event.get('seq')) is not int:
        raise ValueError('路由记忆修改缺少当前用户消息的来源')
    rules = dict(memory['rules'])
    for key in forget:
        if key not in rules:
            raise ValueError('不能删除不存在的路由记忆')
        del rules[key]
    for item in remember:
        rules[item['key']] = {
            'text': item['text'].strip(), 'source_seq': user_event['seq'],
            'source_text': user_event.get('text', ''), 'updated_at': time.time(),
        }
    if len(rules) > MAX_RULES:
        raise ValueError('路由记忆已满；需要明确合并规则，不能自动遗忘旧偏好')
    updated = {'version': VERSION, 'rules': rules}
    path = Path(room['dir']) / 'routing-memory.json'
    atomic_json(path, updated)
    return updated
