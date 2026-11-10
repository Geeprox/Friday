// Exercise the product's actual overview loader with delayed synthetic transport.
// No camera, device, model, browser or household service is started.
const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/static/home-experience.js','utf8');
const loader=source.slice(source.indexOf(' async function load('),source.indexOf('\n async function control('));
const ownerStart=source.lastIndexOf('\n return{');assert(ownerStart>=0);
const ownerSource=source.slice(ownerStart,source.lastIndexOf('\n}'));
const bootSource=source.slice(source.indexOf(' void load(true);'),ownerStart);
const settle=()=>new Promise(setImmediate);
function fixture(){
 const reads=[],events=[],toasts=[],draws=[],updates=[],attrs=new Map();let tick;
 const view={children:[],setAttribute:(k,v)=>attrs.set(k,v),removeAttribute:k=>attrs.delete(k),replaceChildren(...nodes){this.children=nodes;}};
 const h=(tag,text,attrs={})=>({tag,text,attrs,children:[],append(node){this.children.push(node);}});
 const context=vm.createContext({alive:true,page:'Home',loadVersion:0,loadAbort:null,hasDrawn:false,data:null,AbortController,Map,
  requestedPositions:new Map(),view,h,add:(node,...children)=>{node.children.push(...children);return node;},
  homeStatus:null,connectionSetup:null,timer:null,setInterval(fn){tick=fn;return 1;},clearInterval(){tick=null;},stopPreview(){},recordingsView:null,cameraPrivacy:null,cameraConnection:null,recognitionDebug:null,automationView:null,model:null,thumbnails:null,interactions:null,
  ico:name=>h('i',name),btn:(text,run)=>({...h('button',text),run}),go(){},
  document:{dispatchEvent:event=>events.push(event)},CustomEvent:class{constructor(type,opts){this.type=type;Object.assign(this,opts);}},
  draw(){draws.push(context.data);view.replaceChildren(h('section','家庭空间'));},update(){updates.push(context.data);},toast:(...args)=>toasts.push(args),
  api(path,body,options={}){
   if(path==='/api/home/layout')return Promise.resolve({rooms:[]});
   assert.equal(path,'/api/home/overview');
   // Deliberately allow an already-delivered response after abort: generation
   // fencing must protect UI state even when cancellation arrives too late.
   return new Promise((resolve,reject)=>reads.push({resolve,reject,signal:options.signal}));
  }});
 vm.runInContext(loader,context);
 const owner=vm.runInContext('(function(){'+ownerSource+'})()',context);
 const overview=(value,time)=>({devices:[{controls:[{id:'lamp',value,attributes:{}}]}],generated_at:time,connected:true,simulated:true,control_count:1});
 return{context,reads,events,toasts,draws,updates,view,attrs,overview,load:initial=>context.load(initial),
  startPolling(){vm.runInContext(bootSource,context);},tick(){tick();},
  async ready(){const job=this.load(true);reads.at(-1).resolve(overview('on',1));await job;},
  dispose(){owner.dispose();}};
}

test('an older overview cannot rewind a newer control readback or its status event',async()=>{
 const f=fixture();await f.ready();
 const old=f.load(),oldRead=f.reads.at(-1),fresh=f.load(),freshRead=f.reads.at(-1);
 assert.equal(oldRead.signal.aborted,true);
 freshRead.resolve(f.overview('off',3));await fresh;
 oldRead.resolve(f.overview('on',2));await old;
 assert.equal(f.context.data.devices[0].controls[0].value,'off');
 assert.deepEqual(f.events.map(e=>e.detail.generated_at),[1,3]);
 assert.equal(f.updates.length,1);assert.equal(f.toasts.length,0);
});

test('automatic polling does not repeatedly cancel a slow first read',async()=>{
 const f=fixture();f.startPolling();const initial=f.reads.at(-1);
 f.tick();f.tick();f.tick();assert.equal(f.reads.length,1);assert.equal(initial.signal.aborted,false);
 initial.resolve(f.overview('on',1));await settle();assert.equal(f.draws.length,1);
 f.tick();assert.equal(f.reads.length,2);f.reads.at(-1).resolve(f.overview('off',2));await settle();
 assert.equal(f.context.data.generated_at,2);f.dispose();
});

test('a refresh overtaking the first read still draws the page and clears loading',async()=>{
 const f=fixture(),first=f.load(true),firstRead=f.reads.at(-1);
 assert.equal(f.view.children[0].attrs.role,'status');assert.equal(f.attrs.get('aria-busy'),'true');
 const fresh=f.load();f.reads.at(-1).resolve(f.overview('off',2));await fresh;
 assert.equal(f.draws.length,1);assert.equal(f.view.children[0].text,'家庭空间');assert.equal(f.attrs.has('aria-busy'),false);
 firstRead.resolve(f.overview('on',1));await first;
 assert.equal(f.draws.length,1);assert.equal(f.context.data.generated_at,2);
});

test('stale and disposed failures cannot replace the page or announce a false outage',async()=>{
 const f=fixture();await f.ready();const old=f.load(),oldRead=f.reads.at(-1),fresh=f.load();
 f.reads.at(-1).resolve(f.overview('off',3));await fresh;oldRead.reject(Error('late failure'));await old;
 assert.equal(f.toasts.length,0);assert.equal(f.view.children[0].text,'家庭空间');
 const pending=f.load(),pendingRead=f.reads.at(-1);f.dispose();assert.equal(pendingRead.signal.aborted,true);
 pendingRead.reject(Error('navigation finished'));await pending;
 assert.equal(f.toasts.length,0);assert.equal(f.view.children.length,0);assert.deepEqual(f.events.map(e=>e.detail.generated_at),[1,3]);
});

test('an initial outage has a usable retry and a failed refresh retains the last state',async()=>{
 const f=fixture(),first=f.load(true);f.reads.at(-1).reject(Error('offline'));await first;
 assert.equal(f.view.children[0].children.find(n=>n.tag==='h2').text,'家庭连接暂不可用');
 const retry=f.view.children[0].children.find(n=>n.tag==='button'&&n.text==='重试').run();
 assert.equal(f.view.children[0].attrs.role,'status');f.reads.at(-1).resolve(f.overview('off',2));await retry;
 assert.equal(f.draws.length,1);assert.equal(f.attrs.has('aria-busy'),false);
 const refresh=f.load();f.reads.at(-1).reject(Error('offline again'));await refresh;await settle();
 assert.equal(f.context.data.generated_at,2);assert.equal(f.view.children[0].text,'家庭空间');
 assert.equal(f.toasts.length,1);assert.equal(f.toasts[0][0],'家庭状态同步暂时中断');
});
