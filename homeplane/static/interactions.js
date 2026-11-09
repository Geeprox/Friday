import {mountMotionLibrary} from './recognition-practice.js';
import {MOTION_GUIDES,createMotionDiagram} from './motion-guide.js';
const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{children.forEach(c=>c&&n.append(c));return n;};
const btn=(text,run,attrs={})=>{const n=h('button',text,{type:'button',...attrs});n.onclick=run;return n;};
const api=async(path,body)=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});const data=await r.json();if(!r.ok){const error=Error(typeof data.detail==='string'?data.detail:'unavailable');error.status=r.status;throw error;}return data;};
const actionName=value=>({on:'开启',off:'关闭',open:'打开',close:'关闭','mode:off':'关闭','mode:cool':'制冷','mode:heat':'制热','mode:auto':'自动','mode:heat_cool':'自动冷暖','mode:dry':'除湿','mode:fan_only':'送风'}[value]||(value?.startsWith('position:')?'开到 '+value.slice(9)+'%':'调整设备'));
const states={live:'识别中',ready:'识别中',connecting:'正在连接',waiting:'等待画面',stopped:'未开启',unavailable:'连接中断',shutter_closed:'镜头已关闭',stale:'画面已中断'};
function voiceCommandPresentation(voice){
 const command=voice.voice_command;
 if(!command||command.text!==voice.last_transcript)return{state:'unknown',text:'这条指令的结果尚未确认',recover:true};
 if(command.state==='answered'&&command.route==='query'&&command.receipt?.read_only===true&&command.receipt?.device_authority===false&&typeof command.reply==='string'&&command.reply)return{state:'idle',text:command.reply,recover:false};
 if(command.state==='readback'&&['reported_match','already_at_target','observed_at_target'].includes(command.receipt?.status)&&command.receipt?.reached_target===true)return{state:'success',text:'设备状态已更新',recover:false};
 if(command.state==='accepted'&&command.receipt?.acknowledged===true)return{state:'pending',text:'指令已发送，等待设备回报',recover:true};
 return({processing:{state:'pending',text:'正在理解指令'},executing:{state:'pending',text:'正在执行并核对状态'},partial:{state:'unknown',text:'部分设备结果尚未确认',recover:true},unknown_outcome:{state:'unknown',text:'执行结果待核对',recover:true},failed:{state:'failed',text:'这条指令未能完成',recover:true},expired:{state:'unknown',text:'这条指令已过期，请重新说一次',recover:true},clarify:{state:'unknown',text:command.reply||'请补充设备名称或要做的操作',recover:false},ignored:{state:'idle',text:command.reply||'这句话没有触发设备操作',recover:false}})[command.state]||{state:'unknown',text:'这条指令的结果尚未确认',recover:true};
}
const gestureHelp={'body.arms_open':'面对镜头，将双臂向两侧打开并保持片刻。','body.arms_close':'将双臂向身体前方合拢并保持片刻。','body.hands_up':'将双手举过头顶并保持片刻。','hand.ok':'拇指与食指合成圆圈，其余手指伸开。','hand.open_palm':'面向镜头张开手掌，保持手掌清晰可见。','hand.yeah':'面向镜头伸出食指和中指。'};
export function mountInteractions(root,{data,go,toast}){
 let alive=true,config=null,targets=[],gestures={},report={},policy=null,voicePreferences=null,voiceDraft='',busy=false,mode='voice',editing=null,undo=null,previewToken=null,previewCamera=null,image=null,previewLive=false,anchor=null;
 let motionLibrary=null,selectedDiagram=null,configuring=false;const cameras=data.devices.filter(d=>d.kind==='camera');const slot=h('div',null,{class:'interaction-workspace'});root.append(slot);
 let draft={camera:'',target:'',gesture:'body.arms_open',action:''};
 try{const saved=JSON.parse(sessionStorage.getItem('Friday.interactionDraft')||'null');if(saved&&typeof saved==='object')for(const key of Object.keys(draft))if(typeof saved[key]==='string')draft[key]=saved[key];}catch(_){}
 const select=(items,label)=>{const n=h('select',null,{'aria-label':label});for(const[value,text]of items)n.append(h('option',text,{value}));return n;};
 const field=(label,input)=>add(h('label',null,{class:'product-field'}),h('span',label),input);
 function cameraInputStatus(id){
  const session=report.camera_sessions?.find(s=>s.camera===id),live=['live','ready'].includes(session?.state),physical={text:states[session?.state]||'未开启',available:live};
  if(mode==='voice'||!live)return physical;
  const view=report.interactions?.cameras?.find(c=>c.camera===id);if(!view)return physical;
  const stateText={game:'游戏进行中',paused:'手势已暂停',waiting:'等待动作画面',connecting:'等待动作画面',moving:'等待镜头稳定'};
  if(stateText[view.state])return{text:stateText[view.state],available:false};
  const input=view.interaction||{};
  if(view.state==='ready'&&input.release_required){const partial=Number.isInteger(input.ready_bindings)&&input.ready_bindings>0;return{text:partial?'识别中 · 部分动作需先放松':'请先放松，再做一次动作',available:partial};}
  return physical;
 }
 const targetById=id=>targets.find(t=>t.id===id),valid=b=>{const t=targetById(b.target);return t&&(t.actions||[]).includes(b.action);};
 function retain(){try{sessionStorage.setItem('Friday.interactionDraft',JSON.stringify(draft));}catch(_){}}
 function openCamera(id){retain();try{sessionStorage.setItem('Friday.cameraFocus',id);}catch(_){}go('Cameras');}
 function stopPreview(){const token=previewToken,id=previewCamera;previewToken=null;previewCamera=null;previewLive=false;if(token)void api(`/api/embodied/cameras/${id}/preview/stop`,{token}).catch(()=>{});}
 async function perform(run){if(busy)return;busy=true;try{await run();}catch(_){toast('设置未保存，请刷新状态后重试。',true);}finally{busy=false;if(alive)await refresh();}}
 async function save(bindings){const next={...config,enabled:true,bindings:bindings.map(b=>valid(b)?b:{...b,enabled:false})};config=await api('/api/home/interactions',next);draw();}
 function readiness(){
  const voiceExample=slot.querySelector?.('[data-voice-example]'),voiceCue=slot.querySelector?.('[data-voice-cue]');
  if(voicePreferences&&voiceExample&&voiceCue){voiceExample.textContent=`“${voicePreferences.wake_word}，把客厅的灯打开。”`;voiceCue.textContent=`看向镜头直接说话，也可以先叫 ${voicePreferences.wake_word}。`;}
  const chosen=draft.camera?cameras.filter(c=>c.id===draft.camera):cameras;
  const live=chosen.filter(c=>['live','ready'].includes(report.camera_sessions?.find(s=>s.camera===c.id)?.state));
  const ready=live.map(c=>cameraInputStatus(c.id)),available=ready.filter(s=>s.available),waiting=ready.length&&ready.every(s=>s.text===ready[0].text)?ready[0].text:'等待动作就绪';
  const line=slot.querySelector?.('[data-interaction-readiness]');
  if(line&&!line.contains?.(document.activeElement)){
   line.replaceChildren(h('strong',!policy?.enabled||!policy?.recognition?'手势识别未开启':!policy?.interaction?'手势控制已暂停':available.length?`${available.length} 个镜头可使用`:live.length?waiting:'镜头暂不可用'));
   if(!live.length){line.append(h('p','可以先保存设置。使用时，请打开所选房间的镜头。',{class:'caption'}));if(chosen[0])line.append(btn('查看'+chosen[0].room+'镜头',()=>openCamera(chosen[0].id)));}
  }
  const scopeStatus=slot.querySelector?.('[data-all-camera-summary]');if(scopeStatus)scopeStatus.textContent=cameras.length?`${live.length} / ${cameras.length} 个镜头可用`:'还没有接入摄像头';
  const toggle=slot.querySelector?.('[data-interaction-toggle]');if(toggle){toggle.textContent=mode==='voice'?(policy?.enabled&&policy?.recognition&&policy?.interaction&&report.voice_input?'暂停语音输入':'开启语音控制'):policy?.enabled&&policy?.recognition&&policy?.interaction?'暂停手势控制':'启用手势控制';toggle.disabled=busy;}
  const status=slot.querySelector?.('[data-camera-status]');if(status&&!status.contains?.(document.activeElement)){status.replaceChildren();for(const c of cameras){status.append(add(h('div',null,{class:'interaction-camera'}),h('strong',c.room+' · '+c.name),h('span',cameraInputStatus(c.id).text),btn('查看镜头',()=>openCamera(c.id))));}}
  const voiceState=slot.querySelector?.('[data-voice-state]');if(voiceState){
   const voices=(report.voice_sessions||[]).filter(v=>v.last_transcript);if(!voices.length&&report.home?.last_transcript)voices.push({...report.home,camera:report.camera});
   const rows=voices.slice(-3).map(voice=>({voice,room:cameras.find(c=>c.id===voice.camera)?.room||'家中',outcome:voiceCommandPresentation(voice)}));
   const summary=JSON.stringify([report.voice_input,rows.map(({voice,room,outcome})=>[voice.last_transcript,room,outcome])]);
   if(voiceState.voiceSummary!==summary){voiceState.voiceSummary=summary;voiceState.replaceChildren();for(const{voice,room,outcome}of rows){const row=add(h('div',null,{class:'voice-recent-row'}),h('span',room,{class:'caption'}),h('p',voice.last_transcript),h('span',outcome.text,{class:'voice-command-result','data-state':outcome.state}));if(outcome.recover)row.append(add(h('div',null,{class:'voice-command-recovery'}),btn('查看设备',()=>go('Devices')),btn('刷新结果',()=>refresh())));voiceState.append(row);}if(!rows.length)voiceState.append(h('p',report.voice_input?'还没有最近的语音指令':'语音输入已暂停',{class:'caption'}));}
  }
  const history=slot.querySelector?.('[data-interaction-history]');if(history){history.replaceChildren();const events=report.interactions?.events||[];for(const e of events.slice(-5).reverse()){const target=targetById(e.target),camera=cameras.find(c=>c.id===e.camera);history.append(h('p',`${camera?.room||'家中'} · ${target?.name||'设备'} · ${actionName(e.action)} · ${e.reached_target?'状态已更新':e.acknowledged?'指令已发送':'操作未完成'}`));}if(!events.length)history.append(h('p','做出已设置的手势后，可以在这里查看设备是否响应。',{class:'caption'}));}
 }
 async function refresh(){try{const[next,infra]=await Promise.all([api('/api/embodied/status'),api('/api/embodied/infrastructure')]);if(alive){report=next;policy=infra.policy;readiness();if(image?.isConnected&&previewCamera)void pollImage();}}catch(_){const line=slot.querySelector?.('[data-interaction-readiness]');if(line)line.replaceChildren(h('p','状态暂时无法读取，请刷新后重试。'),btn('重试',()=>refresh()));}}
 async function pollImage(){if(!image?.isConnected||!previewCamera)return;try{const result=await api(`/api/embodied/cameras/${previewCamera}/preview`);if(result.state==='live'){image.src=`/api/embodied/cameras/${previewCamera}/frame?t=${Date.now()}`;}else previewLive=false;}catch(_){previewLive=false;}}
 function draw(){if(!alive||!config)return;motionLibrary?.dispose();motionLibrary=null;selectedDiagram?.dispose();selectedDiagram=null;image=null;slot.replaceChildren();
  const intro=add(h('div',null,{class:'interaction-intro'}),add(h('div'),h('h2',mode==='voice'?'开口说，Friday 来处理':mode==='gestures'?'用一个动作，控制家中设备':'指向设备，确认操作'),h('p',mode==='voice'?'全屋镜头共同接收，无需切换房间。':'看懂动作，亲自试一次，再设置设备控制。',{class:'interaction-description'})),btn(mode==='voice'?'开启语音控制':'启用手势控制',()=>perform(async()=>{
   if(mode==='voice'){
    const enabled=!(policy?.enabled&&policy?.recognition&&policy?.interaction&&report.voice_input);
    if(enabled){const result=await api('/api/embodied/infrastructure',{...policy,enabled:true,recognition:true,interaction:true,auto_open_cameras:false});policy=result.policy;}
    await api('/api/embodied/voice-input',{enabled});toast(enabled?'全屋语音输入已开启':'语音输入已暂停');
   }else{const enabled=!(policy?.enabled&&policy?.recognition&&policy?.interaction);const result=await api('/api/embodied/infrastructure',{...policy,...(enabled?{enabled:true,recognition:true}:{}),interaction:enabled,auto_open_cameras:false});policy=result.policy;toast(enabled?'全屋手势控制已启用':'手势控制已暂停');}
  }),{'data-interaction-toggle':'',class:'primary'}));slot.append(intro);
  if(mode==='voice'){intro.setAttribute('class','interaction-intro interaction-intro-voice');const actions=h('div',null,{class:'product-actions interaction-intro-actions'});actions.append(btn('用文字说',()=>go('Ask Home')),intro.querySelector('[data-interaction-toggle]'));intro.append(actions);}
  const tabs=h('div',null,{class:'category-tabs','aria-label':'互动方式'});for(const[key,label]of [['voice','对镜头说话'],['gestures','手势与动作'],['pointing','指向设备']])tabs.append(btn(label,()=>{stopPreview();mode=key;editing=null;if(key==='pointing')anchor=null;draw();},{'aria-pressed':String(mode===key)}));
  if(mode==='gestures'){const toolbar=h('div',null,{class:'interaction-toolbar'});toolbar.append(tabs,intro.querySelector('[data-interaction-toggle]'));intro.remove();slot.append(toolbar);}else slot.append(tabs);
  if(mode==='voice'){
   const name=voicePreferences?.wake_word;
   const panel=h('section',null,{class:'voice-interaction'});
   const status=add(h('section',null,{class:'voice-response'}),h('h3','最近听到'),h('div',null,{'data-voice-state':'','aria-live':'polite'}));
   const reply=btn(report.quiet?'开启语音回复':'静音语音回复',()=>perform(async()=>{await api('/api/embodied/quiet',{quiet:!report.quiet});await refresh();draw();}));
   panel.append(status,add(h('div',null,{class:'product-actions'}),reply,btn('体验手势与动作',()=>{mode='gestures';draw();})));
   const example=add(h('div',null,{class:'voice-example'}),h('span',null,{class:'icon icon-microphone','aria-hidden':'true'}),h('p',name?`“${name}，把客厅的灯打开。”`:'“把客厅的灯打开。”',{'data-voice-example':''}),h('span',name?`看向镜头直接说话，也可以先叫 ${name}。`:'看向镜头直接说话。',{class:'caption','data-voice-cue':''}));panel.append(example);
   if(voicePreferences){
    const form=h('form',null,{class:'voice-wake-settings','aria-label':'语音唤醒词'}),input=h('input',null,{type:'text',minlength:'2',maxlength:'24',autocomplete:'off','aria-label':'唤醒词'}),commit=h('button','保存',{type:'submit','aria-label':'保存唤醒词'}),status=h('p','',{role:'status',class:'caption'});
    input.value=voiceDraft||name;status.hidden=true;commit.disabled=busy||input.value.trim()===name;
    input.oninput=()=>{voiceDraft=input.value;status.hidden=true;status.textContent='';commit.disabled=busy||input.value.trim()===voicePreferences.wake_word;};
    form.onsubmit=async event=>{event.preventDefault();if(busy||commit.disabled)return;busy=true;input.disabled=commit.disabled=true;status.hidden=false;status.textContent='正在保存…';let saved=false;
     try{const next=await api('/api/embodied/voice-preferences',{expected_revision:voicePreferences.revision,wake_word:input.value.trim()});if(alive){voicePreferences=next;voiceDraft=next.wake_word;saved=true;toast('唤醒词已保存');}}
     catch(error){if(alive){status.textContent=error.status===422?'请使用 2–24 个文字、字母或数字。':'唤醒词未保存，请检查后重试。';if(error.message==='voice_preferences_not_saved'){try{voicePreferences=await api('/api/embodied/voice-preferences');status.textContent=`当前唤醒词已改为“${voicePreferences.wake_word}”，确认后可重新保存。`;}catch{}}}}
     finally{busy=false;if(alive){if(saved)draw();else{input.disabled=false;commit.disabled=input.value.trim()===voicePreferences.wake_word;readiness();}}}
    };
    form.append(field('唤醒词',input),commit,status);panel.append(form);
   }else panel.append(h('p','唤醒词设置暂时无法读取。',{class:'caption'}),btn('重新读取唤醒词',async()=>{try{voicePreferences=await api('/api/embodied/voice-preferences');if(alive)draw();}catch{toast('唤醒词设置暂时无法读取。',true);}}));
   const scope=add(h('section',null,{class:'voice-scope'}),add(h('div',null,{class:'voice-scope-heading'}),h('h3','全屋摄像头'),h('span','正在读取状态…',{'data-all-camera-summary':'',class:'caption'})),h('section',null,{class:'interaction-cameras','data-camera-status':''}));panel.append(scope);
   if(!cameras.length)scope.append(h('p','接入摄像头后，即可在家中说出指令。',{class:'caption'}),btn('接入摄像头',()=>go('Cameras')));
   slot.append(panel);readiness();return;
  }
  if(mode==='gestures')motionLibrary=mountMotionLibrary(slot,{go,cameras,keys:null,heading:'选择一个动作',description:'',onChoose:key=>{draft.gesture=key;retain();configuring=true;const form=slot.querySelector('form');if(form)form.hidden=false;const field=slot.querySelector('[aria-label=动作]');if(field){field.value=key;field.onchange();slot.querySelector('form')?.scrollIntoView?.({block:'start',behavior:'auto'});}}});
  const form=h('form',null,{class:'interaction-setup','aria-label':editing?'编辑手势控制':'新建手势控制'});
  form.hidden=!editing&&!configuring;form.append(h('h3',editing?'编辑设备控制':'设置设备控制'));
  const scope=select([['','全屋已开启的镜头'],...cameras.map(c=>[c.id,c.room])],'使用范围');scope.value=cameras.some(c=>c.id===draft.camera)?draft.camera:'';draft.camera=scope.value;
  scope.onchange=()=>{stopPreview();draft.camera=scope.value;retain();if(mode==='pointing'){anchor=null;draw();}else{updateSummary();readiness();}};form.append(field('1 · 在哪里使用',scope),h('div',null,{class:'interaction-readiness','data-interaction-readiness':''}));
  const target=select([['','选择要控制的设备'],...targets.map(t=>[t.id,t.room+' · '+t.name])],'控制设备');target.value=draft.target;
  const gesture=select(Object.entries(gestures),'动作');gesture.value=gestures[draft.gesture]?draft.gesture:Object.keys(gestures)[0];draft.gesture=gesture.value;
  const action=select([],'执行动作'),help=h('p','',{class:'caption'}),gestureVisual=h('div',null,{class:'gesture-selection-preview'}),summary=h('p','',{class:'interaction-summary',role:'status'});
  function updateActions(){const t=targetById(target.value);action.replaceChildren();for(const value of t?.actions||[])action.append(h('option',actionName(value),{value}));if(!t)action.append(h('option','先选择设备',{value:''}));action.disabled=!t;if(t?.actions.includes(draft.action))action.value=draft.action;draft.target=target.value;draft.action=action.value;updateSummary();}
  function updateSummary(){draft.gesture=gesture.value;draft.action=action.value;selectedDiagram?.dispose();gestureVisual.replaceChildren();selectedDiagram=createMotionDiagram(gesture.value);gestureVisual.append(selectedDiagram);help.textContent=MOTION_GUIDES[gesture.value]?.instruction||gestureHelp[gesture.value]||'面对镜头做出所选动作，保持片刻后放松，再进行下一次控制。';const t=targetById(target.value);summary.textContent=t?`${draft.camera?cameras.find(c=>c.id===draft.camera)?.room:'全屋'} · ${mode==='pointing'?'指向设备并比 OK':gestures[gesture.value]||'所选手势'} → ${t.name}${actionName(action.value)}`:'选择设备后，这里会显示完整的控制方式。';retain();}
  target.onchange=()=>{if(mode==='pointing')anchor=null;updateActions();};gesture.onchange=updateSummary;action.onchange=updateSummary;
  form.append(field('2 · 控制哪个设备',target));
  if(mode==='pointing'){
   form.append(h('h4','3 · 在当前画面中标记设备'),h('p','点击设备所在位置，或填写下方的位置百分比。移动镜头后需要重新核对。',{class:'caption'}));
   const stage=h('div',null,{class:'pointing-stage'}),pin=h('span','设备位置',{class:'target-pin',hidden:''}),out=h('p','尚未查看镜头',{role:'status'});image=h('img',null,{alt:'用于标记设备位置的实时画面',hidden:''});add(stage,image,pin);form.append(stage,out);
   const x=h('input',null,{type:'number',min:0,max:100,step:1,'aria-label':'设备水平位置百分比'}),y=h('input',null,{type:'number',min:0,max:100,step:1,'aria-label':'设备垂直位置百分比'});
   const mark=()=>{if(anchor){pin.hidden=false;pin.style.left=anchor[0]*100+'%';pin.style.top=anchor[1]*100+'%';x.value=Math.round(anchor[0]*100);y.value=Math.round(anchor[1]*100);}};
   const typed=()=>{if(!previewLive)return;if(x.value!==''&&y.value!==''&&Number(x.value)>=0&&Number(x.value)<=100&&Number(y.value)>=0&&Number(y.value)<=100){anchor=[Number(x.value)/100,Number(y.value)/100];mark();}};x.oninput=typed;y.oninput=typed;
   image.onload=()=>{image.hidden=false;previewLive=true;out.textContent='点击画面中的设备位置';};image.onerror=()=>{previewLive=false;out.textContent='画面暂不可用，请重新查看。';};image.onclick=e=>{if(!previewLive)return;const r=image.getBoundingClientRect();anchor=[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];mark();};mark();
   form.append(btn('打开镜头并查看',()=>perform(async()=>{const chosen=draft.camera||cameras[0]?.id;if(!chosen)return;draft.camera=chosen;scope.value=chosen;let power=await api(`/api/embodied/cameras/${chosen}/power`);if(!power.enabled){power=await api(`/api/embodied/cameras/${chosen}/power`,{enabled:true,revision:power.revision,event_id:crypto.randomUUID()});if(power.status!=='reported_match')throw Error();}const result=await api(`/api/embodied/cameras/${chosen}/preview`,{});previewCamera=chosen;previewToken=result.preview_token;out.textContent='正在连接画面…';await pollImage();})),add(h('div',null,{class:'pointing-position-fields'}),field('水平位置 %',x),field('垂直位置 %',y)),field('4 · 执行动作',action));
  }else form.append(add(h('div',null,{class:'interaction-action-fields'}),field('3 · 做什么手势',gesture),field('执行动作',action)),gestureVisual,help);
  const saveButton=h('button',editing?'保存修改':'保存手势控制',{type:'submit',class:'primary'});form.append(summary,add(h('div',null,{class:'product-actions'}),saveButton,btn(editing?'取消编辑':'取消',()=>{editing=null;configuring=false;draw();})));
  form.onsubmit=e=>{e.preventDefault();if(!target.value){toast('请先选择要控制的设备',true);target.focus?.();return;}if(mode==='pointing'&&(!anchor||!previewLive&&!editing)){toast('请先查看镜头，并标记设备位置',true);return;}const binding={id:editing||crypto.randomUUID().replaceAll('-',''),camera:draft.camera||null,scope:draft.camera?'camera':'home',gesture:mode==='pointing'?'hand.point':gesture.value,target:target.value,action:action.value,enabled:true,anchor:mode==='pointing'?anchor:null};if(config.bindings.some(b=>b.id!==editing&&b.gesture===binding.gesture&&b.target===binding.target&&b.camera===binding.camera)){toast('这个手势已用于该设备，请编辑已有控制。',true);return;}saveButton.disabled=true;void perform(async()=>{await save([...config.bindings.filter(b=>b.id!==editing),binding]);editing=null;configuring=false;undo=null;stopPreview();toast('手势控制已保存');draw();}).finally(()=>saveButton.disabled=false);};
  slot.append(form);updateActions();
  const list=add(h('section',null,{class:'interaction-list'}),add(h('div',null,{class:'interaction-list-heading'}),h('h2','设备控制'),btn('添加设备控制',()=>{configuring=true;form.hidden=false;form.scrollIntoView?.({block:'start',behavior:'auto'});target.focus?.();})));
  if(!config.bindings.length)list.append(h('p','还没有手势控制。添加一个动作，再选择它要控制的设备。',{class:'empty-binding'}));
  for(const b of config.bindings){const t=targetById(b.target),available=valid(b),row=h('article',null,{class:'interaction-binding'+(!available?' needs-repair':'')});const title=(b.gesture==='hand.point'?'指向设备':gestures[b.gesture]||'手势')+' → '+(t?.name||'重新选择设备');row.append(add(h('div'),h('h3',title),h('p',available?`${b.scope==='camera'||b.gesture==='hand.point'?cameras.find(c=>c.id===b.camera)?.room||'所选房间':'全屋'} · ${actionName(b.action)}`:'原设备已不可用，需要重新选择设备。',{class:'caption'})));
   row.append(btn(available?'编辑':'重新选择设备',()=>{stopPreview();editing=b.id;mode=b.gesture==='hand.point'?'pointing':'gestures';draft={camera:b.scope==='camera'||mode==='pointing'?b.camera||'':'',target:available?b.target:'',gesture:b.gesture,action:b.action};anchor=b.anchor;draw();slot.querySelector?.('form')?.scrollIntoView?.({block:'start',behavior:'auto'});}));
   if(available)row.append(btn(b.enabled?'已启用':'已暂停',()=>perform(()=>save(config.bindings.map(x=>x.id===b.id?{...x,enabled:!x.enabled}:x))),{role:'switch','aria-checked':String(b.enabled),'aria-label':title+'是否启用'}));else row.append(h('span','设备不可用',{class:'binding-unavailable'}));
   row.append(btn('移除',()=>perform(async()=>{undo=b;await save(config.bindings.filter(x=>x.id!==b.id));toast('已移除手势控制，可撤销');})));list.append(row);
  }
  if(undo)list.append(add(h('p',null,{class:'binding-undo'}),h('span','已移除一条控制。'),btn('撤销移除',()=>perform(async()=>{const item=undo;undo=null;await save([...config.bindings,{...item,enabled:valid(item)&&item.enabled}]);}))));slot.append(list);
  const more=add(h('details',null,{class:'interaction-more'}),h('summary','更多控制方式与镜头状态'));
  more.append(add(h('div',null,{class:'product-actions'}),btn('常用手势',()=>{stopPreview();mode='gestures';editing=null;draw();}),btn('指向设备',()=>{stopPreview();mode='pointing';editing=null;anchor=null;draw();})));
  more.append(h('section',null,{class:'interaction-cameras','data-camera-status':''}),h('p','一人面对镜头时可直接使用；多人时，比 OK 选择自己。登记家人姓名后可跨房间识别。',{class:'caption'}),btn('管理家人',()=>go('People')));slot.append(more);
  const recent=add(h('details',null,{class:'interaction-recent'}),h('summary','最近的手势响应'),h('div',null,{'data-interaction-history':'','aria-live':'polite'}));slot.append(recent);readiness();
 }
 async function load(){slot.replaceChildren(h('p','正在读取互动设置…',{role:'status'}));try{const[r,inventory,s,p,v]=await Promise.all([api('/api/home/interactions'),api('/api/embodied/devices'),api('/api/embodied/status'),api('/api/embodied/infrastructure'),api('/api/embodied/voice-preferences').catch(()=>null)]);if(!alive)return;config=r.config;gestures=r.gestures;voicePreferences=v;const product=new Map(data.devices.flatMap(d=>(d.controls||[]).map(c=>[c.id,{name:c.name,room:d.room}])));targets=inventory.devices.filter(t=>t.controllable&&t.actions?.length).map(t=>({...t,...product.get(t.id)}));report=s;policy=p.policy;draw();}catch(_){if(alive)slot.replaceChildren(h('p','互动设置暂时无法读取，请检查家庭连接后重试。',{role:'status'}),btn('重试',load));}}
 try{if(sessionStorage.getItem('Friday.interactionMode')==='gestures'){mode='gestures';sessionStorage.removeItem('Friday.interactionMode');}}catch(_){}
 void load();const timer=setInterval(()=>{if(!busy)void refresh();},1800);
 return{dispose(){alive=false;clearInterval(timer);stopPreview();motionLibrary?.dispose();selectedDiagram?.dispose();slot.remove();}};
}
