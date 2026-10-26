import {Capture} from './capture.js';
const $ = id => document.getElementById(id);
let homeState, reviewedPlan, seenResult, state, capture, states = [], binding = null, drawing = null, changing = false, refreshing = false;
const canvas = $('scene'), ctx = canvas.getContext('2d');
const reasonText = {
  plan_verified:'计划执行完成，设备状态已确认。',plan_error:'接入、规划或能力校验失败。',plan_ignored:'未满足执行条件，已忽略。',plan_clarify:'目标或指令需要进一步明确。',plan_cancelled:'计划已取消。',plan_partial:'部分步骤完成，后续步骤已停止。',plan_unverified:'设备动作结果尚未确认。',plan_skipped:'现场条件未满足，未执行。',plan_confirmation_required:'具体操作计划等待确认。',

  unsupported_command:'不是支持的灯光指令，已忽略。', paused:'请先开始体验。', say_friday:'当前模式需要先说 Friday。',
  attention_uncertain:'没有足够证据表明你在发出指令，已忽略。', target_ambiguous:'你指的是哪盏灯？请说名称或指向它。',
  pointing_ambiguous:'指向不够明确，请重新指向已绑定的灯。', unknown_or_ambiguous_name:'没有找到唯一对应的灯，请使用已登记的名称。',
  brightness_unavailable_or_light_off:'灯未开启或不支持读取亮度，请先开灯。', state_confirmed:'设备状态已确认。', already_at_target:'设备已经是目标状态。',
  state_not_confirmed:'指令已发送，但尚未确认设备状态。', duplicate_event:'重复指令已忽略。', cooldown:'指令过于接近，已忽略重复触发。',
  dispatch_result_unknown:'连接中断，无法确定指令是否已执行；请查看灯的实际状态。',
  adapter_failure:'设备读取或控制失败，请检查连接。', cancelled_before_dispatch:'监听已停止或指令过期，已取消执行。',
  stale_or_wrong_camera:'输入已过期或来自其他摄像头。', camera_view_not_fixed:'请先固定摄像头视角，再绑定指向目标。'
};
const statusText = {confirmation_required:'请确认计划',partial:'部分完成',cancelled:'已取消',skipped:'条件未满足',simulated:'模拟执行',verified:'已确认',unchanged:'无需更改',clarify:'需要澄清',ignored:'已忽略',error:'连接失败',unverified:'尚未确认'};
function feedback(text) {$('feedback').textContent = text;}
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Friday-Token':state.token},body:JSON.stringify(body)});
  if (!r.ok) {let message; try {message=(await r.json()).detail;}catch {} throw new Error(message || `请求失败 (${r.status})`);}
  return r.json();
}
function node(tag, text, className) {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;}
function drawDemo() {
  const w=canvas.width,h=canvas.height;
  ctx.fillStyle='#27312a';ctx.fillRect(0,0,w,h);
  ctx.fillStyle='#303a31';ctx.fillRect(90,70,690,320);
  ctx.fillStyle='#202b24';ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(90,70);ctx.lineTo(90,390);ctx.lineTo(0,h);ctx.fill();
  ctx.fillStyle='#222923';ctx.beginPath();ctx.moveTo(0,h);ctx.lineTo(90,390);ctx.lineTo(780,390);ctx.lineTo(w,h);ctx.fill();
  ctx.strokeStyle='#465047';ctx.lineWidth=1;
  for(let i=0;i<9;i++){ctx.beginPath();ctx.moveTo(90+i*85,390);ctx.lineTo(i*145-100,h);ctx.stroke();}
  ctx.fillStyle='#161f1a';ctx.fillRect(140,105,190,230);ctx.strokeStyle='#657361';ctx.strokeRect(144,109,182,222);
  ctx.strokeStyle='#354339';ctx.beginPath();ctx.moveTo(235,110);ctx.lineTo(235,331);ctx.moveTo(145,220);ctx.lineTo(326,220);ctx.stroke();
  ctx.fillStyle='#76806a';ctx.fillRect(418,139,150,111);ctx.fillStyle='#38473a';ctx.fillRect(427,148,132,93);
  ctx.fillStyle='#64715d';ctx.beginPath();ctx.arc(493,192,33,0,Math.PI*2);ctx.fill();
  const round=(x,y,w,h,r,color)=>{ctx.fillStyle=color;ctx.beginPath();ctx.roundRect(x,y,w,h,r);ctx.fill();};
  round(296,309,350,132,16,'#18231d');round(306,289,330,119,19,'#59624f');
  round(313,292,155,89,15,'#626a55');round(476,292,153,89,15,'#626a55');
  round(298,372,350,47,12,'#4a5444');round(282,338,43,89,13,'#515d4b');round(623,338,43,89,13,'#515d4b');
  round(345,327,77,52,12,'#8b8b70');round(539,324,65,56,11,'#394c3b');
  ctx.fillStyle='#31372c';ctx.beginPath();ctx.ellipse(477,470,162,32,0,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#6a7059';ctx.beginPath();ctx.ellipse(477,450,117,22,0,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#1d241d';ctx.fillRect(448,449,10,45);ctx.fillRect(537,449,9,37);
  ctx.fillStyle='#262f25';ctx.fillRect(421,436,43,8);ctx.fillStyle='#bac1a0';ctx.fillRect(427,431,39,5);
  const floor=states.find(s=>s.id==='floor-lamp'),ceiling=states.find(s=>s.id==='ceiling-light');
  if(floor?.on){const glow=ctx.createRadialGradient(758,165,5,758,200,220);glow.addColorStop(0,`rgba(229,223,151,${.12+(floor.brightness||70)/250})`);glow.addColorStop(1,'rgba(229,223,151,0)');ctx.fillStyle=glow;ctx.fillRect(550,50,410,470);}
  ctx.fillStyle='#161e18';ctx.fillRect(758,161,7,273);ctx.beginPath();ctx.ellipse(762,438,36,8,0,0,Math.PI*2);ctx.fill();
  ctx.fillStyle=floor?.on?'#dbd9a8':'#a2aa8b';ctx.beginPath();ctx.moveTo(722,120);ctx.lineTo(791,120);ctx.lineTo(814,177);ctx.lineTo(701,177);ctx.closePath();ctx.fill();
  ctx.fillStyle=ceiling?.on?'#dcdfb7':'#71806c';ctx.beginPath();ctx.ellipse(472,64,85,15,0,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#53644e';ctx.fillRect(851,365,37,67);for(let i=0;i<6;i++){ctx.beginPath();ctx.ellipse(865+(i%2?18:-15),315+i*7,12,43,(i%2?1:-1)*.5,0,Math.PI*2);ctx.fill();}
  ctx.fillStyle='rgba(8,15,10,.15)';ctx.fillRect(0,0,w,h);
}
function render() {
  $('mode').textContent=state.mode==='demo'?'DEMO / 模拟模式':'LIVE / 真实设备';
  $('room').textContent=`${state.room} · 摄像头控制`;
  $('status').textContent=state.running?(state.mode==='demo'?'体验中':'监听中'):'已暂停';
  $('status').classList.toggle('active',state.running);
  $('listen').textContent=state.running?'暂停监听 ■':state.mode==='demo'?'开始体验 →':'开始监听 →';
  $('source').disabled=state.running;$('wake').disabled=state.running;
  $('examples').hidden=$('testrow').hidden=state.mode!=='demo';
  $('connections').hidden=$('wholeHome').hidden=$('commandForm').hidden=state.mode!=='live';
  $('deviceCount').textContent=String(state.devices.length).padStart(2,'0');
  $('boundary').textContent=state.mode==='demo'?'DEMO · 无真实设备连接':$('source').value==='server'?'LIVE · 本机服务持续监听，可关闭此页面':'LIVE · 当前输入需要保持页面打开';
  $('devices').replaceChildren();$('anchors').replaceChildren();
  for(const d of state.devices){
    const s=states.find(s=>s.id===d.id)||{},row=node('div',undefined,`device ${s.on?'on':''}`);
    row.append(node('span','♧','bulb'));const content=node('div');content.append(node('strong',d.name));
    content.append(node('p',s.not_connected?'等待连接':s.unavailable?'不可用':`${s.on?'已开启':'已关闭'}${s.on&&s.brightness!=null?` · ${s.brightness}%`:''}`));row.append(content);
    const bind=node('button','绑定 ↗');bind.disabled=state.running;bind.setAttribute('aria-label',`绑定${d.name}`);
    bind.onclick=()=>{binding=d.id;$('viewport').classList.add('binding');$('cancelBind').hidden=false;feedback(`在画面中框选「${d.name}」。`);};row.append(bind);$('devices').append(row);
    for(const a of d.anchors.filter(a=>a.camera_id===state.camera_id)){
      const box=node('div',undefined,`anchor ${s.on?'on':''}`),[x1,y1,x2,y2]=a.box;
      Object.assign(box.style,{left:`${x1*100}%`,top:`${y1*100}%`,width:`${(x2-x1)*100}%`,height:`${(y2-y1)*100}%`});
      box.append(node('span',d.name));$('anchors').append(box);
    }
  }
  const events=state.events.filter(e=>e.category==='command').slice(0,15);
  if(events.length){$('events').replaceChildren();for(const e of events){
    const item=node('div',undefined,`event ${['verified','simulated','unchanged'].includes(e.status)?'success':''}`),top=node('div',undefined,'event-top');
    const device=state.devices.find(d=>d.id===e.device_id);
    top.append(node('span',`${statusText[e.status]||e.status}${device?' · '+device.name:''}`),node('time',new Date(e.time*1000).toLocaleTimeString('zh-CN',{hour12:false})));
    item.append(top,node('p',`${reasonText[e.reason]||e.reason}${e.latency_ms?' · '+e.latency_ms+' ms':''}`));$('events').append(item);
  }}
  if(state.mode==='demo')drawDemo();
  if($('source').value==='server')$('level').style.width=`${(state.capture?.level||0)*100}%`;
  if(state.capture?.status==='error'){$('sourceLabel').textContent='连接中断 · 画面已冻结';feedback('摄像头连接中断，已停止监听。请检查 Miloco 登录与摄像头网络。');}else if(!state.running&&state.mode==='live')$('sourceLabel').textContent='已暂停 · 等待摄像头连接';
}
async function refresh(){if(refreshing)return;refreshing=true;try{state=await api('/api/status');if(!state.running&&capture){capture.stop();capture=null;}states=await api('/api/devices');render();if(state.mode==='live'){homeState=await api('/api/home');renderHome();}}finally{refreshing=false;}}
function showResult(result){feedback(`${statusText[result.status]||result.status} · ${reasonText[result.reason]||result.reason}`);refresh().catch(e=>feedback(e.message));}
async function stop(){capture?.stop();capture=null;await api('/api/control',{running:false});$('captureState').textContent='摄像头已停止';$('level').style.width='0';await refresh();}
$('listen').onclick=async()=>{
  if(changing)return;changing=true;$('listen').disabled=true;
  try{
    if(state.running){await stop();return;}
    binding=null;$('cancelBind').hidden=true;$('viewport').classList.remove('binding');
    await api('/api/control',{running:true,source:$('source').value,wake_mode:$('wake').checked?'required':'optional'});
    if(state.mode==='live'&&!['text','server'].includes($('source').value)){
      capture=new Capture(canvas,$('video'),{level:v=>$('level').style.width=`${v*100}%`,state:t=>$('captureState').textContent=t,feedback,result:showResult,
        error:message=>{if(!capture)return;stop().catch(()=>{});feedback(message);}});
      await capture.start($('source').value,state.token);
      $('sourceLabel').textContent=$('source').value==='miloco'?'米家摄像头 · 实时画面':'本机摄像头 · 实时画面';
    }else $('captureState').textContent=state.mode==='demo'?'模拟输入就绪':'文字控制已启用';
    await refresh();feedback(state.mode==='demo'?'点击下方指令，体验完整判断与执行流程。':'监听已开启。看向摄像头说出设备名称和指令。');
  }catch(e){await stop().catch(()=>{});feedback(e.message);}finally{changing=false;$('listen').disabled=false;}
};
document.querySelectorAll('[data-scenario]').forEach(b=>b.onclick=async()=>{try{showResult(await api('/api/demo',{scenario:b.dataset.scenario}));}catch(e){feedback(e.message);}});
$('cancelBind').onclick=()=>{binding=null;drawing=null;$('selection').hidden=true;$('cancelBind').hidden=true;$('viewport').classList.remove('binding');feedback('已取消框选。');};
const point=e=>{const r=$('viewport').getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];};
$('viewport').onpointerdown=e=>{if(!binding||state.running)return;drawing=point(e);$('viewport').setPointerCapture(e.pointerId);$('selection').hidden=false;};
$('viewport').onpointermove=e=>{if(!drawing)return;const p=point(e);Object.assign($('selection').style,{left:`${Math.min(p[0],drawing[0])*100}%`,top:`${Math.min(p[1],drawing[1])*100}%`,width:`${Math.abs(p[0]-drawing[0])*100}%`,height:`${Math.abs(p[1]-drawing[1])*100}%`});};
$('viewport').onpointerup=async e=>{if(!drawing)return;const p=point(e),box=[Math.min(p[0],drawing[0]),Math.min(p[1],drawing[1]),Math.max(p[0],drawing[0]),Math.max(p[1],drawing[1])];drawing=null;$('selection').hidden=true;
  try{await api('/api/anchors',{device_id:binding,box});binding=null;$('cancelBind').hidden=true;$('viewport').classList.remove('binding');feedback('设备位置已保存。摄像头移动后需要重新绑定。');await refresh();}catch(e){feedback(e.message);}};
window.addEventListener('pagehide',()=>{capture?.stop();if(state?.running&&$('source').value!=='server')fetch('/api/control',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json','X-Friday-Token':state.token},body:'{"running":false}'}).catch(()=>{});});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&state?.running&&state.mode==='live'&&$('source').value!=='server')stop().then(()=>feedback('页面转入后台，监听已暂停。')).catch(()=>{});});
setInterval(()=>{$('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});},1000);
try{await refresh();$('wake').checked=state.wake_mode==='required';$('source').value=state.mode==='demo'?'demo':state.camera_configured?'server':'browser';
  [...$('source').options].forEach(o=>o.disabled=state.mode==='demo'?o.value!=='demo':o.value==='demo'||(['miloco','server'].includes(o.value)&&!state.camera_configured));
  if(state.mode==='live'){const textOption=node('option','文字控制 · 暂不采集');textOption.value='text';$('source').append(textOption);ctx.fillStyle='#202923';ctx.fillRect(0,0,960,540);$('sourceLabel').textContent='等待摄像头连接';feedback('连接账号后即可开始。米家后台监听无需浏览器麦克风权限。');}
}catch(e){feedback('服务连接失败：'+e.message);}
setInterval(()=>refresh().catch(()=>{capture?.stop();capture=null;feedback('服务连接已中断，音视频采集已停止。');}),4000);

function renderHome(){
  $('haState').textContent=homeState.connected?'Home Assistant · 已授权':'Home Assistant · 待连接';
  $('connectHA').hidden=false;$('connectHA').textContent=homeState.connected?'重新连接 ↗':'连接我的家 ↗';$('syncHA').hidden=!homeState.connected;
  $('plannerState').textContent=homeState.planner_provider==='codex'?'Codex App-Server':homeState.planner_model;
  const all=homeState.entities.filter(e=>Object.keys(e.capabilities).length),query=$('deviceFilter').value.trim().toLowerCase();
  $('homeCount').textContent=all.length+' 个';$('homeDevices').replaceChildren();
  const matching=all.filter(e=>(e.name+' '+e.room+' '+e.id).toLowerCase().includes(query));
  for(const e of matching.slice(0,100)){const row=node('div',undefined,'home-device'+(['unknown','unavailable'].includes(e.state)?' unavailable':'')),label=node('div');label.append(node('strong',e.name),node('small',(e.room||'未分配房间')+' · '+e.domain));row.append(label,node('span',e.state));$('homeDevices').append(row);}
  if(!matching.length)$('homeDevices').append(node('p',homeState.connected?'尚无匹配设备，请同步或更换关键词。':'授权后自动发现房间、设备和控制能力。','empty'));
  if(homeState.latest&&homeState.latest.event_id!==seenResult){seenResult=homeState.latest.event_id;feedback(`${statusText[homeState.latest.status]||homeState.latest.status} · ${homeState.latest.reason}`);}
  if(homeState.latest?.status==='confirmation_required'&&homeState.latest.plan_id!==reviewedPlan){reviewedPlan=homeState.latest.plan_id;renderPlan(homeState.latest);}
}
function renderPlan(result){const panel=$('planReview');panel.replaceChildren();panel.hidden=false;panel.append(node('strong','确认这次设备操作'),node('p',result.reason));const list=node('ol');for(const step of result.steps){const entity=homeState.entities.find(e=>e.id===step.entity_id);list.append(node('li',`${entity?.name||step.entity_id} · ${step.service}${Object.keys(step.parameters||{}).length?' · '+JSON.stringify(step.parameters):''}`));}panel.append(list);const button=node('button','确认执行 →','primary');button.onclick=async()=>{button.disabled=true;try{showResult(await api('/api/plans/'+encodeURIComponent(result.plan_id)+'/confirm',{}));panel.hidden=true;}catch(e){feedback(e.message);}finally{button.disabled=false;}};panel.append(button,node('p','确认前会重新检查设备状态；暂停监听会取消待执行计划。'));}
$('deviceFilter').oninput=()=>homeState&&renderHome();
$('connectHA').onclick=async()=>{try{const r=await api('/api/auth/ha/start',{});window.location.assign(r.url);}catch(e){feedback(e.message);}};
$('syncHA').onclick=async()=>{const b=$('syncHA');b.disabled=true;try{await api('/api/home/sync',{});await refresh();feedback('全屋设备已同步。');}catch(e){feedback(e.message);}finally{b.disabled=false;}};
$('commandForm').onsubmit=async e=>{e.preventDefault();const b=$('sendCommand');b.disabled=true;try{if(!state.running){$('source').value='text';await api('/api/control',{running:true,source:'text'});await refresh();}feedback('正在理解指令并检查设备状态…');const r=await api('/api/commands',{text:$('commandText').value,event_id:crypto.randomUUID()});showResult(r);$('commandText').value='';}catch(err){feedback(err.message);}finally{b.disabled=false;}};
async function systemStatus(){if(state?.mode!=='live')return;try{const s=await api('/api/system');$('perceptionState').textContent=s.runtime.status==='ready'?'本地感知 · 就绪':'本地感知 · '+(s.runtime.status==='loading'?'模型加载中':'等待服务');$('systemState').textContent=s.ha_connected?'设备已连接':'等待账号连接';}catch{}}
await systemStatus();setInterval(systemStatus,15000);

let previewBusy=false;
setInterval(async()=>{if(previewBusy||!state?.running||$('source').value!=='server'||document.hidden)return;previewBusy=true;try{const r=await fetch('/api/camera/frame');if(r.ok){const frame=await createImageBitmap(await r.blob());ctx.drawImage(frame,0,0,canvas.width,canvas.height);frame.close();$('sourceLabel').textContent='米家摄像头 · 后台实时画面';$('captureState').textContent=state.capture?.status==='processing'?'正在理解指令…':'后台监听中';}}catch{}finally{previewBusy=false;}},800);
