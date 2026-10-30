/* Main controls follow the device's admitted capabilities. No inferred commands. */
const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{children.forEach(c=>c&&n.append(c));return n;};
const cap=(c,name)=>c.capabilities?.find(x=>x.name===name);
export function temperatureStep(c,delta){
 const limits=cap(c,'climate.temperature'),value=c.attributes?.temperature;
 if(!limits||typeof value!=='number'||!Number.isFinite(value))return null;
 const desired=Number((value+delta).toFixed(4)),low=limits.minimum,high=limits.maximum,step=limits.step;
 if(typeof low!=='number'||typeof high!=='number'||desired<low||desired>high)return null;
 if(step&&Math.abs((desired-low)/step-Math.round((desired-low)/step))>1e-6)return null;
 return desired;
}
export function mainDeviceControls(d,{send,wake,readCamera,viewCamera,details,busy=false}){
 const root=h('div',null,{class:'device-main-controls device-controls-'+d.kind});
 function button(label,key,run,{pressed,primary=false,icon}={}){
  const b=h('button',null,{type:'button',class:'device-command'+(primary?' primary-command':''),'data-control-key':key,'aria-label':d.name+' · '+label,title:label});
  if(pressed!==undefined)b.setAttribute('aria-pressed',String(pressed));
  if(icon)b.append(h('span',icon,{class:'command-symbol','aria-hidden':'true'}));b.append(h('span',label,{class:icon?'visually-hidden':'command-text'}));b.disabled=busy;b.addEventListener('click',()=>run(b));return b;
 }
 function power(c,operation,on){
  const action=on?'关闭':'开启',b=button(action,'power:'+c.id,b=>send(c,operation,{power:on?'off':'on'},b),{pressed:on,icon:'⏻'});
  b.classList.add('device-power-button');b.dataset.loadKind=c.kind;b.setAttribute('aria-label',d.name+(c.name!==d.name?' · '+c.name:'')+' · '+action);b.title=c.name+' · '+action;b.disabled||=c.available===false;return b;
 }
 function cover(c){
  const state=cap(c,'cover.state'),position=cap(c,'cover.position');if(!state&&!position)return;
  const angular=c.attributes?.position_semantics==='slat_angle',row=h('div',null,{class:'curtain-commands'});
  for(const[value,label,icon]of [['open','打开','↔'],['stopped','停止','Ⅱ'],['closed','关闭','⇥⇤']]){
   if(value==='stopped'&&!state?.choices?.includes('stopped'))continue;
   const op=value==='stopped'||!position?'cover.state':'cover.position',desired=op==='cover.position'?{position:value==='open'?100:0}:{state:value};
   const b=button(label,value+':'+c.id,b=>send(c,op,desired,b),{icon:angular?(value==='open'?'↺':value==='closed'?'↻':'Ⅱ'):icon});if(angular){b.title=value==='stopped'?'停止':'叶片调光至 '+(value==='open'?'100%':'0%');b.setAttribute('aria-label',d.name+' · '+b.title);}b.disabled||=c.available===false;row.append(b);
  }root.append(row);
  if(angular&&position||cap(c,'cover.tilt')){
   const range=h('input',null,{type:'range',min:0,max:100,step:1,'aria-label':d.name+'叶片调光','data-control-key':'tilt:'+c.id}),value=angular?(c.attributes?.requested_position??(c.attributes?.feedback_quality==='unreliable'?null:c.attributes?.current_position)):c.attributes?.current_tilt_position;
   const uncertain=c.attributes?.feedback_quality==='unreliable',out=h('output',typeof value==='number'?(uncertain?'最后设置 ':'')+value+'%'+(uncertain?' · 待确认':''):'角度待确认');range.value=typeof value==='number'?value:50;if(uncertain)range.setAttribute('aria-valuetext',typeof value==='number'?'最后设置 '+value+'%，实际角度待确认':'实际角度待确认，滑动以设置叶片');range.disabled=busy||c.available===false;
   range.addEventListener('input',()=>{out.textContent='设置为 '+range.value+'%';range.setAttribute('aria-valuetext','设置为 '+range.value+'%');});range.addEventListener('change',()=>send(c,angular?'cover.position':'cover.tilt',{[angular?'position':'tilt_position']:Number(range.value)},range));
   root.append(add(h('label',null,{class:'card-tilt'}),add(h('span'),h('span','叶片调光'),out),range));
  }
 }
 function climate(c){
  const row=h('div',null,{class:'climate-power-row'}),modes=cap(c,'climate.mode')?.choices||[],on=c.value!=='off'&&modes.includes(c.value);
  if(cap(c,'climate.power'))row.append(power(c,'climate.power',on));
  else if(modes.includes('off')&&(on||modes.filter(x=>x!=='off').length===1)){
   const b=button(on?'关闭':'开启','power:'+c.id,b=>send(c,'climate.mode',{hvac_mode:on?'off':modes.find(x=>x!=='off')},b),{pressed:on,icon:'⏻'});b.classList.add('device-power-button');b.disabled||=c.available===false;row.append(b);
  }else if(modes.length)row.append(button('选择模式','mode:'+c.id,details));
  const target=c.attributes?.temperature,limit=cap(c,'climate.temperature');
  row.append(add(h('div',null,{class:'climate-setpoint'}),h('span','设定温度'),add(h('strong'),h('span',typeof target==='number'?String(target):'—'),h('small',limit?.unit||'°C'))));root.append(row);
  if(limit){const steps=h('div',null,{class:'temperature-steps'});for(const[delta,label]of [[-1,'调低 1°'],[1,'调高 1°']]){const desired=temperatureStep(c,delta),b=button(label,'temperature:'+delta+':'+c.id,b=>send(c,'climate.temperature',{temperature:desired},b),{icon:delta<0?'−':'＋'});b.disabled||=c.available===false||desired===null;steps.append(b);}root.append(steps);}
 }
 if(d.kind==='camera'){
  const state=h('p','正在读取镜头状态…',{class:'card-camera-state',role:'status','aria-live':'polite'}),action=button('正在读取…','camera',()=>{},{icon:'⏻'});action.disabled=true;root.append(state,action);
  const setAction=label=>{action.lastElementChild.textContent=label;action.setAttribute('aria-label',d.name+' · '+label);action.title=label;};
  const load=async()=>{try{const status=await readCamera(d.id);if(!root.isConnected)return;state.textContent=status.enabled?'镜头已打开':'镜头已遮蔽';setAction(status.enabled?'查看画面':'唤醒镜头');action.disabled=busy;action.onclick=async()=>{if(action.disabled)return;action.disabled=true;try{if(status.enabled)viewCamera(d);else{state.textContent='正在唤醒镜头…';const result=await wake(d.id);state.textContent=result.status==='reported_match'?'镜头已打开':'已发送，状态尚未确认，请刷新状态';if(result.status==='reported_match'){status.enabled=true;setAction('查看画面');}else{setAction('刷新状态');action.onclick=load;}}}catch(_){state.textContent='操作未完成，请刷新状态后重试';setAction('刷新状态');action.onclick=load;}finally{action.disabled=false;}};}catch(_){if(!root.isConnected)return;state.textContent='镜头状态暂不可用';setAction('重新读取镜头状态');action.disabled=false;action.onclick=load;}};
  queueMicrotask(load);return root;
 }
 for(const trigger of d.triggers||[]){root.append(add(h('div',null,{class:'wireless-key'}),h('span','◉',{class:'wireless-key-symbol','aria-hidden':'true'}),add(h('span'),h('strong',trigger.name),h('small',trigger.role==='unknown'?'按键模式待确认':'动作按键'))));}
 const controls=d.controls||[],primary=controls.find(c=>c.kind===d.kind)||controls[0];
 if(d.kind==='cover'&&primary)cover(primary);
 else if(d.kind==='climate'&&primary)climate(primary);
 else if(['humidifier','water_heater'].includes(d.kind)&&primary){
  const c=primary,kind=d.kind,modeCap=cap(c,kind+'.mode'),powerCap=cap(c,kind+'.power'),on=c.value!=='off'&&c.value!=null&&c.value!=='unknown'&&c.value!=='unavailable';
  if(powerCap)root.append(power(c,kind+'.power',on));
  const measured=kind==='humidifier'?c.attributes?.current_humidity:c.attributes?.current_temperature;
  if(typeof measured==='number')root.append(h('p',(kind==='humidifier'?'当前湿度 ':'当前水温 ')+measured+(kind==='humidifier'?'%':'°C'),{class:'caption'}));
  const humidity=cap(c,'humidifier.humidity'),target=c.attributes?.humidity;
  if(humidity){const row=h('div',null,{class:'temperature-steps'});for(const[delta,symbol]of [[-5,'−'],[5,'＋']]){const desired=target+delta,b=button(delta<0?'调低湿度':'调高湿度','humidity:'+delta+':'+c.id,b=>send(c,'humidifier.humidity',{humidity:desired},b),{icon:symbol});b.disabled||=c.available===false||typeof target!=='number'||desired<humidity.minimum||desired>humidity.maximum||Math.abs((desired-humidity.minimum)/(humidity.step||1)-Math.round((desired-humidity.minimum)/(humidity.step||1)))>1e-6;row.append(b);}root.append(h('p','设定湿度 '+(typeof target==='number'?target+'%':'—'),{class:'caption'}),row);}
  if(modeCap){const select=h('select',null,{'aria-label':d.name+' · 运行模式','data-control-key':'mode:'+c.id});for(const mode of modeCap.choices||[])select.append(h('option',mode==='off'?'关闭':mode,{value:mode}));select.value=c.attributes?.[kind==='humidifier'?'mode':'operation_mode']??c.value;select.disabled=busy||c.available===false;select.addEventListener('change',()=>send(c,kind+'.mode',{[kind==='humidifier'?'mode':'operation_mode']:select.value},select));root.append(select);}
 }

 else for(const c of controls){
  const operation=['light.set','switch.set','fan.set'].find(name=>cap(c,name));
  if(operation){const b=power(c,operation,c.value==='on');if(controls.length>1)root.append(add(h('div',null,{class:'channel-command'}),add(h('span'),h('strong',c.name),h('small',c.available===false?'暂不可用':c.value==='on'?'开启':'关闭')),b));else root.append(b);}
  else if(cap(c,'media_player.power'))root.append(power(c,'media_player.power',c.value!=='off'));
 }
 return root;
}
