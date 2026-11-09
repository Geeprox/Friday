// Execute the actual camera panel; synthetic DOM and original request boundaries only.
const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/static/home-experience.js','utf8');
const cameraPanel=source.slice(source.indexOf(' function cameraPanel('),source.indexOf('\n function inspect('));
const stopPreview=source.slice(source.indexOf(' function stopPreview('),source.indexOf('\n function cameraPanel('));
const all=n=>[n,...n.children.flatMap(all)],settle=()=>new Promise(setImmediate);

function panel({supported=true}={}){
 class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.textContent='';this.isConnected=true;this.hidden=false;}
  append(...nodes){this.children.push(...nodes);}setAttribute(k,v){this.attrs[k]=String(v);}
  get lastElementChild(){return this.children.at(-1);}addEventListener(){}
 }
 const h=(tag,text,attrs={})=>{const n=new Element(tag);n.textContent=text||'';for(const[k,v]of Object.entries(attrs)){n.setAttribute(k,v);if(k==='class')n.className=v;if(k==='hidden')n.hidden=true;}return n;};
 const add=(n,...children)=>{n.append(...children);return n;};
 const slot=new Element('section'),requests=[],pending={};let mode='auto',tick,deferRead=false,deferControl=false;
 const report=()=>({state:'live',primary:true,camera_control:{mode,reason:mode==='pets'?'跟随宠物':'跟随人物'},pet_tracking:{supported}});
 const context=vm.createContext({console,alive:true,previewTimer:null,previewStop:null,slot,
  device:{id:'cam_1111111111',room:'合成房间'},h,add,ico:()=>h('i'),go(){},Date,
  btn:(text,fn,attrs)=>{const n=h('button',text,attrs);n.onclick=fn;return n;},
  setInterval:fn=>{tick=fn;return 1;},clearInterval:()=>{tick=null;},
  api:async(path,body)=>{requests.push({path,body});if(path.endsWith('/power'))return{enabled:true,revision:'e'.repeat(64)};
   if(path.endsWith('/preview/stop'))return{state:'stopped'};
   if(path.endsWith('/control')){if(deferControl){deferControl=false;return new Promise(resolve=>pending.control=()=>{mode=body.mode;resolve({mode});});}mode=body.mode;return{mode};}
   if(path.endsWith('/preview')){if(body)return{preview_token:'synthetic-preview',state:'live'};if(deferRead){deferRead=false;return new Promise(resolve=>pending.read=()=>resolve(report()));}return report();}
   throw Error('unexpected camera request');}});
 vm.runInContext(stopPreview+cameraPanel+'\ncameraPanel(device,slot);',context);
 const button=label=>all(slot).find(n=>n.tagName==='button'&&n.textContent===label);
 return{slot,requests,pending,button,status:()=>all(slot).find(n=>n.className==='camera-status').textContent,
  image:()=>all(slot).find(n=>n.tagName==='img'),tick:()=>tick(),deferRead:()=>deferRead=true,deferControl:()=>deferControl=true,
  async click(label){const b=button(label);assert(b&&!b.disabled,'Enabled button: '+label);await b.onclick();await settle();}};
}

test('follow buttons read back the selected subject and stop viewing disables every camera command',async()=>{
 const c=panel();await settle();await c.click('查看实时画面');
 assert.equal(c.button('跟随人物').attrs['aria-pressed'],'true');
 await c.click('跟随宠物');assert.equal(c.button('跟随宠物').attrs['aria-pressed'],'true');assert.equal(c.status(),'跟随宠物');
 assert(c.requests.some(r=>r.path.endsWith('/control')&&r.body.mode==='pets'));
 await c.click('停止查看');assert.equal(c.image().hidden,true);assert.equal(c.status(),'已停止查看');
 for(const text of ['向左','向上','向下','向右','跟随人物','跟随宠物'])assert.equal(c.button(text).disabled,true);
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
