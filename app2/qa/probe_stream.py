"""Opt-in isolated Claude stdin insertion probe; no actual room/session/tools."""
import json, os, subprocess, tempfile, time
from pathlib import Path
paths=list((Path(os.environ['LOCALAPPDATA'])/'Packages').glob('Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe'))
exe=max(paths,key=lambda p:p.stat().st_mtime)
env=dict(os.environ,GROUPCHAT_OFF='1')
for k in ('GROUPCHAT_CHANNEL','GROUPCHAT_ROOM','GROUPCHAT_HOSTED'):env.pop(k,None)
with tempfile.TemporaryDirectory(prefix='gc-stream-probe-') as cwd:
    cmd=[str(exe),'-p','--model','claude-opus-5-5','--effort','low','--safe-mode','--tools','',
         '--strict-mcp-config','--no-session-persistence','--input-format','stream-json',
         '--output-format','stream-json','--verbose','--include-partial-messages','--replay-user-messages',
         '--system-prompt','Follow the latest short request. No tools.']
    p=subprocess.Popen(cmd,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=0x08000000)
    def send(text):
        p.stdin.write((json.dumps({'type':'user','message':{'role':'user','content':text}})+'\n').encode());p.stdin.flush()
    send('Write the numbers 1 to 40, one number per line.')
    inserted=False;results=[];events=[]
    for raw in p.stdout:
        e=json.loads(raw);kind=e.get('type')
        if kind in ('user','assistant','result'):events.append({'type':kind,'keys':list(e)})
        if kind=='stream_event' and e.get('event',{}).get('type')=='content_block_delta' and not inserted:
            send('New user message: reply only SECOND.');inserted=True
        if kind=='result':
            results.append({k:e.get(k) for k in ('subtype','result','total_cost_usd','usage')})
            if len(results)==2:break
    p.stdin.close()
    try:p.wait(timeout=5)
    except subprocess.TimeoutExpired:p.kill()
    out={'inserted':inserted,'events':events,'results':results}
    Path(__file__).with_name('stream-probe-result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(out,ensure_ascii=False))
