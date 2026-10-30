import {mountCameraConnection} from './camera-connection.js';
import {mountConnectionSetup,mountCameraSetup} from './connection-setup.js';
import {mainDeviceControls} from './device-controls.js';
import {mountHouse} from './house-model.js';
import {directPower,powerOperation,placedDevice} from './house-appearance.js';
import {deviceThumbnails} from './device-models.js';
import {mountAutomations} from './automations.js';
import {mountAutomationCatalog} from './automations-catalog.js';
import {mountSpeechOutput} from './speech-output.js';
import {mountRecognitionDebug} from './recognition-debug.js';
import {mountCameraPrivacy} from './camera-privacy.js';
import {mountRecordings} from './protected-recordings.js';
import {arlPresentation} from './arl-status.js';
const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{children.forEach(c=>c&&n.append(c));return n;};
const ico=name=>h('span',null,{class:'icon icon-'+name,'aria-hidden':'true'});
const btn=(text,run,attrs={})=>{const n=h('button',text,{type:'button',...attrs});n.addEventListener('click',run);return n;};
const api=async(path,body,options={})=>{const r=await fetch(path,{...options,...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),headers:{'Content-Type':'application/json'}});const d=await r.json();if(!r.ok)throw Error(d.detail||d.code||'连接暂时不可用');return d;};
const names={humidifier:'除湿与加湿',water_heater:'热水',trigger:'动作按键',light:'灯光',switch:'开关',cover:'窗帘',climate:'空调',camera:'摄像头',fan:'风扇',vacuum:'清洁',sensor:'环境',binary_sensor:'传感器',media_player:'影音',lock:'门锁'};
const marks={humidifier:'temperature',water_heater:'temperature',trigger:'adjustments-horizontal',light:'bulb',switch:'plug',cover:'adjustments-horizontal',climate:'air-conditioning',camera:'video',fan:'devices',vacuum:'devices',sensor:'temperature',binary_sensor:'devices',media_player:'player-play',lock:'door'};
const visualMark=d=>({ceiling_vent:'ceiling-vent',power_supply:'plug',led_strip:'light-strip',pendant:'pendant',downlight:'downlight',spotlight:'spotlight'}[d.presentation?.fixture]||marks[d.kind]||'devices');
const modeNames={off:'关闭',cool:'制冷',heat:'制热',auto:'自动',heat_cool:'自动冷暖',dry:'除湿',fan_only:'送风',on:'开启',open:'打开',closed:'关闭',opening:'正在打开',closing:'正在关闭',locked:'已上锁',unlocked:'未上锁',playing:'正在播放',paused:'已暂停',idle:'待机',buffering:'正在缓冲'};
const messageNames={reported_match:'已更新',accepted:'已发送，正在等待设备返回',unknown_outcome:'已发送，设备尚未回报结果',unauthorized:'设备连接状态已变化，请刷新后重试',stale_input:'正在更新设备状态，请稍后再试',source_conflict:'设备状态刚刚变化，请重试',unsupported:'设备不支持这项操作',offline_dependency:'家庭连接已中断',expired_request:'操作已过期'};
const stateText=c=>!c.available?'设备离线':modeNames[c.value]??String(c.value??'未返回状态');
const roomOrder=['客厅','主卧','次卧','书房','健身房','厨房','其他'];
export function mountExperience(root,{page,go,legacyConnections,legacyRules,snapshot,openEvidenceRecording}){
 let recognitionDebug=null;
 let automationView=null;
 let cameraPrivacy=null;
 let cameraConnection=null;
 let connectionSetup=null;let connectionDraw=0;
 let recordingsView=null;
 let thumbnails=null;try{thumbnails=deviceThumbnails();}catch(_){}
 const thumbnail=d=>{try{return thumbnails?.render(d)||null;}catch(_){return null;}};
 const deviceImage=d=>{const surface=thumbnail(d);if(surface){surface.classList.add('device-model-image');return surface;}return ico(visualMark(d));};
 let alive=true,data=null,room='全部',category='全部',query='',selected=null,model=null,timer=null,busy=false,previewTimer=null,recordings=null,recordingFolder='',recordingDate='',interactions=null;
 const view=add(h('div',null,{class:'experience'}));root.append(view);
 let listSlot=null,inspectorSlot=null,statsSlot=null,modelSlot=null,spatialScene=null,spacePanel=null,spatialRoom=null,spatialDevice=null,placementNotice=null,placementSaving=false;
 let spatialTrigger=null,inspectorTrigger=null;
 let previewStop=null;
 const cameraStates=new Map(),requestedPositions=new Map();
 async function readCamera(id,fresh=false){const saved=cameraStates.get(id);if(!fresh&&saved&&Date.now()-saved.at<15000)return saved.value;const value=await api(`/api/embodied/cameras/${id}/power`);cameraStates.set(id,{value,at:Date.now()});return value;}
 async function wakeCamera(id){const state=await readCamera(id,true);if(state.enabled)return {...state,status:'reported_match'};const value=await api(`/api/embodied/cameras/${id}/power`,{enabled:true,revision:state.revision,event_id:crypto.randomUUID()});cameraStates.delete(id);if(value.status==='reported_match')cameraStates.set(id,{value,at:Date.now()});return value;}
 const toast=(text,error=false,persistent=false)=>{let n=document.getElementById('home-toast');if(!n){n=h('div',null,{id:'home-toast',role:'status','aria-live':'polite'});document.body.append(n);}n.replaceChildren(h('span',text));if(error)n.append(btn('关闭',()=>n.hidden=true,{'aria-label':'关闭提示'}));n.className=error?'error':'';n.hidden=false;clearTimeout(n.hideTimer);if(!error&&!persistent)n.hideTimer=setTimeout(()=>n.hidden=true,6000);};
 const field=(label,input)=>add(h('label',null,{class:'product-field'}),h('span',label),input);
 const choices=(items,label)=>{const n=h('select',null,{'aria-label':label});for(const[value,text]of items)n.append(h('option',text,{value}));return n;};
 const readingValue=r=>r.available===false||r.value==null?'暂时不可用':String(r.value)+(r.unit||'');
 function deviceSummary(d){
  if(d.available===false)return '暂时不可用';
  if(d.kind==='camera')return ({shutter_closed:'镜头已关闭',privacy_shutter_closed:'镜头已关闭',privacy_closed:'镜头已关闭',paused:'摄像头感知已暂停',live:'正在接收画面',connecting:'正在连接',stopped:'尚未查看'})[d.camera_state]||'查看画面与隐私状态';
  if(d.kind==='trigger')return d.triggers?.some(t=>t.role==='unknown')?'按键模式待确认':`${d.triggers?.length||0} 个动作按键`;
  if(d.kind==='lock'){const r=d.readings.find(r=>r.available!==false&&(/门状态|锁定状态/.test(r.name)||r.device_class==='lock'));return r?readingValue(r):'锁定状态未知';}
  if(d.controls.length&&d.triggers?.length)return `${d.controls.filter(c=>c.available&&c.value==='on').length} 路开启 · ${d.triggers.length} 个动作按键`;
  if(d.controls.length===1){const c=d.controls[0];if(c.attributes?.feedback_quality==='unreliable')return '叶片角度待确认';const state=stateText(c);if(c.kind==='cover'&&c.attributes.current_position!=null)return `${state} · ${c.attributes.current_position}%`;if(['climate','water_heater'].includes(c.kind)&&c.attributes.current_temperature!=null)return `${state} · 室温 ${c.attributes.current_temperature}°`;return state;}
  if(d.controls.length)return `${d.controls.filter(c=>c.available&&c.value==='on').length} 路开启`+(d.triggers?.length?` · ${d.triggers.length} 个动作按键`:'');
  const rank=r=>/电池|电量|battery|异常|故障/i.test(r.name)||r.device_class==='battery'?80:({occupancy:0,motion:0,moisture:0,door:0,opening:0,smoke:0,temperature:1,humidity:2,illuminance:3,carbon_dioxide:3,pm25:3}[r.device_class]??({'°C':1,'%':2,'lx':3,'ppm':3,'µg/m³':3,'dB':4,'B/s':5}[r.unit]??(/有人无人|门状态|漏水/.test(r.name)?0:50)));
  const seen=new Set(),readings=d.readings.filter(r=>r.available!==false&&r.value!=null).sort((a,b)=>rank(a)-rank(b)).filter(r=>{const key=String(r.value)+':'+r.unit;if(seen.has(key))return false;seen.add(key);return true;});
  return readings.slice(0,2).map(r=>rank(r)===80?r.name+' '+readingValue(r):!r.unit&&/^\d+(\.\d+)?$/.test(String(r.value))?r.name+' '+readingValue(r):readingValue(r)).join(' · ')||'状态暂不可用';
 }
 function deviceReadings(d,slot){
  if(!d.readings.length)return;
  const readings=add(h('details',null,{class:'device-readings'}),h('summary','设备状态'));
  for(const r of d.readings)readings.append(add(h('div',null,{class:'reading-line'}),h('span',r.name),h('strong',readingValue(r))));
  readings.append(h('p','状态由设备上报。暂时不可用的读数会在连接恢复后更新。',{class:'caption'}));slot.append(readings);
 }
 function drawCameras(){
  recognitionDebug?.dispose();cameraPrivacy?.dispose();view.replaceChildren();
  const layout=h('div',null,{class:'product-device-layout camera-device-layout'});listSlot=h('div');inspectorSlot=h('aside',null,{class:'product-inspector',hidden:''});add(layout,listSlot,inspectorSlot);view.append(layout);listSlot.append(tiles(data.devices.filter(d=>d.kind==='camera')));
  cameraPrivacy=mountCameraPrivacy(view);
  const advanced=add(h('details',null,{class:'camera-tools'}),h('summary','人物与手势检查'));
  advanced.addEventListener('toggle',()=>{if(advanced.open&&!recognitionDebug)recognitionDebug=mountRecognitionDebug(advanced,{go});else if(!advanced.open){recognitionDebug?.dispose();recognitionDebug=null;}});view.append(advanced);
  try{const id=sessionStorage.getItem('Friday.cameraFocus');sessionStorage.removeItem('Friday.cameraFocus');const d=data.devices.find(d=>d.kind==='camera'&&d.id===id);if(d)inspect(d);}catch(_){}
 }
 async function load(initial=false){if(initial&&page==='Settings'){void drawConnections();return;}try{const next=await api('/api/home/overview');next.layout=await api('/api/home/layout').catch(()=>null);if(!alive)return;data=next;for(const d of data.devices)for(const c of d.controls)if(requestedPositions.has(c.id))c.attributes.requested_position=requestedPositions.get(c.id);document.dispatchEvent(new CustomEvent('Friday.homeStatus',{detail:{generated_at:next.generated_at,connected:next.connected,simulated:next.simulated,control_count:next.control_count}}));if(initial)draw();else update();}catch(e){if(initial)view.replaceChildren(add(h('div',null,{class:'product-empty'}),ico('home'),h('h2','家庭连接暂不可用'),h('p','请到连接设置完成授权，或恢复已有连接。'),btn('连接家庭',()=>go('Settings'),{class:'primary'}),btn('重试',()=>load(true))));else toast('家庭状态同步暂时中断',true);}}
 async function control(c,operation,desired,button){if(busy)return;busy=true;toast('正在设置'+c.name+'…',false,true);if(button)button.setAttribute('aria-busy','true');model?.setBusy(true);spatialScene?.setAttribute('aria-busy','true');if(button)button.disabled=true;try{const trace=await api('/api/home/control',{target:c.id,operation,desired,expected_state_version:c.state_version,event_id:crypto.randomUUID()});if(c.attributes?.feedback_quality==='unreliable'&&['accepted','reported_match'].includes(trace.status)&&desired.position!=null)requestedPositions.set(c.id,desired.position);toast(c.attributes?.feedback_quality==='unreliable'&&['accepted','reported_match'].includes(trace.status)?'已发送，叶片角度待确认':messageNames[trace.status]||messageNames[trace.code]||'操作未完成',trace.status==='rejected'||trace.status==='failed');await load();}catch(e){toast('操作未完成，正在重新读取设备状态',true);await load();}finally{busy=false;updateTiles();model?.setBusy(false);spatialScene?.removeAttribute('aria-busy');if(button){button.disabled=false;button.removeAttribute('aria-busy');}if(spatialRoom)drawSpatialPanel();const fresh=data?.devices.find(d=>d.id===selected);if(fresh&&fresh.kind!=='camera')inspect(fresh);}}
 function typeControl(c,{showName=true}={}){
  const block=h('section',null,{class:'type-control'});if(showName)block.append(h('h3',c.name));
  if(!c.available)block.append(h('p','设备离线',{class:'muted'}));
  const caps=new Map(c.capabilities.map(x=>[x.name,x]));
  const power=caps.has('light.set')?'light.set':caps.has('switch.set')?'switch.set':caps.has('fan.set')?'fan.set':null;
  if(power)block.append(mainDeviceControls({id:c.id,name:c.name,kind:c.kind,controls:[c]},{send:control,busy}));
  function slider(label,cap,current,operation,key,format=x=>`${x}${cap.unit||''}`){const input=h('input',null,{type:'range',min:cap.minimum??0,max:cap.maximum??100,step:cap.step??1,'aria-label':label});input.value=current??cap.minimum??0;input.disabled=!c.available;const out=h('output',current==null?'—':format(Number(current)));input.addEventListener('input',()=>out.textContent=format(Number(input.value)));input.addEventListener('change',()=>control(c,operation,{[key]:Number(input.value),...(operation==='light.set'?{power:'on'}:{})},input));block.append(add(h('div',null,{class:'range-field'}),add(h('div'),h('span',label),out),input));}
  const brightness=caps.get('light.set');if(brightness?.maximum!=null&&c.value==='on')slider('亮度',brightness,c.attributes.brightness,'light.set','brightness',v=>Math.round(v/255*100)+'%');
  if(caps.has('light.color_temperature')&&c.value==='on')slider('色温',caps.get('light.color_temperature'),c.attributes.color_temp_kelvin,'light.color_temperature','color_temp_kelvin',v=>v+' K');
  const angular=c.attributes?.position_semantics==='slat_angle';if(angular)block.append(mainDeviceControls({id:c.id,name:c.name,kind:c.kind,controls:[c]},{send:control,busy}));
  if(caps.has('cover.position')&&!angular){
   const row=h('div',null,{class:'segmented'});for(const[v,label]of [[0,'关闭'],[50,'半开'],[100,'打开']]){const b=btn(label,()=>control(c,'cover.position',{position:v},b));b.disabled=!c.available;row.append(b);}block.append(row);slider('开合程度',caps.get('cover.position'),c.attributes.current_position,'cover.position','position',v=>v+'%');
  }
  if(caps.has('cover.state')&&!caps.has('cover.position')){const row=h('div',null,{class:'segmented'});for(const[state,label]of [['closed','关闭'],['open','打开']]){const b=btn(label,()=>control(c,'cover.state',{state},b));b.disabled=!c.available;row.append(b);}block.append(row);}
  if(!angular&&caps.get('cover.state')?.choices?.includes('stopped')){const stop=btn('停止',()=>control(c,'cover.state',{state:'stopped'},stop));stop.disabled=!c.available;block.append(stop);}
  if(['climate','humidifier','water_heater'].includes(c.kind))block.append(mainDeviceControls({id:c.id,name:c.name,kind:c.kind,controls:[c]},{send:control,busy}));
  if(caps.has('cover.tilt'))slider('叶片角度',caps.get('cover.tilt'),c.attributes.current_tilt_position,'cover.tilt','tilt_position',v=>v+'%');
  if(caps.has('climate.mode')){
   const select=choices(caps.get('climate.mode').choices.map(x=>[x,modeNames[x]||x]),'空调模式');select.value=c.value;select.disabled=!c.available;select.addEventListener('change',()=>control(c,'climate.mode',{hvac_mode:select.value},select));block.append(field('模式',select));
  }
  if(['climate','water_heater'].includes(c.kind)&&c.attributes.current_temperature!=null)block.append(h('p',`${c.kind==='water_heater'?'当前水温':'当前室温'} ${c.attributes.current_temperature}°`,{class:'caption'}));
  if(caps.has('climate.fan_mode')){const select=choices(caps.get('climate.fan_mode').choices.map(x=>[x,modeNames[x]||x]),'空调风速');select.value=c.attributes.fan_mode??'';select.disabled=!c.available;select.addEventListener('change',()=>control(c,'climate.fan_mode',{fan_mode:select.value},select));block.append(field('风速',select));}
  if(caps.has('media_player.power')){const row=h('div',null,{class:'segmented'});for(const[power,label]of [['on','开启'],['off','关闭']]){const b=btn(label,()=>control(c,'media_player.power',{power},b));b.disabled=!c.available;row.append(b);}block.append(row);}
  if(caps.has('media_player.volume'))slider('音量',caps.get('media_player.volume'),c.attributes.volume_level,'media_player.volume','volume_level',v=>Math.round(v*100)+'%');
  if(caps.has('media_player.mute')){const muted=c.attributes.is_volume_muted===true,b=btn(muted?'取消静音':'静音',()=>control(c,'media_player.mute',{is_volume_muted:!muted},b),{'aria-pressed':String(muted)});b.disabled=!c.available;block.append(b);}
  if(caps.has('media_player.playback')){const row=h('div',null,{class:'segmented'});for(const[state,label]of [['playing','播放'],['paused','暂停']]){const b=btn(label,()=>control(c,'media_player.playback',{state},b));b.disabled=!c.available;row.append(b);}block.append(row);}
  if(caps.has('lock.set')){const row=h('div',null,{class:'segmented'});for(const[value,label]of [['locked','锁门'],['unlocked','解锁']]){const b=btn(label,()=>control(c,'lock.set',{lock:value},b));b.disabled=!c.available;row.append(b);}block.append(row);}
  if(caps.has('fan.percentage'))slider('风速',caps.get('fan.percentage'),c.attributes.percentage,'fan.percentage','percentage',v=>v+'%');
  return block;
 }
 function stopPreview(){clearInterval(previewTimer);previewTimer=null;const stop=previewStop;previewStop=null;if(stop)void stop();}
 function cameraPanel(d,slot,{start=false}={}){
  stopPreview();let power=null,pending=false,viewing=false;
  const frame=h('div',null,{class:'camera-frame'}),image=h('img',null,{alt:d.room+'摄像头实时画面',hidden:''}),placeholder=add(h('div',null,{class:'camera-placeholder'}),ico('video'),h('span','正在读取镜头状态…'));
  add(frame,image,placeholder);slot.append(frame);
  const status=h('p','正在读取镜头状态…',{class:'camera-status',role:'status','aria-live':'polite'});slot.append(status);
  const watch=btn('正在读取…',async()=>{if(pending||!power)return;pending=true;drawPower();try{
   if(!power.enabled){status.textContent='正在打开镜头…';const result=await api(`/api/embodied/cameras/${d.id}/power`,{enabled:true,revision:power.revision,event_id:crypto.randomUUID()});if(result.status!=='reported_match'){status.textContent='指令已发送，镜头状态尚未确认。请刷新状态。';power=null;return;}power=result;}
   status.textContent='正在连接画面…';const report=await api(`/api/embodied/cameras/${d.id}/preview`,{});
   if(!alive||!frame.isConnected){if(report.preview_token)void api(`/api/embodied/cameras/${d.id}/preview/stop`,{token:report.preview_token}).catch(()=>{});return;}
   viewing=true;const token=report.preview_token;previewStop=token?()=>api(`/api/embodied/cameras/${d.id}/preview/stop`,{token}).catch(()=>{}):null;
   await poll();if(alive&&frame.isConnected)previewTimer=setInterval(poll,1500);
  }catch(_){status.textContent='画面暂不可用，请刷新状态后重试。';}finally{pending=false;drawPower();}},{class:'primary'});
  const close=btn('关闭镜头',async()=>{if(pending||!power)return;pending=true;stopPreview();viewing=false;image.hidden=true;placeholder.hidden=false;drawPower();try{
   const result=await api(`/api/embodied/cameras/${d.id}/power`,{enabled:false,revision:power.revision,event_id:crypto.randomUUID()});
   if(result.status==='reported_match'){power=result;status.textContent='镜头已关闭';}else{power=null;status.textContent='关闭指令已发送，状态尚未确认。请刷新状态。';}
  }catch(_){status.textContent='关闭结果尚未确认，请刷新状态或在米家中核对。';power=null;}finally{pending=false;drawPower();}});
  const stop=btn('停止查看',()=>{stopPreview();viewing=false;image.hidden=true;placeholder.hidden=false;status.textContent='已停止查看';drawPower();});
  const refresh=btn('刷新状态',()=>readPower());
  const cameraControls=h('div',null,{class:'camera-directions'}),directions=[];
  for(const[direction,label]of [['left','向左'],['up','向上'],['down','向下'],['right','向右']]){const b=btn(label,async()=>{b.disabled=true;try{await api(`/api/embodied/cameras/${d.id}/control`,{direction});status.textContent='调整指令已发送';}catch(_){status.textContent='云台尚未就绪，请等待画面恢复后重试。';}finally{b.disabled=false;}});b.disabled=true;directions.push(b);cameraControls.append(b);}
  const follow=btn('自动跟随',async()=>{try{await api(`/api/embodied/cameras/${d.id}/control`,{mode:'auto'});status.textContent='已恢复自动跟随';}catch(_){status.textContent='请先开启人物识别，再使用自动跟随。';}});follow.disabled=true;cameraControls.append(follow);
  function drawPower(){watch.textContent=power?.enabled?'查看实时画面':'打开镜头并查看';watch.disabled=pending||!power||viewing;close.hidden=power?.enabled!==true;close.disabled=pending;stop.disabled=!viewing;refresh.disabled=pending;placeholder.lastElementChild.textContent=power?.enabled===false?'镜头已关闭':power?.enabled===true?'点击查看实时画面':'镜头状态暂不可用';}
  async function readPower(){if(pending)return;pending=true;refresh.disabled=true;try{const result=await api(`/api/embodied/cameras/${d.id}/power`);if(!alive||!frame.isConnected)return;power=result;status.textContent=result.enabled?'镜头已打开':'镜头已关闭';}catch(_){power=null;status.textContent='无法读取镜头状态，请刷新状态或检查网络。';}finally{pending=false;drawPower();}}
  async function poll(){if(!alive||!frame.isConnected){stopPreview();return;}try{const report=await api(`/api/embodied/cameras/${d.id}/preview`);const live=report.frame_available||report.state==='live'||report.status==='live';directions.forEach(b=>b.disabled=!live||!(report.primary||report.manual_control));follow.disabled=!live||report.recognition_enabled===false;
   if(live){image.src=`/api/embodied/cameras/${d.id}/frame?t=${Date.now()}`;image.hidden=false;placeholder.hidden=true;status.textContent=report.recognition_enabled===false?'实时画面 · 后台识别设置保持原样':'实时画面';}
   else{const reason=report.reason||report.state||report.status;image.hidden=true;placeholder.hidden=false;if(['privacy_closed','privacy_shutter_closed','shutter_closed'].includes(reason)){stopPreview();viewing=false;power=null;void readPower();}else status.textContent='正在连接画面，请稍候…';}
  }catch(_){image.hidden=true;placeholder.hidden=false;status.textContent='画面连接中断，正在重连…';}}
  image.addEventListener('error',()=>{image.hidden=true;placeholder.hidden=false;});
  slot.append(add(h('div',null,{class:'product-actions camera-view-actions'}),watch,close,stop,refresh),cameraControls);
  slot.append(add(h('div',null,{class:'product-actions'}),btn('回看录像',()=>go('Activity')),btn('人物识别',()=>go('People'))));
  slot.append(h('p','只有点“打开镜头并查看”才会打开这台镜头。停止查看不关闭镜头；要保护隐私，请点“关闭镜头”。',{class:'caption'}));
  void readPower().then(()=>{if(start&&alive&&frame.isConnected&&power?.enabled)watch.click();});
 }
 function inspect(d,{viewCamera=false}={}){if(!inspectorSlot)return;const wasOpen=selected===d.id&&!inspectorSlot.hidden;if(!wasOpen)inspectorTrigger={device:d.id,key:document.activeElement?.dataset?.controlKey||'details'};selected=d.id;stopPreview();inspectorSlot.replaceChildren();inspectorSlot.setAttribute('tabindex','-1');inspectorSlot.setAttribute('aria-label',d.name+'设备详情');
  const close=btn('关闭',()=>{selected=null;inspectorSlot.hidden=true;stopPreview();updateTiles();const card=[...listSlot.querySelectorAll('[data-device]')].find(n=>n.dataset.device===inspectorTrigger?.device);([...card?.querySelectorAll('[data-control-key]')||[]].find(n=>n.dataset.controlKey===inspectorTrigger?.key)||card?.querySelector('.device-open'))?.focus?.({preventScroll:true});},{class:'inspector-close','aria-label':'关闭设备详情'});
  add(inspectorSlot,close,add(h('div',null,{class:'product-device-heading'}),ico(visualMark(d)),h('p',d.room,{class:'eyebrow'}),h('h2',d.name)));
  inspectorSlot.hidden=false;
  if(d.kind!=='camera')inspectorSlot.append(h('p',deviceSummary(d),{class:'device-state-summary',role:'status'}));
  if(d.kind==='camera')cameraPanel(d,inspectorSlot,{start:viewCamera});
  else if(d.controls.length)d.controls.forEach(c=>inspectorSlot.append(typeControl(c,{showName:d.controls.length>1})));
  else inspectorSlot.append(h('p',deviceSummary(d),{class:'device-state-summary'}));
  if(d.triggers?.length)inspectorSlot.append(mainDeviceControls({...d,controls:[]},{send:control,busy}));
  deviceReadings(d,inspectorSlot);
  const details=add(h('details',null,{class:'device-diagnostics'}),h('summary','设备信息'),h('p',d.presentation?.label||names[d.kind]||'设备'),h('p',`房间：${d.room}`));inspectorSlot.append(details);
  inspectorSlot.onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();close.click();}};updateTiles();if(!wasOpen){inspectorSlot.focus?.({preventScroll:true});inspectorSlot.scrollIntoView?.({block:'nearest'});}
 }
 function toggleSpatial(controlId,button){
  const c=data.devices.flatMap(d=>d.controls).find(c=>c.id===controlId),operation=powerOperation(c);
  if(!c||!operation)return;if(!c.available){toast('设备离线，暂时无法控制',true);return;}
  return control(c,operation,{power:c.value==='on'?'off':'on'},button);
 }
 function spatialQuick(entry){
  if(entry.control)return toggleSpatial(entry.control);
  const d=data.devices.find(d=>d.id===entry.device);if(d)openSpatialDevice(d);
 }
 function openSpatialDevice(device,direct=false){
  const d=data.devices.find(d=>d.id===device.id);if(!d)return;
  const power=directPower(d);if(direct&&power)return toggleSpatial(power.id);
  spatialTrigger=document.activeElement;focusRoom(d.room);spatialRoom=d.room;spatialDevice=d.id;drawSpatialPanel();spacePanel?.focus?.({preventScroll:true});
 }
 function openRoomDevices(name){spatialTrigger=document.activeElement;focusRoom(name);spatialRoom=name;spatialDevice=null;drawSpatialPanel();spacePanel?.focus?.({preventScroll:true});}
 function closeSpatialPanel(){stopPreview();spatialRoom=null;spatialDevice=null;if(spacePanel)spacePanel.hidden=true;spatialScene?.classList.remove('with-spatial-panel');if(spatialTrigger?.isConnected)spatialTrigger.focus?.({preventScroll:true});}
 function drawSpatialPanel(){
  if(!spacePanel||!spatialRoom)return;
  const d=spatialDevice&&data.devices.find(d=>d.id===spatialDevice);
  spacePanel.hidden=false;spatialScene.classList.add('with-spatial-panel');spacePanel.replaceChildren();
  const header=h('div',null,{class:'spatial-panel-head'}),close=btn('×',closeSpatialPanel,{'aria-label':'关闭空间控制',class:'spatial-close'});
  if(d)header.append(btn('‹ '+spatialRoom+'设备',()=>{spatialDevice=null;drawSpatialPanel();},{class:'spatial-back'}));
  else header.append(h('h3',spatialRoom+' · 房间内设备'));
  header.append(close);spacePanel.append(header);
  const content=h('div',null,{class:'spatial-panel-content'});spacePanel.append(content);
  if(d){
   content.append(add(h('div',null,{class:'spatial-model-preview'}),deviceImage(d)));
   add(content,add(h('div',null,{class:'spatial-device-title'}),ico(visualMark(d)),h('h3',d.name)),h('p',deviceSummary(d),{class:'device-state-summary',role:'status'}));
   if(d.kind==='camera')cameraPanel(d,content);
   else if(d.controls.length)d.controls.forEach(c=>content.append(typeControl(c,{showName:d.controls.length>1})));
   if(d.triggers?.length)content.append(mainDeviceControls({...d,controls:[]},{send:control,busy}));
   deviceReadings(d,content);
   content.append(btn('在空间中定位',()=>beginPlacement(d),{class:'spatial-position','aria-label':'定位'+d.name}));
  }else{
   const list=data.devices.filter(d=>d.room===spatialRoom);
   for(const d of list){
    const row=h('div',null,{class:'spatial-device-row'}),open=btn(null,()=>openSpatialDevice(d),{class:'spatial-device-open','aria-label':'空间控制'+d.name});
    add(open,deviceImage(d),add(h('span'),h('strong',d.name),h('small',deviceSummary(d))));row.append(open);
    const power=directPower(d);
    if(power){const on=power.value==='on',toggle=btn(null,()=>toggleSpatial(power.id,toggle),{class:'spatial-power'+(on?' on':''),role:'switch','aria-checked':String(on),'aria-label':'空间'+(on?'关闭':'开启')+d.name});toggle.textContent='⏻';toggle.disabled=!power.available||busy;row.append(toggle);}
    else row.append(btn('›',()=>openSpatialDevice(d),{'aria-label':'调整'+d.name,class:'spatial-expand'}));
    const located=data.layout.devices.some(p=>placedDevice([d],p.id));row.append(btn(located?'移位':'定位',()=>beginPlacement(d),{class:'spatial-position','aria-label':(located?'重新定位':'定位')+d.name}));content.append(row);
   }
   if(!list.length)content.append(h('p','这个房间还没有设备。',{class:'caption'}));
  }
 }
 function beginPlacement(d){
  if(!model)return;focusRoom(d.room);closeSpatialPanel();model.place(d);spatialScene.classList.add('placing-device');placementNotice.hidden=false;placementNotice.replaceChildren(h('span','点击 '+d.name+' 的实际位置；也可用方向键移动、回车保存，Esc 取消。'),btn('取消',cancelPlacement));
 }
 function cancelPlacement(){model?.place(null);spatialScene?.classList.remove('placing-device');if(placementNotice)placementNotice.hidden=true;}
 function tiles(devices){const grid=h('div',null,{class:'product-device-grid'});devices.forEach(d=>{
  const card=h('article',null,{class:'product-device device-kind-'+d.kind+(selected===d.id?' selected':''),'data-device':d.id});
  const open=btn(null,()=>inspect(d),{class:'device-open','aria-label':'查看'+d.name,'data-control-key':'details'});add(open,add(h('span',null,{class:'tile-icon'+(d.controls.some(c=>c.value==='on')?' active':'')}),ico(visualMark(d))),h('span',d.room,{class:'tile-room'}),h('strong',d.name));
  if(d.kind!=='camera')open.append(h('span',deviceSummary(d),{class:'tile-state'}));card.append(open);
  card.append(mainDeviceControls(d,{send:control,wake:wakeCamera,readCamera,viewCamera:d=>inspect(d,{viewCamera:true}),details:()=>inspect(d),busy}));grid.append(card);
 });return grid;}
 function updateTiles(){if(!listSlot||!data)return;if(listSlot.contains(document.activeElement)&&document.activeElement?.type==='range')return;const active=document.activeElement,focused=listSlot.contains(active)?active?.closest?.('[data-device]')?.dataset.device:null,key=active?.dataset?.controlKey;const ds=data.devices.filter(d=>(page!=='Cameras'||d.kind==='camera')&&(room==='全部'||d.room===room)&&(category==='全部'||d.kind===category||category==='switch'&&d.kind==='trigger'||category==='light'&&d.controls.some(c=>c.kind==='light'))&&(!query||(d.name+d.room).toLowerCase().includes(query.toLowerCase())));listSlot.replaceChildren(tiles(ds));if(!ds.length)listSlot.append(h('p','没有符合条件的设备',{class:'product-empty'}));if(focused){const card=[...listSlot.querySelectorAll('[data-device]')].find(n=>n.dataset.device===focused);([...card?.querySelectorAll('[data-control-key]')||[]].find(n=>n.dataset.controlKey===key)||card?.querySelector('.device-open'))?.focus?.({preventScroll:true});}}
 function focusRoom(name){room=name;for(const b of view.querySelectorAll('.room-pills button'))b.setAttribute('aria-pressed',String(b.textContent===room));model?.focus(room==='全部'?null:room);const title=view.querySelector('[data-room-heading]');if(title)title.textContent=room==='全部'?'常用设备':room;updateTiles();if(spatialRoom&&name!==spatialRoom){if(name==='全部')closeSpatialPanel();else{spatialRoom=name;spatialDevice=null;drawSpatialPanel();}}}
 function filters(parent){const row=h('div',null,{class:'room-pills',role:'group','aria-label':'房间'});for(const name of ['全部',...roomOrder.filter(x=>data.rooms.includes(x)),...data.rooms.filter(x=>!roomOrder.includes(x))]){const b=btn(name,()=>{if(page==='Home'&&name!=='全部')openRoomDevices(name);else focusRoom(name);},{'aria-pressed':String(room===name)});row.append(b);}parent.append(row);}
 function drawHome(){stopPreview();model?.dispose();model=null;spacePanel=null;spatialScene=null;listSlot=null;inspectorSlot=null;statsSlot=null;view.replaceChildren();connectionSetup?.dispose();connectionSetup=null;if(page==='Home'&&(data.simulated||!data.connected))connectionSetup=mountConnectionSetup(view);else if(!data.connected)view.append(add(h('div',null,{class:'setup-banner'}),add(h('div'),h('strong','真实家庭尚未连接'),h('p',data.simulated?'当前为示例设备，连接后可查看自己的家。':'状态同步已中断，请恢复家庭连接。')),btn('连接家庭',()=>go('Settings'))));const head=add(h('div',null,{class:'product-section-head'}),h('div',page==='Home'?'家庭空间':'按房间查看',{class:'eyebrow'}),h('span',`${data.devices.length} 个设备`,{class:'caption'}));view.append(head);
  filters(view);if(page==='Home'&&!data.layout?.rooms?.length)view.append(add(h('div',null,{class:'layout-import-callout'}),h('p','导入家庭户型，在空间中查看和控制设备。'),btn('导入户型',editHouse)));
  if(page==='Home'&&data.layout?.rooms?.length){
   const scene=add(h('section',null,{class:'house-scene','aria-label':'家庭空间控制'}));spatialScene=scene;modelSlot=h('div',null,{class:'house-canvas'});scene.append(modelSlot);
   const toolbar=add(h('div',null,{class:'model-toolbar'}),h('span','选择房间或设备',{class:'eyebrow'}),btn('显示全屋',()=>{focusRoom('全部');closeSpatialPanel();model?.reset();}),btn('编辑户型与位置',editHouse));scene.append(toolbar);
   scene.append(h('span','设备按房间展示 · 拖动旋转 · 滚动缩放',{class:'model-hint'}));
   spacePanel=h('aside',null,{class:'spatial-panel',hidden:'',tabindex:'-1','aria-label':'空间设备控制'});spacePanel.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();closeSpatialPanel();}});placementNotice=h('div',null,{class:'spatial-placement',hidden:'',role:'status'});add(scene,spacePanel,placementNotice);view.append(scene);
   const rooms=data.layout.rooms;try{model=mountHouse(modelSlot,{rooms,placements:data.layout.devices,devices:data.devices,selectedRoom:room,onRoom:openRoomDevices,onDevice:d=>openSpatialDevice(d,true),onPlace:savePlacement,onCancelPlace:cancelPlacement,onQuick:spatialQuick,onRoomDevices:openRoomDevices,thumbnail});}catch(_){modelSlot.append(add(h('div',null,{class:'product-empty'}),h('p','空间视图暂时无法显示。可以选择上方房间控制设备，或重新加载。'),btn('重新加载',()=>load(true))));}
   statsSlot=h('div',null,{class:'home-summary'});view.append(statsSlot);drawStats();if(spatialRoom)drawSpatialPanel();
  }
  if(page==='Home'){
   if(!data.layout?.rooms?.length)view.append(btn('打开设备',()=>go('Devices')));
   return;
  }
  const toolbar=h('div',null,{class:'product-section-head'});add(toolbar,h('h2',room==='全部'?'常用设备':room,{'data-room-heading':''}),h('span','',{class:'caption'}));const search=h('input',null,{type:'search',placeholder:'搜索设备','aria-label':'搜索设备'});search.value=query;search.addEventListener('input',()=>{query=search.value;updateTiles();});toolbar.append(search);view.append(toolbar);
  const kinds=h('div',null,{class:'category-tabs'});for(const[k,v]of [['全部','全部'],...Object.entries(names).filter(([k])=>data.devices.some(d=>d.kind===k))])kinds.append(btn(v,()=>{category=k;updateTiles();for(const n of kinds.children)n.setAttribute('aria-pressed',String(n.textContent===v));},{'aria-pressed':String(category===k)}));view.append(kinds);
  const layout=h('div',null,{class:'product-device-layout'});listSlot=h('div');inspectorSlot=h('aside',null,{class:'product-inspector',hidden:''});add(layout,listSlot,inspectorSlot);view.append(layout);updateTiles();if(selected){const d=data.devices.find(x=>x.id===selected);if(d)inspect(d);}
 }
 function drawStats(){if(!statsSlot)return;const lights=data.devices.flatMap(d=>d.controls).filter(c=>c.presentation?.emits_light);const on=lights.filter(c=>c.available&&c.value==='on').length;statsSlot.replaceChildren(add(h('div'),ico('bulb'),h('strong',String(on)),h('span','盏灯开启')),add(h('div'),ico('video'),h('strong',String(data.devices.filter(d=>d.kind==='camera').length)),h('span','台摄像头')),add(h('div'),ico('home'),h('strong',String(data.rooms.length)),h('span','个房间')));}
 async function savePlacement(device,placedRoom,position){if(placementSaving)return;placementSaving=true;model?.place(null);try{data.layout=await api('/api/home/layout/device-position',{expected_revision:data.layout.revision,device_id:device.bindings?.find(b=>b.source==='xiaomi-identity')?.external_id||device.id,room_id:placedRoom.id,position});cancelPlacement();toast(device.name+'的位置已保存');drawHome();}catch(_){toast('位置未保存，请刷新户型后重试',true);cancelPlacement();}finally{placementSaving=false;}}
 function editHouse(){const dialog=h('dialog',null,{class:'product-dialog'});add(dialog,h('h2','家庭空间档案'),h('p','导入米家房间轮廓或完整户型。更新时按房间与设备 ID 对齐，已有设备位置会保留。'));const file=h('input',null,{type:'file',accept:'.json,application/json','aria-label':'导入家庭空间档案'}),out=h('div',null,{role:'status'});file.onchange=async()=>{const f=file.files[0];if(!f)return;if(f.size>2*1024*1024){out.textContent='文件过大，请选择家庭空间 JSON。';return;}try{const incoming=JSON.parse(await f.text()),report=await api('/api/home/layout/preview',{layout:incoming});out.replaceChildren(h('p',`房间：新增 ${report.rooms?.added?.length||0}，更新 ${report.rooms?.changed?.length||0}；设备点位：新增 ${report.devices?.added?.length||0}`));if(report.conflicts?.some(c=>c.kind!=='device_position_changed')){out.append(h('p','发现房间或设备归属冲突，请检查导入档案后重试。'));return;}if(report.conflicts?.length)out.append(h('p',`${report.conflicts.length} 个设备位置发生变化，导入会同步到新位置。`));out.append(btn('导入更新',async()=>{data.layout=await api('/api/home/layout/import',{layout:incoming,expected_revision:report.base_revision,accept_device_changes:Boolean(report.conflicts?.length)});dialog.close();drawHome();},{class:'primary'}));}catch(_){out.textContent='文件格式不正确，请使用 Friday 空间档案或米家 FloorMapV3 数据。';}};dialog.append(field('选择档案',file),out);if(data.layout){dialog.append(btn('撤销上次导入',async()=>{try{data.layout=await api('/api/home/layout/restore',{expected_revision:data.layout.revision});dialog.close();drawHome();}catch(_){toast('没有可恢复的上一版户型',true);}}));dialog.append(btn('导出当前档案',()=>{const blob=new Blob([JSON.stringify(data.layout,null,2)],{type:'application/json'}),a=h('a',null,{href:URL.createObjectURL(blob),download:'Friday-home-layout.json'});a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}));const target=choices([['','选择需要定位的设备'],...data.devices.map(d=>[d.id,d.room+' · '+d.name])],'定位设备');dialog.append(field('设备位置',target),btn('在户型中定位',()=>{const d=data.devices.find(d=>d.id===target.value);if(!d)return;dialog.close();beginPlacement(d);}));const placed=new Set(data.layout.devices.map(d=>d.id)),unplaced=data.devices.filter(d=>!placed.has(d.id)&&!d.bindings?.some(b=>placed.has(b.external_id)));dialog.append(h('p',`${unplaced.length} 个设备尚未定位。未定位设备可以在列表中正常控制。`));}presentationEditor(dialog);dialog.append(btn('完成',()=>dialog.close()));document.body.append(dialog);dialog.onclose=()=>dialog.remove();dialog.showModal();}
 function presentationEditor(dialog){
  const section=add(h('section',null,{class:'presentation-editor'}),h('h3','Friday 设备样式'),h('p','为家中的实际设备选择外观。样式单独保存，重新导入户型时会保留。',{class:'caption'}));
  const profile=structuredClone(data.presentation),climate=choices([['auto','跟随设备类型'],['ceiling_vent','中央空调 · 吊顶出风口'],['wall_unit','壁挂空调']],'全屋空调样式');climate.value=profile.climate_style;
  const targets=data.devices.flatMap(d=>d.controls.filter(c=>['light','switch','climate'].includes(c.kind)).map(c=>({...c,room:d.room}))),target=choices([['','选择设备或灯光回路'],...targets.map(c=>[c.id,c.room+' · '+c.name])],'设备样式目标'),style=choices([['','自动识别']],'设备展示样式');
  target.onchange=()=>{const c=targets.find(c=>c.id===target.value);style.replaceChildren(h('option','自动识别',{value:''}));const options=c?.kind==='climate'?[['ceiling_vent','吊顶出风口'],['wall_unit','壁挂空调']]:[['main_light','主灯'],['pendant','吊灯'],['led_strip','吊顶灯带'],['downlight','筒灯'],['spotlight','射灯'],['accent_light','氛围灯'],['power_supply','照明供电（不发光）']];for(const[v,t]of options)style.append(h('option',t,{value:v}));style.value=profile.overrides[target.value]?.fixture||'';};
  style.onchange=()=>{if(!target.value)return;if(style.value)profile.overrides[target.value]={fixture:style.value};else delete profile.overrides[target.value];};
  const status=h('p','',{role:'status',class:'caption'}),save=btn('保存样式',async()=>{save.disabled=true;profile.climate_style=climate.value;try{data.presentation=await api('/api/home/presentation',{expected_revision:data.presentation.revision,profile});await load();dialog.close();drawHome();toast('设备样式已保存');}catch(_){status.textContent='样式未保存，请刷新后重试。';}finally{save.disabled=false;}},{class:'primary'});
  add(section,field('空调安装方式',climate),field('设备或回路',target),field('展示样式',style),save,status);dialog.append(section);
 }
 async function drawRecordings(){
  recordingsView?.dispose();view.replaceChildren();
  const recordingApi=async(path,body,options)=>{if(path.startsWith('/api/home/recordings?')){await api('/api/home/storage/prepare',{}).catch(()=>null);if(!alive)throw new DOMException('View closed','AbortError');}return api(path,body,options);};
  recordingsView=mountRecordings(view,{api:recordingApi,isAlive:()=>alive,go,openEvidenceRecording});
 }
 async function drawConnections(){const generation=++connectionDraw;connectionSetup?.dispose();cameraConnection?.dispose();cameraPrivacy?.dispose();view.replaceChildren();connectionSetup=mountConnectionSetup(view,{onCameraSetup:slot=>mountCameraSetup(slot)});const statusSlot=h('div');view.append(statusSlot);statusSlot.append(h('p','正在检查可选服务…'));let connections,desktop,worker;try{[connections,desktop,worker]=await Promise.all([api('/api/home/connections'),api('/desktop/status').catch(()=>null),api('/api/embodied/status')]);}catch(_){if(alive&&generation===connectionDraw)statusSlot.replaceChildren(h('p','可选服务状态暂不可用，家庭连接仍可在上方设置。'),btn('重试',drawConnections));return;}if(!alive||generation!==connectionDraw)return;statusSlot.remove();
  const arl=arlPresentation(desktop?.arl);
  const rows=[['家庭设备',connections.home.simulated?'Simulator · 示例家庭':'Home Assistant',connections.home.transport==='online',connections.home.simulated?'合成示例 · 未连接真实家庭':'房间、状态与设备控制'],['录像与文件','家庭 NAS',connections.nas.connected,connections.nas.connected?'已连接 · 可准备隐私预览':'连接后可查看录像'],['智能服务','ARL',arl.connected,arl.description,arl.label,arl.recovery]];
  const list=h('section',null,{class:'connection-list'});rows.forEach(([title,source,on,description,label,recovery])=>{const info=add(h('div'),h('h2',title),h('p',description));if(recovery)info.append(h('p',recovery,{class:'caption'}));const row=add(h('article',null,{class:'connection-row'}),add(h('span',null,{class:'connection-dot'+(on?' online':'')})),info,h('span',source,{class:'caption'}),h('strong',label||(on?'已连接':'需要连接')));list.append(row);});view.append(list);cameraConnection?.dispose();cameraConnection=mountCameraConnection(list,{api,go,legacyConnections,worker,isAlive:()=>alive});
  cameraPrivacy?.dispose();cameraPrivacy=mountCameraPrivacy(view);
  const quiet=add(h('section',null,{class:'preference-row'}),add(h('div'),h('h2','安静模式'),h('p','Friday 的语音回复保持静音',{class:'muted'})));const toggle=btn(worker.quiet?'已开启':'已关闭',async()=>{await api('/api/embodied/quiet',{quiet:!worker.quiet});await drawConnections();},{role:'switch','aria-checked':String(worker.quiet)});quiet.append(toggle);view.append(quiet);
  mountSpeechOutput(view,{api,isAlive:()=>alive,toast});
  const advanced=add(h('details',null,{class:'advanced-connections'}),h('summary','高级连接设置'));advanced.addEventListener('toggle',()=>{if(advanced.open&&!advanced.loaded){advanced.loaded=true;legacyConnections(advanced);}});view.append(advanced);
  const experiments=add(h('details',null,{class:'advanced-connections'}),h('summary','实验功能与调试'));if(document.body.dataset.fileStewardEnabled==='true')experiments.append(h('p','家庭资料索引与整理预览仍在完善中。',{class:'caption'}),btn('打开资料索引预览',()=>go('Files')));experiments.append(btn('文字指令调试',()=>go('Ask Home')));view.append(experiments);
 }
 async function drawAutomations(){automationView?.dispose();view.replaceChildren();automationView=mountAutomations(view,{api,data,snapshot,go,toast,legacyRules});}
 async function drawInteraction(){const {mountInteractions}=await import('./interactions.js');if(alive){interactions?.dispose();view.replaceChildren();interactions=mountInteractions(view,{data,go,toast});}}
 function draw(){if(['Home','Devices'].includes(page))drawHome();else if(page==='Cameras')drawCameras();else if(page==='Activity')void drawRecordings();else if(page==='Settings')void drawConnections();else if(page==='Automations')void drawAutomations();else if(page==='Interaction')void drawInteraction();}
 function update(){if(page==='Cameras')updateTiles();if(['Home','Devices'].includes(page)){model?.updateDevices(data.devices);updateTiles();drawStats();if(spatialRoom&&!busy&&data.devices.find(d=>d.id===spatialDevice)?.kind!=='camera'&&!spacePanel?.contains(document.activeElement))drawSpatialPanel();if(selected&&!busy&&inspectorSlot?.isConnected){const d=data.devices.find(x=>x.id===selected);if(d&&d.kind!=='camera'&&!inspectorSlot.contains(document.activeElement))inspect(d);}}}
 void load(true);if(['Home','Devices','Cameras'].includes(page))timer=setInterval(()=>load(),10000);
 return{refresh:()=>recordingsView?recordingsView.refresh():page==='Settings'?drawConnections():load(),dispose(){alive=false;connectionSetup?.dispose();clearInterval(timer);stopPreview();recordingsView?.dispose();cameraPrivacy?.dispose();cameraConnection?.dispose();recognitionDebug?.dispose();automationView?.dispose();model?.dispose();thumbnails?.dispose();interactions?.dispose();view.replaceChildren();}};
}
