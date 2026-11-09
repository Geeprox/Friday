const test=require('node:test'),assert=require('node:assert/strict');
const Feed=require('../integrations/ktv_public/web/status-feed.js');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function fixture(options={}){
 const tasks=new Map();let time=0,next=0;const statuses=[],errors=[];
 const f={statuses,errors,options,clock:()=>time,setTimer(fn,delay){const id=++next;tasks.set(id,{at:time+delay,fn});return id;},clearTimer:id=>tasks.delete(id)};
 f.feed=new Feed({events:options.events||(()=>new Promise(()=>{})),probe:options.probe||(()=>Promise.resolve()),onStatus:v=>statuses.push(v),onFailure:e=>errors.push(e),visible:options.visible||(()=>true),setTimer:f.setTimer,clearTimer:f.clearTimer,now:()=>time,probeEvery:options.probeEvery||2500});
 f.advance=async ms=>{const target=time+ms;let loops=0;for(;;){await flush();const rows=[...tasks].filter(([,v])=>v.at<=target).sort((a,b)=>a[1].at-b[1].at);if(!rows.length)break;if(++loops>500)throw Error('unbounded timer loop');const [id,value]=rows[0];tasks.delete(id);time=value.at;value.fn();}time=target;await flush();};return f;
}
test('events keep exactly one connection, cursor resumes, and stopped late responses cannot publish',async()=>{
 const requests=[];const f=fixture({events:options=>{const d=deferred();requests.push({...options,d});return d.promise;}});f.feed.start();f.feed.start();assert.equal(requests.length,1);
 requests[0].d.resolve({cursor:3,status:{server_time_ms:1000}});await flush();assert.equal(f.statuses.length,1);await f.advance(100);assert.equal(requests[1].cursor,3);
 f.feed.stop();assert.equal(requests[1].signal.aborted,true);f.feed.start();await f.advance(50);assert.equal(requests.length,2,'aborted connection settles before another begins');
 requests[1].d.resolve({cursor:999,status:{server_time_ms:9999}});await flush();await f.advance(50);assert.equal(requests.length,3);assert.equal(f.statuses.length,1);assert.equal(requests[2].cursor,3);f.feed.close();requests[2].d.resolve({cursor:4,status:{server_time_ms:1001}});await flush();
});
test('404 from an older service falls back to bounded ordinary probes without retrying events',async()=>{
 let events=0,probes=0;const f=fixture({events:async()=>{events++;throw {status:404,code:'not_found'};},probe:async()=>{probes++;}});f.feed.start();await f.advance(0);assert.equal(events,1);assert.equal(probes,1);await f.advance(2000);assert.equal(probes,3);assert.equal(events,1);assert.equal(f.errors.length,0);f.feed.close();
});
test('subscription capacity falls back to ordinary status without showing an operation failure',async()=>{
 let events=0,probes=0;const f=fixture({events:async()=>{events++;throw {status:429,code:'events_capacity'};},probe:async()=>{probes++;}});
 f.feed.start();await f.advance(2000);assert.equal(events,1);assert.equal(probes,3);assert.equal(f.errors.length,0);f.feed.close();
});
test('network failures use finite exponential backoff and room closure is terminal rather than legacy fallback',async()=>{
 const at=[];let f;f=fixture({events:async()=>{at.push(f.clock());throw {code:'network_error'};}});f.feed.start();await f.advance(7750);assert.deepEqual(at,[0,250,750,1750,3750,7750]);assert.equal(f.errors.length,6);f.feed.close();
 const closed=fixture({events:async()=>{throw {status:404,code:'room_closed'};}});closed.feed.start();await flush();assert.equal(closed.feed.closed,true);assert.equal(closed.feed.legacy,false);
});
test('short clock probes are independent of a waiting event connection and both abort on hide',async()=>{
 const event=deferred(),probe=deferred();let eventSignal,probeSignal,visible=true;
 const f=fixture({events:({signal})=>{eventSignal=signal;return event.promise;},probe:({signal})=>{probeSignal=signal;return probe.promise;},visible:()=>visible});f.feed.start();await f.advance(2500);assert.ok(probeSignal);assert.equal(eventSignal.aborted,false);
 visible=false;f.feed.stop();assert.equal(eventSignal.aborted,true);assert.equal(probeSignal.aborted,true);event.resolve({cursor:1,status:{server_time_ms:5}});probe.resolve();await flush();assert.equal(f.statuses.length,0);f.feed.start();assert.equal(f.feed.active,false);
});
test('a legitimate server restart cursor regression re-synchronizes instead of retrying forever',async()=>{
 const replies=[{cursor:100,status:{server_time_ms:1000}},{cursor:2,status:{server_time_ms:2000}}],requested=[];
 const f=fixture({events:async({cursor})=>{requested.push(cursor);return replies.shift()||new Promise(()=>{});}});f.feed.start();await f.advance(200);assert.deepEqual(requested,[0,100,2]);assert.equal(f.statuses.length,2);assert.equal(f.feed.cursor,2);f.feed.close();
});
