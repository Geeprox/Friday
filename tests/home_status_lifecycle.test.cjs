// 实际首页状态组件的后台初次加载、恢复和退出；只用合成 API 与确定时钟。
const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const {fixture,settle}=require('./scheduled_automation_fixture.cjs');
function mount({hidden=false,read=null}={}){
 const f=fixture();f.owner.dispose();const context=f.context,document=context.document,calls=[],timers=new Map(),intervals=new Map();let sequence=0,cpu=14;
 document.hidden=hidden;
 Object.assign(context,{AbortController,
  setTimeout(fn,delay){const id=++sequence;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
  setInterval(fn){const id=++sequence;intervals.set(id,fn);return id;},clearInterval(id){intervals.delete(id);}});
 context.homeApi=async(path,body,options)=>{
  assert.equal(body,undefined);assert(options.signal instanceof AbortSignal);calls.push(path);
  if(read)await read(path,options.signal);
  if(path==='/api/home/resources')return {available:true,cpu_percent:cpu,memory_used_bytes:1073741824,memory_total_bytes:8589934592,sampled_at:new Date().toISOString(),valid_for_seconds:15};
  if(path==='/api/embodied/status')return {configured:false};
  if(path==='/api/rules/core/runtime')return {available:true,automatic_enabled:true,execution_state:'ready'};
  if(path==='/api/settings/arl/status')return null;
  return {events:[],proposals:[]};
 };
 context.homeRoot=f.root;
 const source=fs.readFileSync('homeplane/static/home-status.js','utf8').replace(/^import .*;$/gm,'').replace(/export /g,'');
 vm.runInContext('globalThis.homeOwner=(function(){'+source+';return mountHomeStatus(homeRoot,{api:homeApi,go(){}});})()',context);
 return {root:f.root,calls,timers,intervals,owner:context.homeOwner,
  show(value){document.hidden=!value;document.dispatchEvent(new Event('visibilitychange'));},cpu(value){cpu=value;},
  expire(){const [id,item]=[...timers].find(([,row])=>row.delay===16000);timers.delete(id);item.fn();},
  poll(){for(const fn of intervals.values())fn();}};
}

test('a background-created homepage reads once and returning to it refreshes immediately',async()=>{
 const f=mount({hidden:true});await settle();
 try{
  assert.equal(f.calls.length,7);assert.match(f.root.textContent,/14%/);assert.doesNotMatch(f.root.textContent,/正在更新待处理/);
  f.poll();await settle();assert.equal(f.calls.length,7);
  f.expire();assert.doesNotMatch(f.root.textContent,/14%|正在更新待处理/);assert.match(f.root.textContent,/提醒暂未更新/);
  f.cpu(31);f.show(true);await settle();assert.equal(f.calls.length,14);assert.match(f.root.textContent,/31%/);
 }finally{f.owner.dispose();}
 assert.equal(f.timers.size,0);assert.equal(f.intervals.size,0);f.show(true);await settle();assert.equal(f.calls.length,14);
});

test('manual refresh remains available while background polling stays suspended',async()=>{
 const f=mount({hidden:true});await settle();
 try{f.cpu(22);await f.owner.refresh();assert.equal(f.calls.length,14);assert.match(f.root.textContent,/22%/);}
 finally{f.owner.dispose();}
});

test('leaving during a status read aborts the old owner and late data cannot revive it',async()=>{
 let release,aborted=false;
 const f=mount({read:async(path,signal)=>{if(path==='/api/home/resources')await new Promise(resolve=>{release=resolve;signal.addEventListener('abort',()=>{aborted=true;resolve();},{once:true});});}});
 assert.equal(f.calls.length,7);f.owner.dispose();release();await settle();
 assert(aborted);assert.equal(f.root.children.length,0);assert.equal(f.timers.size,0);assert.equal(f.intervals.size,0);
 f.show(true);await settle();assert.equal(f.calls.length,7);
});
