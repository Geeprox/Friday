// Execute the actual camera panel; synthetic DOM and original request boundaries only.
const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/static/home-experience.js','utf8');
const cameraPanel=source.slice(source.indexOf(' function cameraPanel('),source.indexOf('\n function inspect('));
const stopPreview=source.slice(source.indexOf(' function stopPreview('),source.indexOf('\n function cameraPanel('));
const all=n=>[n,...n.children.flatMap(all)],settle=()=>new Promise(setImmediate);

function panel({supported=true,recognition=true,powerEnabled=true,initialMode='auto'}={}){
 class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.textContent='';this.isConnected=true;this.hidden=false;this.classList={toggle(){}};}
  append(...nodes){this.children.push(...nodes);}setAttribute(k,v){this.attrs[k]=String(v);}removeAttribute(k){delete this.attrs[k];}
  get lastElementChild(){return this.children.at(-1);}addEventListener(){}
 }
 const h=(tag,text,attrs={})=>{const n=new Element(tag);n.textContent=text||'';for(const[k,v]of Object.entries(attrs)){n.setAttribute(k,v);if(k==='class')n.className=v;if(k==='hidden')n.hidden=true;}return n;};
 const add=(n,...children)=>{n.append(...children);return n;};
 const slot=new Element('section'),requests=[],pending={},routes=[];let mode=initialMode,phase=mode==='survey'?'patrolling':'following',tick,deferRead=false,deferControl=false,deferPower=false;
 let state='live',revision=0,result=null,confirmed=mode==='survey'?4:0,oldReport=null,failRead=false;
 const report=()=>({state,primary:true,recognition_enabled:recognition,camera_control:{owner:'friday',mode,phase,
  intent_revision:String(revision),survey_result:result,survey_confirmed_moves:confirmed,survey_total_steps:10,
  reason:mode==='pets'?'跟随宠物':mode==='hold'?'停留当前视角':mode==='survey'?(result==='complete'?'本轮巡视完成':result?'本轮巡视未完成':'巡视低处'):'跟随人物'},pet_tracking:{supported}});
 function apply(next){mode=next;revision++;phase=next==='survey'?'patrolling':next==='hold'?'held':'following';result=null;confirmed=0;return{mode,intent_revision:String(revision)};}
 const context=vm.createContext({console,alive:true,previewStop:null,slot,
  device:{id:'cam_1111111111',room:'合成房间',name:'合成镜头'},h,add,ico:()=>h('i'),go:(...args)=>routes.push(args),Date,
  crypto:{randomUUID:()=> 'synthetic-event'},
  btn:(text,fn,attrs)=>{const n=h('button',text,attrs);n.onclick=fn;return n;},
  setInterval:fn=>{tick=fn;return 1;},clearInterval:()=>{tick=null;},
  api:async(path,body)=>{requests.push({path,body});if(path.endsWith('/power')){
   const report=()=>({enabled:powerEnabled,revision:'e'.repeat(64),status:'reported_match'});
   if(!body)return report();
   powerEnabled=body.enabled;
   if(deferPower)return new Promise(resolve=>pending.power=()=>resolve(report()));
   return report();
  }
   if(path.endsWith('/preview/stop'))return{state:'stopped'};
   if(path.endsWith('/control')){if(deferControl){deferControl=false;return new Promise(resolve=>pending.control=()=>resolve(apply(body.mode)));}return apply(body.mode);}
   if(path.endsWith('/preview')){if(body)return{preview_token:'synthetic-preview',state:'live'};if(failRead){failRead=false;throw Error('synthetic_disconnect');}const value=oldReport||report();oldReport=null;if(deferRead){deferRead=false;return new Promise(resolve=>pending.read=()=>resolve(value));}return value;}
   throw Error('unexpected camera request');}});
 vm.runInContext(stopPreview+cameraPanel+'\ncameraPanel(device,slot);',context);
 const button=label=>all(slot).find(n=>n.tagName==='button'&&(n.textContent===label||n.attrs['aria-label']===label));
 return{slot,requests,pending,button,routes,close:()=>context.stopPreview(),deferPower:()=>deferPower=true,status:()=>all(slot).find(n=>n.className==='camera-status').textContent,
  survey:()=>all(slot).find(n=>n.className==='camera-survey-readback').children[0].textContent,
  surveyVisible:()=>!all(slot).find(n=>n.className==='camera-survey').hidden,
  progress:()=>all(slot).find(n=>n.tagName==='progress'),
  image:()=>all(slot).find(n=>n.tagName==='img'),tick:()=>tick(),deferRead:()=>deferRead=true,deferControl:()=>deferControl=true,
  delayModeRead:()=>oldReport=report(),failRead:()=>failRead=true,setState:value=>state=value,
  finishSurvey:outcome=>{phase='survey_finished';result=outcome;confirmed=outcome==='complete'?10:4;},
  async click(label){const b=button(label);assert(b&&!b.disabled,'Enabled button: '+label);await b.onclick();await settle();}};
}

test('follow buttons read back the selected subject and stop viewing disables every camera command',async()=>{
 const c=panel();await settle();await c.click('查看实时画面');
 assert.equal(c.button('跟随人物').attrs['aria-pressed'],'true');
 await c.click('跟随宠物');assert.equal(c.button('跟随宠物').attrs['aria-pressed'],'true');assert.equal(c.status(),'跟随宠物');
 assert(c.requests.some(r=>r.path.endsWith('/control')&&r.body.mode==='pets'));
 await c.click('停止查看');assert.equal(c.image().hidden,true);assert.equal(c.status(),'已停止查看');
 for(const text of ['向左','向上','向下','向右','跟随人物','跟随宠物','巡视一轮'])assert.equal(c.button(text).disabled,true);
 assert.equal(c.button('跟随宠物').attrs['aria-pressed'],'false');
});

