const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...c)=>{c.forEach(x=>n.append(x));return n;};
const btn=(text,f,attrs={})=>{const n=h('button',text,{type:'button',...attrs});n.onclick=f;return n;};
const api=async(path,body)=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw Error(d.detail||'操作未完成');return d;};
const states={live:'识别中',ready:'识别中',connecting:'连接中',waiting:'等待画面',stopped:'已停止',unavailable:'正在重连',shutter_closed:'镜头遮蔽',stale:'画面中断'};
const actionOptions=item=>(item?.actions||[]).map(a=>[a,({on:'开启',off:'关闭',open:'打开',close:'关闭','mode:cool':'制冷','mode:heat':'制热','mode:auto':'自动','mode:heat_cool':'自动冷暖','mode:dry':'除湿','mode:fan_only':'送风'}[a]|| (a.startsWith('position:')?'开到 '+a.slice(9)+'%':a))]);
export function mountInteractions(root,{data,go,toast}){
 let alive=true,config=null,tab='gestures',camera=data.devices.find(x=>x.kind==='camera')?.id,anchor=null,targets=[],gestures={},report={},policy=null,busy=false;
 let image=null,history=null,cameraStatus=null,runtimeButton=null,voiceButton=null,muteButton=null;
 const cameras=data.devices.filter(d=>d.kind==='camera'),slot=h('div',null,{class:'interaction-workspace'});root.append(slot);
 const select=(items,label)=>{const n=h('select',null,{'aria-label':label});items.forEach(([v,t])=>n.append(h('option',t,{value:v})));return n;};
 const field=(label,input)=>add(h('label',null,{class:'product-field'}),h('span',label),input);
 async function perform(run){if(busy)return;busy=true;update();try{await run();await refresh();}catch(e){toast('操作未完成：'+e.message,true);}finally{busy=false;update();}}
 async function save(next){config=await api('/api/home/interactions',next);toast('互动设置已保存');draw();}
 function update(){if(!alive||!config)return;
  if(runtimeButton){runtimeButton.textContent=busy?'正在连接…':policy?.interaction?'暂停互动':'恢复互动';runtimeButton.disabled=busy;runtimeButton.setAttribute('aria-pressed',String(!!policy?.interaction));}
  if(voiceButton){voiceButton.textContent=report.voice_input?'语音指令：开':'语音指令：关';voiceButton.setAttribute('aria-pressed',String(!!report.voice_input));voiceButton.disabled=busy;}
  if(muteButton){muteButton.textContent=report.quiet?'语音回复：静音':'语音回复：开';muteButton.setAttribute('aria-pressed',String(!report.quiet));muteButton.disabled=busy;}
  if(cameraStatus){cameraStatus.replaceChildren();for(const c of cameras){const s=report.camera_sessions?.find(x=>x.camera===c.id),interaction=report.interactions?.cameras?.find(x=>x.camera===c.id),operator=interaction?.operator;
   cameraStatus.append(add(h('div',null,{class:'interaction-camera','data-state':s?.state||'stopped'}),h('strong',c.room),h('span',states[s?.state]||'已停止'),h('small',operator!=null?'动作控制已就绪':s?.state==='live'?(interaction?.people>1?'比 OK 选择操作者':'单人自动响应'):'连接后自动参与互动')));}}
  if(history){const events=report.interactions?.events||[];history.replaceChildren();for(const e of events.slice(-3).reverse()){const t=targets.find(x=>x.id===e.target),c=cameras.find(x=>x.id===e.camera);history.append(h('p',`${c?.room||'摄像头'} · ${t?.name||'设备'} · ${actionOptions(t).find(x=>x[0]===e.action)?.[1]||e.action} · ${e.reached_target?'状态已更新':e.acknowledged?'指令已发送':'未完成'}`));}if(!events.length)history.append(h('p','识别到动作后，这里会显示设备的响应。',{class:'caption'}));}
  if(image?.isConnected&&report.owned)image.src=`/api/embodied/cameras/${camera}/frame?t=${Date.now()}`;
 }
 function draw(){image=null;voiceButton=null;muteButton=null;slot.replaceChildren();
  const intro=add(h('div',null,{class:'interaction-intro'}),add(h('div'),h('h2','举手之间，家就回应'),h('p','后台持续运行，随时响应你的动作。',{class:'interaction-description'})));
  runtimeButton=btn('',()=>perform(async()=>{const next=await api('/api/embodied/infrastructure',{...policy,interaction:!policy.interaction});policy=next.policy;toast(policy.interaction?'互动已恢复':'互动已暂停，人物识别继续运行');}),{class:'primary'});intro.append(runtimeButton);slot.append(intro);
  cameraStatus=h('section',null,{class:'interaction-cameras','aria-label':'全屋摄像头识别状态'});slot.append(cameraStatus);
  slot.append(h('p','一人在镜头前可直接控制；多人时，比 OK 选择自己。人物登记用于跨房间识别，无需登记也能使用动作控制。',{class:'interaction-description'}));
  const tabs=h('div',null,{class:'interaction-tabs'});for(const[k,v]of [['gestures','动作绑定'],['pointing','指哪儿控哪儿'],['gaze','注视与语音']])tabs.append(btn(v,()=>{tab=k;anchor=null;draw();},{'aria-pressed':String(tab===k)}));slot.append(tabs);
  if(tab==='gaze'){
   add(slot,h('h2','看向 Friday，说出需求'),h('p','面对任意镜头，说“打开客厅灯”或“把梦幻帘开到一半”。未确认注视时，先说 Friday。',{class:'interaction-description'}));
   voiceButton=btn('',()=>perform(()=>api('/api/embodied/voice-input',{enabled:!report.voice_input})));muteButton=btn('',()=>perform(()=>api('/api/embodied/quiet',{quiet:!report.quiet})));
   add(slot,add(h('div',null,{class:'product-actions'}),voiceButton,muteButton),h('p','语音指令与语音回复分别设置。静音时仍能识别指令；多个镜头共用本地语音模型，每次只接收一个镜头的声音。',{class:'caption'}));
   slot.append(h('p',report.home?.voice==='ready'?'本地语音已就绪':report.owned?'语音模型正在准备，稍后可说出指令':'开启全屋互动后即可使用',{class:'interaction-description'}));
  }else{
   const point=tab==='pointing';slot.append(h('h2',point?'标记画面中的设备':'把动作变成家中的开关'));
   if(point){slot.append(h('p','每个镜头分别标记设备位置。指向目标并保持片刻，再比 OK 确认。移动镜头后，空间控制会等待视角恢复。',{class:'interaction-description'}));
    const cameraSelect=select(cameras.map(d=>[d.id,d.room+' · '+d.name]),'查看与标定镜头');cameraSelect.value=camera;cameraSelect.onchange=()=>{camera=cameraSelect.value;anchor=null;draw();};slot.append(field('查看与标定镜头',cameraSelect));
    const stage=h('div',null,{class:'pointing-stage'}),pin=h('span','设备位置',{class:'target-pin',hidden:''});image=h('img',null,{alt:'点击实时画面中的设备位置',hidden:''});const waiting=h('p','等待实时画面',{class:'camera-placeholder'});image.onload=()=>{image.hidden=false;waiting.hidden=true;};image.onerror=()=>{image.hidden=true;waiting.hidden=false;waiting.textContent='画面暂不可用，请检查镜头隐私或连接';anchor=null;pin.hidden=true;};add(stage,image,waiting,pin);slot.append(stage);
    image.onclick=e=>{if(!image.naturalWidth)return;const r=image.getBoundingClientRect();anchor=[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];pin.hidden=false;pin.style.left=anchor[0]*100+'%';pin.style.top=anchor[1]*100+'%';};
    slot.append(h('p','这里切换的是查看画面，所有镜头始终共同识别。',{class:'caption'}));
   }
   const form=h('form',null,{class:'binding-row'}),gesture=select(Object.entries(gestures),'动作'),target=select([['','选择设备'],...targets.map(t=>[t.id,t.room+' · '+t.name])],'控制设备'),action=select([],'执行动作');
   const redrawAction=()=>{action.replaceChildren();const options=actionOptions(targets.find(t=>t.id===target.value));action.disabled=!options.length;if(!options.length)action.append(h('option','先选择设备',{value:''}));options.forEach(([v,n])=>action.append(h('option',n,{value:v})));};target.onchange=redrawAction;redrawAction();if(!point)form.append(field('当我',gesture));add(form,field('控制设备',target),field('就执行',action),h('button','添加绑定',{type:'submit',class:'primary'}));slot.append(form);
   form.onsubmit=e=>{e.preventDefault();if(!target.value){toast('请选择设备',true);return;}if(point&&!anchor){toast('请先在实时画面中标记设备位置',true);return;}void perform(()=>save({...config,enabled:true,bindings:[...config.bindings,{id:crypto.randomUUID().replaceAll('-',''),camera:point?camera:null,gesture:point?'hand.point':gesture.value,target:target.value,action:action.value,enabled:true,anchor:point?anchor:null}]}));};
   const list=h('section',null,{class:'interaction-list'}),matching=config.bindings.filter(b=>(b.gesture==='hand.point')===point&&(!point||b.camera===camera));
   if(!matching.length)list.append(h('p',point?'还没有标记指向目标':'还没有动作绑定。选一个动作和设备，立即开始。',{class:'empty-binding'}));
   for(const b of matching){const t=targets.find(t=>t.id===b.target);list.append(add(h('article',null,{class:'installed-rule'}),h('h3',(point?'指向':gestures[b.gesture]||b.gesture)+' → '+(t?.name||'设备已移除')),h('span',(point?'当前镜头':'全屋镜头')+' · '+(actionOptions(t).find(x=>x[0]===b.action)?.[1]||b.action)),btn(b.enabled?'已开启':'已停用',()=>perform(()=>save({...config,bindings:config.bindings.map(x=>x.id===b.id?{...x,enabled:!x.enabled}:x)}))),btn('移除',()=>perform(()=>save({...config,bindings:config.bindings.filter(x=>x.id!==b.id)})))));}
   slot.append(list);
  }
  history=h('section',null,{class:'interaction-recent','aria-live':'polite'});slot.append(h('h3','最近响应'),history,add(h('div',null,{class:'product-actions'}),btn('人物与登记',()=>go('People')),btn('体感游戏',()=>go('Games'))));update();
 }
 async function refresh(){try{const [next,infra]=await Promise.all([api('/api/embodied/status'),api('/api/embodied/infrastructure')]);if(alive){report=next;policy=infra.policy;update();}}catch{if(alive&&cameraStatus)cameraStatus.textContent='连接中断，正在重试';}}
 Promise.all([api('/api/home/interactions'),api('/api/embodied/devices'),api('/api/embodied/status'),api('/api/embodied/infrastructure')]).then(([r,i,s,p])=>{if(!alive)return;config=r.config;gestures=r.gestures;targets=i.devices.filter(x=>x.controllable&&x.actions?.length);report=s;policy=p.policy;draw();}).catch(()=>slot.append(h('p','互动设置暂不可读，请检查家庭连接。')));
 const timer=setInterval(()=>{if(!busy)void refresh();},1500);
 return{dispose(){alive=false;clearInterval(timer);slot.remove();}};
}
