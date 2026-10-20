'use strict';
const $ = selector => document.querySelector(selector);
const names = {user:'你',gpt:'GPT',claude:'Claude',gemini:'Gemini',controller:'群聊'};
const appState = {room:'',cursor:null,generation:0,seen:new Set(),tree:'',models:'',members:[],busy:false,uploading:false,online:false,authenticated:false};
// Keep chat drafts only in this live page. Do not persist chat text to browser storage.
const drafts = new Map();
const storage = {get:(key,fallback=null)=>drafts.has(key)?JSON.parse(drafts.get(key)):fallback,
  set:(key,value)=>drafts.set(key,JSON.stringify(value)),clear:()=>drafts.clear()};
function clearLegacyStorage(){
  for(const name of ['localStorage','sessionStorage'])try{
    const area=window[name];
    for(let i=area.length-1;i>=0;i--){const key=area.key(i);if(key?.startsWith('gc-mobile:'))area.removeItem(key)}
  }catch{}
}
clearLegacyStorage();
let draft = {text:'',route:'auto',images:[],pending:null};
let pollTimer, toastTimer, syncFlight;
const escapeHTML = value => String(value).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function requestID(){const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Date.now()+'-'+[...bytes].map(n=>n.toString(16).padStart(2,'0')).join('')}
function notify(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,4500)}
async function request(path, body){
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),12000);
  try{
    const response=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',
      headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined,signal:controller.signal});
    const result=await response.json();
    if(!response.ok||result.error){const error=new Error(result.error||'连接失败');error.status=response.status;throw error}
    return result;
  }catch(error){
    if((error.status===401||error.status===403) && path!=='/api/pair')showPair();
    if(error.name==='AbortError'||error instanceof TypeError)throw new Error('连接中断，请检查 Tailscale 已连接、Mac 仍在运行');
    throw error;
  }finally{clearTimeout(timer)}
}
function action(kind, values={}){return request('/api/action',{action:kind,room:appState.room,request_id:requestID(),...values})}
function showPair(){
  appState.authenticated=false;appState.generation++;clearTimeout(pollTimer);
  appState.room='';appState.cursor=null;appState.seen.clear();appState.tree='';appState.models='';appState.members=[];
  storage.clear();clearLegacyStorage();draft={text:'',route:'auto',images:[],pending:null};
  for(const id of ['messages','room-list','model-list','attachments'])$('#'+id).innerHTML='';
  $('#message').value='';$('#files').value='';$('#title').textContent='';document.title='群聊 · 手机';note();
  $('#app').hidden=true;$('#pair').hidden=false;
  for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
}
function connection(online, message=''){
  appState.online=online;$('#connection').textContent=online?'已连接 Mac':'连接中断';$('#connection').classList.toggle('offline',!online);
  $('#network').hidden=online;$('#network').textContent=message||'正在重新连接 Mac，消息草稿已保留。';
}
function saveDraft(){
  if(!appState.room)return;
  draft.text=$('#message').value;draft.route=$('#route').value||draft.route;
  storage.set('draft:'+appState.room,draft);
}
function note(message=''){$('#send-note').textContent=message;$('#send-note').hidden=!message}
function autosize(){const input=$('#message');input.style.height='auto';input.style.height=Math.min(150,input.scrollHeight)+'px'}
function loadDraft(){
  draft=storage.get('draft:'+appState.room,{text:'',route:'auto',images:[],pending:null});
  $('#message').value=draft.text||'';renderRoutes();renderAttachments();autosize();
  note(draft.pending?'上次发送尚未确认，点击发送可用同一编号重试。':'');
}
function renderRoutes(){
  const options=[['auto','自动'],...appState.members.map(m=>[m,names[m]]),[appState.members.includes('gemini')?'all':'both',appState.members.includes('gemini')?'全体':'双方']];
  $('#route').innerHTML=options.map(([id,label])=>`<option value="${id}">${label}</option>`).join('');
  $('#route').value=options.some(([id])=>id===draft.route)?draft.route:'auto';
}
function imageURL(path){return '/api/image?'+new URLSearchParams({room:appState.room,path})}
function inline(text){
  return escapeHTML(text).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}
