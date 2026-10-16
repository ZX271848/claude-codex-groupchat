"""Opt-in tiny real classifier probe: no room writes, hooks, workers or credentials logged."""
from pathlib import Path
import os
import sys
import json
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routing import Classifier

paths = list((Path(os.environ['LOCALAPPDATA'])/'Packages').glob('Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe'))
paths += list((Path(os.environ['APPDATA'])/'Claude/claude-code').glob('*/claude.exe'))
exe = max(paths, key=lambda p: p.stat().st_mtime)
records = []
def audit(room, kind, **data):
    records.append({k:v for k,v in data.items() if k != 'chat'})
def env():
    out = dict(os.environ, GROUPCHAT_OFF='1')
    for key in ('GROUPCHAT_CHANNEL','GROUPCHAT_HOSTED','GROUPCHAT_ROOM'):
        out.pop(key, None)
    return out
classifier = Classifier(lambda: str(exe), Path(__file__).resolve().parents[1]/'prompts/coordinator.md', audit, env)
cases = [([{'from':'gpt','role':'work','text':'客户端的图标已修好。'},
           {'from':'user','text':'你们各自说说这个布局怎么样？'}], 'both'),
         ([{'from':'claude','role':'work','text':'实验已经启动，我继续监控。'},
           {'from':'user','text':'刚才这个实验现在到哪一步了？'}], 'claude')]
for messages, expected in cases:
    result = classifier.choose({}, messages)
    print(json.dumps({'expected':expected,'actual':result,'pass':result==expected,'usage':records[-1]},ensure_ascii=False),flush=True)
    if result != expected: raise SystemExit(1)
