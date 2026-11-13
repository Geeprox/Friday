const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/static/werewolf-camera-view.js','utf8').replace('export function mountSeatCamera','function mountSeatCamera');
const settle=()=>new Promise(setImmediate),all=n=>[n,...n.children.flatMap(all)];
const photo=()=>({available:true,camera:'cam_1234567890',session:'SYNTHETIC-camera',ttl_ms:980,width:640,height:360,jpeg:Buffer.from('SYNTHETIC-codec-input').toString('base64'),people:[{track:7,box:{x:.1,y:.2,w:.3,h:.6}}]});
const key=track=>({camera:'cam_1234567890',session:'SYNTHETIC-camera',track});
function harness(){
 class Element{
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.style={};this.attributes={};this.textContent='';}
  append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n);}}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  setAttribute(k,v){this.attributes[k]=String(v);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
  getContext(){return{drawImage:(bitmap)=>draws.push(bitmap)};}
 }
 let now=0,id=0,owned=true,nextPhoto=photo(),handler=null,decode=null,closed=0;
 const draws=[],requests=[],timers=new Map(),events=new Map(),destinations=[],changes=[];
 const document={hidden:false,createElement:tag=>new Element(tag),addEventListener:(k,v)=>events.set(k,v),removeEventListener:k=>events.delete(k)};
 const setTimeout=(fn,delay)=>{const n=++id;timers.set(n,{fn,at:now+delay});return n;},clearTimeout=id=>timers.delete(id);
 const context=vm.createContext({document,console,AbortController,performance:{now:()=>now},setTimeout,clearTimeout,Uint8Array,Blob,
  atob:s=>Buffer.from(s,'base64').toString('binary'),
  createImageBitmap:async blob=>{if(decode)return decode(blob);return{width:nextPhoto.width,height:nextPhoto.height,close(){closed++;}};}});
 vm.runInContext(source,context);
 const section=new Element('section'),heading=new Element('div');section.append(heading);
 const view=context.mountSeatCamera(section,{heading,session:'SYNTHETIC-party',
  api:async(body,signal)=>{requests.push({body,signal});return handler?handler():structuredClone(nextPhoto);},
  isCurrent:()=>owned,onChange:()=>changes.push(view.candidates([key(7)]).map(x=>x.track)),go:value=>destinations.push(value)});
 return{section,view,draws,requests,changes,events,timers,destinations,
  frame:()=>all(section).find(n=>n.className==='ww-seat-camera-frame'),canvas:()=>all(section).find(n=>n.tagName==='CANVAS'),
  labels:()=>all(section).filter(n=>n.dataset.track).map(n=>Number(n.dataset.track)),
  button:text=>all(section).find(n=>n.tagName==='BUTTON'&&n.textContent===text),
  setPhoto:value=>nextPhoto=value,handler:fn=>handler=fn,decode:fn=>decode=fn,closed:()=>closed,
  hidden:flag=>{document.hidden=flag;events.get('visibilitychange')?.();},retire:()=>owned=false,
  async advance(ms){const target=now+ms;for(let i=0;i<100;i++){const due=[...timers].filter(([,t])=>t.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;now=due[1].at;timers.delete(due[0]);due[1].fn();await settle();}now=target;await settle();}
 };
}

test('the source stays idle until explicitly shown, and its numbered view is one coherent sample',async()=>{
 const h=harness();assert.equal(h.requests.length,0);assert.deepEqual([...h.view.candidates([key(8)])].map(x=>x.track),[8]);
 h.button('摄像头设置').onclick();assert.deepEqual(h.destinations,['Cameras']);
 h.button('显示画面').onclick();await settle();
 assert.deepEqual(JSON.parse(JSON.stringify(h.requests[0].body)),{session:'SYNTHETIC-party'});
 assert.equal(h.frame().hidden,false);assert.deepEqual(h.labels(),[7]);assert.equal(h.canvas().width,640);assert.equal(h.canvas().height,360);
 assert.deepEqual([...h.view.candidates([key(8)])].map(x=>x.track),[7],'visible numbers, not a separately polled list, supply the choices');
 assert.equal(h.button('隐藏画面').attributes['aria-pressed'],'true');assert.equal(h.closed(),1);h.view.dispose();
});

