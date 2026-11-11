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
import {arlPresentation,arlRuntimePresentation} from './arl-status.js';
import {mountHomeStatus} from './home-status.js';
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
export function mountExperience(root,{page,go,legacyConnections,legacyRules,snapshot,openEvidenceRecording,archiveSettings=()=>{},requestArchiveSettings=false,onArchiveSettingsOpened=()=>{},developerMode=false,setDeveloperMode=()=>{},attentionId=null,recordingCamera=null,onOpenArchiveSettings}){
 let recognitionDebug=null;
 let automationView=null;
 let cameraPrivacy=null;
 let cameraConnection=null;
 let connectionSetup=null;let connectionDraw=0;
 let recordingsView=null,homeStatus=null;
 let thumbnails=null;try{thumbnails=deviceThumbnails();}catch(_){}
 const thumbnail=d=>{try{return thumbnails?.render(d)||null;}catch(_){return null;}};
 const deviceImage=d=>{const surface=thumbnail(d);if(surface){surface.classList.add('device-model-image');return surface;}return ico(visualMark(d));};
 let alive=true,data=null,room='全部',category='全部',query='',selected=null,model=null,timer=null,busy=false,recordings=null,recordingFolder='',recordingDate='',interactions=null;
 let loadVersion=0,loadAbort=null,hasDrawn=false;
 const view=add(h('div',null,{class:'experience','data-page':page}));root.append(view);
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
  const layout=h('div',null,{class:'product-device-layout camera-device-layout'});listSlot=h('div');inspectorSlot=h('aside',null,{class:'product-inspector',id:'device-detail-panel',hidden:''});add(layout,listSlot,inspectorSlot);view.append(layout);const cameras=data.devices.filter(d=>d.kind==='camera');listSlot.append(tiles(cameras));if(!cameras.length)listSlot.append(add(h('div',null,{class:'product-empty'}),ico('video'),h('h2','还没有接入摄像头'),h('p','接入后可以查看画面、使用语音与动作控制。'),btn('接入摄像头',()=>go('Settings'))));
  view.append(add(h('section',null,{class:'camera-teaching-link'}),ico('hand'),add(h('div'),h('h2','手势与动作教学'),h('p','看动作示意，走到镜头前试一试。')),btn('开始体验',()=>{try{sessionStorage.setItem('Friday.interactionMode','gestures');}catch(_){}go('Interaction');})));
  cameraPrivacy=mountCameraPrivacy(view);
  const advanced=add(h('details',null,{class:'camera-tools'}),h('summary','人物与手势检查'));
  advanced.addEventListener('toggle',()=>{if(advanced.open&&!recognitionDebug)recognitionDebug=mountRecognitionDebug(advanced,{go});else if(!advanced.open){recognitionDebug?.dispose();recognitionDebug=null;}});view.append(advanced);
  try{const id=sessionStorage.getItem('Friday.cameraFocus');sessionStorage.removeItem('Friday.cameraFocus');const d=data.devices.find(d=>d.kind==='camera'&&d.id===id);if(d)inspect(d);}catch(_){}
 }
 async function load(initial=false){
  if(!alive)return;
  if(initial&&page==='Settings'){void drawConnections();return;}
  const version=++loadVersion;
  loadAbort?.abort();
  const controller=new AbortController();loadAbort=controller;
  if(!hasDrawn){view.setAttribute('aria-busy','true');view.replaceChildren(h('p','正在读取家庭状态…',{class:'loading-state',role:'status'}));}
  try{
   const next=await api('/api/home/overview',undefined,{signal:controller.signal});
   if(!alive||version!==loadVersion)return;
   next.layout=await api('/api/home/layout',undefined,{signal:controller.signal}).catch(()=>null);
   if(!alive||version!==loadVersion)return;
   data=next;
   for(const d of data.devices)for(const c of d.controls)if(requestedPositions.has(c.id))c.attributes.requested_position=requestedPositions.get(c.id);
   document.dispatchEvent(new CustomEvent('Friday.homeStatus',{detail:{generated_at:next.generated_at,connected:next.connected,simulated:next.simulated,control_count:next.control_count}}));
   if(initial||!hasDrawn){draw();hasDrawn=true;if(page==='Devices'){try{const id=sessionStorage.getItem('Friday.deviceFocus');sessionStorage.removeItem('Friday.deviceFocus');const target=data.devices.find(d=>d.id===id);if(target)inspect(target);}catch(_){}}}else update();
  }catch(e){
   if(!alive||version!==loadVersion)return;
   if(initial||!hasDrawn)view.replaceChildren(add(h('div',null,{class:'product-empty'}),ico('home'),h('h2','家庭连接暂不可用'),h('p','请到连接设置完成授权，或恢复已有连接。'),btn('连接家庭',()=>go('Settings'),{class:'primary'}),btn('重试',()=>load(true))));
   else toast('家庭状态同步暂时中断',true);
  }finally{
   if(alive&&version===loadVersion){view.removeAttribute('aria-busy');loadAbort=null;}
  }
 }
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
 function stopPreview(){
  const dispose=previewStop;
  previewStop=null;
  dispose?.();
 }
 function cameraPanel(d,slot,{start=false}={}){
  stopPreview();
  const endpoint='/api/embodied/cameras/'+d.id;
  let power=null,pending=false,viewing=false,previewEpoch=0,previewTimer=null,previewToken=null;
  let modePending=false,directionPending=false,followReport=null,cameraControl=null,previousIntent=null;
  let modeVersion=0,pollSequence=0,appliedPoll=0;
  let picker=null;
  const targetPath=endpoint+'/follow-target';
  const frame=h('div',null,{class:'camera-frame'});
  const image=h('img',null,{alt:d.name+'实时画面',hidden:''});
  const placeholder=add(h('div',null,{class:'camera-placeholder','aria-hidden':'true'}),ico('video'));
  add(frame,image,placeholder);
  const targetCanvas=h('canvas',null,{'aria-label':'选取目标时的画面',role:'img'});
  const targetMarks=h('div',null,{class:'camera-target-marks','aria-hidden':'true'});
  const targetPhoto=add(h('div',null,{class:'camera-target-photo',hidden:''}),targetCanvas,targetMarks);
  const targetChoices=h('div',null,{class:'camera-target-choices',role:'group','aria-label':'画面中的目标',hidden:''});
  const status=h('p','正在读取镜头状态…',{class:'camera-status',role:'status','aria-live':'polite'});
  const powerStatus=h('span','正在读取…');
  const workspace=h('div',null,{class:'camera-workspace'});
  const liveControls=h('div',null,{class:'camera-live-controls',hidden:''});
  const followControls=h('div',null,{class:'camera-follow-actions',role:'group','aria-label':'镜头模式'});
  const cameraControls=h('div',null,{class:'camera-directions',role:'group','aria-label':'调整视角'});
  const followButtons=[],directions=[];
  const owns=()=>alive&&frame.isConnected&&previewStop===dispose;
  const current=epoch=>owns()&&epoch===previewEpoch;
  const live=()=>viewing&&(followReport?.frame_available||followReport?.state==='live'||followReport?.status==='live');
  const surveying=()=>cameraControl?.mode==='survey'&&cameraControl.phase!=='survey_finished';
  const selectingMode=()=>['person_target','pet_target'].includes(cameraControl?.mode);
  const setStatus=value=>{if(status.textContent!==value)status.textContent=value;};
  const release=token=>{if(token)void api(endpoint+'/preview/stop',{token}).catch(()=>{});};
  const modeAvailability=(button,available)=>{
   button.disabled=!available;
   button.setAttribute('aria-disabled',String(!available||modePending||!!picker));
  };

  function retireTargetPhoto(entry){
   if(!entry)return;
   clearTimeout(entry.expiry);
   entry.photo=null;
   targetCanvas.width=targetCanvas.height=0;
   targetMarks.replaceChildren();targetChoices.replaceChildren();
   targetPhoto.hidden=targetChoices.hidden=true;
   frame.hidden=false;
  }
  function leavePicker(){
   if(!picker)return;
   document.removeEventListener('visibilitychange',onPickerVisibility);
   picker.read?.abort();
   retireTargetPhoto(picker);
   picker=null;
   pickerTools.hidden=true;
  }

  function clearFrame(){
   image.hidden=true;
   placeholder.hidden=false;
  }
  function dispose(){
   previewEpoch++;
   clearInterval(previewTimer);
   previewTimer=null;
   const token=previewToken;
   previewToken=null;
   release(token);
   viewing=false;
   leavePicker();
   followReport=cameraControl=previousIntent=null;
   clearFrame();
   image.removeAttribute('src');
  }
  function syncControls(){
   workspace.setAttribute('data-viewing',String(viewing));
   workspace.setAttribute('data-picking',String(!!picker));
   liveControls.hidden=!viewing||!!picker;
   watch.textContent=viewing?'停止查看':power?.enabled===false?'打开镜头并查看':'查看实时画面';
   watch.classList.toggle('primary',!viewing);
   watch.disabled=!viewing&&(pending||!power);
   close.hidden=power?.enabled!==true;
   close.disabled=pending;
   refresh.hidden=Boolean(power);
   refresh.disabled=pending;
   powerStatus.textContent=power?.enabled===true?'镜头已开启':power?.enabled===false?'镜头已关闭':pending?'正在读取…':'镜头状态待确认';
   const frameCurrent=live();
   const canStart=frameCurrent&&followReport?.recognition_enabled!==false;
   const canPick=canStart&&followReport?.follow_target_available===true;
   targetToggle.hidden=!canPick;
   targetToggle.disabled=modePending||directionPending||!!picker;
   targetToggle.textContent=selectingMode()&&cameraControl?.phase!=='target_selecting'?'重新选择目标':'选择跟随目标';
   stopTargetButton.hidden=!viewing||!selectingMode()||!!picker;
   stopTargetButton.disabled=modePending;
   pickerTools.hidden=!picker;
   refreshTarget.hidden=!picker?.expired;
   cancelTarget.disabled=!!picker?.busy;
   for(const [kind,button]of kindButtons){
    button.setAttribute('aria-pressed',String(picker?.kind===kind));
    button.setAttribute('aria-disabled',String(!!picker?.busy||!canPick));
    button.disabled=kind==='pet'&&followReport?.pet_tracking?.supported!==true;
   }
   for(const [mode,b,label]of followButtons){
    const unsupported=mode==='pets'&&followReport?.pet_tracking?.supported!==true;
    const active=viewing&&cameraControl?.mode===mode;
    const retry=active&&cameraControl?.phase==='target_lost';
    const title=retry?'重新'+label:label;
    if(b.textContent!==title)b.textContent=title;
    modeAvailability(b,canStart&&!unsupported);
    b.title=!frameCurrent?'等待实时画面':followReport?.recognition_enabled===false?'当前镜头未开启识别':unsupported?'当前识别服务暂不支持宠物跟随':'';
    b.setAttribute('aria-pressed',String(active&&!retry));
   }
   const running=viewing&&surveying();
   survey.textContent=running?'停止巡视':'巡视一轮';
   survey.setAttribute('data-running',String(running));
   modeAvailability(survey,running||canStart);
   for(const b of directions)b.disabled=directionPending||!!picker||!frameCurrent||!(followReport?.primary||followReport?.manual_control);
  }
  async function readPower(){
   if(!owns()||pending)return;
   pending=true;syncControls();
   try{
    const result=await api(endpoint+'/power');
    if(!owns())return;
    power=result;
    if(!viewing)setStatus(power.enabled?'尚未查看':'镜头已关闭');
   }catch(_){
    if(owns()){power=null;setStatus('暂时无法读取镜头状态');}
   }finally{
    pending=false;
    if(owns())syncControls();
   }
  }
  const watch=btn('查看实时画面',async()=>{
   if(!owns())return;
   if(viewing){
    dispose();setStatus('已停止查看');syncControls();
    return;
   }
   if(pending||!power)return;
   const epoch=++previewEpoch;
   pending=true;syncControls();
   try{
    if(!power.enabled){
     setStatus('正在打开镜头…');
     const result=await api(endpoint+'/power',{enabled:true,revision:power.revision,event_id:crypto.randomUUID()});
     if(!current(epoch))return;
     if(result.status!=='reported_match'){
      power=null;setStatus('打开状态待确认');return;
     }
     power=result;
    }
    setStatus('正在连接画面…');
    const report=await api(endpoint+'/preview',{});
    if(!current(epoch)){release(report.preview_token);return;}
    viewing=true;previewToken=report.preview_token;
    syncControls();
    await poll();
    if(current(epoch)&&viewing)previewTimer=setInterval(poll,1500);
   }catch(_){if(current(epoch))setStatus('暂时无法查看画面');}
   finally{pending=false;if(owns())syncControls();}
  },{class:'primary'});
  const close=btn('关闭镜头',async()=>{
   if(!owns()||pending||!power?.enabled)return;
   pending=true;dispose();setStatus('正在关闭镜头…');syncControls();
   try{
    const result=await api(endpoint+'/power',{enabled:false,revision:power.revision,event_id:crypto.randomUUID()});
    if(!owns())return;
    if(result.status==='reported_match'){power=result;setStatus('镜头已关闭');}
    else{power=null;setStatus('关闭状态待确认');}
   }catch(_){if(owns()){power=null;setStatus('关闭状态待确认');}}
   finally{pending=false;if(owns())syncControls();}
  },{class:'text-button'});
  const refresh=btn('刷新状态',()=>void readPower(),{class:'text-button',hidden:''});
  const recordings=btn('回看录像',()=>go('Activity',{recordingCamera:d.id}));
  const pickerIsCurrent=entry=>owns()&&viewing&&picker===entry;
  const kindNames={person:'人物',pet:'宠物'};
  const expiredTarget=entry=>{
   const restoreFocus=targetChoices.contains(document.activeElement);
   retireTargetPhoto(entry);
   entry.expired=true;
   setStatus('请刷新画面后再选');
   syncControls();
   if(restoreFocus)refreshTarget.focus({preventScroll:true});
  };
  function onPickerVisibility(){
   if(!document.hidden||!picker)return;
   picker.read?.abort();picker.read=null;
   expiredTarget(picker);
  }
  function receiveTargetMode(result,reason){
   modeVersion++;
   const prior=cameraControl?.intent_revision;
   previousIntent=prior&&prior!==result.intent_revision?prior:null;
   cameraControl={mode:result.mode,reason,intent_revision:result.intent_revision};
   setStatus(reason);
  }
  function validTargetView(report){
   return /^[a-f0-9]{32}$/.test(report.snapshot)&&typeof report.jpeg==='string'&&report.jpeg.length<=2666668
    &&Number.isFinite(report.ttl_ms)&&report.ttl_ms>0&&report.ttl_ms<=8000
    &&Array.isArray(report.choices)&&report.choices.length>0&&report.choices.length<=10
    &&report.choices.every((row,index)=>{
     const b=row?.box;
     return row?.choice===index+1&&b&&['x','y','w','h'].every(key=>Number.isFinite(b[key])&&b[key]>=0)
      &&b.w>0&&b.h>0&&b.x+b.w<=1.000001&&b.y+b.h<=1.000001;
    });
  }
  async function readTargetPhoto(entry){
   if(!pickerIsCurrent(entry)||document.hidden||entry.read||entry.busy||entry.photo||entry.expired||!entry.revision)return;
   const read=new AbortController(), began=performance.now();
   entry.read=read;
   try{
    const report=await api(targetPath,undefined,{signal:read.signal});
    if(!pickerIsCurrent(entry)||entry.read!==read||report.intent_revision!==entry.revision)return;
    if(report.status==='waiting'){setStatus('等'+kindNames[entry.kind]+'入镜');return;}
    if(report.status!=='ready'||!validTargetView(report))throw Error('invalid_target_view');
    const bytes=Uint8Array.from(atob(report.jpeg),value=>value.charCodeAt(0));
    const bitmap=await createImageBitmap(new Blob([bytes],{type:'image/jpeg'}));
    try{
     if(!pickerIsCurrent(entry)||entry.read!==read||document.hidden)return;
     const remaining=report.ttl_ms-(performance.now()-began);
     if(remaining<=0||bitmap.width>4096||bitmap.height>4096){expiredTarget(entry);return;}
     targetCanvas.width=bitmap.width;targetCanvas.height=bitmap.height;
     targetCanvas.getContext('2d').drawImage(bitmap,0,0);
     const controls=[],markers=[];
     for(const row of report.choices){
      const {x,y,w,h:height}=row.box;
      const marker=add(h('div',null,{class:'camera-target-mark'}),h('span',row.choice));
      Object.assign(marker.style,{left:x*100+'%',top:y*100+'%',width:w*100+'%',height:height*100+'%'});
      markers.push(marker);
      const name={cat:'猫',dog:'狗'}[row.species]||kindNames[entry.kind];
      const choice=btn(name+' '+row.choice,()=>void chooseTarget(entry,row.choice));
      choice.addEventListener('focus',()=>marker.setAttribute('data-focus','true'));
      choice.addEventListener('blur',()=>marker.removeAttribute('data-focus'));
      controls.push(choice);
     }
     targetMarks.replaceChildren(...markers);targetChoices.replaceChildren(...controls);
     entry.photo={token:report.snapshot,choices:report.choices,deadline:performance.now()+remaining};
     entry.expiry=setTimeout(()=>{if(pickerIsCurrent(entry))expiredTarget(entry);},remaining);
     targetPhoto.hidden=targetChoices.hidden=false;frame.hidden=true;
     setStatus('点选要跟随的目标');
    }finally{bitmap.close();}
   }catch(_){if(pickerIsCurrent(entry)&&entry.read===read)expiredTarget(entry);}
   finally{if(entry.read===read)entry.read=null;}
  }
  async function prepareTarget(kind){
   if(!owns()||!live()||modePending||directionPending||picker?.busy||!cameraControl?.intent_revision)return;
   const revision=cameraControl.intent_revision;
   leavePicker();
   const entry={kind,busy:true,photo:null};picker=entry;
   document.addEventListener('visibilitychange',onPickerVisibility);
   modeVersion++;syncControls();setStatus('正在准备画面…');
   kindButtons.get(kind).focus({preventScroll:true});
   try{
    const result=await api(targetPath,{action:'prepare',kind,revision});
    if(!pickerIsCurrent(entry))return;
    if(result.mode!==(kind==='pet'?'pet_target':'person_target')||!result.intent_revision)throw Error('camera_mode_unconfirmed');
    entry.revision=result.intent_revision;
    receiveTargetMode(result,'等'+kindNames[kind]+'入镜');
    entry.busy=false;
    await readTargetPhoto(entry);
   }catch(_){
    if(pickerIsCurrent(entry)){
     leavePicker();syncControls();setStatus('暂时无法选择目标');targetToggle.focus({preventScroll:true});
    }
   }
   finally{if(pickerIsCurrent(entry))entry.busy=false;if(owns())syncControls();}
  }
  async function chooseTarget(entry,choice){
   if(!pickerIsCurrent(entry)||entry.busy||!entry.photo)return;
   if(performance.now()>=entry.photo.deadline){expiredTarget(entry);return;}
   const token=entry.photo.token;
   entry.busy=true;
   modeVersion++;syncControls();setStatus('正在确认所选目标…');
   try{
    const result=await api(targetPath,{action:'choose',snapshot:token,choice});
    if(!pickerIsCurrent(entry))return;
    if(result.status!=='selected'||result.intent_revision!==entry.revision)throw Error('follow_target_changed');
    leavePicker();receiveTargetMode(result,'正在确认所选目标');
    syncControls();targetToggle.focus({preventScroll:true});await poll();
   }catch(_){if(pickerIsCurrent(entry))expiredTarget(entry);}
   finally{entry.busy=false;if(owns())syncControls();}
  }
  async function stopTarget(){
   if(!owns()||modePending||picker?.busy)return;
   const epoch=previewEpoch,revision=picker?picker.revision:cameraControl?.intent_revision;
   leavePicker();modeVersion++;syncControls();
   if(!revision){targetToggle.focus({preventScroll:true});return;}
   modePending=true;syncControls();
   try{
    const result=await api(targetPath,{action:'stop',revision});
    if(!current(epoch))return;
    if(result.mode!=='hold')throw Error('camera_mode_unconfirmed');
    receiveTargetMode(result,'停留当前视角');await poll();
   }catch(_){if(current(epoch))setStatus('停止状态待确认');}
   finally{modePending=false;if(current(epoch)){syncControls();targetToggle.focus({preventScroll:true});}}
  }
  const targetToggle=btn('选择跟随目标',()=>{
   if(!picker)void prepareTarget(cameraControl?.mode?.includes('pet')&&followReport?.pet_tracking?.supported===true?'pet':'person');
  },{hidden:''});
  const stopTargetButton=btn('停止跟随',()=>void stopTarget(),{hidden:''});
  const kindButtons=new Map(Object.entries(kindNames).map(([kind,label])=>[kind,btn(label,()=>{
   if(picker?.kind!==kind&&!picker?.busy)void prepareTarget(kind);
  },{'aria-pressed':'false'})]));
  const refreshTarget=btn('刷新画面',()=>{if(picker&&!picker.busy){picker.expired=false;syncControls();void readTargetPhoto(picker);}},{hidden:''});
  const cancelTarget=btn('取消',()=>void stopTarget(),{class:'text-button'});
  const pickerTools=add(h('div',null,{class:'camera-target-tools',hidden:''}),
   add(h('div',null,{role:'group','aria-label':'目标类型'}),...kindButtons.values()),refreshTarget,cancelTarget);
  targetPhoto.addEventListener('click',event=>{
   const entry=picker;
   if(!entry?.photo||entry.busy)return;
   const bounds=targetCanvas.getBoundingClientRect(), x=(event.clientX-bounds.left)/bounds.width,
    y=(event.clientY-bounds.top)/bounds.height;
   const hits=entry.photo.choices.filter(({box:b})=>b.x<=x&&x<=b.x+b.w&&b.y<=y&&y<=b.y+b.h);
   if(hits.length===1)void chooseTarget(entry,hits[0].choice);
   else if(hits.length>1)setStatus('请选择画面下方的编号');
  });
  pickerTools.addEventListener('keydown',event=>{if(event.key==='Escape'){event.stopPropagation();void stopTarget();}});
  targetChoices.addEventListener('keydown',event=>{if(event.key==='Escape'){event.stopPropagation();void stopTarget();}});
  async function changeMode(mode){
   if(!owns()||modePending||!viewing||picker)return;
   const epoch=previewEpoch,prior=cameraControl?.intent_revision;
   modeVersion++;modePending=true;syncControls();
   try{
    const result=await api(endpoint+'/control',{mode});
    if(!current(epoch))return;
    if(result.mode!==mode)throw Error('camera_mode_unconfirmed');
    modeVersion++;
    const reason={auto:'等待人物',pets:'等待宠物',survey:'准备巡视',hold:'巡视已停止'}[mode];
    previousIntent=result.intent_revision&&prior&&result.intent_revision!==prior?prior:null;
    cameraControl={mode,reason,intent_revision:result.intent_revision};
    setStatus(reason);
    await poll();
   }catch(_){
    if(current(epoch))setStatus(mode==='survey'?'巡视状态待确认':mode==='hold'?'停止状态待确认':'暂时无法切换跟随');
   }finally{modePending=false;if(owns())syncControls();}
  }
  followControls.append(targetToggle,stopTargetButton);
  for(const [mode,label]of [['auto','跟随人物'],['pets','跟随宠物']]){
   const b=btn(label,()=>changeMode(mode),{'aria-pressed':'false'});
   followButtons.push([mode,b,label]);followControls.append(b);
  }
  const survey=btn('巡视一轮',()=>changeMode(surveying()?'hold':'survey'));
  followControls.append(survey);
  for(const [direction,label]of [['up','向上'],['left','向左'],['right','向右'],['down','向下']]){
   const control=btn(null,async()=>{
    if(!owns()||directionPending||!live()||picker)return;
    const epoch=previewEpoch;
    directionPending=true;syncControls();
    try{
     await api(endpoint+'/control',{direction});
     if(current(epoch))setStatus('调整指令已发送');
    }catch(_){if(current(epoch))setStatus('云台暂不可用');}
    finally{directionPending=false;if(owns())syncControls();}
   },{'aria-label':label,'data-direction':direction});
   control.append(ico('chevron-right'));
   directions.push(control);cameraControls.append(control);
  }
  async function poll(){
   if(!owns()||!viewing)return;
   const epoch=previewEpoch,version=modeVersion,sequence=++pollSequence;
   const accepted=()=>current(epoch)&&viewing&&version===modeVersion&&sequence>=appliedPoll;
   try{
    const report=await api(endpoint+'/preview');
    if(!accepted())return;
    appliedPoll=sequence;followReport=report;
    if(report.recognition_enabled===false){cameraControl=null;previousIntent=null;}
    else if(report.camera_control&&report.camera_control.intent_revision!==previousIntent){
     cameraControl=report.camera_control;previousIntent=null;
    }
    if(live()){
     image.src=endpoint+'/frame?t='+Date.now();
     image.hidden=false;placeholder.hidden=true;
     if(picker?.revision&&(!selectingMode()||cameraControl?.intent_revision!==picker.revision||!report.follow_target_available))leavePicker();
     if(picker)void readTargetPhoto(picker);
     else setStatus(report.recognition_enabled===false?'实时画面':cameraControl?.reason||'实时画面');
    }else{
     leavePicker();
     clearFrame();
     const reason=report.reason||report.state||report.status;
     if(['privacy_closed','privacy_shutter_closed','shutter_closed'].includes(reason)){
      dispose();power=null;setStatus('镜头已关闭');void readPower();
     }else{
      setStatus(cameraControl?.survey_result||modePending&&cameraControl?.mode==='hold'?cameraControl.reason:'正在连接画面，请稍候…');
     }
    }
   }catch(_){
    if(!accepted())return;
    appliedPoll=sequence;followReport=null;leavePicker();clearFrame();setStatus('画面连接中断，正在重连…');
   }finally{if(owns())syncControls();}
  }
  image.addEventListener('error',()=>{
   if(owns()&&viewing&&live()){clearFrame();if(!picker)setStatus('暂时无法显示画面');}
  });
  const viewActions=add(h('div',null,{class:'camera-view-actions'}),watch,recordings);
  const viewBar=add(h('div',null,{class:'camera-view-bar'}),status,viewActions);
  const picture=add(h('div',null,{class:'camera-picture'}),pickerTools,frame,targetPhoto,targetChoices,viewBar);
  add(liveControls,add(h('div'),h('h3','镜头模式'),followControls),add(h('div'),h('h3','调整视角'),cameraControls));
  const powerRow=add(h('div',null,{class:'camera-power-row'}),add(h('div'),ico('power'),powerStatus),close,refresh,
    btn('人物识别',()=>go('People'),{class:'text-button'}));
  add(workspace,picture,liveControls,powerRow);slot.append(workspace);
  previewStop=dispose;
  void readPower().then(()=>{if(start&&owns()&&power?.enabled)watch.click();});
 }

 function inspect(d,{viewCamera=false}={}){if(!inspectorSlot)return;const wasOpen=selected===d.id&&!inspectorSlot.hidden;if(!wasOpen)inspectorTrigger={device:d.id,key:document.activeElement?.dataset?.controlKey||'details'};selected=d.id;stopPreview();inspectorSlot.replaceChildren();inspectorSlot.setAttribute('tabindex','-1');inspectorSlot.setAttribute('aria-label',d.name+'设备详情');
  const close=btn('关闭',()=>{selected=null;inspectorSlot.hidden=true;stopPreview();updateTiles();const card=[...listSlot.querySelectorAll('[data-device]')].find(n=>n.dataset.device===inspectorTrigger?.device);([...card?.querySelectorAll('[data-control-key]')||[]].find(n=>n.dataset.controlKey===inspectorTrigger?.key)||card?.querySelector('.device-open'))?.focus?.({preventScroll:true});},{class:'inspector-close','aria-label':'关闭设备详情'});
  add(inspectorSlot,close,add(h('div',null,{class:'product-device-heading'}),ico(visualMark(d)),h('h2',d.name),h('p',d.room,{class:'device-room'})));
  inspectorSlot.hidden=false;
  if(d.kind!=='camera')inspectorSlot.append(h('p',deviceSummary(d),{class:'device-state-summary',role:'status'}));
  if(d.kind==='camera')cameraPanel(d,inspectorSlot,{start:viewCamera});
  else if(d.controls.length)d.controls.forEach(c=>inspectorSlot.append(typeControl(c,{showName:d.controls.length>1})));
  else inspectorSlot.append(h('p',deviceSummary(d),{class:'device-state-summary'}));
  if(d.triggers?.length)inspectorSlot.append(mainDeviceControls({...d,controls:[]},{send:control,busy}));
  deviceReadings(d,inspectorSlot);
  const details=add(h('details',null,{class:'device-diagnostics'}),h('summary','设备信息'),h('p',d.presentation?.label||names[d.kind]||'设备'),h('p',`房间：${d.room}`));inspectorSlot.append(details);
  inspectorSlot.onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();close.click();}};updateTiles();if(!wasOpen){inspectorSlot.focus?.({preventScroll:true});if(globalThis.matchMedia?.('(max-width:1200px)').matches)inspectorSlot.scrollIntoView?.({block:'start'});}
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
  const header=h('div',null,{class:'spatial-panel-head'}),close=add(btn(null,closeSpatialPanel,{'aria-label':'关闭空间控制',class:'spatial-close'}),ico('x'));
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
    if(power){const on=power.value==='on',toggle=btn(null,()=>toggleSpatial(power.id,toggle),{class:'spatial-power'+(on?' on':''),role:'switch','aria-checked':String(on),'aria-label':'空间'+(on?'关闭':'开启')+d.name});toggle.append(ico('power'));toggle.disabled=!power.available||busy;row.append(toggle);}
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
  const open=btn(null,()=>inspect(d),{class:'device-open','aria-label':'查看'+d.name,'data-control-key':'details'});add(open,add(h('span',null,{class:'tile-icon'+(d.controls.some(c=>c.available&&c.value==='on')?' active':'')}),ico(visualMark(d))),h('span',d.room,{class:'tile-room'}),h('strong',d.name));
  if(d.kind!=='camera')open.append(h('span',deviceSummary(d),{class:'tile-state'}));card.append(open);
  card.append(mainDeviceControls(d,{send:control,wake:wakeCamera,readCamera,viewCamera:d=>inspect(d,{viewCamera:true}),details:()=>inspect(d),busy}));grid.append(card);
 });return grid;}
 function updateTiles(){if(!listSlot||!data)return;if(listSlot.contains(document.activeElement)&&document.activeElement?.type==='range')return;const active=document.activeElement,focused=listSlot.contains(active)?active?.closest?.('[data-device]')?.dataset.device:null,key=active?.dataset?.controlKey;const ds=data.devices.filter(d=>(page!=='Cameras'||d.kind==='camera')&&(room==='全部'||d.room===room)&&(category==='全部'||d.kind===category||category==='switch'&&d.kind==='trigger'||category==='light'&&d.controls.some(c=>c.kind==='light'))&&(!query||(d.name+d.room).toLowerCase().includes(query.toLowerCase())));listSlot.replaceChildren(page==='Devices'?deviceRows(ds):tiles(ds));const count=view.querySelector('[data-device-count]');if(count)count.textContent=ds.length+' 个设备';if(!ds.length)listSlot.append(add(h('div',null,{class:'product-empty'}),ico('devices'),h('h3',data.devices.length?'没有符合条件的设备':'还没有设备'),h('p',data.devices.length?'试试其他房间、类型或关键词。':'连接家庭后，设备会显示在这里。'),btn(data.devices.length?'显示全部设备':'连接家庭',()=>{if(!data.devices.length){go('Settings');return;}query='';category='全部';room='全部';drawHome();})));if(focused){const card=[...listSlot.querySelectorAll('[data-device]')].find(n=>n.dataset.device===focused);([...card?.querySelectorAll('[data-control-key]')||[]].find(n=>n.dataset.controlKey===key)||card?.querySelector('.device-open'))?.focus?.({preventScroll:true});}}
 function focusRoom(name){
  room=name;
  for(const b of view.querySelectorAll('.room-pills button'))b.setAttribute('aria-pressed',String((b.dataset.room||b.textContent)===room));
  model?.focus(room==='全部'?null:room);
  const title=view.querySelector('[data-room-heading]');if(title)title.textContent=room==='全部'?'设备':room+'的设备';
  updateTiles();
  if(spatialRoom&&name!==spatialRoom){if(name==='全部')closeSpatialPanel();else{spatialRoom=name;spatialDevice=null;drawSpatialPanel();}}
 }
 function filters(parent){
  const row=h('div',null,{class:'room-pills',role:'group','aria-label':'房间'});
  for(const name of ['全部',...data.rooms])row.append(btn(name,()=>{focusRoom(name);if(page==='Home'&&name!=='全部')openRoomDevices(name);},{'aria-pressed':String(room===name)}));
  parent.append(row);
 }
 function roomDirectory(parent){
  const rows=h('nav',null,{class:'room-pills device-room-list','aria-label':'房间'});
  for(const name of ['全部',...data.rooms]){
   const count=name==='全部'?data.devices.length:data.devices.filter(d=>d.room===name).length;
   const row=btn(null,()=>focusRoom(name),{'data-room':name,'aria-pressed':String(room===name)});
   row.append(h('span',name==='全部'?'全部设备':name),h('span',count,{class:'room-device-count'}));rows.append(row);
  }
  parent.append(rows);
 }
 function deviceRows(devices){
  const list=h('div',null,{class:'device-rows'}),groups=new Map();
  for(const d of devices){const name=room==='全部'?d.room:names[d.kind]||'其他';if(!groups.has(name))groups.set(name,[]);groups.get(name).push(d);}
  for(const[name,items]of groups){
   const group=add(h('section',null,{class:'device-group'}),h('h3',name));
   for(const d of items){
    const row=h('article',null,{class:'product-device device-list-row'+(selected===d.id?' selected':''),'data-device':d.id});
    const open=btn(null,()=>inspect(d),{class:'device-open','data-control-key':'details','aria-label':'查看'+d.name,'aria-expanded':String(selected===d.id),'aria-controls':'device-detail-panel'});
    open.append(add(h('span',null,{class:'tile-icon'+(d.controls.some(c=>c.available&&c.value==='on')?' active':'')}),ico(visualMark(d))),add(h('span',null,{class:'device-row-copy'}),h('strong',d.name),h('span',deviceSummary(d),{class:'tile-state'})));
    row.append(open,mainDeviceControls(d,{send:control,wake:wakeCamera,readCamera,viewCamera:d=>inspect(d,{viewCamera:true}),details:()=>inspect(d),busy}));
    group.append(row);
   }
   list.append(group);
  }
  return list;
 }
 function drawHome(){
  stopPreview();model?.dispose();model=null;spacePanel=null;spatialScene=null;listSlot=null;inspectorSlot=null;statsSlot=null;
  homeStatus?.dispose();homeStatus=null;view.replaceChildren();connectionSetup?.dispose();connectionSetup=null;
  if(!data.connected&&!data.simulated)view.append(add(h('section',null,{class:'home-source-banner source-offline',role:'status'}),ico('alert-circle'),add(h('div'),h('strong','家庭连接已中断'),h('p','显示上次记录，请恢复连接后再操作。')),btn('恢复连接',()=>go('Settings'))));
  const dashboard=page==='Home'?h('div',null,{class:'home-dashboard'}):null;const space=dashboard?h('section',null,{class:'home-space','aria-label':'家庭空间'}):null;if(dashboard){const tools=add(h('div',null,{class:'home-space-tools'}),h('h2','家庭空间'),btn('户型与位置',editHouse,{class:'text-button'}));space.append(tools);filters(space);dashboard.append(space);view.append(dashboard);}
  if(page==='Home'&&data.layout?.rooms?.length){
   const scene=h('section',null,{class:'house-scene','aria-label':'家庭空间控制'});spatialScene=scene;modelSlot=h('div',null,{class:'house-canvas'});scene.append(modelSlot);
   scene.append(add(h('div',null,{class:'model-toolbar'}),btn('显示全屋',()=>{focusRoom('全部');closeSpatialPanel();model?.reset();}),btn('重置视角',()=>model?.reset())));
   scene.append(h('span','拖动旋转 · 滚动缩放',{class:'model-hint'}));
   spacePanel=h('aside',null,{class:'spatial-panel',hidden:'',tabindex:'-1','aria-label':'空间设备控制'});
   spacePanel.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();closeSpatialPanel();}});
   placementNotice=h('div',null,{class:'spatial-placement',hidden:'',role:'status'});add(scene,spacePanel,placementNotice);space.append(scene);
   try{model=mountHouse(modelSlot,{rooms:data.layout.rooms,placements:data.layout.devices,devices:data.devices,selectedRoom:room,onRoom:openRoomDevices,onDevice:d=>openSpatialDevice(d,true),onPlace:savePlacement,onCancelPlace:cancelPlacement,onQuick:spatialQuick,onRoomDevices:openRoomDevices,thumbnail});}
   catch(_){modelSlot.append(add(h('div',null,{class:'product-empty'}),h('p','户型暂时无法显示，仍可在下方控制设备。'),btn('重新加载',()=>load(true))));}
   if(spatialRoom)drawSpatialPanel();
  }
  if(page==='Home'){
   if(!data.layout?.rooms?.length)space.append(add(h('div',null,{class:'home-plan-empty'}),ico('home'),h('h3','把你的家放在这里'),h('p','添加户型，即可在空间中查看和控制设备。'),btn('添加户型',editHouse,{class:'primary'})));
   space.append(add(h('div',null,{class:'home-space-footer'}),h('span',data.rooms.length+' 个房间 · '+data.devices.length+' 个设备'),btn('查看全部设备',()=>go('Devices'),{class:'text-button'})));
   const aside=h('aside',null,{class:'home-brief','aria-label':'运行与待处理事项'});dashboard.append(aside);
   homeStatus?.dispose();homeStatus=mountHomeStatus(aside,{api,go,devices:data.devices,simulated:data.simulated,onDevice:d=>{if(model)openSpatialDevice(d);else{try{sessionStorage.setItem('Friday.deviceFocus',d.id);}catch(_){}go('Devices');}}});
   return;
  }
  const browser=h('div',null,{class:'device-browser'});view.append(browser);roomDirectory(browser);
  const main=h('section',null,{class:'device-browser-main','aria-label':'设备列表'});browser.append(main);
  const toolbar=add(h('div',null,{class:'product-section-head device-list-heading'}),h('span',data.devices.length+' 个设备',{'data-device-count':'',class:'device-list-count'}));
  const search=h('input',null,{type:'search',placeholder:'搜索设备','aria-label':'搜索设备'});search.value=query;
  search.addEventListener('input',()=>{query=search.value;updateTiles();});toolbar.append(search);main.append(toolbar);
  const kinds=choices([['全部','所有类型'],...Object.entries(names).filter(([k])=>data.devices.some(d=>d.kind===k))],'设备类型');kinds.value=category;kinds.addEventListener('change',()=>{category=kinds.value;updateTiles();});toolbar.insertBefore(kinds,search);
  const layout=h('div',null,{class:'product-device-layout'});listSlot=h('div');inspectorSlot=h('aside',null,{class:'product-inspector',id:'device-detail-panel',hidden:''});add(layout,listSlot,inspectorSlot);main.append(layout);
  updateTiles();if(selected){const d=data.devices.find(x=>x.id===selected);if(d)inspect(d);}
 }
 function drawStats(){}
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
  recordingsView=mountRecordings(view,{api:recordingApi,isAlive:()=>alive,go,openEvidenceRecording,
   camera:recordingCamera,openArchiveSettings:onOpenArchiveSettings});
 }
 async function drawConnections(){const generation=++connectionDraw;connectionSetup?.dispose();cameraConnection?.dispose();cameraPrivacy?.dispose();view.replaceChildren();connectionSetup=mountConnectionSetup(view,{includeCamera:false,onCameraSetup:slot=>mountCameraSetup(slot)});const statusSlot=h('div');view.append(statusSlot);statusSlot.append(h('p','正在检查可选服务…'));let connections,desktop,worker;try{[connections,desktop,worker]=await Promise.all([api('/api/home/connections'),api('/desktop/status').catch(async()=>({arl:await api('/api/settings/arl/status').then(v=>({...v,metadataFreshness:v.metadata_freshness,refreshPending:v.metadata_refresh_pending})).catch(()=>null)})),api('/api/embodied/status')]);}catch(_){if(alive&&generation===connectionDraw)statusSlot.replaceChildren(h('p','可选服务状态暂不可用，家庭连接仍可在上方设置。'),btn('重试',drawConnections));return;}if(!alive||generation!==connectionDraw)return;statusSlot.remove();
  const arl=desktop?.arl?.metadata_freshness!==undefined?arlRuntimePresentation(desktop.arl):arlPresentation(desktop?.arl);const arlLabel=({connected:'已连接',registering:'正在连接',handshake_required:'尚未连接',upgrade_required:'需要更新'}[arl.state]||arl.label);
  const rows=[['家庭设备',connections.home.simulated?'Simulator · 示例家庭':'Home Assistant',connections.home.transport==='online',connections.home.simulated?'合成示例 · 未连接真实家庭':'房间、状态与设备控制'],['录像存储','系统共享目录',connections.nas.connected,connections.nas.connected?'已连接 · 可准备隐私预览':'连接后可查看录像'],['智能服务','ARL',arl.connected,arl.description,arlLabel,arl.recovery]];
  const direct=connections.file_source;if(direct){const online=direct.state==='online'&&direct.reachable===true&&direct.authenticated===true&&direct.readable===true;rows.splice(2,0,['家庭资料','NAS 直连',online,online?'资料目录可读取':'连接后可查看家庭资料目录']);}
  const list=h('section',null,{class:'connection-list'});rows.forEach(([title,source,on,description,label,recovery])=>{const info=add(h('div'),h('h2',title));if(recovery)info.append(add(h('details',null,{class:'connection-state-details'}),h('summary','连接详情'),h('p',description),h('p',recovery)));else info.append(h('p',description));const row=add(h('article',null,{class:'connection-row'}),add(h('span',null,{class:'connection-dot'+(on?' online':'')})),info,h('span',source,{class:'caption'}),h('strong',label||(on?'已连接':'需要连接')));if(title==='录像存储')info.append(btn('录像对应',()=>void openArchiveMapping(),{class:'text-button'}));list.append(row);});view.append(list);cameraConnection?.dispose();cameraConnection=mountCameraConnection(list,{api,go,legacyConnections,worker,isAlive:()=>alive});
  const archiveMapping=h('section',null,{class:'archive-mapping-settings',hidden:''});view.append(archiveMapping);let mappingLoaded=false,mappingPending=false;
  async function openArchiveMapping(){if(mappingPending)return;archiveMapping.hidden=false;if(!mappingLoaded){mappingPending=true;archiveMapping.replaceChildren(h('p','正在读取录像设置…',{role:'status'}));try{await archiveSettings(archiveMapping);mappingLoaded=true;}catch{if(alive)archiveMapping.replaceChildren(h('p','录像设置暂不可读取。',{role:'status'}),btn('重试',()=>void openArchiveMapping()));}finally{mappingPending=false;}}if(alive)archiveMapping.scrollIntoView?.({block:'start',behavior:'instant'});}
  if(requestArchiveSettings){requestArchiveSettings=false;onArchiveSettingsOpened();void openArchiveMapping();}
  cameraPrivacy?.dispose();cameraPrivacy=mountCameraPrivacy(view);
  const quiet=add(h('section',null,{class:'preference-row'}),add(h('div'),h('h2','安静模式'),h('p','Friday 的语音回复保持静音',{class:'muted'})));const toggle=btn(worker.quiet?'已开启':'已关闭',async()=>{await api('/api/embodied/quiet',{quiet:!worker.quiet});await drawConnections();},{role:'switch','aria-checked':String(worker.quiet)});quiet.append(toggle);view.append(quiet);
  mountSpeechOutput(view,{api,isAlive:()=>alive,toast});
  const advanced=add(h('details',null,{class:'advanced-connections'}),h('summary','连接与权限详情'));advanced.addEventListener('toggle',()=>{if(advanced.open&&!advanced.loaded){advanced.loaded=true;legacyConnections(advanced);}});view.append(advanced);const nasSetup=btn('设置家庭资料连接',()=>{if(!advanced.loaded){advanced.loaded=true;legacyConnections(advanced,'settings-nas');}advanced.open=true;advanced.scrollIntoView?.({block:'start',behavior:'instant'});},{class:'text-button'});list.append(nasSetup);
  const experiments=add(h('details',null,{class:'advanced-connections'}),h('summary','实验功能与调试'));if(document.body.dataset.fileStewardEnabled==='true')experiments.append(h('p','家庭资料索引与整理预览仍在完善中。',{class:'caption'}),btn('打开资料索引预览',()=>go('Files')));const debug=h('input',null,{type:'checkbox'});debug.checked=developerMode;debug.addEventListener('change',()=>setDeveloperMode(debug.checked));experiments.append(add(h('label',null,{class:'settings-debug-toggle'}),debug,h('span','在导航中显示系统状态与调试工具')),btn('文字指令调试',()=>go('Ask Home')));view.append(experiments);
 }
 async function drawAutomations(){automationView?.dispose();view.replaceChildren();automationView=mountAutomations(view,{api,data,snapshot,go,toast,legacyRules,attentionId});}
 async function drawInteraction(){const {mountInteractions}=await import('./interactions.js');if(alive){interactions?.dispose();view.replaceChildren();interactions=mountInteractions(view,{data,go,toast});}}
 function draw(){if(['Home','Devices'].includes(page))drawHome();else if(page==='Cameras')drawCameras();else if(page==='Activity')void drawRecordings();else if(page==='Settings')void drawConnections();else if(page==='Automations')void drawAutomations();else if(page==='Interaction')void drawInteraction();}
 function update(){homeStatus?.update(data.devices);if(page==='Cameras')updateTiles();if(['Home','Devices'].includes(page)){model?.updateDevices(data.devices);updateTiles();drawStats();if(spatialRoom&&!busy&&data.devices.find(d=>d.id===spatialDevice)?.kind!=='camera'&&!spacePanel?.contains(document.activeElement))drawSpatialPanel();if(selected&&!busy&&inspectorSlot?.isConnected){const d=data.devices.find(x=>x.id===selected);if(d&&d.kind!=='camera'&&!inspectorSlot.contains(document.activeElement))inspect(d);}}}
 void load(true);if(['Home','Devices','Cameras'].includes(page))timer=setInterval(()=>{if(!loadAbort)void load();},10000);
 return{selectTaskAttention(id){attentionId=id;automationView?.selectAttention(id);},refresh:()=>{if(homeStatus)void homeStatus.refresh();return recordingsView?recordingsView.refresh():page==='Settings'?drawConnections():load();},dispose(){alive=false;++loadVersion;loadAbort?.abort();loadAbort=null;homeStatus?.dispose();connectionSetup?.dispose();clearInterval(timer);stopPreview();recordingsView?.dispose();cameraPrivacy?.dispose();cameraConnection?.dispose();recognitionDebug?.dispose();automationView?.dispose();model?.dispose();thumbnails?.dispose();interactions?.dispose();view.replaceChildren();}};
}
