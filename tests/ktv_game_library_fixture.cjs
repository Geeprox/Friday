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
 showModal(){this.open=true;document.activeElement=walk(this).find(n=>n.autofocus)||this;}
 close(){this.open=false;this.handlers.close?.();}
 focus(){document.activeElement=this;}reportValidity(){return true;}scrollIntoView(){}
}
const selectOne=(node,s)=>walk(node).find(n=>s.startsWith('#')?n.id===s.slice(1):s.startsWith('.')?n.className.split(' ').includes(s.slice(1)):s==='details[open]'?n.tagName==='details'&&n.open:false)||null;
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
(async()=>{
 const root=new Element('main'),body=new Element('body');document={createElement:t=>new Element(t),querySelector:()=>null,body,hidden:false,activeElement:body};
 const state={active:true,room_id:'SYNTHETIC-room',simulated:true,defaults:{theme:'content',original:0,vocal_preset:'dry',music_gain:50,mic_gain:40,sfx_gain:80},status:{theme:'content',microphone_available:true,mac_online:true,queue_revision:4,controls:{music_gain:50,mic_gain:40,sfx_gain:80,original:0,mic_muted:1,control_revision:3,vocal_preset:'dry'},playback:{online:true,current:'current',played:[],state:'playing',mic_muted:true,applied_revision:3,safety_latched:true,vocal_preset:'dry',vocal_preset_state:'unavailable',audio_capabilities:{sfx:true}},songs:[{id:'current',song_id:'1',source:'song',title:'合成当前曲',nickname:'朋友',state:'ready'},{id:'next',song_id:'2',source:'song',title:'合成下一曲',nickname:'朋友',state:'ready'}]}};
 const host={available:true,configured:true,bytes:1024**3,policy:{enabled:false,limit_bytes:20*1024**3}};
 const inventory={mac:{...host,last_cleanup:{state:'protected_over_limit'}},nas:{...host},entries:[{id:'song:1',title:'空闲合成曲',last_used:1,mac:{bytes:100,can_delete:true},nas:{bytes:100,can_delete:true}},{id:'song:2',title:'受保护合成曲',last_used:2,mac:{bytes:200,can_delete:false,reason:'in_queue'},nas:{bytes:200,can_delete:false,reason:'in_queue'}}]};
 const unavailableTrack={id:'00000000-0000-4000-8000-000000000001',title:'合成曲库版本',state:'failed',source_preference:'song',code:'source_rights_denied',can_remove:true,can_retry:false,retry_reason:'source_rights_denied',progress:{completed_steps:1}};
 const oldStorageFailure={id:'00000000-0000-4000-8000-000000000002',title:'合成云盘制备失败',state:'failed',source_preference:'cloud',code:'storage_full',can_remove:true,can_retry:true,retry_reason:'',progress:{completed_steps:2}};
 const activeJob={id:'00000000-0000-4000-8000-000000000003',title:'合成正在制备',state:'separating',source_preference:'song',progress:{completed_steps:2}};
 const laterPrepared={...unavailableTrack,id:'00000000-0000-4000-8000-000000000004',title:'合成旧云盘失败',source_preference:'cloud',retry_reason:'failed_history_already_prepared'};
 let jobs=[unavailableTrack,oldStorageFailure,activeJob,laterPrepared];
 let sourceStatus={logged_in:true,state:'logged_in',settings:{level:'standard'}};
 const background={id:'a'.repeat(64),title:'合成背景歌曲',source:'song',status:'ready',ready:true};
 let backgroundAllowance=3,backgroundLimit=100;
 const writes=[],reads=[];let deleteAttempts=0,cleared=false,timer,lateResolve=null;
 let holdLibrary=false,resolveLibrary,failLibrary=false;
 const scope=vm.createContext({document,console,Date,JSON,Set,Map,URL,AbortController,AbortSignal,performance,crypto:require('node:crypto').webcrypto,setTimeout,clearTimeout,setInterval:fn=>{timer=fn;return 17;},clearInterval:id=>{assert.equal(id,17);cleared=true;},fetch:async(path,options={})=>{
  assert.ok(path.startsWith('/api/games/ktv/'),'No cross-origin request or management key');
  assert.equal(options.credentials,'same-origin');assert.ok(!Object.keys(options.headers).some(k=>/key|authorization/i.test(k)));
  if(options.method==='POST'){
   const data=JSON.parse(options.body);writes.push({path,data});
   if(path.endsWith('manage/preview'))return reply({plan:'SYNTHETIC-plan',songs:[{title:'空闲合成曲'}],bytes:{mac:100,nas:100}});
   if(path.endsWith('manage/delete')){deleteAttempts++;return reply(deleteAttempts===1?{complete:false,retryable:true,code:'nas_management_unavailable',results:{mac:{}},nas_stores:{}}:{complete:true});}
   if(path.endsWith('manage/room/command'))return reply({seq:9});
   if(path.endsWith('manage/room/songs'))return reply({accepted:true});
   if(path.endsWith('manage/room/controls')||path.endsWith('manage/room/preferences'))return reply({revision:5});
   if(path.endsWith('source/login')){
    sourceStatus={...sourceStatus,logged_in:false,state:'waiting_official'};
    return reply(structuredClone(sourceStatus));
   }
   if(path.endsWith('source/settings')){
    sourceStatus.settings=data;
    return reply(structuredClone(data));
   }
   if(path.endsWith(`/manage/jobs/${oldStorageFailure.id}/retry`)){
    jobs=jobs.map(job=>job.id===oldStorageFailure.id?{...job,state:'queued',code:'none'}:job);
    return reply({accepted:true});
   }
   if(path.endsWith(`/manage/jobs/${unavailableTrack.id}/remove`)){
    jobs=jobs.filter(job=>job.id!==unavailableTrack.id);
    return reply({removed:true});
   }
   if(path.endsWith(`/manage/backgrounds/${background.id}/regenerate`)){
    backgroundAllowance--;
    background.status='preparing';
    return reply({status:background.status,ready:background.ready});
   }
   if(path.endsWith('manage/backgrounds/policy')){
    backgroundAllowance+=data.daily_images-backgroundLimit;
    backgroundLimit=data.daily_images;
    return reply({daily_limit:backgroundLimit,daily_remaining:backgroundAllowance});
   }
   throw Error('unexpected write '+path);
  }
  reads.push(path);
  if(lateResolve!==null)return new Promise(resolve=>{lateResolve=()=>resolve(reply(structuredClone(state)));});
  if(path.endsWith('manage/room'))return reply(structuredClone(state));
  if(path.endsWith('manage/library')){
   if(holdLibrary)return new Promise(resolve=>{resolveLibrary=resolve;});
   if(failLibrary)return libraryFailure();
   return reply(structuredClone(inventory));
  }
  if(path.includes('manage/search?')){
   const query=new URL(path,'https://fixture.invalid').searchParams;
   const cloud=query.get('source')==='cloud';
   return reply({songs:[{song_id:'7',title:cloud?'合成云盘同名歌曲':'搜到的合成曲',artists:['歌手'],source:cloud?'cloud':'song'}]});
  }
  if(path.endsWith('source/status'))return reply(structuredClone(sourceStatus));
  if(path.endsWith('manage/backgrounds'))return reply({items:[structuredClone(background)],daily_limit:backgroundLimit,daily_remaining:backgroundAllowance});
  if(path.endsWith('scheduler'))return reply({hardware:{cpu:'SYNTHETIC',memory_bytes:8*1024**3},settings:{mode:'auto',download:1,model:1,encode:1},limits:{download:3,model:2,encode:4}});
  if(path.endsWith('jobs'))return reply({jobs:structuredClone(jobs)});
  throw Error('unexpected read '+path);
 }});
 function reply(data){return{ok:true,json:async()=>data};}
 function libraryFailure(){return{ok:false,json:async()=>({detail:'ktv_offline'})};}
 vm.runInContext(fs.readFileSync('integrations/ktv_public/web/party-audio.js','utf8'),scope);
 vm.runInContext(fs.readFileSync('homeplane/static/ktv-console/app.js','utf8').replace('export function mountKtv','function mountKtv'),scope);
 scope.root=root;scope.back=()=>{};const owner=vm.runInContext("mountKtv(root,{endpoint:'/api/games/ktv',embedded:true,onBack:back})",scope);
 const nodes=()=>walk(root),byId=id=>root.querySelector('#ktv-'+id),buttons=text=>nodes().filter(n=>n.tagName==='button'&&n.textContent===text),click=async n=>{assert.ok(n,'button exists');assert.ok(!n.disabled,'button enabled');await n.onclick({currentTarget:n});await settle();};
 await settle();assert.equal(writes.length,0,'Opening KTV never opens a room, microphone or song');assert.match(root.textContent,/合成当前曲/);assert.equal(buttons('已点').length,1);assert.ok(!nodes().some(n=>n.id==='content'),'Must not collide with Friday content');
 const microphoneCopy=/麦克风|人声音效|语音保护|混响/;
 assert.doesNotMatch(root.textContent,microphoneCopy);
 await click(buttons('暂停')[0]);assert.equal(writes[0].path,'/api/games/ktv/manage/room/command');assert.match(root.textContent,/等待电视回报|等待电视回报|等待电视确认/);assert.ok(!root.textContent.includes('电视已回报本次播放状态'),'ACK is not observed playback');
 const mix=nodes().find(n=>n.className==='mix-form');
 mix.elements.music_gain.value='61';
 mix.elements.sfx_gain.value='42';
 mix.onsubmit({preventDefault(){}});
 await settle();
 assert.deepEqual(writes.find(write=>write.path.endsWith('/controls')).data.value,{music_gain:61,sfx_gain:42,original:0});
 await click(buttons('鼓掌')[0]);
 assert.equal(writes.at(-1).data.value.action,'sfx_applause','Atmosphere works on its own capability');
 await click(byId('queue-tab'));assert.match(root.textContent,/合成下一曲/);const currentCheck=nodes().find(n=>n.tagName==='input'&&n.attrs['aria-label']==='选择 合成当前曲');assert.equal(currentCheck.disabled,true,'Cannot remove current song');
 await click(byId('search-tab'));const search=nodes().find(n=>n.tagName==='input'&&n.type==='search');search.value='合成';const form=nodes().find(n=>n.className==='search-form');await form.onsubmit({preventDefault(){}});await settle();assert.match(root.textContent,/搜到的合成曲/);await click(buttons('点歌')[1]);assert.match(root.textContent,/已点 1 首/);assert.equal(writes.filter(w=>w.path.endsWith('/songs')).length,1);
 await click(byId('cache-tab'));
 assert.doesNotMatch(root.textContent,/空闲歌曲已清理|仍超出上限/,'Disabled Mac policy ignores legacy pressure state');
 const all=byId('select-page');all.checked=true;all.onchange();assert.match(root.textContent,/已选 1 项/);assert.equal(nodes().filter(n=>n.tagName==='input'&&n.type==='checkbox'&&n.disabled).length,1,'Protected content is not selectable');
 await click(byId('delete-selected'));assert.equal(deleteAttempts,0,'Preview must not delete');assert.equal(byId('delete-dialog').open,true);await click(byId('cancel-delete'));assert.equal(deleteAttempts,0);assert.equal(document.activeElement,byId('delete-selected'),'Cancel restores focus');
 await click(byId('delete-selected'));await click(byId('confirm-delete'));assert.equal(deleteAttempts,1);assert.equal(byId('delete-dialog').open,true);assert.match(byId('delete-error').textContent,/重试剩余删除/);await click(byId('confirm-delete'));assert.equal(deleteAttempts,2);assert.equal(byId('delete-dialog').open,false);assert.ok(writes.filter(w=>w.path.endsWith('/delete')).every(w=>w.data.plan==='SYNTHETIC-plan'),'Partial retry reuses the reviewed deletion plan');
 const libraryReads=()=>reads.filter(path=>path.endsWith('manage/library')).length;
 const previousLibraryReads=libraryReads();
 holdLibrary=true;
 await click(byId('settings-tab'));
 assert.ok(buttons('保存制作设置')[0],'Scheduler renders before inventory');
 assert.ok(buttons('保存房间默认设置')[0],'Room defaults render before inventory');
 assert.match(root.textContent,/容量与自动清理正在读取/);
 assert.equal(byId('status').textContent,'');
 await click(byId('cache-tab'));
 assert.match(byId('status').textContent,/正在读取歌曲文件/);
 assert.equal(libraryReads(),previousLibraryReads+1,'Cache and settings share one inventory read');
 await click(byId('room-tab'));
 assert.match(root.textContent,/合成当前曲/,'Cache request must not hold the room tab');
 await click(byId('settings-tab'));
 assert.equal(libraryReads(),previousLibraryReads+1,'Switching back cannot start a parallel inventory read');
 const schedulerSave=buttons('保存制作设置')[0];
 holdLibrary=false;
 resolveLibrary(libraryFailure());
 await settle();
 assert.equal(byId('status').textContent,'','An old cache failure cannot replace the active tab status');
 assert.equal(buttons('保存制作设置')[0],schedulerSave,'Inventory failure preserves the scheduler form');
 await click(buttons('重试')[0]);
 assert.equal(buttons('保存容量策略').length,2);
 assert.equal(buttons('保存制作设置')[0],schedulerSave,'Inventory retry only renders its own settings');
 assert.doesNotMatch(root.textContent,microphoneCopy);
 assert.match(root.textContent,/已核对 NAS 归档的本机播放成品/);
 buttons('保存房间默认设置')[0].parent.onsubmit({preventDefault(){}});
 await settle();
 assert.deepEqual(writes.find(write=>write.path.endsWith('/preferences')).data,{theme:'content',original:0,music_gain:50,sfx_gain:80});
 const opener=buttons('音源授权与下载品质')[0],settingsBeforeSource=buttons('保存制作设置')[0];
 const sourceWrites=()=>writes.filter(w=>w.path.includes('/source/'));
 await click(opener);
 assert.equal(byId('source-dialog').open,true);
 assert.match(byId('source-state').textContent,/音源已连接/);
 assert.equal(sourceWrites().length,0,'Opening authorization reads status only');
 assert.equal(buttons('保存制作设置')[0],settingsBeforeSource,'Settings remain mounted under authorization');
 assert.equal(document.activeElement.getAttribute('aria-label'),'关闭音源授权');
 assert.equal(buttons('返回设置').length,0);
 await click(buttons('重新授权')[0]);
 assert.equal(sourceWrites().filter(w=>w.path.endsWith('/login')).length,1);
 assert.match(byId('source-state').textContent,/在网易云官方窗口完成登录/);
 sourceStatus={...sourceStatus,logged_in:true,state:'logged_in'};
 timer();await settle();
 assert.match(byId('source-state').textContent,/音源已连接/);
 const quality=walk(byId('source-dialog')).find(n=>n.tagName==='select');
 quality.value='exhigh';await quality.onchange();await settle();
 assert.deepEqual(sourceWrites().find(w=>w.path.endsWith('/settings')).data,{level:'exhigh'});
 await click(buttons('×')[0]);
 assert.equal(document.activeElement,opener);
 assert.equal(buttons('保存制作设置')[0],settingsBeforeSource);
 const backgroundPanel=nodes().find(n=>n.className.includes('background-settings'));
 assert.ok(!reads.some(path=>path.endsWith('manage/backgrounds')),'The settings page does not eagerly read backgrounds');
 backgroundPanel.open=true;backgroundPanel.ontoggle();await settle();
 assert.match(backgroundPanel.textContent,/今日还可生成 3 张.*合成背景歌曲.*已生成/);
 assert.ok(!writes.some(w=>w.path.includes('/manage/backgrounds')),'Opening backgrounds never generates a new image');
 const regenerate=buttons('重新生成')[0];
 await click(regenerate);
 assert.match(backgroundPanel.textContent,/今日还可生成 2 张.*正在更新背景/);
 assert.equal(regenerate.disabled,true);
 assert.deepEqual(writes.find(w=>w.path.endsWith('/regenerate')).data,{});
 background.status='ready';timer();await settle();
 assert.equal(regenerate.disabled,false);
 assert.equal(buttons('重新生成')[0],regenerate,'Background activity updates the existing card');
 const limitForm=walk(backgroundPanel).find(n=>n.className==='background-limit');
 const limitInput=walk(limitForm).find(n=>n.tagName==='input');
 assert.equal(limitInput.value,'100');
 limitInput.value='120';limitInput.valueAsNumber=120;
 await limitForm.onsubmit({preventDefault(){}});await settle();
 assert.equal(limitInput.value,'120');
 assert.match(backgroundPanel.textContent,/已保存。今日还可生成 22 张/);
 assert.deepEqual(writes.find(w=>w.path.endsWith('/backgrounds/policy')).data,{daily_images:120});
 await click(buttons('查看制作进度')[0]);
 const jobRow=title=>nodes().find(n=>n.tagName==='article'&&n.textContent.includes(title));
 assert.match(jobRow(unavailableTrack.title).textContent,/曲库版本/);
 assert.doesNotMatch(jobRow(unavailableTrack.title).textContent,/账户无权|权限/);
 assert.match(jobRow(oldStorageFailure.title).textContent,/音乐云盘文件.*这次制作时 Mac 可用空间不足/);
 assert.match(jobRow(laterPrepared.title).textContent,/同一版本已准备完成/);
 assert.doesNotMatch(jobRow(laterPrepared.title).textContent,/去音乐云盘搜这首|重试制备/);
 assert.equal(walk(jobRow(unavailableTrack.title)).filter(node=>node.tagName==='progress').length,0);
 assert.equal(walk(jobRow(oldStorageFailure.title)).filter(node=>node.tagName==='progress').length,0);
 assert.equal(walk(jobRow(activeJob.title)).filter(node=>node.tagName==='progress').length,1);
 await click(buttons('重试制备')[0]);
 assert.match(writes.find(w=>w.path.endsWith('/retry')).data.request_id,/^[a-f0-9-]{36}$/);
 await click(buttons('去音乐云盘搜这首')[0]);
 assert.match(root.textContent,/合成云盘同名歌曲/);
 const cloudRead=reads.find(path=>path.includes('manage/search?')&&path.includes('source=cloud'));
 const cloudQuery=new URL(cloudRead,'https://fixture.invalid').searchParams;
 assert.equal(cloudQuery.get('q'),unavailableTrack.title);
 assert.equal(writes.filter(w=>w.path.endsWith('/songs')).length,1,'Searching the cloud never queues a song');
 await click(byId('settings-tab'));
 await click(buttons('查看制作进度')[0]);
 const remove=walk(jobRow(unavailableTrack.title)).find(n=>n.tagName==='button'&&n.textContent==='删除失败记录');
 await click(remove);
 assert.deepEqual(writes.find(w=>w.path.endsWith('/remove')).data,{});
 assert.equal(jobRow(unavailableTrack.title),undefined);
 failLibrary=true;
 await click(byId('cache-tab'));
 assert.equal(byId('status').textContent,'歌曲文件暂时无法读取。');
 failLibrary=false;
 await click(buttons('重试读取歌曲文件')[0]);
 assert.match(root.textContent,/空闲合成曲/);
 assert.equal(byId('status').textContent,'');
 inventory.mac={...host,policy:{...host.policy,enabled:true},hot_cache:{bytes:3*1024**3,limit_bytes:2*1024**3},last_cleanup:{state:'retained_over_target'}};
 await click(byId('refresh'));
 assert.match(root.textContent,/本机播放成品 3\.00 GB/);
 assert.match(root.textContent,/尚未核对 NAS 归档的成品暂时保留/);
 await click(byId('room-tab'));lateResolve=()=>{};timer();await settle();assert.ok(lateResolve);owner.dispose();lateResolve();await settle();assert.equal(root.children.length,0);assert.equal(cleared,true,'Dispose cancels polling');assert.equal(sourceWrites().filter(w=>w.path.endsWith('/login')).length,1,'Only explicit authorization starts login');
 console.log('KTV song controls, modal authorization, failed preparation actions, cloud lookup and backgrounds: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
