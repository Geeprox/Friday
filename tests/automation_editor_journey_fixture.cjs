// Export only the original editor's submitted payload. No network or device I/O.
const fs=require('node:fs'),assert=require('node:assert/strict');
const {fixture,all,settle}=require('./scheduled_automation_fixture.cjs');
(async()=>{
 const input=JSON.parse(fs.readFileSync(0,'utf8'));
 const f=fixture(input.options);
 try{
  await settle();
  if(input.mode==='sequence'){
   f.by('button','新建自动化').fire('click');await settle();
   f.label('自动化名称').value='SYNTHETIC ordered light sequence';
   f.label('触发设备').value=input.options.sensorId;f.label('触发设备').fire('change');
   f.label('动作设备').value=input.options.controlId;f.label('动作设备').fire('change');
   f.by('button','添加等待').fire('click');f.label('等待秒数').value='0.05';f.label('等待秒数').fire('input');
   f.by('button','添加一个动作').fire('click');
   const targets=all(f.body).filter(n=>n.attrs['aria-label']==='动作设备');targets.at(-1).value=input.options.controlId;targets.at(-1).fire('change');
   const values=all(f.body).filter(n=>n.attrs['aria-label']==='动作值');values.at(-1).value='off';values.at(-1).fire('change');
  }else{
   f.by('button','自动运行').fire('click');f.by('button','编辑').fire('click');await settle();
   f.label('自动化名称').value='SYNTHETIC renamed safe lighting';f.label('自动化名称').fire('input');
  }
  await all(f.body).find(n=>n.tagName==='form').fire('submit');
  assert(f.planned);assert.equal(f.applied,0);
  process.stdout.write(JSON.stringify({request:f.planned,review:f.body.textContent,applied:f.applied}));
 }finally{f.owner.dispose();}
})().catch(error=>{console.error(error);process.exitCode=1;});
