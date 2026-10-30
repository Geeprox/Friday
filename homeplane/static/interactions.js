const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{children.forEach(c=>c&&n.append(c));return n;};
const btn=(text,run,attrs={})=>{const n=h('button',text,{type:'button',...attrs});n.onclick=run;return n;};
const api=async(path,body)=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});const data=await r.json();if(!r.ok)throw Error(data.detail||'unavailable');return data;};
const actionName=value=>({on:'开启',off:'关闭',open:'打开',close:'关闭','mode:off':'关闭','mode:cool':'制冷','mode:heat':'制热','mode:auto':'自动','mode:heat_cool':'自动冷暖','mode:dry':'除湿','mode:fan_only':'送风'}[value]||(value?.startsWith('position:')?'开到 '+value.slice(9)+'%':'调整设备'));
const states={live:'识别中',ready:'识别中',connecting:'正在连接',waiting:'等待画面',stopped:'未开启',unavailable:'连接中断',shutter_closed:'镜头已关闭',stale:'画面已中断'};
const gestureHelp={'body.arms_open':'面对镜头，将双臂向两侧打开并保持片刻。','body.arms_close':'将双臂向身体前方合拢并保持片刻。','body.hands_up':'将双手举过头顶并保持片刻。','hand.ok':'拇指与食指合成圆圈，其余手指伸开。','hand.open_palm':'面向镜头张开手掌，保持手掌清晰可见。','hand.yeah':'面向镜头伸出食指和中指。'};
export function mountInteractions(root,{data,go,toast}){
 let alive=true,config=null,targets=[],gestures={},report={},policy=null,busy=false,mode='voice',editing=null,undo=null,previewToken=null,previewCamera=null,image=null,previewLive=false,anchor=null;
 const cameras=data.devices.filter(d=>d.kind==='camera');let voiceCamera=cameras.find(c=>c.camera?.selected)?.id||cameras[0]?.id||'';const slot=h('div',null,{class:'interaction-workspace'});root.append(slot);
 let draft={camera:'',target:'',gesture:'body.arms_open',action:''};
 try{const saved=JSON.parse(sessionStorage.getItem('Friday.interactionDraft')||'null');if(saved&&typeof saved==='object')for(const key of Object.keys(draft))if(typeof saved[key]==='string')draft[key]=saved[key];}catch(_){}
 const select=(items,label)=>{const n=h('select',null,{'aria-label':label});for(const[value,text]of items)n.append(h('option',text,{value}));return n;};
 const field=(label,input)=>add(h('label',null,{class:'product-field'}),h('span',label),input);
 const targetById=id=>targets.find(t=>t.id===id),valid=b=>{const t=targetById(b.target);return t&&(t.actions||[]).includes(b.action);};
 function retain(){try{sessionStorage.setItem('Friday.interactionDraft',JSON.stringify(draft));}catch(_){}}
 function openCamera(id){retain();try{sessionStorage.setItem('Friday.cameraFocus',id);}catch(_){}go('Cameras');}
 function stopPreview(){const token=previewToken,id=previewCamera;previewToken=null;previewCamera=null;previewLive=false;if(token)void api(`/api/embodied/cameras/${id}/preview/stop`,{token}).catch(()=>{});}
 async function perform(run){if(busy)return;busy=true;try{await run();}catch(_){toast('设置未保存，请刷新状态后重试。',true);}finally{busy=false;if(alive)await refresh();}}
 async function save(bindings){const next={...config,enabled:true,bindings:bindings.map(b=>valid(b)?b:{...b,enabled:false})};config=await api('/api/home/interactions',next);draw();}
 function readiness(){
  const chosen=draft.camera?cameras.filter(c=>c.id===draft.camera):cameras;
  const live=chosen.filter(c=>['live','ready'].includes(report.camera_sessions?.find(s=>s.camera===c.id)?.state));
  const line=slot.querySelector?.('[data-interaction-readiness]');
  if(line&&!line.contains?.(document.activeElement)){
   line.replaceChildren(h('strong',!policy?.enabled||!policy?.recognition?'手势识别未开启':!policy?.interaction?'手势控制已暂停':live.length?`${live.length} 个房间可使用`:'镜头暂不可用'));
   if(!live.length){line.append(h('p','可以先保存设置。使用时，请打开所选房间的镜头。',{class:'caption'}));if(chosen[0])line.append(btn('查看'+chosen[0].room+'镜头',()=>openCamera(chosen[0].id)));}
  }
  const toggle=slot.querySelector?.('[data-interaction-toggle]');if(toggle){toggle.textContent=mode==='voice'?(policy?.enabled&&policy?.recognition&&policy?.interaction&&report.voice_input?'暂停语音输入':'开启语音控制'):policy?.enabled&&policy?.recognition&&policy?.interaction?'暂停手势控制':'启用手势控制';toggle.disabled=busy;}
  const status=slot.querySelector?.('[data-camera-status]');if(status&&!status.contains?.(document.activeElement)){status.replaceChildren();for(const c of cameras){const s=report.camera_sessions?.find(s=>s.camera===c.id);status.append(add(h('div',null,{class:'interaction-camera'}),h('strong',c.room),h('span',states[s?.state]||'未开启'),btn('查看镜头',()=>openCamera(c.id))));}}
  const voiceState=slot.querySelector?.('[data-voice-state]');if(voiceState){const voice=report.voice_sessions?.find(v=>v.camera===voiceCamera)||(report.camera===voiceCamera?report.home:null);voiceState.replaceChildren();if(voice?.last_transcript){voiceState.append(h('p','听到：'+voice.last_transcript),h('p',voice.reason||'正在处理这条指令…'));}else voiceState.append(h('p',report.voice_input?'说出指令后，这里会显示听到的内容和设备的响应。':'语音输入已暂停，开启后再对着镜头说话。',{class:'caption'}));}
  const history=slot.querySelector?.('[data-interaction-history]');if(history){history.replaceChildren();const events=report.interactions?.events||[];for(const e of events.slice(-5).reverse()){const target=targetById(e.target),camera=cameras.find(c=>c.id===e.camera);history.append(h('p',`${camera?.room||'家中'} · ${target?.name||'设备'} · ${actionName(e.action)} · ${e.reached_target?'状态已更新':e.acknowledged?'指令已发送':'操作未完成'}`));}if(!events.length)history.append(h('p','做出已设置的手势后，可以在这里查看设备是否响应。',{class:'caption'}));}
 }
 async function refresh(){try{const[next,infra]=await Promise.all([api('/api/embodied/status'),api('/api/embodied/infrastructure')]);if(alive){report=next;policy=infra.policy;readiness();if(image?.isConnected&&previewCamera)void pollImage();}}catch(_){const line=slot.querySelector?.('[data-interaction-readiness]');if(line)line.replaceChildren(h('p','状态暂时无法读取，请刷新后重试。'),btn('重试',()=>refresh()));}}
 async function pollImage(){if(!image?.isConnected||!previewCamera)return;try{const result=await api(`/api/embodied/cameras/${previewCamera}/preview`);if(result.state==='live'){image.src=`/api/embodied/cameras/${previewCamera}/frame?t=${Date.now()}`;}else previewLive=false;}catch(_){previewLive=false;}}
 function draw(){if(!alive||!config)return;image=null;slot.replaceChildren();
  const intro=add(h('div',null,{class:'interaction-intro'}),add(h('div'),h('h2','对着镜头，控制家里的设备'),h('p','说出设备和操作，或使用你设置好的手势。',{class:'interaction-description'})),btn(mode==='voice'?'开启语音控制':'启用手势控制',()=>perform(async()=>{
   if(mode==='voice'){
    const enabled=!(policy?.enabled&&policy?.recognition&&policy?.interaction&&report.voice_input);
    if(enabled){const result=await api('/api/embodied/infrastructure',{...policy,enabled:true,recognition:true,interaction:true,auto_open_cameras:false});policy=result.policy;}
    await api('/api/embodied/voice-input',{enabled});toast(enabled?'语音控制已开启，请打开要使用的镜头':'语音输入已暂停');
   }else{const enabled=!(policy?.enabled&&policy?.recognition&&policy?.interaction);const result=await api('/api/embodied/infrastructure',{...policy,...(enabled?{enabled:true,recognition:true}:{}),interaction:enabled,auto_open_cameras:false});policy=result.policy;toast(enabled?'手势控制已启用，已关闭的镜头保持关闭':'手势控制已暂停');}
  }),{'data-interaction-toggle':'',class:'primary'}));slot.append(intro);
  const tabs=h('div',null,{class:'category-tabs','aria-label':'互动方式'});for(const[key,label]of [['voice','对镜头说话'],['gestures','手势控制'],['pointing','指向设备']])tabs.append(btn(label,()=>{stopPreview();mode=key;editing=null;if(key==='pointing')anchor=null;draw();},{'aria-pressed':String(mode===key)}));slot.append(tabs);
  if(mode==='voice'){
   const panel=h('section',null,{class:'voice-interaction'}),selectCamera=select(cameras.map(c=>[c.id,c.room]),'语音使用的镜头');selectCamera.value=voiceCamera;selectCamera.onchange=()=>{voiceCamera=selectCamera.value;draw();};panel.append(field('1 · 选择你所在的房间',selectCamera));
   const cameraState=h('p','正在读取镜头状态…',{class:'voice-camera-state',role:'status'}),wake=btn('唤醒镜头',()=>perform(async()=>{const before=await api(`/api/embodied/cameras/${voiceCamera}/power`);if(!before.enabled){const result=await api(`/api/embodied/cameras/${voiceCamera}/power`,{enabled:true,revision:before.revision,event_id:crypto.randomUUID()});if(result.status!=='reported_match')throw Error();}draw();}));wake.disabled=true;panel.append(add(h('div',null,{class:'voice-camera-ready'}),cameraState,wake));
   void api(`/api/embodied/cameras/${voiceCamera}/power`).then(power=>{if(!cameraState.isConnected)return;cameraState.textContent=power.enabled?'镜头已打开':'镜头已遮蔽，使用前请唤醒';wake.hidden=power.enabled;wake.disabled=false;}).catch(()=>{if(cameraState.isConnected){cameraState.textContent='镜头状态暂不可用，请查看镜头后重试。';wake.hidden=true;}});
   panel.append(h('h3','2 · 开启语音控制'),h('p','点击上方“开启语音控制”。镜头需要保持打开，Friday 才能接收这个房间的声音。',{class:'caption'}),h('h3','3 · 面对镜头说出指令'),h('p','例如：“Friday，打开客厅主灯”或“Friday，把主卧空调设为 24 度”。'),h('p','说清设备名称；如果有多个同类设备，请加上房间。没有听清或无法确定设备时，Friday 会请你补充。',{class:'caption'}));
   const status=add(h('section',null,{class:'voice-response'}),h('h3','听到与执行'),h('div',null,{'data-voice-state':'','aria-live':'polite'}));panel.append(status);
   const reply=btn(report.quiet?'开启语音回复':'静音语音回复',()=>perform(async()=>{await api('/api/embodied/quiet',{quiet:!report.quiet});await refresh();draw();}));panel.append(add(h('div',null,{class:'product-actions'}),reply,btn('查看所选镜头',()=>openCamera(voiceCamera))));slot.append(panel);readiness();return;
  }
  const form=h('form',null,{class:'interaction-setup','aria-label':editing?'编辑手势控制':'新建手势控制'});
  form.append(h('h3',editing?'编辑手势控制':'新建手势控制'));
  const scope=select([['','全屋已开启的镜头'],...cameras.map(c=>[c.id,c.room])],'使用范围');scope.value=cameras.some(c=>c.id===draft.camera)?draft.camera:'';draft.camera=scope.value;
  scope.onchange=()=>{stopPreview();draft.camera=scope.value;retain();if(mode==='pointing'){anchor=null;draw();}else{updateSummary();readiness();}};form.append(field('1 · 在哪里使用',scope),h('div',null,{class:'interaction-readiness','data-interaction-readiness':''}));
  const target=select([['','选择要控制的设备'],...targets.map(t=>[t.id,t.room+' · '+t.name])],'控制设备');target.value=draft.target;
  const gesture=select(Object.entries(gestures),'动作');gesture.value=gestures[draft.gesture]?draft.gesture:Object.keys(gestures)[0];draft.gesture=gesture.value;
  const action=select([],'执行动作'),help=h('p','',{class:'caption'}),summary=h('p','',{class:'interaction-summary',role:'status'});
  function updateActions(){const t=targetById(target.value);action.replaceChildren();for(const value of t?.actions||[])action.append(h('option',actionName(value),{value}));if(!t)action.append(h('option','先选择设备',{value:''}));action.disabled=!t;if(t?.actions.includes(draft.action))action.value=draft.action;draft.target=target.value;draft.action=action.value;updateSummary();}
  function updateSummary(){draft.gesture=gesture.value;draft.action=action.value;help.textContent=gestureHelp[gesture.value]||'面对镜头做出所选动作，保持片刻后放松，再进行下一次控制。';const t=targetById(target.value);summary.textContent=t?`${draft.camera?cameras.find(c=>c.id===draft.camera)?.room:'全屋'} · ${mode==='pointing'?'指向设备并比 OK':gestures[gesture.value]||'所选手势'} → ${t.name}${actionName(action.value)}`:'选择设备后，这里会显示完整的控制方式。';retain();}
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
  }else form.append(add(h('div',null,{class:'interaction-action-fields'}),field('3 · 做什么手势',gesture),field('执行动作',action)),help);
  const saveButton=h('button',editing?'保存修改':'保存手势控制',{type:'submit',class:'primary'});form.append(summary,add(h('div',null,{class:'product-actions'}),saveButton,editing?btn('取消编辑',()=>{editing=null;draw();}):null));
  form.onsubmit=e=>{e.preventDefault();if(!target.value){toast('请先选择要控制的设备',true);target.focus?.();return;}if(mode==='pointing'&&(!anchor||!previewLive&&!editing)){toast('请先查看镜头，并标记设备位置',true);return;}const binding={id:editing||crypto.randomUUID().replaceAll('-',''),camera:draft.camera||null,scope:draft.camera?'camera':'home',gesture:mode==='pointing'?'hand.point':gesture.value,target:target.value,action:action.value,enabled:true,anchor:mode==='pointing'?anchor:null};if(config.bindings.some(b=>b.id!==editing&&b.gesture===binding.gesture&&b.target===binding.target&&b.camera===binding.camera)){toast('这个手势已用于该设备，请编辑已有控制。',true);return;}saveButton.disabled=true;void perform(async()=>{await save([...config.bindings.filter(b=>b.id!==editing),binding]);editing=null;undo=null;stopPreview();toast('手势控制已保存');draw();}).finally(()=>saveButton.disabled=false);};
  slot.append(form);updateActions();
  const list=add(h('section',null,{class:'interaction-list'}),h('h2','已设置的控制'));
  if(!config.bindings.length)list.append(h('p','还没有手势控制。保存上面的设置后，就会显示在这里。',{class:'empty-binding'}));
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
 async function load(){slot.replaceChildren(h('p','正在读取手势控制…',{role:'status'}));try{const[r,inventory,s,p]=await Promise.all([api('/api/home/interactions'),api('/api/embodied/devices'),api('/api/embodied/status'),api('/api/embodied/infrastructure')]);if(!alive)return;config=r.config;gestures=r.gestures;const product=new Map(data.devices.flatMap(d=>(d.controls||[]).map(c=>[c.id,{name:c.name,room:d.room}])));targets=inventory.devices.filter(t=>t.controllable&&t.actions?.length).map(t=>({...t,...product.get(t.id)}));report=s;policy=p.policy;draw();}catch(_){if(alive)slot.replaceChildren(h('p','手势设置暂时无法读取，请检查家庭连接后重试。',{role:'status'}),btn('重试',load));}}
 void load();const timer=setInterval(()=>{if(!busy)void refresh();},1800);
 return{dispose(){alive=false;clearInterval(timer);stopPreview();slot.remove();}};
}
