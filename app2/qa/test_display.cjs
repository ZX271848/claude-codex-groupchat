const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../ui.html'),'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(code); // validate the generated UI, not only the helper under test
const removed=[],state={live:{},status:{}};
const context={window:{},state,addLines(){},removeLive(k){removed.push(k);delete state.live[k]},
 renderStatus(){},markBusy(){},fillSelect(){},$(){},inner:{}};
vm.createContext(context);
vm.runInContext(code.slice(code.indexOf('window.gc = {'),code.indexOf('const api =')),context);
for(const value of ['—','', 'final output']){
 state.live['claude:work']={text:value};
 context.window.gc.liveEnd('claude:work',false);
 assert.equal(state.live['claude:work'],undefined,'normal completion always removes a bubble');
}
const cleanup=code.match(/for\(const \[key\] of Object\.entries\(state\.live\)\)\{[^\n]+/)[0];
state.live={'claude:work':{},'gpt:front':{}};
state.status={claude:{lanes:{work:'idle'}},gpt:{lanes:{front:'busy'}}};
vm.runInContext(cleanup,context);
assert.equal(state.live['claude:work'],undefined);
assert.ok(state.live['gpt:front'],'do not clear the other live lane');
const filter=code.match(/lines=lines\.filter\([^\n]+/)[0];
context.lines=[{from:'user',text:'hello'},{from:'gpt',role:'working',text:'note'},
 {from:'claude',role:'front',text:'交给工作：gpt\nprivate'},
 {from:'claude',kind:'routing',text:'assignment'},
 {from:'gpt',role:'work',text:'done'}];
vm.runInContext(filter,context);
assert.deepEqual(context.lines.map(x=>x.text),['hello','done']);
vm.runInContext(code.slice(code.indexOf('function esc(s)'),code.indexOf('function inline(s)')),context);
context.NAMES={gpt:'GPT',claude:'Claude'};
const elements={'#dispatch-retries':{},'#dispatch-history':{open:false},'#dispatch-summary':{}};
context.$=s=>elements[s];
context.window.gc.status({models:{claude:{busy:true,error:null}},retry:[
 {id:'old',model:'claude',seqs:[1096],status:'interrupted',error:'error_during_execution'},
 {id:'stopped',model:'claude',seqs:[1104],status:'interrupted',interruption:'user_stop'},
 {id:'restart',model:'gpt',seqs:[2],status:'interrupted',interruption:'client_restart'},
 {id:'failed',model:'claude',seqs:[3],status:'failed',error:'<transport error>'}
]});
assert.match(elements['#dispatch-retries'].innerHTML,/Claude · 消息 1096 · 已中断/);
assert.doesNotMatch(elements['#dispatch-retries'].innerHTML,/error_during_execution/);
assert.match(elements['#dispatch-retries'].innerHTML,/消息 1104 · 已停止/);
assert.match(elements['#dispatch-retries'].innerHTML,/客户端重开后未确认完成/);
assert.match(elements['#dispatch-retries'].innerHTML,/&lt;transport error&gt;/);
assert.equal(elements['#dispatch-summary'].textContent,'历史中断 / 失败（4）');
assert.equal(elements['#dispatch-history'].hidden,false);
assert.equal(elements['#dispatch-history'].open,false,'polling never expands historical errors');
assert.equal(state.status.claude.busy,true,'historical failures do not override current work');
context.window.gc.status({retry:[]});
assert.equal(elements['#dispatch-history'].hidden,true);
console.log('Display checks passed: completion cleanup, filtering and historical/current status separation.');