function markdown(text){
  return String(text).split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g).map(part=>{
    if(part.startsWith('```'))return '<pre><code>'+escapeHTML(part.replace(/^```[^\n]*\n/,'').replace(/```$/,''))+'</code></pre>';
    return part.split(/\n{2,}/).filter(Boolean).map(p=>/^#{1,6} /.test(p)?'<h3>'+inline(p.replace(/^#{1,6} /,''))+'</h3>':'<p>'+inline(p)+'</p>').join('');
  }).join('');
}
function renderMessage(row){
  const el=document.createElement('article'), who=Object.hasOwn(names,row.from)?row.from:'controller';
  el.className='message '+who;el.dataset.seq=row.seq;
  const date=new Date(row.ts), stamp=Number.isNaN(date.valueOf())?'':date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  el.innerHTML=`<div class="byline"><b class="author">${names[who]}</b><span>${escapeHTML(stamp)}</span>${row.kind==='progress'?'<span>进展</span>':''}</div><div class="bubble"></div>`;
  const bubble=el.querySelector('.bubble');
  if(who==='user'){const p=document.createElement('p');p.textContent=row.text||'';bubble.append(p)}
  else bubble.innerHTML=markdown(row.text||'');
  const paths=[...(row.images||[])];
  if(who!=='user')for(const match of String(row.text||'').matchAll(/!\[[^\]\n]*\]\(<?([^)>\n]+)>?\)/g))if(!/^(https?:|data:)/i.test(match[1]))paths.push(match[1]);
  for(const path of new Set(paths)){
    const img=document.createElement('img');img.src=imageURL(path);img.alt='消息图片';img.loading='lazy';
    img.addEventListener('error',()=>{const caption=document.createElement('p');caption.textContent='图片无法显示，可在 Mac 查看';img.replaceWith(caption)});
    bubble.append(img);
  }
  return el;
}
function appendLines(rows){
  const log=$('#log'), atEnd=log.scrollHeight-log.scrollTop-log.clientHeight<100;
  if(rows.length)$('#messages .empty')?.remove();
  let added=false;
  for(const row of rows){
    if(appState.seen.has(row.seq))continue;
    appState.seen.add(row.seq);$('#messages').append(renderMessage(row));added=true;
    if(draft.pending && draft.pending.request_id===row.mobile_request_id)clearAcceptedDraft(draft.pending);
  }
  if(atEnd&&added)log.scrollTop=log.scrollHeight;
  else if(added)$('#new-messages').hidden=false;
}
function renderTree(tree){
  const signature=JSON.stringify([tree,appState.room]);if(signature===appState.tree)return;appState.tree=signature;
  $('#room-list').innerHTML=tree.map(p=>`<section><div class="project-head"><h3>${escapeHTML(p.name)}</h3>${p.can_create?`<button class="text-button" data-new="${escapeHTML(p.id)}">＋ 新群聊</button>`:''}</div>`+
    p.rooms.map(r=>`<button class="room${r.id===appState.room?' active':''}" data-room="${escapeHTML(r.id)}">${escapeHTML(r.title)}<small>${escapeHTML(r.when)}</small></button>`).join('')+'</section>').join('');
}
function renderModels(data){
  const status=data.status;
  $('#mode-label').textContent=status.mode==='collaboration'?'协同已开启':'普通模式';
  if(document.activeElement!==$('#mode'))$('#mode').value=status.mode;
  const signature=JSON.stringify([status.models,data.models]);if(signature===appState.models)return;appState.models=signature;
  $('#model-list').innerHTML=appState.members.map(m=>{
    const info=status.models[m]||{}, q=info.quota||{};
    const quota=q.remaining!=null?`剩余额度 ${q.remaining}%${q.resets?' · '+q.resets:''}`:(q.note||'');
    return `<article class="model-card"><div class="model-head"><b>${names[m]}</b><span class="model-state ${info.busy?'busy':''}">${info.busy?'● 工作中':info.ready?'● 已就绪':'○ 未连接'}${info.queued?' · 排队 '+info.queued:''}</span>${info.busy?`<button class="stop" data-stop="${m}">停止</button>`:''}</div><p class="model-name">${escapeHTML(data.models[m]||m)}</p><div class="quota">${escapeHTML(quota)}</div>${info.error?'<p class="model-error">'+escapeHTML(info.error)+'</p>':''}</article>`;
  }).join('');
  $('#retry-note').textContent=status.retry?.length?`有 ${status.retry.length} 条历史中断或失败记录，可在 Mac 查看和重新递送。`:'';
}
function applySnapshot(data, reset){
  if(reset){
    appState.room=data.room;storage.set('room',data.room);appState.seen.clear();appState.members=data.members;
    $('#messages').innerHTML='<div class="empty">从这里继续，与三位成员一起工作。</div>';$('#new-messages').hidden=true;
    loadDraft();
  }
  $('#title').textContent=data.title;document.title=data.title+' · 群聊';
  renderTree(data.tree);renderModels(data);appendLines(data.lines);appState.cursor=data.cursor;
  if(reset)$('#log').scrollTop=$('#log').scrollHeight;
}
function sync(reset=false){
  const generation=appState.generation;
  if(syncFlight?.generation===generation)return syncFlight.promise;
  const flight={generation};
  flight.promise=fetchSnapshot(reset).finally(()=>{if(syncFlight===flight)syncFlight=null});
  syncFlight=flight;return flight.promise;
}
async function fetchSnapshot(reset=false){
  const generation=appState.generation;
  const query=new URLSearchParams({room:appState.room});if(!reset&&appState.cursor!==null)query.set('after',appState.cursor);
  try{
    const data=await request('/api/snapshot?'+query);
    if(generation!==appState.generation)return;
    appState.authenticated=true;$('#pair').hidden=true;$('#app').hidden=false;
    applySnapshot(data,reset||appState.cursor===null);connection(true);$('#composer').inert=false;
  }catch(error){
    if(generation!==appState.generation)return;
    if(error.status===404&&appState.room){saveDraft();appState.room='';appState.cursor=null;return fetchSnapshot(true)}
    connection(false,error.message);
    if(!appState.authenticated)$('#pair-error').textContent=error.status===401?'':error.message;
  }
}
async function poll(){
  clearTimeout(pollTimer);
  if(!document.hidden)await sync();
  if(appState.authenticated)pollTimer=setTimeout(poll,document.hidden?8000:1800);
}
async function selectRoom(rid){
  if(appState.busy||appState.uploading){notify('请等当前发送或上传完成');return}
  saveDraft();appState.generation++;appState.room=rid;appState.cursor=null;
  $('#rooms').close();$('#composer').inert=true;
  $('#messages').innerHTML='<div class="empty">正在载入群聊…</div>';$('#title').textContent='正在连接…';
  loadDraft();await sync(true);
}
function clearAcceptedDraft(sent){
  if(!sent||!draft.pending||draft.pending.request_id!==sent.request_id)return;
  draft.pending=null;
  if($('#message').value===sent.text)$('#message').value='';
  draft.images=draft.images.filter(img=>!sent.images.includes(img.name));
  renderAttachments();autosize();saveDraft();note();
}
async function send(event){
  event.preventDefault();if(appState.busy||appState.uploading)return;
  saveDraft();if(!draft.text.trim()&&!draft.images.length)return;
  const payload={action:'send',room:appState.room,text:draft.text,route:draft.route,images:draft.images.map(img=>img.name)};
  const same=draft.pending&&Object.keys(payload).every(key=>JSON.stringify(payload[key])===JSON.stringify(draft.pending[key]));
  if(!same)draft.pending={...payload,request_id:requestID()};
  const generation=appState.generation, sent=draft.pending;saveDraft();appState.busy=true;$('#send').disabled=true;note('正在发送…');
  try{await request('/api/action',sent);if(generation!==appState.generation)return;clearAcceptedDraft(sent);await sync()}
  catch(error){if(draft.pending?.request_id===sent.request_id)note(error.message+'。草稿已保留；同一内容再次发送不会重复递送。')}
  finally{appState.busy=false;$('#send').disabled=false}
}
function renderAttachments(){
  $('#attachments').hidden=!draft.images.length;
  $('#attachments').innerHTML=draft.images.map((img,i)=>`<div class="attachment"><img src="${escapeHTML(imageURL('attachments/'+img.name))}" alt="附件"><button type="button" data-remove="${i}" aria-label="移除图片">×</button></div>`).join('');
}
async function shrink(file){
  const url=URL.createObjectURL(file);
  try{
    const img=new Image();img.src=url;await img.decode();
    const scale=Math.min(1,2000/Math.max(img.width,img.height)), canvas=document.createElement('canvas');
    canvas.width=Math.round(img.width*scale);canvas.height=Math.round(img.height*scale);canvas.getContext('2d').drawImage(img,0,0,canvas.width,canvas.height);
    return canvas.toDataURL('image/jpeg',.88);
  }finally{URL.revokeObjectURL(url)}
}
$('#pair-form').addEventListener('submit',async event=>{
  event.preventDefault();$('#pair-submit').disabled=true;$('#pair-error').textContent='';
  try{await request('/api/pair',{code:$('#pair-code').value.trim()});$('#pair-code').value='';appState.generation++;await sync(true);poll()}
  catch(error){$('#pair-error').textContent=error.message}
  finally{$('#pair-submit').disabled=false}
});
$('#composer').addEventListener('submit',send);
$('#message').addEventListener('input',()=>{autosize();saveDraft()});
$('#message').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)&&!event.isComposing)send(event)});
$('#route').addEventListener('change',saveDraft);
$('#rooms-open').addEventListener('click',()=>$('#rooms').showModal());
$('#models-open').addEventListener('click',()=>$('#models').showModal());
for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>$('#'+button.dataset.close).close());
$('#room-list').addEventListener('click',async event=>{
  const room=event.target.closest('[data-room]');if(room)return selectRoom(room.dataset.room);
  const create=event.target.closest('[data-new]');if(!create||appState.busy||appState.uploading)return;
  create.disabled=true;const generation=appState.generation;
  try{const result=await action('new_room',{project:create.dataset.new});if(generation!==appState.generation)return;await selectRoom(result.room)}catch(error){notify(error.message)}finally{create.disabled=false}
});
$('#model-list').addEventListener('click',async event=>{
  const button=event.target.closest('[data-stop]');if(!button)return;button.disabled=true;
  try{await action('stop',{model:button.dataset.stop});notify('已请求停止');await sync()}catch(error){notify(error.message)}finally{button.disabled=false}
});
$('#mode').addEventListener('change',async event=>{
  event.target.disabled=true;try{await action('mode',{mode:event.target.value});await sync()}catch(error){notify(error.message)}finally{event.target.disabled=false}
});
$('#attach').addEventListener('click',()=>$('#files').click());
$('#files').addEventListener('change',async event=>{
  if(appState.uploading||appState.busy)return;appState.uploading=true;$('#attach').disabled=true;
  const generation=appState.generation;
  try{
    for(const file of event.target.files){
      if(draft.images.length>=8){notify('每条消息最多附 8 张图片');break}
      if(file.size>25_000_000){notify('原图过大，请先缩小到 25 MB 以下');continue}
      const data=await shrink(file);if(generation!==appState.generation)return;
      const result=await action('attach',{data});if(generation!==appState.generation)return;
      draft.images.push(result);saveDraft();renderAttachments();
    }
  }catch(error){notify(error.message)}finally{event.target.value='';appState.uploading=false;$('#attach').disabled=false}
});
$('#attachments').addEventListener('click',event=>{const button=event.target.closest('[data-remove]');if(button){draft.images.splice(Number(button.dataset.remove),1);renderAttachments();saveDraft()}});
$('#logout').addEventListener('click',async()=>{try{await request('/api/logout',{});showPair()}catch(error){notify(error.message)}});
$('#new-messages').addEventListener('click',()=>{$('#log').scrollTop=$('#log').scrollHeight;$('#new-messages').hidden=true});
$('#log').addEventListener('scroll',()=>{if($('#log').scrollHeight-$('#log').scrollTop-$('#log').clientHeight<100)$('#new-messages').hidden=true});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&appState.authenticated)poll()});
window.addEventListener('online',()=>{if(appState.authenticated)poll()});
function resize(){document.documentElement.style.setProperty('--app-height',(window.visualViewport?.height||window.innerHeight)+'px')}
window.visualViewport?.addEventListener('resize',resize);resize();
appState.room=storage.get('room','');
sync(true).then(()=>{if(appState.authenticated)poll()});
