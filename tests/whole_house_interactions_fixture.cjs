const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const all=n=>[n,...n.children.flatMap(all)];
class Element{
 get style(){return this.__style??={};}set style(value){this.__style=value;}
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this._text='';this.style={};this.value='';this.disabled=false;this.hidden=false;this.dataset={};}
 set textContent(v){this._text=v;this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=String(v);}getAttribute(k){return this.attrs[k];}
 removeAttribute(k){delete this.attrs[k];}set src(value){this.setAttribute('src',value);}get src(){return this.getAttribute('src');}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}if(this.tagName==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
 get isConnected(){return true;}getBoundingClientRect(){return {left:0,top:0,width:640,height:360};}focus(){}scrollIntoView(){}contains(n){return all(this).includes(n);}
 querySelector(selector){const attr=selector.match(/^\[([^=\]]+)(?:=["']?([^\]"']+)["']?)?\]$/);return all(this).slice(1).find(n=>attr?(attr[2]===undefined?n.attrs[attr[1]]!==undefined:n.attrs[attr[1]]===attr[2]):selector.startsWith('.')?(n.attrs.class||'').split(' ').includes(selector.slice(1)):n.tagName===selector);}
}
(async()=>{
 const cameras=['cam_1111111111','cam_2222222222','cam_3333333333'].map((id,i)=>({id,kind:'camera',room:['客厅','书房','卧室'][i],name:'合成镜头'}));
 let config={revision:0,enabled:true,bindings:[]},policy={revision:0,enabled:true,recognition:true,interaction:true},voice=true;
 let voicePreferences={revision:0,wake_word:'Friday'};
 const voiceRead='/api/embodied/voice-receipts';
 const heard={camera:cameras[0].id,text:'Friday，打开客厅主灯',state:'unknown_outcome',remaining_ms:30000};
 let voiceReceipts=[heard],holdReceipt=false,delayedReceipt;
 let interactionCameras=[],cameraStates=['live','live','live'];
 const writes=[],reads=[],root=new Element('main'),find=(tag,text)=>all(root).find(n=>n.tagName===tag&&n.textContent===text),byLabel=label=>all(root).find(n=>n.attrs['aria-label']===label);
 const targets=[{id:'lamp',kind:'light',room:'客厅',name:'主灯',controllable:true,actions:['on','off']},{id:'curtain',kind:'cover',room:'客厅',name:'窗帘',controllable:true,actions:['open','close']}];
 const fetch=async(path,options={})=>({ok:true,json:async()=>{
  if(options.method==='POST'){
   const body=JSON.parse(options.body);writes.push({path,body});
   if(path==='/api/embodied/infrastructure'){policy={...body,revision:policy.revision+1};return {policy};}
   if(path==='/api/embodied/voice-input'){voice=body.enabled;if(!voice)voiceReceipts=[];return{};}
   if(path==='/api/embodied/voice-preferences'){assert.equal(body.expected_revision,voicePreferences.revision);voicePreferences={revision:voicePreferences.revision+1,wake_word:body.wake_word};return voicePreferences;}
   if(path==='/api/home/interactions'){config={...body,revision:config.revision+1};return config;}
   if(path.endsWith('/preview')){assert.equal(body.purpose,'pointing');return{preview_token:'e'.repeat(32)};}
   if(path.endsWith('/preview/stop'))return{};
   throw Error('unexpected write '+path);
  }
  reads.push(path);
  if(path==='/api/home/interactions')return{config,gestures:{'hand.ok':'OK','body.arms_open':'双臂打开'}};
  if(path==='/api/embodied/devices')return{devices:targets};
  if(path==='/api/embodied/status')return{owned:true,voice_input:voice,quiet:true,camera_sessions:cameras.map((c,i)=>({camera:c.id,state:cameraStates[i]})),interactions:{events:[],cameras:interactionCameras}};
  if(path===voiceRead){
   assert.ok(options.signal,'voice fetch is cancellable');
   const receipts=structuredClone(voiceReceipts);
   if(holdReceipt)await new Promise(resolve=>{delayedReceipt={resolve,signal:options.signal};});
   return{receipts};
  }
  if(path==='/api/embodied/infrastructure')return{policy};
  if(path==='/api/embodied/voice-preferences')return voicePreferences;
  if(path.endsWith('/power'))return{enabled:true,revision:'e'.repeat(64)};
  if(path.endsWith('/preview')){assert.equal(options.headers['X-Friday-Live-View'],'e'.repeat(32));return{state:'live'};}
  throw Error('unexpected read '+path);
 }});
 const diagram=()=>{const n=new Element('svg');n.dispose=()=>{};return n;};
 let poll,now=0,nextTimeout,visibility;
 const doc={createElement:t=>new Element(t),activeElement:null,hidden:false,
  addEventListener(event,listener){assert.equal(event,'visibilitychange');visibility=listener;},removeEventListener(){visibility=null;}};
 const sandbox={document:doc,fetch,crypto:require('node:crypto').webcrypto,setInterval:callback=>{poll=callback;return 1;},clearInterval(){},
  setTimeout:fn=>{nextTimeout=fn;return 1;},clearTimeout(){nextTimeout=null;},performance:{now:()=>now},
  AbortController,Date,console,mountMotionLibrary:()=>({dispose(){}}),createMotionDiagram:diagram,MOTION_GUIDES:{}};
 const navigations=[];
 const context=vm.createContext(sandbox);context.root=root;context.options={data:{devices:cameras},go(page){navigations.push(page);},toast(){}};
 vm.runInContext(fs.readFileSync('homeplane/static/interactions.js','utf8').replace(/^import.*$/gm,'').replace('export function mountInteractions','function mountInteractions'),context);
 const owner=vm.runInContext('mountInteractions(root,options)',context),settle=()=>new Promise(setImmediate);await settle();
 assert.equal(all(root).filter(n=>n.attrs.class==='interaction-camera').length,3);
 find('button','用文字说').onclick();assert.deepEqual(navigations,['Ask Home']);assert.equal(writes.length,0,'opening text never enables cameras or voice');
 assert.ok(!byLabel('语音使用的镜头'));assert.ok(!all(root).some(n=>n.tagName==='select'),'Voice needs no room or camera selection');assert.equal(writes.length,0);
 assert.match(root.textContent,/执行结果待核对/);
 Object.assign(heard,{state:'accepted',receipt:{status:'accepted',acknowledged:true,reached_target:false}});
 await find('button','刷新结果').onclick();await settle();assert.match(root.textContent,/等待设备回报/);assert(!root.textContent.includes('设备状态已更新'));
 Object.assign(heard,{state:'readback',receipt:{status:'accepted',reached_target:true}});
 await find('button','刷新结果').onclick();await settle();assert(!root.textContent.includes('设备状态已更新'),'ACK does not become a reported match');
 heard.receipt.status='reported_match';await find('button','刷新结果').onclick();await settle();assert.match(root.textContent,/设备状态已更新/);assert.equal(writes.length,0,'outcome refresh never repeats a device command');
 Object.assign(heard,{text:'Friday，客厅温度多少？',state:'answered',route:'query',reply:'客厅温度计的温度24.5度。',receipt:{status:'home_question_read',read_only:true,device_authority:false}});
 await poll();await settle();assert.match(root.textContent,/客厅温度计的温度24.5度/);assert(!root.textContent.includes('设备状态已更新'));assert.equal(writes.length,0,'a read answer never becomes a control acknowledgement');
 const replyState=all(root).find(n=>'data-voice-state'in n.attrs),replyRow=replyState.children[0];await poll();await settle();assert.equal(replyState.children[0],replyRow,'unchanged polling does not replace and reannounce the answer');
 now=30001;nextTimeout();assert(!root.textContent.includes('24.5度'),'the displayed transcript and answer expire between polls');
 holdReceipt=true;await poll();await settle();assert(delayedReceipt);
 doc.hidden=true;visibility();assert(delayedReceipt.signal.aborted);delayedReceipt.resolve();await settle();
 assert(!root.textContent.includes('24.5度'));
 holdReceipt=false;doc.hidden=false;visibility();await settle();assert.match(root.textContent,/24.5度/);
 const voicePanel=all(root).find(n=>n.attrs.class==='voice-interaction');assert.equal(voicePanel.children[0].attrs.class,'voice-response','Recent feedback is the first voice work area');
 const wake=byLabel('唤醒词');wake.value='小福';wake.oninput();await byLabel('语音唤醒词').onsubmit({preventDefault(){}});await settle();assert.equal(voicePreferences.wake_word,'小福');assert(root.textContent.includes('小福，把客厅的灯打开'));assert.equal(writes.length,1,'saving a cue never starts voice or cameras');
 await find('button','暂停语音输入').onclick();await settle();const paused=writes.find(w=>w.path==='/api/embodied/voice-input');assert.equal(paused.body.enabled,false);assert.equal(policy.recognition,true);assert(!root.textContent.includes('24.5度'));
 find('button','手势与动作').onclick();await settle();
 interactionCameras=[
  {camera:cameras[0].id,state:'game',interaction:{reason:'game_active',release_required:true}},
  {camera:cameras[1].id,state:'ready',interaction:{reason:'release_required',release_required:true,ready_bindings:0}},
  {camera:cameras[2].id,state:'ready',interaction:{reason:'ready',release_required:true,ready_bindings:1}},
 ];
 const voiceReads=reads.filter(path=>path===voiceRead).length;
 await poll();await settle();
 assert.equal(reads.filter(path=>path===voiceRead).length,voiceReads,'gesture mode does not read speech receipts');
 let cameraRows=all(root).filter(n=>n.attrs.class==='interaction-camera');
 assert(cameraRows[0].textContent.includes('游戏进行中'));
 assert(cameraRows[1].textContent.includes('先放松，再做一次动作'));
 assert(cameraRows[2].textContent.includes('部分动作需先放松'));
 assert.equal(writes.filter(w=>w.path==='/api/embodied/infrastructure').length,0,'game handoff is not an enable/disable policy write');
 cameraStates[1]='shutter_closed';await poll();await settle();cameraRows=all(root).filter(n=>n.attrs.class==='interaction-camera');
 assert(cameraRows[1].textContent.includes('镜头已关闭'));assert(!cameraRows[1].textContent.includes('先放松'));
 cameraStates[1]='live';interactionCameras[0].state='waiting';await poll();await settle();cameraRows=all(root).filter(n=>n.attrs.class==='interaction-camera');
 assert(!cameraRows[0].textContent.includes('游戏进行中'),'stale interaction metadata cannot keep a game status alive');
 find('button','对镜头说话').onclick();await settle();assert(!root.textContent.includes('先放松'),'voice keeps its independent input status');
 interactionCameras=[];find('button','手势与动作').onclick();await settle();
 find('button','添加设备控制').onclick();const target=byLabel('控制设备');target.value='lamp';target.onchange();
 all(root).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});await settle();assert.equal(config.bindings[0].camera,null);assert.equal(config.bindings[0].scope,'home');
 find('button','指向设备').onclick();const scope=byLabel('使用范围');scope.value=cameras[2].id;scope.onchange();const pointTarget=byLabel('控制设备');pointTarget.value='curtain';pointTarget.onchange();
 await find('button','打开镜头并查看').onclick();await settle();const image=all(root).find(n=>n.tagName==='img');
 assert(image.src.includes('view='+ 'e'.repeat(32)));assert(find('button','停止查看'));image.onload();image.onclick({clientX:320,clientY:180});all(root).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});await settle();
 assert.equal(config.bindings[1].camera,cameras[2].id);assert.equal(config.bindings[1].gesture,'hand.point');assert.deepEqual(config.bindings[1].anchor,[.5,.5]);
 assert.ok(!writes.some(w=>['/api/embodied/start','/api/embodied/stop'].includes(w.path)),'Viewing or saving interactions must not restart perception');
 assert.ok(!reads.some(p=>p.includes('/cameras/')&&p.includes(cameras[0].id)),'Pointing preview stays on its explicitly selected camera');owner.dispose();console.log('All-camera voice, passive browsing, home bindings and scoped pointing preview: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
