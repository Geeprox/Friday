const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),vm=require('node:vm');
const{fixture,all,settle}=require('./scheduled_automation_fixture.cjs');
const utc={type:'once',at:'2030-12-09T03:13:00+00:00',timezone:'America/New_York'};
test('one-off form sends local date and family timezone, then reviews the original canonical result',async()=>{
 const f=fixture({trialTrigger:utc,rule:{trigger:{type:'schedule',at:'22:00',weekdays:[1,2,3,4,5],timezone:'America/New_York'}}});try{
  await settle();f.by('button','自动运行').fire('click');f.by('button','编辑').fire('click');await settle();
  const type=f.label('触发方式');type.value='once';type.fire('change');const input=f.label('执行日期与时间');
  assert.equal(input.disabled,false);assert.equal(f.label('执行时间').disabled,true);
  input.value='2030-12-08T22:13:00';input.fire('input');await all(f.body).find(n=>n.tagName==='form').fire('submit');
  assert.deepEqual(f.planned.rule.trigger,{type:'once',at:'2030-12-08T22:13:00',timezone:'America/New_York'},'editing retains the displayed existing timezone');
  assert(f.body.textContent.includes('2030-12-08 22:13:00'));assert(f.body.textContent.includes('UTC-5'));
  assert.equal(f.applied,0);await f.by('button','确认保存并启用').fire('click');assert.equal(f.applied,1);
 }finally{f.owner.dispose();}
});
test('editing an existing second-fold instant preserves the exact UTC time when its wall input is unchanged',async()=>{
 const trigger={type:'once',at:'2026-11-01T06:30:00+00:00',timezone:'America/New_York'},f=fixture({rule:{trigger},trialTrigger:trigger});try{
  await settle();f.by('button','自动运行').fire('click');f.by('button','编辑').fire('click');await settle();
  assert.equal(f.label('执行日期与时间').value,'2026-11-01T01:30:00');
  f.label('自动化名称').value='SYNTHETIC retained exact deadline';f.label('自动化名称').fire('input');
  await all(f.body).find(n=>n.tagName==='form').fire('submit');assert.equal(f.planned.rule.trigger.at,trigger.at);
 }finally{f.owner.dispose();}
});
test('one-off result updates in place and offers rescheduling without an active switch or immediate-run action',async()=>{
 const f=fixture({rule:{trigger:utc}});try{
  await settle();f.by('button','自动运行').fire('click');const status=key=>({clock:{one_off_tasks:{record:{fingerprint:'a'.repeat(64),state:key}}}});
  f.modeStatus(status('scheduled'));assert(f.root.textContent.includes('等待执行'));
  const editor=f.by('button','编辑');f.modeStatus(status('completed'));assert(f.root.textContent.includes('已执行'));
  assert.equal(f.by('button','已启用').hidden,true);assert.equal(f.by('button','执行一次').hidden,true);assert.equal(f.by('button','重新安排'),editor);
  f.modeStatus(status('unconfirmed'));assert(f.root.textContent.includes('执行结果待确认'));
  f.modeStatus(status('missed'));assert(f.root.textContent.includes('已错过执行时间'));
  f.modeStatus({clock:{one_off_tasks:{record:{fingerprint:'b'.repeat(64),state:'completed'}}}});assert(f.root.textContent.includes('执行状态待核对'));
 }finally{f.owner.dispose();}
});
test('one-off form cannot confirm an unnormalized or absent canonical time',async()=>{
 const f=fixture({rule:{trigger:utc}});try{
  await settle();f.by('button','自动运行').fire('click');f.by('button','编辑').fire('click');await settle();
  f.label('执行日期与时间').value='2030-12-08T22:13:01';f.label('执行日期与时间').fire('input');
  await all(f.body).find(n=>n.tagName==='form').fire('submit');assert.equal(f.by('button','确认保存并启用'),undefined);assert.equal(f.applied,0);
 }finally{f.owner.dispose();}
});
test('the original status owner rejects an older reply, stops hidden polling and releases its timer',async()=>{
 const f=fixture();await settle();f.owner.dispose();const timers=new Map(),pending=[],seen=[];let next=0;
 const c=vm.createContext({document:{hidden:false,createElement:tag=>new f.body.constructor(tag)},setInterval:(fn,ms)=>{timers.set(++next,{fn,ms});return next;},clearInterval:id=>timers.delete(id)});
 vm.runInContext(fs.readFileSync('homeplane/static/automation-mode.js','utf8').replace('export ',''),c);
 const owner=c.mountAutomationMode(f.root,{api:()=>new Promise(resolve=>pending.push(resolve)),toast(){},onStatus:report=>seen.push(report)});
 try{
  assert.equal(timers.get(1).ms,2000);timers.get(1).fn();assert.equal(pending.length,2);
  const report=state=>({enabled:true,revision:1,runtime:{available:true,clock:{one_off_tasks:{record:{state}}}},mode_source:{state:'current',mode:'awake',context:'SYNTHETIC-context'}});
  pending[1](report('completed'));await settle();pending[0](report('scheduled'));await settle();assert.equal(seen.length,1);assert.equal(seen[0].clock.one_off_tasks.record.state,'completed');
  c.document.hidden=true;timers.get(1).fn();assert.equal(pending.length,2);
 }finally{owner.dispose();assert.equal(timers.size,0);}
});

test('a changed reply timezone or deadline cannot become the reviewed one-off intent',async()=>{
 for(const trigger of [{...utc,timezone:'Asia/Shanghai'},{...utc,at:'2030-12-09T04:13:00+00:00'}]){
  const f=fixture({rule:{trigger:utc},trialTrigger:trigger});try{
   await settle();f.by('button','自动运行').fire('click');f.by('button','编辑').fire('click');await settle();
   await all(f.body).find(n=>n.tagName==='form').fire('submit');assert.equal(f.by('button','确认保存并启用'),undefined);assert.equal(f.applied,0);
  }finally{f.owner.dispose();}
 }
});
test('the original API exposes only the fixed schedule expiry code to date recovery',async()=>{
 const source=fs.readFileSync('homeplane/static/app.js','utf8'),begin=source.indexOf('async function api('),end=source.indexOf('\nfunction safetyEventId(',begin);
 for(const detail of ['schedule_time_unavailable','SYNTHETIC-unreviewed-error',{private:'SYNTHETIC-content'}]){
  const c=vm.createContext({fetch:async()=>({ok:false,status:409,json:async()=>({detail})}),clearHomeConversation(){}});vm.runInContext(source.slice(begin,end),c);
  await assert.rejects(c.api('/api/rules/core/plans'),error=>{assert.equal(error.code,detail==='schedule_time_unavailable'?detail:undefined);assert(!error.message.includes('SYNTHETIC'));return true;});
 }
});
