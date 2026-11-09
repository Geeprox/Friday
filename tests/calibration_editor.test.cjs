const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('homeplane/embodied/static/index.js','utf8');
const functions=source.slice(source.indexOf('async function editorFailure('),source.indexOf('function toggleAutoSeats('))+
 source.slice(source.indexOf('async function save('),source.indexOf('setInterval('));
const camera=source.slice(source.indexOf('async function cameraMode('),source.indexOf('function options('));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const response=(value,ok=true)=>({ok,json:async()=>structuredClone(value)});
const snapshot=()=>({anchors:[],seats:[],dynamic_seats:true,_source:{worker:'synthetic',generation:1,motion_generation:1,frame:'a'.repeat(32)},
 _reference:{jpeg:Buffer.from('FROZEN FRAME').toString('base64'),meta:{width:640,height:360,room:{registration:{status:'locked'}},camera_control:{mode:'auto'}}}});
function harness(fetch){
 const nodes=new Map(),modes=[],renders=[];
 const make=()=>({disabled:false,textContent:'',style:{},focus(){this.focused=true;}});
 for(const id of ['edit','cameraMode','cameraStatus','status','editor','seatCount','autoSeats','editerror','editopenerror','empty','mapTitle'])nodes.set(id,make());
 const save=make(),cancel=make();save.textContent='保存';
 nodes.get('editor').querySelector=selector=>selector.includes('save()')?save:selector.includes('cancel()')?cancel:null;
 const context=vm.createContext({console,AbortSignal,AbortController,setTimeout,clearTimeout,Blob,Uint8Array,atob,btoa,
  document:{body:{classList:{add(){},remove(){}}}},$:(id)=>nodes.get(id),url:path=>path,
  fetch:async(path,options={})=>{if(path==='/api/camera'){modes.push(JSON.parse(options.body).mode);return response({})}return fetch(path,options)},
  createImageBitmap:async()=>({close(){}}),render:(item)=>renders.push(item),draw(){},options(){},coordinateInput:()=>true});
 vm.runInContext(`let latest={meta:{room:{registration:{status:'locked'}},camera_control:{mode:'auto'}},jpeg:new Uint8Array([1]).buffer},pending=null,frozen=null,draft=null,editing=false,editCameraMode=null,editRequest=null,saving=false;${camera}${functions}
  globalThis.api={edit,save,cancel,state:()=>({latest,pending,frozen,draft,editing,saving}),advance:()=>{latest={meta:{},jpeg:new Uint8Array([9]).buffer}}};`,context);
 return {api:context.api,nodes,modes,renders,save,cancel};
}

test('the editor displays and posts the immutable API frame while live frames advance',async()=>{
 let release,posted;
 const waiting=new Promise(resolve=>release=resolve);
 const h=harness(async(path,options)=>{
  if(options.method==='POST'){posted=JSON.parse(options.body);return response({saved:true})}
  return waiting;
 });
 const editing=h.api.edit();await settle();h.api.advance();release(response(snapshot()));await editing;
 assert.equal(h.api.state().editing,true);
 assert.equal(Buffer.from(h.api.state().frozen.jpeg).toString(),'FROZEN FRAME');
 assert.equal(h.api.state().draft._reference,undefined);
 assert.equal(h.api.state().frozen.meta.camera_control,undefined);
 assert.equal(h.nodes.get('cameraMode').value,'hold');
 assert.equal(h.nodes.get('mapTitle').textContent,'位置图');
 await h.api.save();await settle();
 assert.equal(Buffer.from(posted.reference_jpeg,'base64').toString(),'FROZEN FRAME');
 assert.equal(posted.config._source.frame,'a'.repeat(32));
 assert.equal(posted.config._reference,undefined);
 assert.equal(h.api.state().editing,false);
 assert.equal(h.api.state().latest,null);
 assert.equal(h.nodes.get('mapTitle').textContent,'玩家位置');
 assert.deepEqual(h.modes,['hold','auto']);
});

test('a failed open restores camera mode, controls and focus without opening an empty editor',async()=>{
 const h=harness(async()=>response({detail:'等待画面稳定后再校准'},false));
 await h.api.edit();await settle();
 assert.equal(h.api.state().editing,false);
 assert.equal(h.nodes.get('editor').style.display,'none');
 assert.equal(h.nodes.get('edit').disabled,false);
 assert.equal(h.nodes.get('edit').focused,true);
 h.nodes.get('status').textContent='本地实时 · 0 人';
 assert.match(h.nodes.get('editopenerror').textContent,/等待画面稳定/);
 assert.deepEqual(h.modes,['hold','auto']);
});

test('cancelling an ignored-abort read cannot reopen the editor with a late reply',async()=>{
 let release;
 const waiting=new Promise(resolve=>release=resolve);
 const h=harness(async()=>waiting);
 const editing=h.api.edit();await settle();h.api.cancel();release(response(snapshot()));await editing;await settle();
 assert.equal(h.api.state().editing,false);
 assert.equal(h.renders.length,0);
 assert.deepEqual(h.modes,['hold','auto']);
});

test('a failed save keeps the exact draft and frame available for review',async()=>{
 const h=harness(async(_,options)=>options.method==='POST'?response({detail:'画面已更新，请重新校准'},false):response(snapshot()));
 await h.api.edit();const frozen=h.api.state().frozen;await h.api.save();
 assert.equal(h.api.state().editing,true);
 assert.equal(h.api.state().frozen,frozen);
 assert.equal(h.api.state().saving,false);
 assert.equal(h.save.textContent,'保存');
 assert.equal(h.save.disabled,false);
 assert.match(h.nodes.get('editerror').textContent,/重新校准/);
 h.api.cancel();
});

test('duplicate save and cancel clicks cannot split a pending save',async()=>{
 let release,posts=0;
 const waiting=new Promise(resolve=>release=resolve);
 const h=harness(async(_,options)=>{if(options.method==='POST'){posts++;return waiting}return response(snapshot())});
 await h.api.edit();const save=h.api.save();await settle();await h.api.save();h.api.cancel();
 assert.equal(posts,1);assert.equal(h.api.state().editing,true);assert.equal(h.save.disabled,true);assert.equal(h.cancel.disabled,true);
 release(response({saved:true}));await save;
 assert.equal(h.api.state().editing,false);assert.equal(h.nodes.get('empty').textContent,'正在更新空间');
});
