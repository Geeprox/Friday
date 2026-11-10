// Carry an actual ASGI task outcome through the original UI; capture only its explicit acknowledgement.
const fs=require('node:fs'),assert=require('node:assert/strict');
const {fixture,settle}=require('./scheduled_automation_fixture.cjs');
async function main(){
 const input=JSON.parse(fs.readFileSync(0,'utf8')),f=fixture({recordOverride:input.record,attentionId:input.id,modeReport:input.runtime,
  controlId:input.record.rule.actions.find(a=>a.target)?.target,acknowledge:()=>new Promise(()=>{})});
 await settle();
 if(input.cleared){assert(!f.by('button','已了解'));assert.match(f.root.textContent,/已处理，或任务已更新/);console.log(JSON.stringify({cleared:true,device_actions:0}));}
 else{assert.equal(f.focused.textContent,input.runtime.task_attention.items.find(item=>item.id===input.id).title);void f.by('button','已了解').fire('click');await settle();
  const request=f.calls.find(call=>call.path.endsWith('/acknowledge'));assert(request);assert(!f.calls.some(call=>call.path.endsWith('/run')));console.log(JSON.stringify(request));}
 f.owner.dispose();
}
main().catch(error=>{console.error(error);process.exitCode=1;});