test('neither numbers nor options appear before the matching image has decoded',async()=>{
 const h=harness();let release;h.decode(()=>new Promise(resolve=>release=resolve));h.button('显示画面').onclick();await settle();
 assert.equal(h.frame().hidden,true);assert.deepEqual(h.labels(),[]);assert.equal(h.view.candidates([key(7)]).length,0);
 release({width:640,height:360,close(){}});await settle();assert.equal(h.frame().hidden,false);assert.deepEqual(h.labels(),[7]);h.view.dispose();
});

test('a stalled refresh expires its old image and numbers together without overlapping requests',async()=>{
 const h=harness();h.button('显示画面').onclick();await settle();let release;h.handler(()=>new Promise(resolve=>release=resolve));
 await h.advance(1500);assert.equal(h.requests.length,2);assert.equal(h.frame().hidden,true);assert.equal(h.canvas().width,0);assert.deepEqual(h.labels(),[]);assert.equal(h.view.candidates([key(7)]).length,0);
 release(photo());await settle();assert.equal(h.frame().hidden,true,'request transit time exhausts the frame budget');assert.equal(h.draws.length,1);h.view.dispose();
});

test('hiding while image decoding is held retires that picture, and reopening owns only the new one',async()=>{
 const h=harness();let release,closed=0;h.decode(()=>new Promise(resolve=>release=resolve));h.button('显示画面').onclick();await settle();
 h.button('隐藏画面').onclick();h.decode(null);h.setPhoto({...photo(),people:[{track:8,box:{x:.4,y:.1,w:.2,h:.8}}]});h.button('显示画面').onclick();await settle();
 release({width:640,height:360,close(){closed++;}});await settle();
 assert.deepEqual(h.labels(),[8]);assert.equal(h.draws.length,1);assert.equal(closed,1);h.view.dispose();
});

test('page hiding clears the picture and aborts its read; a late response cannot replace the resumed view',async()=>{
 const h=harness();h.button('显示画面').onclick();await settle();let release;h.handler(()=>new Promise(resolve=>release=resolve));await h.advance(400);
 h.hidden(true);assert.equal(h.requests[1].signal.aborted,true);assert.equal(h.frame().hidden,true);assert.deepEqual(h.labels(),[]);
 h.handler(null);h.setPhoto({...photo(),people:[{track:9,box:{x:.2,y:.1,w:.2,h:.8}}]});h.hidden(false);await settle();release(photo());await settle();
 assert.deepEqual(h.labels(),[9]);assert.equal(h.draws.length,2);h.view.dispose();assert.equal(h.events.size,0);assert.equal(h.timers.size,0);
});

test('a codec size mismatch cannot put numbers on the wrong image',async()=>{
 const h=harness();let closed=0;h.decode(async()=>({width:360,height:640,close(){closed++;}}));h.button('显示画面').onclick();await settle();
 assert.equal(h.frame().hidden,true);assert.equal(h.draws.length,0);assert.deepEqual(h.labels(),[]);assert.equal(h.view.candidates([key(7)]).length,0);assert.equal(closed,1);h.view.dispose();
});

test('an unavailable source and a changed source each clear the original correspondence',async()=>{
 const h=harness();h.button('显示画面').onclick();await settle();h.setPhoto({available:false});await h.advance(400);
 assert.equal(h.frame().hidden,true);assert.deepEqual(h.labels(),[]);
 h.setPhoto(photo());await h.advance(400);assert.equal(h.frame().hidden,false);
 assert.equal(h.view.candidates([{...key(7),session:'SYNTHETIC-replacement'}]).length,0);assert.equal(h.frame().hidden,true);h.view.dispose();
});

test('体感游戏占用时撤下画面，释放后在原位置恢复人物编号',async()=>{
 const h=harness();h.button('显示画面').onclick();await settle();
 h.setPhoto({available:false,reason:'motion_game_camera_busy'});await h.advance(400);
 assert.equal(h.frame().hidden,true);assert.deepEqual(h.labels(),[]);
 assert(all(h.section).some(node=>node.textContent==='体感游戏正在使用镜头，结束后可对应座位。'));
 h.setPhoto(photo());await h.advance(400);
 assert.equal(h.frame().hidden,false);assert.deepEqual(h.labels(),[7]);h.view.dispose();
});

test('disposing the host view prevents a held response from decoding or drawing',async()=>{
 const h=harness();let release;h.handler(()=>new Promise(resolve=>release=resolve));h.button('显示画面').onclick();await settle();
 h.view.dispose();assert.equal(h.requests[0].signal.aborted,true);release(photo());await settle();
 assert.equal(h.draws.length,0);assert.equal(h.events.size,0);assert.equal(h.timers.size,0);
});
