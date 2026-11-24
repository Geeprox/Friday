const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const all=n=>[n,...n.children.flatMap(all)];
class Element{
 get style(){return this.__style??={};}set style(value){this.__style=value;}
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this._text='';this.style={};this.value='';this.disabled=false;this.hidden=false;this.dataset={};}
 set textContent(v){this._text=v;this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=String(v);}getAttribute(k){return this.attrs[k];}
 removeAttribute(k){delete this.attrs[k];}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}if(this.tagName==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
 get isConnected(){return true;}getBoundingClientRect(){return {left:0,top:0,width:640,height:360};}focus(){}scrollIntoView(){}contains(n){return all(this).includes(n);}
 querySelector(selector){const attr=selector.match(/^\[([^=\]]+)(?:=["']?([^\]"']+)["']?)?\]$/);return all(this).slice(1).find(n=>attr?(attr[2]===undefined?n.attrs[attr[1]]!==undefined:n.attrs[attr[1]]===attr[2]):selector.startsWith('.')?(n.attrs.class||'').split(' ').includes(selector.slice(1)):n.tagName===selector);}
}
(async()=>{
 const cameras=['cam_1111111111','cam_2222222222','cam_3333333333'].map((id,i)=>({id,kind:'camera',room:['客厅','书房','卧室'][i],name:'合成镜头'}));
 let config={revision:0,enabled:true,bindings:[]},policy={revision:0,enabled:true,recognition:true,interaction:true},voice=true;
 let voicePreferences={revision:0,wake_word:'Friday'};
 let voiceSessions=[{camera:cameras[0].id,last_transcript:'Friday，打开客厅主灯'}];
 let interactionCameras=[],cameraStates=['live','live','live'],sessionDetails=[{},{},{}],failStatus=false,statusWait=null;
 const writes=[],reads=[],root=new Element('main'),find=(tag,text)=>all(root).find(n=>n.tagName===tag&&n.textContent===text),byLabel=label=>all(root).find(n=>n.attrs['aria-label']===label);
 const targets=[{id:'lamp',kind:'light',room:'客厅',name:'主灯',controllable:true,actions:['on','off']},{id:'curtain',kind:'cover',room:'客厅',name:'窗帘',controllable:true,actions:['open','close']}];
 const fetch=async(path,options={})=>({ok:true,json:async()=>{
  if(options.method==='POST'){
   const body=JSON.parse(options.body);writes.push({path,body});
   if(path==='/api/embodied/infrastructure'){policy={...body,revision:policy.revision+1};return {policy};}
   if(path==='/api/embodied/voice-input'){voice=body.enabled;return{};}
   if(path==='/api/embodied/voice-preferences'){assert.equal(body.expected_revision,voicePreferences.revision);voicePreferences={revision:voicePreferences.revision+1,wake_word:body.wake_word};return voicePreferences;}
   if(path==='/api/home/interactions'){config={...body,revision:config.revision+1};return config;}
   if(path.endsWith('/preview'))return path.includes(cameras[1].id)?{preview_token:'e'.repeat(32),state:'waiting'}:{state:'live',camera:cameras[2].id,primary:true};
   if(path.endsWith('/preview/stop'))return{};
   throw Error('unexpected write '+path);
  }
  reads.push(path);
  if(path==='/api/home/interactions')return{config,gestures:{'hand.ok':'OK','body.arms_open':'双臂打开'}};
  if(path==='/api/embodied/devices')return{devices:targets};
  if(path==='/api/embodied/status'){if(statusWait)await Promise.race([statusWait,new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(Error('synthetic aborted read')),{once:true}))]);if(failStatus)throw Error('synthetic status unavailable');return{owned:true,voice_input:voice,quiet:true,camera_sessions:cameras.map((c,i)=>({camera:c.id,state:cameraStates[i],...sessionDetails[i]})),voice_sessions:voiceSessions,interactions:{events:[],cameras:interactionCameras}};}
  if(path==='/api/embodied/infrastructure')return{policy};
  if(path==='/api/embodied/voice-preferences')return voicePreferences;
  if(path.endsWith('/power'))return{enabled:true,revision:'e'.repeat(64)};
  if(path.endsWith('/preview'))return{state:'live'};
  throw Error('unexpected read '+path);
 }});
 const diagram=()=>{const n=new Element('svg');n.dispose=()=>{};return n;};
 let poll,timeoutCallback;const sandbox={document:{createElement:t=>new Element(t),activeElement:null},fetch,AbortController,setTimeout:callback=>{timeoutCallback=callback;return 1;},clearTimeout(){timeoutCallback=null;},crypto:require('node:crypto').webcrypto,setInterval:callback=>{poll=callback;return 1;},clearInterval(){},Date,console,mountMotionLibrary:()=>({dispose(){}}),createMotionDiagram:diagram,MOTION_GUIDES:{}};
 const navigations=[];
 const context=vm.createContext(sandbox);context.root=root;context.options={data:{devices:cameras},go(page){navigations.push(page);},toast(){}};
 vm.runInContext(fs.readFileSync('homeplane/static/interactions.js','utf8').replace(/^import.*$/gm,'').replace('export function mountInteractions','function mountInteractions'),context);
 const owner=vm.runInContext('mountInteractions(root,options)',context),settle=()=>new Promise(setImmediate);await settle();
 assert.equal(all(root).filter(n=>n.attrs.class==='interaction-camera').length,3);
 const summary=()=>all(root).find(n=>'data-all-camera-summary'in n.attrs);
 sessionDetails=[{recognition:'running',preview_fresh:true},{recognition:'waiting',preview_fresh:true},{recognition:'waiting',preview_fresh:true}];
 cameraStates=['live','unavailable','stale'];await poll();await settle();
 let diagnosticRows=all(root).filter(n=>n.attrs.class==='interaction-camera');
 assert.equal(summary().textContent,'1 / 3 个镜头可用');
 assert(diagnosticRows[1].textContent.includes('画面正常 · 感知延迟'));
 assert(diagnosticRows[2].textContent.includes('画面正常 · 感知延迟'));
 cameraStates[1]='live';await poll();await settle();assert.equal(summary().textContent,'1 / 3 个镜头可用','a transport-live waiting lane is not ready');
 sessionDetails[1]={recognition:'recovering',preview_fresh:true};cameraStates[1]='connecting';await poll();await settle();assert(root.textContent.includes('感知恢复中'));
 sessionDetails[1]={recognition:'waiting',preview_fresh:false};cameraStates[1]='unavailable';await poll();await settle();assert(root.textContent.includes('连接中断'));
 cameraStates[1]='shutter_closed';sessionDetails[1].preview_fresh=true;await poll();await settle();assert(root.textContent.includes('镜头已关闭'),'privacy takes priority over a mismatched preview flag');
 const focusedRow=all(root).find(n=>n.attrs.class==='interaction-camera');sandbox.document.activeElement=focusedRow.children.find(n=>n.tagName==='button');
 failStatus=true;await poll();await settle();assert.equal(summary().textContent,'状态暂时无法读取');
 assert.equal(all(root).find(n=>n.attrs.class==='interaction-camera'),focusedRow,'polling preserves the focused row while updating its status');
 sandbox.document.activeElement=null;
 assert(!all(root).filter(n=>n.attrs.class==='interaction-camera').some(n=>n.textContent.includes('识别中')),'a failed read retires the old ready rows');
 failStatus=false;cameraStates=['live','live','live'];sessionDetails=[{recognition:'running'},{recognition:'running'},{recognition:'running'}];
 let release;statusWait=new Promise(resolve=>{release=resolve;});const beforeReads=reads.length;
 poll();poll();poll();await settle();assert.equal(reads.slice(beforeReads).filter(p=>p==='/api/embodied/status').length,1,'slow polling keeps one status request');
 release();statusWait=null;await settle();assert.equal(summary().textContent,'3 / 3 个镜头可用');
 statusWait=new Promise(()=>{});poll();await settle();assert.equal(typeof timeoutCallback,'function');timeoutCallback();await settle();
 assert.equal(summary().textContent,'状态暂时无法读取','a hung read has a finite deadline');
 statusWait=null;poll();await settle();assert.equal(summary().textContent,'3 / 3 个镜头可用','the next poll recovers after an aborted read');
 find('button','用文字说').onclick();assert.deepEqual(navigations,['Ask Home']);assert.equal(writes.length,0,'opening text never enables cameras or voice');
 assert.ok(!byLabel('语音使用的镜头'));assert.ok(!all(root).some(n=>n.tagName==='select'),'Voice needs no room or camera selection');assert.equal(writes.length,0);
 assert.match(root.textContent,/这条指令的结果尚未确认/);
 const heard=voiceSessions[0];heard.voice_command={text:heard.last_transcript,state:'accepted',receipt:{status:'accepted',acknowledged:true,reached_target:false}};
 await find('button','刷新结果').onclick();await settle();assert.match(root.textContent,/等待设备回报/);assert(!root.textContent.includes('设备状态已更新'));
 heard.voice_command={text:'上一条不同的指令',state:'readback',receipt:{status:'reported_match',reached_target:true}};
 await find('button','刷新结果').onclick();await settle();assert.match(root.textContent,/结果尚未确认/,'a different command cannot supply this transcript’s result');
 heard.voice_command={text:heard.last_transcript,state:'readback',receipt:{status:'accepted',reached_target:true}};
 await find('button','刷新结果').onclick();await settle();assert(!root.textContent.includes('设备状态已更新'),'ACK does not become a reported match');
 heard.voice_command.receipt.status='reported_match';await find('button','刷新结果').onclick();await settle();assert.match(root.textContent,/设备状态已更新/);assert.equal(writes.length,0,'outcome refresh never repeats a device command');
 heard.last_transcript='Friday，客厅温度多少？';heard.voice_command={text:heard.last_transcript,state:'answered',route:'query',reply:'客厅温度计的温度24.5度。',receipt:{status:'home_question_read',read_only:true,device_authority:false}};
 await poll();await settle();assert.match(root.textContent,/客厅温度计的温度24.5度/);assert(!root.textContent.includes('设备状态已更新'));assert.equal(writes.length,0,'a read answer never becomes a control acknowledgement');
 const replyState=all(root).find(n=>'data-voice-state'in n.attrs),replyRow=replyState.children[0];await poll();await settle();assert.equal(replyState.children[0],replyRow,'unchanged polling does not replace and reannounce the answer');
 const voicePanel=all(root).find(n=>n.attrs.class==='voice-interaction');assert.equal(voicePanel.children[0].attrs.class,'voice-response','Recent feedback is the first voice work area');
 const wake=byLabel('唤醒词');wake.value='小福';wake.oninput();await byLabel('语音唤醒词').onsubmit({preventDefault(){}});await settle();assert.equal(voicePreferences.wake_word,'小福');assert(root.textContent.includes('小福，把客厅的灯打开'));assert.equal(writes.length,1,'saving a cue never starts voice or cameras');
 await find('button','暂停语音输入').onclick();await settle();const paused=writes.find(w=>w.path==='/api/embodied/voice-input');assert.equal(paused.body.enabled,false);assert.equal(policy.recognition,true);
 find('button','手势与动作').onclick();await settle();
 interactionCameras=[
  {camera:cameras[0].id,state:'game',interaction:{reason:'game_active',release_required:true}},
  {camera:cameras[1].id,state:'ready',interaction:{reason:'release_required',release_required:true,ready_bindings:0}},
  {camera:cameras[2].id,state:'ready',interaction:{reason:'ready',release_required:true,ready_bindings:1}},
 ];
 await poll();await settle();
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
 find('button','指向设备').onclick();find('button','添加设备控制').onclick();
 const scope=byLabel('用于标记设备的镜头');assert.equal(scope.value,'');assert(find('button','查看画面').disabled);
 scope.value=cameras[2].id;scope.onchange();const pointTarget=byLabel('控制设备');pointTarget.value='curtain';pointTarget.onchange();await settle();
 await find('button','查看画面').onclick();await settle();const image=all(root).find(n=>n.tagName==='img');
 assert(image.src.includes(cameras[2].id+'/frame'),'运行中的镜头没有预览令牌，仍返回实时画面');
 image.onload();image.onclick({clientX:320,clientY:180});all(root).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});await settle();
 assert.equal(config.bindings[1].camera,cameras[2].id);assert.equal(config.bindings[1].gesture,'hand.point');assert.deepEqual(config.bindings[1].anchor,[.5,.5]);
 assert.ok(!writes.some(w=>['/api/embodied/start','/api/embodied/stop'].includes(w.path)),'Viewing or saving interactions must not restart perception');
 assert(!writes.some(w=>w.path.includes(cameras[2].id+'/preview/stop')),'查看运行中的共享镜头只收起页面画面');
 find('button','添加设备控制').onclick();
 const manual=byLabel('用于标记设备的镜头');manual.value=cameras[1].id;manual.onchange();await settle();
 await find('button','查看画面').onclick();await find('button','停止查看').onclick();
 assert(writes.some(w=>w.path.includes(cameras[1].id+'/preview/stop')&&w.body.token==='e'.repeat(32)),'手动预览只释放自己的令牌');
 assert.ok(!reads.some(p=>p.includes('/cameras/')&&p.includes(cameras[0].id)),'Pointing preview stays on its explicitly selected camera');owner.dispose();console.log('All-camera voice, passive browsing, home bindings and scoped pointing preview: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
