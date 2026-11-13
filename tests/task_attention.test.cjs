const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const {fixture,all,settle}=require('./scheduled_automation_fixture.cjs');
const item=(changes={})=>({id:'d'.repeat(64),record_id:'record',fingerprint:'a'.repeat(64),run_id:null,kind:'missed',title:'SYNTHETIC missed lighting',at:'2026-11-09T12:00:00+00:00',timezone:'Asia/Shanghai',record_current:true,...changes});
const report=items=>({task_attention:{available:true,items,has_more:false}});

test('home uses the actual task outcome and does not revive an acknowledged raw run',async()=>{
 const {homeAttention}=await import('../homeplane/static/home-status.js');
 const missed=item(),unknown=item({id:'e'.repeat(64),kind:'unconfirmed',run_id:'source-run'}),unavailable=item({id:'f'.repeat(64),kind:'not_executed'});
 const rows=homeAttention([],[],[{id:'source-run',status:'unknown_outcome'}],null,[],report([missed,unknown,unavailable]).task_attention);
 assert.equal(rows.length,3);assert.equal(rows[0].attentionId,missed.id);assert.equal(rows[0].title,missed.title);assert.match(rows[0].detail,/重新安排/);assert.match(rows[2].detail,/本次未执行/);
 assert.deepEqual(homeAttention([],[],[{id:'source-run',status:'unknown_outcome'}],null,[],report([]).task_attention),[]);
});

test('the original home attention button carries the exact task entry into the workspace',async()=>{
 const missed=item(),f=fixture({modeReport:report([missed])});await settle();
 const scope=f.context;scope.AbortController=AbortController;scope.setInterval=()=>1;scope.clearInterval=()=>{};scope.setTimeout=()=>2;scope.clearTimeout=()=>{};
 scope.homeApi=async path=>path==='/api/rules/core/runtime'?{available:true,execution_state:'ready',...report([missed])}:path==='/api/embodied/safety/attention'?{events:[],pending:0}:path==='/api/settings/arl/status'?null:{};
 vm.runInContext(fs.readFileSync('homeplane/static/safety-event-labels.js','utf8').replace(/export /g,''),scope);
 let selected;scope.homeGo=(page,options)=>{assert.equal(page,'Automations');selected=options.attentionId;f.owner.selectAttention(selected);};
 const source=fs.readFileSync('homeplane/static/home-status.js','utf8').replace(/^import .*;$/gm,'').replace(/export /g,'');
 vm.runInContext('globalThis.homeOwner=(function(){'+source+';const root=document.createElement("aside");document.body.append(root);return mountHomeStatus(root,{api:homeApi,go:homeGo});})()',scope);
 await settle();await settle();const button=all(f.body).find(node=>node.attrs.class==='home-attention-item'&&node.textContent.includes(missed.title));assert(button);
 button.fire('click');assert.equal(selected,missed.id);assert.equal(f.focused.textContent,missed.title);scope.homeOwner.dispose();f.owner.dispose();
});

test('opening an exact attention entry selects automatic tasks and focuses that outcome',async()=>{
 const missed=item(),f=fixture({attentionId:missed.id,modeReport:report([missed]),rule:{trigger:{type:'once',at:missed.at,timezone:missed.timezone}}});await settle();
 assert.equal(f.by('button','自动运行').attrs['aria-pressed'],'true');assert.equal(f.focused.textContent,missed.title);
 assert.match(f.root.textContent,/2026年11月9日 20:00/);
 assert.equal(f.calls.filter(c=>c.body).length,0);await f.by('button','重新安排').fire('click');await settle();
 assert.equal(f.label('触发方式').value,'once');assert.equal(f.applied,0);assert(!f.calls.some(c=>c.path.endsWith('/run')));f.owner.dispose();
});

test('polling preserves the focused control and a delayed old report cannot resurrect acknowledgement',async()=>{
 let release;const pending=new Promise(resolve=>{release=resolve;}),unknown=item({kind:'unconfirmed',run_id:'source-run'});
 const f=fixture({attentionId:unknown.id,modeReport:report([unknown]),acknowledge:()=>pending});await settle();
 const button=f.by('button','已了解');button.focus();f.modeStatus(report([unknown]));assert.equal(f.by('button','已了解'),button);assert.equal(f.focused,button);
 const sending=button.fire('click');assert.equal(button.disabled,true);assert.equal(f.calls.filter(c=>c.body).length,1);
 const request=f.calls.find(c=>c.path.endsWith('/acknowledge'));assert.deepEqual(request.body,{record_id:unknown.record_id,expected_fingerprint:unknown.fingerprint,run_id:unknown.run_id});
 release({status:'acknowledged',id:unknown.id,device_actions:false});await sending;f.modeStatus(report([unknown]));assert.equal(f.by('button','已了解'),undefined);assert.equal(f.toasts.length,1);
 assert(!f.calls.some(c=>c.path.endsWith('/run')));f.owner.dispose();
});

test('a mismatched acknowledgement stays visible and retired work cannot report success',async()=>{
 const missed=item();let release;const f=fixture({attentionId:missed.id,modeReport:report([missed]),acknowledge:()=>new Promise(resolve=>{release=resolve;})});await settle();
 const button=f.by('button','已了解'),sending=button.fire('click');release({status:'acknowledged',id:'f'.repeat(64),device_actions:false});await sending;
 assert.equal(f.by('button','已了解'),button);assert.match(f.root.textContent,/提醒未更新/);assert.equal(f.toasts.length,0);
 const second=button.fire('click');f.owner.dispose();release({status:'acknowledged',id:missed.id,device_actions:false});await second;assert.equal(f.toasts.length,0);
});

test('a resolved or replaced attention link never opens or runs an obsolete task',async()=>{
 const f=fixture({attentionId:'d'.repeat(64),modeReport:report([])});await settle();
 assert.match(f.root.textContent,/这条提醒已处理，或任务已更新/);assert.equal(f.calls.filter(c=>c.body).length,0);
 f.by('button','查看全部自动化').fire('click');assert.equal(f.goCalls.at(-1)[0],'Automations');f.owner.dispose();
});

test('the original app route keeps the exact attention identifier across reload and same-page selection',()=>{
 const source=fs.readFileSync('homeplane/static/app.js','utf8'),start=source.indexOf('let requestedTaskAttention=null;');assert(start>=0);
 let stored,selected;const scope=vm.createContext({page:'Home',pages:['Home','Automations','Safety'],location:{hash:'#automations?attention='+'d'.repeat(64)},URLSearchParams,
  requestedSafetyEvent:null,archiveVersion:0,archiveSetupVersion:0,lastTrace:null,stopArchivePlayer(){},render(){},history:{replaceState(_a,_b,path){stored=path;}},
  experienceView:{selectTaskAttention(id){selected=id;}},$:()=>({setAttribute(){},focus(){}})});
 vm.runInContext(source.slice(start,source.indexOf('function button(',start)),scope);
 assert.equal(scope.pageLocation().attentionId,'d'.repeat(64));scope.go('Automations',{attentionId:'d'.repeat(64)});assert.equal(stored,'#automations?attention='+'d'.repeat(64));assert.equal(selected,'d'.repeat(64));
 scope.go('Automations',{attentionId:'e'.repeat(64)});assert.equal(selected,'e'.repeat(64));
 scope.location.hash='#automations?attention=../invalid';assert.equal(scope.pageLocation().attentionId,null);
 scope.location.hash='#safety?attention='+'d'.repeat(64)+'&event='+'a'.repeat(32);assert.equal(scope.pageLocation().attentionId,null);assert.equal(scope.pageLocation().eventId,'a'.repeat(32));
});
