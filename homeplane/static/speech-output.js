/* Speech output is independent of camera ownership and the Mac's audio route. */
export function mountSpeechOutput(container,{api,isAlive=()=>true,toast=()=>{}}){
 const base='/api/embodied/speech-output';let current=null,generation=0;
 const el=(tag,text,attrs={})=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;for(const[k,v]of Object.entries(attrs))node.setAttribute(k,v);return node;};
 const section=el('section',null,{class:'speech-output-settings','aria-label':'Friday 播报音箱'});container.append(section);
 if(!document.getElementById('friday-speech-output-style')){const style=el('link',null,{id:'friday-speech-output-style',rel:'stylesheet',href:'/static/speech-output.css'});document.head.append(style);}
 const alive=()=>isAlive()&&section.isConnected;
 const button=(text,action,attrs={})=>{const b=el('button',text,{type:'button',...attrs});b.onclick=action;return b;};
 const names={panel:'已配置的通知设备',media_player:'已选择的媒体播放器',homepod:'HomePod'};
 const reason=result=>({homepod_not_found:'还未找到这台 HomePod，请检查它的局域网地址。',homepod_setup_required:'先查找并选择一台 HomePod。',homepod_pairing_required:'这台 HomePod 需要完成 AirPlay 配对。',homepod_playback_state_unavailable:'暂时无法读取音箱播放状态，请稍后重试。',homepod_connection_unavailable:'暂时连不上 HomePod，请检查它的局域网连接。',homepod_stream_unavailable:'这台音箱当前无法接收语音播报。'}[result.reason]||'暂时无法连接，请稍后重试。');
 async function refresh(){const revision=++generation;try{const value=await api(base);if(!alive()||revision!==generation)return;current=value;render();}catch{if(alive())section.replaceChildren(el('h2','播报音箱'),el('p','音箱设置暂时无法读取。'),button('重试',refresh));}}
 function render(){
  let saving=false;
  section.replaceChildren();const heading=el('div',null,{class:'speech-output-heading'}),intro=el('div');intro.append(el('h2','播报音箱'),el('p','只播放 Friday 的回复与提醒，不改变 Mac 的音频输出。',{class:'muted'}));heading.append(intro);section.append(heading);
  const output=el('select',null,{'aria-label':'播报到'});for(const[value,name]of Object.entries(names))output.append(el('option',name,{value}));output.value=current.output;heading.append(output);
  const status=el('p',current.configured===false?'尚未设置播报设备':current.output==='homepod'?(current.homepod.name||'HomePod')+' · 已选择':names[current.output]+' · 已选择',{class:'speech-output-status',role:'status'});section.append(status);
  const actions=el('div',null,{class:'speech-output-actions'}),lookup=button('查找 HomePod',()=>find()),check=button('检查连接',async()=>{check.disabled=true;status.textContent='正在检查连接…';try{const result=await api(base+'/probe',{});if(alive())status.textContent=result.status==='ready'?'HomePod 已连接，可以播报。':result.status==='busy'?'HomePod 正在播放，暂不播报。':reason(result);}catch{if(alive())status.textContent='连接检查未完成，请重试。';}finally{check.disabled=false;}}),test=button('试听播报',async()=>{test.disabled=true;status.textContent='正在发送测试语音…';try{const result=await api(base+'/test',{});if(alive())status.textContent=result.status==='accepted'?'测试语音已发送。':result.status==='paused'?'安静模式已开启，未播放声音。':result.status==='busy'?'音箱正在播放，稍后再试。':reason(result);}catch{if(alive())status.textContent='测试语音未发送，请先检查连接。';}finally{test.disabled=current.quiet;}});
  check.disabled=current.output!=='homepod'||!current.homepod.configured;test.disabled=current.output!=='homepod'||!current.homepod.configured||current.quiet;actions.append(lookup,check,test);section.append(actions);
  if(current.quiet)section.append(el('p','安静模式已开启。关闭后，Friday 才会通过音箱讲话。',{class:'caption'}));
  const results=el('div',null,{class:'speech-output-devices'});section.append(results);
  const advanced=el('details',null,{class:'speech-output-address'});advanced.append(el('summary','按 IP 地址查找'));const form=el('form'),host=el('input',null,{type:'text',inputmode:'decimal',placeholder:'例如 192.0.2.123','aria-label':'HomePod IP 地址'}),connect=el('button','查找',{type:'submit'});form.append(host,connect);form.onsubmit=e=>{e.preventDefault();void find(host.value.trim());};advanced.append(form,el('p','可在路由器的已连接设备列表中找到 HomePod 的地址。',{class:'caption'}));section.append(advanced);
  async function choose(device,host){if(saving)return;saving=true;output.disabled=true;for(const b of results.children)b.disabled=true;status.textContent='正在设置播报音箱…';try{await api(base,{output:'homepod',identifier:device.identifier,...(host?{host}:{})});if(alive()){toast('播报音箱已设为 '+device.name);await refresh();}}catch{if(alive()){status.textContent='音箱未保存，请重新查找后再试。';output.value=current.output;}}finally{saving=false;output.disabled=false;for(const b of results.children)b.disabled=false;}}
  async function find(host){lookup.disabled=true;connect.disabled=true;results.replaceChildren();status.textContent='正在查找 HomePod…';try{const query=host?'?'+new URLSearchParams({host}):'',result=await api(base+'/devices'+query);if(!alive())return;output.value=current.output;if(!result.devices?.length){status.textContent='还未发现 HomePod。可以用路由器中的 IP 地址继续查找。';return;}status.textContent='选择 Friday 要使用的 HomePod';for(const device of result.devices)results.append(button(device.name,()=>choose(device,host),{class:'speech-output-device'}));}catch{if(alive())status.textContent='查找未完成，请检查 IP 地址和家庭网络。';}finally{lookup.disabled=false;connect.disabled=false;}}
  output.onchange=async()=>{const selected=output.value;if(selected==='homepod'){await find();return;}output.disabled=true;try{await api(base,{output:selected});if(alive())await refresh();}catch{if(alive()){status.textContent='设置未保存，请重试。';output.value=current.output;}}finally{output.disabled=false;}};
 }
 section.append(el('p','正在读取播报音箱…'));void refresh();
 return{dispose(){generation++;section.remove();}};
}
