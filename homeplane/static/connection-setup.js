const h=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const add=(n,...children)=>{children.forEach(c=>c&&n.append(c));return n;};
const button=(text,run,primary=false)=>{const n=h('button',text,{type:'button',...(primary?{class:'primary'}:{})});n.onclick=run;return n;};
const link=(text,url)=>h('a',text,{href:url,target:'_blank',rel:'noopener noreferrer',class:'setup-link'});
const request=async(path,body)=>{const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);try{const r=await fetch(path,{signal:controller.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});const value=await r.json();if(!r.ok)throw Error(value.code||'connection_unavailable');return value;}finally{clearTimeout(timer);}};
export function setupPresentation(config,auth){
 const connected=config.home_readable===true&&config.active_source!=='mijia-lan';
 const authorized=connected||auth.credential_available===true&&auth.server_acceptance!=='unavailable';
 const state=connected?'ready':!config.ha_url?'address':!authorized?'authorize':'read';
 return{state,connected,authorized,remaining:state==='address'||state==='authorize'?2:state==='read'?1:0};
}
export function mountCameraSetup(root){
 const existing=root.querySelector('.camera-setup');if(existing){existing.querySelector('summary')?.focus();return{dispose(){existing.remove();}};}
 const slot=h('section',null,{class:'setup-form camera-setup','aria-label':'摄像头接入方式'}),form=h('form',null,{class:'setup-form'}),url=h('input',null,{type:'url',required:'',placeholder:'http://你的服务地址:端口'}),result=h('p','',{role:'status','aria-live':'polite'}),submit=h('button','连接服务',{type:'submit'});
 const guide=add(h('details',null,{class:'setup-help'}),h('summary','已有兼容的 Miloco 服务？'),h('p','此入口适用于旧版 Miloco 本机服务。在服务自己的本机页面完成米家登录后，填写服务地址。当前 Miloco 插件请按上方官方指南部署。'));
 slot.append(h('h4','选择摄像头接入方式'),h('p','使用本机米家 SDK 的摄像头，可在下方“摄像头与识别”中检查授权或重新登录。首次安装 SDK 与模型请按部署指南准备。'),link('Friday 摄像头部署指南','https://github.com/Geeprox/Friday/blob/main/docs/deployment.md#4-摄像头与-miloco'),guide,result);
 guide.append(form);form.append(add(h('label'),h('span','Miloco 兼容服务地址'),url),submit);
 let alive=true;
 void request('/api/settings/setup').then(c=>{if(alive)url.value=c.camera_service_url||'';}).catch(()=>{if(alive)result.textContent='暂时无法读取已有配置，可以填写地址重新连接。';});
 form.onsubmit=async e=>{e.preventDefault();if(!form.reportValidity())return;submit.disabled=true;result.textContent='正在连接摄像头服务…';try{await request('/api/settings/setup',{camera_service_url:url.value.trim()});const report=await request('/api/settings/camera/probe',{});if(!alive||!slot.isConnected)return;result.textContent=report.account_bound===true?'米家账号已连接。实时画面与识别仍需单独启用。':report.status==='service_login_required'||report.status==='account_login_required'?'服务需要登录。请在服务自己的本机页面完成米家授权，再点击连接服务。':'尚未连通兼容服务。请确认服务已启动、地址正确并已在本机登录后重试。';if(report.service_reachable||report.status==='service_login_required'||report.status==='account_login_required'){const open=link('打开服务登录',url.value.trim());result.append(document.createTextNode(' '),open);}}catch(_){if(alive)result.textContent='连接未完成。请检查服务地址与本机登录状态，然后重试。';}finally{submit.disabled=false;}};
 root.append(slot);slot.querySelector('summary').focus();return{dispose(){alive=false;slot.remove();}};
}
export function mountConnectionSetup(root,{standalone=false,onConnected=()=>location.assign('/#home'),onCameraSetup=null,includeCamera=true}={}){
 const haReturn=new URLSearchParams(location.search).get('ha');
 let alive=true,busy=false,config=null,auth=null,haOpen=!!haReturn,poll=null;
 const panel=h('section',null,{class:'connection-setup','aria-label':'家庭连接设置'}),body=h('div'),notice=h('p','',{class:'setup-notice',role:'status','aria-live':'polite'});
 panel.append(body,notice);root.append(panel);
 function say(text){if(alive)notice.textContent=text;}
 const errors={
  mijia_readback_required:'暂时无法读取已接入的米家设备。请检查米家中枢，再试一次。',
  mijia_setup_required:'请先在这台 Mac 上完成米家授权。',
  ha_rules_in_use:'仍有启用的 Friday 规则使用 Home Assistant。请先核对自动化。',
  mijia_interaction_targets_missing:'还有手势目标或动作未接入米家直连。请先核对互动控制。',
  ha_unavailable:'暂时无法读取 Home Assistant。请检查网关或登录。',
  connection_in_progress:'家庭正在连接，请稍候再试。',
 };
 async function run(action,message){
  if(busy||!alive)return;
  busy=true;
  clearTimeout(poll);
  panel.setAttribute('aria-busy','true');
  const buttons=[...panel.querySelectorAll('button:not(:disabled)')];
  buttons.forEach(b=>b.disabled=true);
  say(message);
  try{await action();}catch(error){say(errors[error.message]||'连接未完成，请稍后再试。');}
  finally{busy=false;panel.removeAttribute('aria-busy');buttons.forEach(b=>b.disabled=false);scheduleRefresh();}
 }
 function step(number,title,state,description,optional=false){const copy=add(h('div',null,{class:'setup-step-copy'}),add(h('h3',title),optional?h('small','可选'):null),h('span',state,{class:'setup-state'}),h('p',description)),card=add(h('li',null,{class:'setup-step'}),h('span',number,{'aria-hidden':'true',class:'setup-number'}),copy);return{card,copy};}
 const help=(title,...children)=>add(h('details',null,{class:'setup-help'}),h('summary',title),...children);
 async function read(){const [c,a]=await Promise.all([request('/api/settings/setup'),request('/api/settings/ha/status')]);if(!alive)return;config=c;auth=a;draw();scheduleRefresh();}
 function scheduleRefresh(){
  clearTimeout(poll);
  if(!alive||busy||haOpen||!config?.mijia?.configured||config.mijia.ready>0)return;
  // 保留正在编辑的地址及焦点，只轮询米家的小型状态。
  poll=setTimeout(async()=>{
   try{
    const next=await request('/api/settings/setup');
    if(!alive||busy||haOpen)return;
    const unchanged=JSON.stringify(next.mijia)===JSON.stringify(config.mijia)
          &&next.home_readable===config.home_readable&&next.active_source===config.active_source;
    if(!unchanged&&!panel.contains(document.activeElement)){config=next;draw();}
   }catch{}finally{scheduleRefresh();}
  },4000);
 }
 async function login(){const value=await request('/api/settings/ha/connect',{});if(!alive)return;if(!value.authorization_url)throw Error();location.assign(value.authorization_url);}
 async function saveAddress(url,instance=null){await request('/api/settings/setup',{ha_url:url,ha_instance:instance});if(alive){config.ha_url=url;config.ha_instance=instance;}await login();}
 function addressForm(slot){const form=h('form',null,{class:'setup-form'}),input=h('input',null,{type:'url',required:'',autocomplete:'url',placeholder:'http://homeassistant.local:8123','aria-describedby':'setup-address-help'});input.value=config.ha_url||'';panel.querySelectorAll('button.primary').forEach(b=>b.classList.remove('primary'));const submit=h('button','保存并登录',{type:'submit',class:'primary'});form.append(add(h('label'),h('span','Home Assistant 地址'),input),h('p','使用浏览器中打开 Home Assistant 的完整地址。无需填写密码或令牌。',{id:'setup-address-help'}),add(h('div',null,{class:'setup-actions'}),submit,button('取消',draw)));form.onsubmit=e=>{e.preventDefault();if(form.reportValidity())void run(()=>saveAddress(input.value.trim()),'正在打开 Home Assistant 登录…');};slot.replaceChildren(form);input.focus();}
 async function discover(slot){await run(async()=>{const result=await request('/api/settings/ha/discover');if(!alive)return;const instances=result.instances||[];if(instances.length===1&&instances[0].addresses?.length===1){await saveAddress(instances[0].addresses[0],instances[0].instance_id);return;}slot.replaceChildren();if(!instances.length){say('没有找到 Home Assistant。可以填写地址连接，或查看下方安装指引。');addressForm(slot);return;}say('找到多个可用地址，请选择自己的 Home Assistant。');for(const item of instances)for(const url of item.addresses||[])slot.append(button(`${item.name||'Home Assistant'} · ${url}`,()=>run(()=>saveAddress(url,item.instance_id),'正在打开登录…')));},'正在查找同一网络中的 Home Assistant…');}
 async function connect(){
  await run(async()=>{
   const result=await request(config.desktop?(config.mijia?'/desktop/home-source':'/desktop/connect'):'/api/settings/ha/activate',config.mijia?{source:'ha-read-only'}:{});
   await read();
   if(config.desktop&&result.homeReadable!==true)throw Error('ha_unavailable');
   if(config.home_readable){say('房间与设备已读取。');if(standalone)onConnected();}
  },'正在读取房间与设备…');
 }
 async function useMijia(){
  await run(async()=>{
   await request('/desktop/home-source',{source:'mijia-lan'});
   await read();
   if(standalone)onConnected();
   else say(config.mijia?.ready?'已切换到米家直连。':'正在读取米家设备…');
  },'正在连接米家中枢…');
 }
 function drawMijia(){
  const status=config.mijia;
  const selected=config.home_source==='mijia-lan';
  const local=config.active_source==='mijia-lan'||selected;
  const row=add(h('section',null,{class:'setup-provider','aria-label':'米家直连'}),h('h3','米家直连'));
  const text=status.inventory_state==='mijia_authorization_required'?'米家需要重新授权。'
   :status.inventory_state==='home_selection_required'?'请先确认要接入的米家家庭。'
   :status.ha_rules?status.ha_rules+' 条正在使用的 Friday 规则需先迁移。'
   :status.missing_interaction_targets===null?'暂时无法核对手势绑定。'
   :status.missing_interaction_targets?status.missing_interaction_targets+' 个手势目标或动作还未接入米家直连。'
   :local&&status.ready?status.ready+' 项设备功能已读回状态。'
   :status.routable?status.routable+' 项已接入的设备功能可通过米家中枢读取。'
   :local?'正在等待米家中枢。':status.configured?'尚未读到可迁移的米家设备。':'请先在这台 Mac 上完成米家授权与运行环境准备。';
  row.append(h('p',text));
  const actions=h('div',null,{class:'setup-actions'});
  if(local&&standalone&&status.ready)actions.append(button('进入我的家',onConnected,true));
  if(!selected&&status.configured&&!status.ha_rules&&status.missing_interaction_targets===0){
   const connectButton=button(config.home_readable?'切换到米家直连':'连接米家中枢',useMijia,!local||!standalone);
   connectButton.disabled=config.active_source==='ha-read-only'&&!status.routable;
   actions.append(connectButton);
  }
  if(!status.configured)actions.append(link('米家接入说明','https://github.com/Geeprox/Friday/blob/main/docs/deployment.md#2-米家直连'));
  if(actions.childElementCount)row.append(actions);
  return row;
 }
 function draw(){
  if(!alive)return;
  const p=setupPresentation(config,auth);
  const desktopDirect=!!config.mijia;
  body.replaceChildren();
  const intro=add(h('header',null,{class:'setup-intro'}),h(standalone?'h1':'h2',standalone?'连接你的家':'家庭连接'));
  if(!desktopDirect)intro.append(h('p',p.connected?'管理家庭连接与读取权限。':'连接 Home Assistant，读取你的房间和设备。'));
  body.append(intro);
  if(desktopDirect)body.append(drawMijia());
  const steps=h('ol',null,{class:'setup-steps'});
  if(desktopDirect){
   const title=p.connected?'Home Assistant · 已连接':'Home Assistant';
   const details=add(h('details',null,{class:'setup-completed setup-provider'}),h('summary',title),steps);
   details.open=haOpen;
   details.ontoggle=()=>{if(details.isConnected){haOpen=details.open;scheduleRefresh();}};
   body.append(details);
  }else if(p.connected&&!standalone)body.append(add(h('details',null,{class:'setup-completed'}),h('summary','家庭连接详情'),steps));else body.append(steps);
  const ha=step('1',desktopDirect?'网关地址与登录':'连接 Home Assistant',p.authorized?'已授权':config.ha_url?'等待登录':'尚未连接',p.authorized?'已有本机授权，可读取已授权的家庭设备。':config.ha_url?'地址已保存。在 Home Assistant 的登录页确认账号与访问权限。':'连接已有的 Home Assistant，在官方页面完成登录。');steps.append(ha.card);const actions=h('div',null,{class:'setup-actions'}),slot=h('div',null,{class:'setup-actions'});ha.copy.append(actions,slot);
  if(!config.ha_url)actions.append(button('查找并连接',()=>discover(slot),true),button('填写地址',()=>addressForm(slot)));
  else actions.append(button(p.authorized?'重新登录':'登录 Home Assistant',()=>run(login,'正在打开登录…'),p.state==='authorize'),button('更改地址',()=>addressForm(slot)));
  if(!desktopDirect)ha.copy.append(help('还没有 Home Assistant？',h('p','先在独立主机或 Linux 虚拟机上安装 Home Assistant，再在其“设备与服务”中添加 Xiaomi Home。完成米家登录并选择设备后，回到这里连接。'),link('Home Assistant 安装指南','https://www.home-assistant.io/installation/'),link('Xiaomi Home 接入指南','https://github.com/XiaoMi/ha_xiaomi_home')));
  const friday=step('2','读取房间与设备',p.connected?'已完成':p.authorized?'可以连接':'等待上一步',p.connected?'房间和设备来自你授权的家庭。具体控制以设备支持的能力与控制权限为准。':'授权后读取房间与设备，不会操作家中设备。');steps.append(friday.card);
  if(p.authorized&&!p.connected)friday.copy.append(button(config.active_source==='mijia-lan'?'切换到 Home Assistant':'连接并读取设备',connect,true));
  if(p.connected&&!config.control_enabled)friday.copy.append(h('p','当前仅查看状态。'),link('设备控制权限说明','https://github.com/Geeprox/Friday/blob/main/docs/deployment.md#7-设备控制权限'));
  if(p.connected&&standalone)friday.copy.append(button('进入我的家',onConnected,true));
  if(!includeCamera)return;
  const camera=add(h('section',null,{class:'setup-provider'}),h('h3','摄像头与互动'));
  body.append(camera);
  if(onCameraSetup)camera.append(button(config.camera_configured?'管理摄像头连接':'设置摄像头连接',()=>onCameraSetup(camera)));
  else camera.append(h('p','连接家庭后，可以在设置中添加摄像头。'));
 }
 body.append(h('p','正在读取连接配置…',{role:'status'}));
 void read().then(()=>{if(haReturn==='failed')say('Home Assistant 登录未完成，请重新登录。');}).catch(()=>{if(!alive)return;body.replaceChildren(h('h2','暂时无法读取连接配置'),button('重新读取',()=>run(read,'正在读取连接配置…'),true));});
 return{refresh:read,dispose(){alive=false,clearTimeout(poll);panel.remove();}};
}
