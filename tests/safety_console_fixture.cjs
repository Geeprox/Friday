const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let focusedElement;
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.textContent='';this.disabled=false;}
 setAttribute(k,v){this.attrs[k]=String(v);}
 getAttribute(k){return this.attrs[k]??null;}
 contains(other){return this===other||this.children.some(n=>n.contains(other));}
 querySelector(selector){
  const matches=selector.startsWith('.')
   ? node=>(node.attrs.class||node.className||'').split(/\s+/).includes(selector.slice(1))
   : node=>node.tagName===selector;
  for(const child of this.children){
   if(matches(child))return child;
   const match=child.querySelector(selector);
   if(match)return match;
  }
  return null;
 }
 focus(){focusedElement=this;}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}
 click(){this.onclick?.();}
}
(async()=>{
 const root=new Element('main'),head=new Element('head'),all=n=>[n,...n.children.flatMap(all)];
 const find=(tag,text)=>all(root).find(n=>n.tagName===tag&&n.textContent===text),calls=[];
 const fakeEvent={id:'a'.repeat(32),at:1800000000,kind:'fall',simulated:true,status:'test',message:'测试播报',review:'unreviewed',announcement:{status:'paused'},frames:[],camera:null};
 let policy={enabled:false,announce:false,visual_review:false,sound_review:false,revision:0},soundSearch=false,events=[],interval,opened=[],setups=0,sound={available:true,state:'idle',supported_types:['crying']};
 let reference={event_id:fakeEvent.id,code:'archive_capture_mapping_required',recordings:[]};
 const fetch=async(path,options)=>{const body=options.body&&JSON.parse(options.body);calls.push({path,body});
   let value;
   if(path.endsWith('/policy')){policy={...body,revision:policy.revision+1};if(!policy.sound_review)soundSearch=false;value=policy;}
   else if(path.endsWith('/sound-search')){assert.equal(body.expected_revision,policy.revision);soundSearch=body.enabled;value={enabled:soundSearch,revision:++policy.revision};}
   else if(path.endsWith('/review')){events[0]={...events[0],review:body.review};value=events[0];}
   else if(path.endsWith('/test')){events=[fakeEvent];value=fakeEvent;}
   else if(path.endsWith('/events'))value={events};
   else if(path.endsWith('/recordings'))value=reference;
   else value={policy,worker:{cameras:[]},visual_review:{available:true,state:'idle'},sound_review:sound,sound_search:soundSearch,capabilities:[{name:'疑似跌倒',status:'experimental',basis:'骨骼时序'},{name:'粗暴照护',status:'unavailable',basis:'尚未接入'}]};
   return {ok:true,json:async()=>value};
 };
 const document={head,createElement:t=>new Element(t),getElementById:id=>all(head).find(n=>n.attrs.id===id),get activeElement(){return focusedElement;}};
 const safetyOptions={onOpenRecording:(output,item)=>{output.replaceChildren();if(item){opened.push(item);output.append(new Element('video'));}},onArchiveSettings:()=>setups++};
 const context=vm.createContext({document,fetch,Date,JSON,console,root,safetyOptions,setInterval:f=>(interval=f,1),clearInterval(){},setTimeout(){},clearTimeout(){},Blob,URL});
 const labelsSource=fs.readFileSync('homeplane/static/safety-event-labels.js','utf8').replace(/export /g,'');
 const labels=vm.runInContext('(function(){'+labelsSource+';return {soundReviewNames,isSafetyOutputTest,safetyEventTitle,isRapidMovementReview,isPendingFall,isPendingSafetyEvent};})()',context);
 Object.assign(context,labels,{soundNames:labels.soundReviewNames,isOutputTest:labels.isSafetyOutputTest,eventTitle:labels.safetyEventTitle});
 vm.runInContext(fs.readFileSync('homeplane/static/safety.js','utf8').replace(/^import .*;$/gm,'').replace('export function mountSafety','function mountSafety'),context);
 const owner=vm.runInContext('mountSafety(root,safetyOptions)',context),settle=()=>new Promise(setImmediate);await settle();
 assert.equal(calls.length,2);assert(calls.every(c=>!c.body),'viewing safety must be silent and read-only');
 assert.equal(find('button','开启异响后环视').hidden,true);
 assert(find('span','尚未提供'));assert(find('p','监测已暂停'));
 await find('button','开启监测').onclick();assert.equal(policy.enabled,true);assert.equal(policy.announce,false);
 const visualOn=find('button','开启画面复核');
 assert(visualOn);assert.equal(visualOn.disabled,false);assert.equal(visualOn.attrs['aria-checked'],'false');
 await visualOn.onclick();assert.equal(policy.visual_review,true);
 await find('button','开启声音复核').onclick();assert.equal(policy.sound_review,true);
 assert.equal(find('button','关闭声音复核').attrs['aria-checked'],'true');assert(find('p','已开启 · 疑似哭声'));
 const search=find('button','开启异响后环视');assert.equal(search.hidden,false);assert.equal(search.attrs['aria-checked'],'false');
 search.focus();await search.onclick();assert.equal(soundSearch,true);
 assert.equal(find('button','关闭异响后环视').attrs['aria-checked'],'true');assert.equal(focusedElement,search);
 const visualOff=find('button','关闭画面复核');
 assert(visualOff);assert.equal(visualOff.attrs['aria-checked'],'true');
 assert.equal(visualOff.disabled,false,'successful refresh must restore the model switch');
 assert(calls.every(c=>!c.path.endsWith('/test')),'enabling visual review must never test or request speech');
 await find('button','开启语音提醒').onclick();assert.equal(policy.announce,true);
 await find('button','试听跌倒提醒').onclick();find('button','').onclick();
 assert.equal(find('button','').attrs['aria-current'],'true');
 assert(find('p','安静模式，未播报'));assert(find('p','播报测试不采集摄像头画面。'));
 find('button','标记误报').focus();const previousFocus=focusedElement;
 await find('button','标记误报').onclick();assert.equal(events[0].review,'false_alarm');
 assert.notEqual(focusedElement,previousFocus);assert.equal(focusedElement.textContent,'标记误报','review refresh preserves keyboard position');
 events=[{...fakeEvent,status:'suspected',person_name:'<img src=x onerror=alert(1)>',simulated:false,message:'<unsafe>',frames:[{ordinal:0,at:1800000000}]}];interval();await settle();
 find('button','').onclick();assert(find('h2','<img src=x onerror=alert(1)> · 疑似跌倒'));
 assert.equal(all(root).filter(n=>n.tagName==='img').length,1);
 assert(all(root).find(n=>n.tagName==='img').attrs.src.endsWith('/frames/0'));
 events=[{...events[0],camera:'cam_1111111111'}];interval();await settle();find('button','').onclick();
 assert(!calls.some(c=>c.path.endsWith('/recordings')),'viewing an event never scans or opens NAS');
 find('button','查看附近录像').onclick();await settle();assert(find('button','录像设置'));find('button','录像设置').onclick();assert.equal(setups,1);assert.equal(opened.length,0);
 const recording={event_id:fakeEvent.id,camera_name:'SYNTHETIC recording',
  anchor_at:new Date(fakeEvent.at*1000).toISOString(),
  request:{expected_context:'b'.repeat(64),camera_id:'SYNTHETIC camera',
   time_range:{start:new Date((fakeEvent.at-15)*1000).toISOString(),end:new Date((fakeEvent.at+15)*1000).toISOString()}}};
 reference={event_id:'b'.repeat(32),code:'configured_camera_time_lookup',recordings:[recording]};
 find('button','查看附近录像').onclick();await settle();assert(find('p','暂时无法读取录像。'));assert.equal(opened.length,0);
 reference={...reference,event_id:fakeEvent.id};
 find('button','查看附近录像').onclick();await settle();assert.equal(opened.length,1);
 const player=all(root).find(n=>n.tagName==='video');assert(player);
 events=[{...events[0],review:'acknowledged'}];interval();await settle();assert.equal(all(root).find(n=>n.tagName==='video'),player,'history refresh preserves the selected event player');
 events=[{...events[0],announcement:{status:'unconfirmed'}}];interval();await settle();find('button','').onclick();
 assert(find('p','播报结果待确认'));assert(!find('p','播报状态未知'),'an uncertain external send has a specific user-facing state');
 events=[{...events[0],kind:'sound_review',sound_kind:'crying',simulated:true,status:'pending_review',person_name:null,frames:[],message:'听到疑似哭声，请查看附近画面。'}];interval();await settle();find('button','').onclick();
 assert(find('strong','疑似哭声'));assert(find('h2','疑似哭声'));assert(!find('h2','疑似跌倒'));
 assert(!find('p','安静模式，未播报'),'unrequested sound review must not show irrelevant speech diagnostics');
 sound={available:false};interval();await settle();assert.equal(find('button','关闭声音复核').disabled,false,'an unavailable saved feature can still be disabled');
 await find('button','关闭声音复核').onclick();assert.equal(policy.sound_review,false);assert.equal(find('button','开启声音复核').disabled,true);
 assert.equal(soundSearch,false);assert.equal(find('button','开启异响后环视').hidden,true);
 owner.dispose();assert.equal(root.children.length,0);interval();await settle();assert.equal(root.children.length,0);
 console.log('Safety UI: silent reads, policy revisions, explicit quiet test, review, safe names, keyframes, disposal: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