test('late preview read cannot put a frame or enabled controls back after stop',async()=>{
 const c=panel();await settle();await c.click('查看实时画面');c.deferRead();const read=c.tick();await settle();
 await c.click('停止查看');c.pending.read();await read;
 assert.equal(c.image().hidden,true);assert.equal(c.status(),'已停止查看');assert.equal(c.button('跟随宠物').disabled,true);
 assert.equal(c.requests.filter(r=>r.path.endsWith('/preview/stop')).length,1);
});

test('late follow reply cannot overwrite the stopped view',async()=>{
 const c=panel();await settle();await c.click('查看实时画面');c.deferControl();const choice=c.click('跟随宠物');await settle();
 await c.click('停止查看');c.pending.control();await choice;
 assert.equal(c.status(),'已停止查看');assert.equal(c.image().hidden,true);assert.equal(c.button('跟随宠物').disabled,true);
});

test('an old or unavailable native pet path offers only the working human follow control',async()=>{
 const c=panel({supported:false});await settle();await c.click('查看实时画面');
 assert.equal(c.button('跟随人物').disabled,false);assert.equal(c.button('跟随宠物').disabled,true);
 assert.match(c.button('跟随宠物').title,/暂不支持/);
 assert.equal(c.requests.filter(r=>r.path.endsWith('/control')).length,0);
});

test('the camera panel starts a survey and can stop it while the picture is unavailable',async()=>{
 const c=panel();await settle();await c.click('查看实时画面');
 const action=c.button('巡视一轮');c.delayModeRead();
 await c.click('巡视一轮');assert.equal(c.button('停止巡视'),action);
 assert.equal(c.survey(),'准备巡视','旧意图读回不能覆盖已接收的巡视请求');
 await c.tick();assert.equal(c.survey(),'巡视低处');
 c.setState('connecting');await c.tick();assert.equal(c.image().hidden,true);
 assert.equal(c.button('停止巡视').disabled,false);
 c.failRead();await c.tick();assert.equal(c.button('停止巡视').disabled,false);
 assert.equal(c.survey(),'巡视状态待更新');
 await c.click('停止巡视');assert.equal(c.button('巡视一轮'),action);
 assert.equal(c.status(),'停留当前视角');
 const modes=c.requests.filter(r=>r.path.endsWith('/control')).map(r=>r.body.mode);
 assert.deepEqual(modes,['survey','hold']);
});

for(const outcome of ['complete','interrupted'])test(`a ${outcome} survey returns the same control to ready`,async()=>{
 const c=panel();await settle();await c.click('查看实时画面');await c.click('巡视一轮');
 c.finishSurvey(outcome);await c.tick();assert.equal(c.button('巡视一轮').disabled,false);
 assert.equal(c.survey(),outcome==='complete'?'本轮巡视完成':'本轮巡视未完成');
 assert.equal(c.progress().value,outcome==='complete'?10:4);
});

test('详情可以在未打开预览时读到巡视进度并停止该轮',async()=>{
 const c=panel({initialMode:'survey'});await settle();
 assert.equal(c.image().hidden,true);assert.equal(c.surveyVisible(),true);
 assert.equal(c.progress().value,4);assert.equal(c.progress().max,10);
 assert.equal(c.progress().attrs['aria-valuetext'],'4 / 10 个视角已确认');
 await c.click('查看实时画面');await c.click('停止查看');
 assert.equal(c.survey(),'巡视低处');assert.equal(c.image().hidden,true);
 const before=c.requests.length;
 await c.click('停止巡视');
 const writes=c.requests.slice(before).filter(row=>row.body);
 assert.equal(writes.length,1);
 assert.match(writes[0].path,/\/control$/);
 assert.equal(writes[0].body.survey_revision,'0');
 assert.equal(writes[0].body.mode,'hold');
 assert.equal(c.surveyVisible(),false);
 assert.equal(c.status(),'巡视已停止');
});

test('manual-only preview cannot start a recognition survey',async()=>{
 const c=panel({recognition:false});await settle();await c.click('查看实时画面');
 assert.equal(c.button('巡视一轮').disabled,true);
 assert.equal(c.surveyVisible(),false);
 assert(!c.requests.some(r=>r.path.endsWith('/control')));
});

test('the same view control stops preview without changing power and opens this camera in recordings',async()=>{
 const c=panel();await settle();
 const watch=c.button('查看实时画面');
 await c.click('查看实时画面');assert.equal(c.button('停止查看'),watch);
 await c.click('停止查看');assert.equal(c.button('查看实时画面'),watch);
 assert.equal(c.requests.filter(r=>r.path.endsWith('/power')&&r.body).length,0);
 await c.click('回看录像');
 assert.equal(c.routes[0][0],'Activity');assert.equal(c.routes[0][1].recordingCamera,'cam_1111111111');
});

test('closing a detail during explicit wake cannot open a retired preview after the power reply',async()=>{
 const c=panel({powerEnabled:false});await settle();c.deferPower();
 const opening=c.click('打开镜头并查看');await settle();
 c.close();c.pending.power();await opening;
 assert.equal(c.requests.filter(r=>r.path.endsWith('/preview')&&r.body).length,0);
 assert.equal(c.image().hidden,true);
});
