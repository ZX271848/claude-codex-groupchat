// State-machine regressions; HTTP integration is in test_mobile.py.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map(),saved=new Map([['gc-mobile:draft:old','private legacy draft'],['unrelated','keep']]);
const legacyStore={get length(){return saved.size},key:i=>[...saved.keys()][i],removeItem:k=>saved.delete(k)};
function element(){return {value:'',innerHTML:'',textContent:'',hidden:false,inert:false,disabled:false,style:{setProperty(){}},
  scrollTop:0,scrollHeight:0,clientHeight:600,classList:{toggle(){}},children:[],
  remove(){},addEventListener(){},append(row){this.children.push(row)},querySelector(){return null},close(){},showModal(){}}}
function query(key){if(!elements.has(key))elements.set(key,element());return elements.get(key)}
const context=vm.createContext({console,URLSearchParams,AbortController,crypto:require('node:crypto').webcrypto,
  setTimeout,clearTimeout,Uint8Array,localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},
  document:{querySelector:query,querySelectorAll:()=>[],addEventListener(){},documentElement:element(),hidden:false},
  window:{innerHeight:844,addEventListener(){},localStorage:legacyStore,sessionStorage:legacyStore},assert});
const source=fs.readFileSync(require('node:path').join(__dirname,'../mobile/mobile.js'),'utf8');
new vm.Script(source); // validate the shipped whole script
vm.runInContext(source.replace(/sync\(true\)\.then\(\(\)=>\{if\(appState.authenticated\)poll\(\)\}\);\s*$/,''),context);
vm.runInContext(`
  assert.equal(window.localStorage.length,1);
  assert.equal(window.localStorage.key(0),'unrelated');
  appState.room='phone';appState.members=['gpt','claude','gemini'];
  renderMessage=row=>({seq:row.seq});
  draft={text:'',route:'gpt',images:[],pending:null};
  appendLines([{seq:1,text:'old message'}]); // no pending ID and no row ID must not be treated as a match
  appendLines([{seq:1,text:'duplicate'},{seq:2,text:'second'}]);
  assert.equal(document.querySelector('#messages').children.length,2);
  clearAcceptedDraft(null);
  document.querySelector('#message').value='new text typed while sending';
  draft.pending={request_id:'sent',text:'old text',images:['old.png']};
  draft.images=[{name:'old.png'},{name:'new.png'}];
  renderAttachments=()=>{};
  clearAcceptedDraft(draft.pending);
  assert.equal(document.querySelector('#message').value,'new text typed while sending');
  assert.deepEqual(draft.images.map(i=>i.name),['new.png']);
  assert.equal(draft.pending,null);
  assert.ok(!markdown('<script>alert(1)</script>').includes('<script>'));
  assert.ok(!markdown('[bad](javascript:alert(1))').includes('href='));
  assert.ok(markdown('**bold**').includes('<strong>bold</strong>'));
`,context);
(async()=>{
  await vm.runInContext(`(async()=>{
    let calls=[];
    request=async(path,body)=>{calls.push(body);throw new Error('offline')};
    document.querySelector('#message').value='keep this';
    document.querySelector('#route').value='gpt';
    draft={text:'keep this',route:'gpt',images:[],pending:null};
    await send({preventDefault(){}});await send({preventDefault(){}});
    assert.equal(calls.length,2);assert.equal(calls[0].request_id,calls[1].request_id);
    assert.equal(document.querySelector('#message').value,'keep this');
    assert.equal(appState.busy,false);
    // A late snapshot from the old room must not overwrite the newly selected room.
    let resolveOld;
    request=()=>new Promise(resolve=>resolveOld=resolve);
    const pending=sync();appState.generation++;appState.room='other-room';
    resolveOld({room:'phone',title:'old'});await pending;
    assert.equal(appState.room,'other-room');
    storage.set('draft:other-room',{text:'private draft'});
    document.querySelector('#messages').innerHTML='private chat';
    document.querySelector('#room-list').innerHTML='private projects';
    document.querySelector('#model-list').innerHTML='private status';
    showPair();
    assert.equal(storage.get('draft:other-room'),null);
    assert.equal(appState.room,'');
    assert.equal(appState.cursor,null);
    assert.equal(document.querySelector('#message').value,'');
    for(const id of ['messages','room-list','model-list','attachments'])assert.equal(document.querySelector('#'+id).innerHTML,'');
    assert.equal(document.querySelector('#app').hidden,true);
    assert.equal(appState.authenticated,false);
  })()`,context);
  console.log('Mobile UI checks passed: retry identity, draft retention, stale snapshots, deduplication, safe text, legacy storage cleanup and logout erasure.');
})().catch(error=>{console.error(error);process.exitCode=1});
