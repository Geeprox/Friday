// The original mounted workspace and native control events, with inert APIs.
const test=require('node:test'),assert=require('node:assert/strict');
const {fixture,all,settle}=require('./scheduled_automation_fixture.cjs');
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};};
async function editing(options={}){const f=fixture(options);await settle();f.by('button','自动运行').fire('click');f.by('button','编辑').fire('click');await settle();return f;}
const form=f=>all(f.body).find(n=>n.tagName==='form');
const minimum={id:'dim',type:'light.set',target:'light',power:'on',brightness:'minimum_verified',require_no_bright_flash:true};

test('renaming an approved low-light automation preserves its actual brightness and smooth switch-on requirement',async()=>{
 const f=await editing({rule:{actions:[minimum]}});
 try{
  f.label('自动化名称').value='SYNTHETIC renamed night light';f.label('自动化名称').fire('input');
  await form(f).fire('submit');
  assert.deepEqual(f.planned.rule.actions,[minimum]);
  assert(f.body.textContent.includes('最低亮度'));
  assert(f.body.textContent.includes('平稳点亮'));
  assert.equal(f.applied,0);
 }finally{f.owner.dispose();}
});

test('renaming an ordinary legacy light action keeps the original numeric brightness',async()=>{
 const action={id:'dim',type:'light.set',target:'light',power:'on',brightness:64,require_no_bright_flash:false};
 const f=await editing({rule:{actions:[action]},capabilities:[{name:'light.set',minimum:1,maximum:255}]});
 try{await form(f).fire('submit');assert.deepEqual(f.planned.rule.actions,[action]);}finally{f.owner.dispose();}
});

test('missing capability detail never turns an existing dimmed device action into unrestricted power-on',async()=>{
 const action={id:'dim',type:'device.set',target:'light',operation:'light.set',desired:{power:'on',brightness:2}};
 const f=await editing({rule:{actions:[action]}});
 try{await form(f).fire('submit');assert.deepEqual(f.planned.rule.actions,[action]);}finally{f.owner.dispose();}
});

test('editing a name preserves exact API brightness values at both ends of the range',async()=>{
 for(const brightness of [1,2,3,64,127,254,255]){
  const action={id:'dim',type:'light.set',target:'light',power:'on',brightness,require_no_bright_flash:false};
  const f=await editing({rule:{actions:[action]},capabilities:[{name:'light.set',minimum:1,maximum:255}]});
  try{await form(f).fire('submit');assert.deepEqual(f.planned.rule.actions,[action]);}finally{f.owner.dispose();}
 }
});

test('removing dimming and turning a light off both require explicit control changes',async()=>{
 const action={id:'dim',type:'device.set',target:'light',operation:'light.set',desired:{power:'on',brightness:64}};
 for(const mode of ['keep','off']){
  const f=await editing({rule:{actions:[action]},capabilities:[{name:'light.set',maximum:255}]});
  try{
   if(mode==='keep'){f.label('亮度方式').value='keep';f.label('亮度方式').fire('change');}
   else{f.label('动作值').value='off';f.label('动作值').fire('change');assert.equal(f.label('亮度百分比').required,false);}
   await form(f).fire('submit');assert.deepEqual(f.planned.rule.actions[0].desired,{power:mode==='off'?'off':'on'});
  }finally{f.owner.dispose();}
 }
});

test('removing an action after review invalidates the old approval before any apply request',async()=>{
 const action={id:'one',type:'device.set',target:'light',operation:'light.set',desired:{power:'on'}};
 const f=await editing({rule:{actions:[action,{...action,id:'two',desired:{power:'off'}}]}});
 try{
  await form(f).fire('submit');const stale=f.by('button','确认保存并启用');assert(stale);
  f.by('button','移除此动作').fire('click');
  assert(!f.by('button','确认保存并启用'),'removed action must retire the approval');
  await stale.fire('click');assert.equal(f.applied,0);
  await form(f).fire('submit');assert.deepEqual(f.planned.rule.actions.map(a=>a.id),['two']);
 }finally{f.owner.dispose();}
});

for(const kind of ['add_action','add_delay','remove_condition'])test(`${kind} retires the reviewed plan`,async()=>{
 const f=await editing({rule:{conditions:[{type:'time_window',start:'08:00',end:'22:00',timezone:'Asia/Shanghai'}]}});
 try{
  await form(f).fire('submit');const stale=f.by('button','确认保存并启用');assert(stale);
  f.by('button',kind==='add_action'?'添加一个动作':kind==='add_delay'?'添加等待':'移除此条件').fire('click');
  assert(!f.by('button','确认保存并启用'),'structure change must retire the approval');await stale.fire('click');assert.equal(f.applied,0);
 }finally{f.owner.dispose();}
});

for(const phase of ['context','plan'])for(const action of ['dispose','cancel','escape','input'])test(`${action} while ${phase} is pending stops the next API stage`,async()=>{
 const gate=deferred();let block=false;
 const target=phase==='context'?'/api/rules/core/context':'/api/rules/core/plans';
 const f=await editing({beforeRequest:async path=>{if(block&&path===target)await gate.promise;}});
 try{
  block=true;const start=f.calls.length,pending=form(f).fire('submit');await settle();
  if(action==='dispose')f.owner.dispose();
  else if(action==='cancel')f.by('button','取消').fire('click');
  else if(action==='escape')all(f.body).find(n=>n.tagName==='dialog').fire('cancel');
  else{f.label('自动化名称').value='SYNTHETIC changed';f.label('自动化名称').fire('input');}
  gate.resolve();await pending;
  const advanced=f.calls.slice(start).map(c=>c.path).filter(p=>p!==target);
  assert(!advanced.includes(phase==='context'?'/api/rules/core/plans':'/api/rules/core/plans/trial'),JSON.stringify(advanced));
  assert.equal(f.applied,0);
 }finally{gate.resolve();f.owner.dispose();}
});

test('delay steps are visible, editable and included in the exact reviewed sequence',async()=>{
 const delay={type:'delay',id:'wait',milliseconds:5000,cancel_on_new_event:true};
 const f=await editing({rule:{actions:[minimum,delay],execution:{preferred_backend:'home_core',max_event_age_ms:8000}}});
 try{
  assert.equal(f.label('等待秒数').value,'5');f.label('等待秒数').value='6';f.label('等待秒数').fire('input');
  await form(f).fire('submit');
  assert.deepEqual(f.planned.rule.actions,[minimum,{...delay,milliseconds:6000}]);
  assert.equal(f.planned.rule.execution.max_event_age_ms,9000);
  assert(f.body.textContent.includes('等待 6 秒'));
  assert.equal(f.applied,0);
 }finally{f.owner.dispose();}
});

test('the original delay budget rejects a thirty-second total before drafting',async()=>{
 const f=await editing({rule:{actions:[minimum,{type:'delay',id:'wait',milliseconds:5000,cancel_on_new_event:true}]}});
 try{
  f.label('等待秒数').value='30';f.label('等待秒数').fire('input');await form(f).fire('submit');
  assert.equal(f.planned,null);assert(f.body.textContent.includes('小于 30 秒'));assert.equal(f.applied,0);
 }finally{f.owner.dispose();}
});

test('an already retired form cannot begin another context read',async()=>{
 const f=await editing();const retired=form(f);f.owner.dispose();const count=f.calls.length;
 await retired.fire('submit');assert.equal(f.calls.length,count);
});
