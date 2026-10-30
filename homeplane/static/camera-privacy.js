const node=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
async function request(path,body){const r=await fetch(path,{...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)}),headers:{'Content-Type':'application/json'}});if(!r.ok)throw Error('unavailable');return r.json();}

export function mountCameraPrivacy(root){
 let disposed=false,busy=false,refreshing=false,policy=null;
 const panel=node('section',null,{class:'preference-row camera-privacy','aria-label':'摄像头隐私'}),copy=node('div');
 const state=node('p','正在读取感知状态…',{class:'muted',role:'status','aria-live':'polite'});
 copy.append(node('h2','后台摄像头感知'),state,node('p','暂停后停止后台画面、声音和人物动作识别，重启后仍保持暂停。手动查看时，只连接你选择的镜头，不恢复后台识别。',{class:'muted'}),node('p','自动重连不会打开已关闭的镜头。你可以在单台摄像头中主动打开或关闭镜头。退出接管时恢复原有巡航与跟踪设置。',{class:'caption'}));
 const toggle=node('button','正在读取…',{type:'button'});toggle.disabled=true;panel.append(copy,toggle);root.append(panel);
 function draw(report){
  policy=report.policy;const running=policy.enabled&&policy.recognition;
  toggle.textContent=running?'暂停摄像头感知':'恢复摄像头感知';toggle.disabled=busy;
  state.textContent=report.failure?(running?'感知服务暂不可用':'暂停已保存，服务停止状态待确认'):running?'已开启 · 尊重米家的镜头开关':'已暂停 · 重启 Friday 后仍保持暂停';
 }
 async function refresh(){
  if(disposed||busy||refreshing)return;refreshing=true;
  try{const report=await request('/api/embodied/infrastructure');if(!disposed&&!busy)draw(report);}
  catch{if(!disposed){state.textContent='感知状态暂不可读，请稍后重试';toggle.disabled=true;}}
  finally{refreshing=false;}
 }
 toggle.onclick=async()=>{
  if(disposed||busy||refreshing||!policy)return;
  busy=true;toggle.disabled=true;
  const enabling=!(policy.enabled&&policy.recognition);state.textContent=enabling?'正在恢复感知…':'正在停止感知…';
  try{
   const report=await request('/api/embodied/infrastructure',{...policy,enabled:enabling,recognition:true,auto_open_cameras:false});
   if(!disposed){busy=false;draw(report);}
  }catch{
   // A timeout can follow a successful save. Read back before another write;
   // never retry a stale revision or silently resume a saved pause.
   if(!disposed){busy=false;await refresh();state.textContent='操作结果待确认，请核对当前状态后重试';}
  }finally{busy=false;}
 };
 void refresh();const timer=setInterval(refresh,5000);
 return{dispose(){disposed=true;clearInterval(timer);panel.remove();}};
}
