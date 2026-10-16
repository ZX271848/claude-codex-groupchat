"""Manual live classifier probe: isolated room, safe mode, no worker delivery."""
import argparse
import atexit
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from routing import Classifier
from routing_memory import load_memory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    room = {'dir': str(args.output), 'name': 'isolated router cache QA'}
    candidates = list((Path(os.environ['LOCALAPPDATA'])/'Packages').glob(
        'Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe'))
    exe = max(candidates, key=lambda p: p.stat().st_mtime)
    env = dict(os.environ, GROUPCHAT_OFF='1', CODEX_BRIDGE_DIR=str(args.output))
    for key in ('GROUPCHAT_HOSTED','GROUPCHAT_ROOM','GROUPCHAT_CHANNEL','CLAUDECODE'):
        env.pop(key, None)
    audits = []

    def audit(room, kind, **data):
        audits.append(data)
        with (args.output/'routing-log.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'kind':kind, **data}, ensure_ascii=False)+'\n')

    classifier = Classifier(lambda: str(exe), ROOT/'prompts/coordinator-memory.md', audit, lambda: env)
    atexit.register(classifier.close)
    events = [
        {'seq':1,'from':'user','text':'Claude is deploying a training update. GPT is available.'},
        {'seq':2,'from':'claude','text':'I am testing the update. I will report when deployment is complete.'},
        {'seq':3,'from':'user','text':'Remember for future routing: when one assistant is busy, send an independent new topic to the idle one. Keep corrections and follow-ups with the original worker.'},
    ]
    results = []
    results.append(classifier.choose(room, events, {'gpt':'idle','claude':'busy'}))
    events += [
        {'seq':4,'from':'gpt','text':'Routing preference recorded.'},
        {'seq':5,'from':'user','text':'What is the difference between mid-training and post-training?'},
    ]
    results.append(classifier.choose(room, events, {'gpt':'idle','claude':'busy'}))
    events += [
        {'seq':6,'from':'gpt','text':'Mid-training adapts capabilities before later behavior-focused training; the terminology can overlap.'},
        {'seq':7,'from':'user','text':'For the training deployment that is still running, preserve the optimizer state and do not change the validation set.'},
    ]
    results.append(classifier.choose(room, events, {'gpt':'idle','claude':'busy'}))
    classifier.close()
    report = {'recipients': results, 'memory_rule_count': len(load_memory(room)['rules']),
              'calls': [{k:a.get(k) for k in ('session_id','autocompact','seconds','usage','cost_usd')}
                        for a in audits],
              'compaction_triggered': False}
    (args.output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    assert results == ['gpt','gpt','claude'], results
    assert report['memory_rule_count'] >= 1
    assert len({a['session_id'] for a in audits}) == 1
    initial_prefix = sum(audits[0]['usage'].get(k,0) for k in (
        'cache_read_input_tokens','cache_creation_input_tokens'))
    assert audits[1]['usage']['cache_read_input_tokens'] >= initial_prefix
    assert audits[2]['usage']['cache_read_input_tokens'] > initial_prefix


if __name__ == '__main__':
    main()
