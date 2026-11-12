'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const walk=n=>[n,...n.children.flatMap(walk)];
let document;
class Element{
 constructor(tag){Object.defineProperty(this,'tagName',{value:tag,writable:true,configurable:true});this.children=[];this.attrs={};this.handlers={};this.querySelector=selector=>selectOne(this,selector);this._text='';this.value='';this.className='';this.dataset={};this.isConnected=true;this.disabled=false;this.open=false;this.classList={toggle:(name,on)=>{const s=new Set(this.className.split(' ').filter(Boolean));on?s.add(name):s.delete(name);this.className=[...s].join(' ');},add:name=>{this.className+=' '+name;}};}
 set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 get options(){return this.children;}get elements(){return Object.fromEntries(walk(this).filter(n=>n.name).map(n=>[n.name,n]));}
 append(...cs){for(const n of cs){this.children.push(n);n.parent=this;}}
 prepend(...cs){this.children.unshift(...cs);for(const n of cs)n.parent=this;}
 replaceChildren(...cs){this._text='';for(const n of this.children)n.removeTree();this.children=[];this.append(...cs);}
 removeTree(){this.isConnected=false;for(const n of this.children)n.removeTree();}
 remove(){this.removeTree();if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
 setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k]??null;}
 addEventListener(k,fn){this.handlers[k]=fn;}
 contains(n){return walk(this).includes(n);}
 querySelector(s){return walk(this).find(n=>s.startsWith('#')?n.id===s.slice(1):s.startsWith('.')?n.className.split(' ').includes(s.slice(1)):s==='details[open]'?n.tagName==='details'&&n.open:false)||null;}
 after(n){const at=this.parent.children.indexOf(this);this.parent.children.splice(at+1,0,n);n.parent=this.parent;}
 showModal(){this.open=true;document.activeElement=this.querySelector('#ktv-cancel-delete');}
 close(){this.open=false;this.handlers.close?.();}
 focus(){document.activeElement=this;}reportValidity(){return true;}scrollIntoView(){}
}
const selectOne=(node,s)=>walk(node).find(n=>s.startsWith('#')?n.id===s.slice(1):s.startsWith('.')?n.className.split(' ').includes(s.slice(1)):s==='details[open]'?n.tagName==='details'&&n.open:false)||null;
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
(async()=>{
 const root=new Element('main'),body=new Element('body');document={createElement:t=>new Element(t),querySelector:()=>null,body,hidden:false,activeElement:body};
 const state={active:true,room_id:'SYNTHETIC-room',simulated:true,defaults:{theme:'content',original:0,vocal_preset:'dry',music_gain:50,mic_gain:40,sfx_gain:80},status:{theme:'content',microphone_available:true,mac_online:true,queue_revision:4,controls:{music_gain:50,mic_gain:40,sfx_gain:80,original:0,mic_muted:1,control_revision:3,vocal_preset:'dry'},playback:{online:true,current:'current',played:[],state:'playing',mic_muted:true,applied_revision:3,safety_latched:false,vocal_preset:'dry',vocal_preset_state:'applied',audio_capabilities:{vocal_presets:['dry','studio','echo'],sfx:true}},songs:[{id:'current',song_id:'1',source:'song',title:'合成当前曲',nickname:'朋友',state:'ready'},{id:'next',song_id:'2',source:'song',title:'合成下一曲',nickname:'朋友',state:'ready'}]}};
 const host={available:true,configured:true,bytes:1024**3,policy:{enabled:false,limit_bytes:20*1024**3}};
 const inventory={mac:{...host},nas:{...host},entries:[{id:'song:1',title:'空闲合成曲',last_used:1,mac:{bytes:100,can_delete:true},nas:{bytes:100,can_delete:true}},{id:'song:2',title:'受保护合成曲',last_used:2,mac:{bytes:200,can_delete:false,reason:'in_queue'},nas:{bytes:200,can_delete:false,reason:'in_queue'}}]};
 const writes=[],reads=[];let deleteAttempts=0,cleared=false,timer,lateResolve=null;
 const scope=vm.createContext({document,console,Date,JSON,Set,Map,URL,AbortController,AbortSignal,performance,crypto:require('node:crypto').webcrypto,setTimeout,clearTimeout,setInterval:fn=>{timer=fn;return 17;},clearInterval:id=>{assert.equal(id,17);cleared=true;},fetch:async(path,options={})=>{
  assert.ok(path.startsWith('/api/games/ktv/'),'No cross-origin request or management key');
  assert.equal(options.credentials,'same-origin');assert.ok(!Object.keys(options.headers).some(k=>/key|authorization/i.test(k)));
  if(options.method==='POST'){
   const data=JSON.parse(options.body);writes.push({path,data});
   if(path.endsWith('manage/preview'))return reply({plan:'SYNTHETIC-plan',songs:[{title:'空闲合成曲'}],bytes:{mac:100,nas:100}});
   if(path.endsWith('manage/delete')){deleteAttempts++;return reply(deleteAttempts===1?{complete:false,retryable:true,code:'nas_management_unavailable',results:{mac:{}},nas_stores:{}}:{complete:true});}
   if(path.endsWith('manage/room/command'))return reply({seq:9});
   if(path.endsWith('manage/room/songs'))return reply({accepted:true});
   throw Error('unexpected write '+path);
  }
  reads.push(path);
  if(lateResolve!==null)return new Promise(resolve=>{lateResolve=()=>resolve(reply(structuredClone(state)));});
  if(path.endsWith('manage/room'))return reply(structuredClone(state));
  if(path.endsWith('manage/library'))return reply(structuredClone(inventory));
  if(path.includes('manage/search?'))return reply({songs:[{song_id:'7',title:'搜到的合成曲',artists:['歌手'],source:'song'}]});
  if(path.endsWith('source/status'))return reply({logged_in:true,settings:{level:'standard'}});
  if(path.endsWith('scheduler'))return reply({hardware:{cpu:'SYNTHETIC',memory_bytes:8*1024**3},settings:{mode:'auto',download:1,model:1,encode:1},limits:{download:3,model:2,encode:4}});
  if(path.endsWith('jobs'))return reply({jobs:[]});
  throw Error('unexpected read '+path);
 }});
 function reply(data){return{ok:true,json:async()=>data};}
 vm.runInContext(fs.readFileSync('integrations/ktv_public/web/party-audio.js','utf8'),scope);
 vm.runInContext(fs.readFileSync('homeplane/static/ktv-console/app.js','utf8').replace('export function mountKtv','function mountKtv'),scope);
 scope.root=root;scope.back=()=>{};const owner=vm.runInContext("mountKtv(root,{endpoint:'/api/games/ktv',embedded:true,onBack:back})",scope);
 const nodes=()=>walk(root),byId=id=>root.querySelector('#ktv-'+id),buttons=text=>nodes().filter(n=>n.tagName==='button'&&n.textContent===text),click=async n=>{assert.ok(n,'button exists');assert.ok(!n.disabled,'button enabled');await n.onclick({currentTarget:n});await settle();};
 await settle();assert.equal(writes.length,0,'Opening KTV never opens a room, microphone or song');assert.match(root.textContent,/合成当前曲/);assert.equal(buttons('已点').length,1);assert.ok(!nodes().some(n=>n.id==='content'),'Must not collide with Friday content');
 await click(buttons('暂停')[0]);assert.equal(writes[0].path,'/api/games/ktv/manage/room/command');assert.match(root.textContent,/等待电视回报|等待电视回报|等待电视确认/);assert.ok(!root.textContent.includes('电视已回报本次播放状态'),'ACK is not observed playback');
 await click(byId('queue-tab'));assert.match(root.textContent,/合成下一曲/);const currentCheck=nodes().find(n=>n.tagName==='input'&&n.attrs['aria-label']==='选择 合成当前曲');assert.equal(currentCheck.disabled,true,'Cannot remove current song');
 await click(byId('search-tab'));const search=nodes().find(n=>n.tagName==='input'&&n.type==='search');search.value='合成';const form=nodes().find(n=>n.className==='search-form');await form.onsubmit({preventDefault(){}});await settle();assert.match(root.textContent,/搜到的合成曲/);await click(buttons('点歌')[1]);assert.match(root.textContent,/已点 1 首/);assert.equal(writes.filter(w=>w.path.endsWith('/songs')).length,1);
 await click(byId('cache-tab'));const all=byId('select-page');all.checked=true;all.onchange();assert.match(root.textContent,/已选 1 项/);assert.equal(nodes().filter(n=>n.tagName==='input'&&n.type==='checkbox'&&n.disabled).length,1,'Protected content is not selectable');
 await click(byId('delete-selected'));assert.equal(deleteAttempts,0,'Preview must not delete');assert.equal(byId('delete-dialog').open,true);await click(byId('cancel-delete'));assert.equal(deleteAttempts,0);assert.equal(document.activeElement,byId('delete-selected'),'Cancel restores focus');
 await click(byId('delete-selected'));await click(byId('confirm-delete'));assert.equal(deleteAttempts,1);assert.equal(byId('delete-dialog').open,true);assert.match(byId('delete-error').textContent,/重试剩余删除/);await click(byId('confirm-delete'));assert.equal(deleteAttempts,2);assert.equal(byId('delete-dialog').open,false);assert.ok(writes.filter(w=>w.path.endsWith('/delete')).every(w=>w.data.plan==='SYNTHETIC-plan'),'Partial retry reuses the reviewed deletion plan');
 await click(byId('settings-tab'));assert.match(root.textContent,/容量与自动清理/);await click(buttons('音源授权与下载品质')[0]);assert.match(root.textContent,/音源已连接/);
 await click(byId('room-tab'));lateResolve=()=>{};timer();await settle();assert.ok(lateResolve);owner.dispose();lateResolve();await settle();assert.equal(root.children.length,0);assert.equal(cleared,true,'Dispose cancels polling');assert.ok(writes.every(w=>!w.path.includes('source/login')),'Browsing must not launch authorization');
 console.log('KTV inline lifecycle, search/add, ACK boundary, protected selection, reviewed partial-delete retry and focus: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
