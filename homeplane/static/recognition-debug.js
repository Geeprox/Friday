const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
async function read(path){const r=await fetch(path);if(!r.ok)throw Error('unavailable');return r.json();}

export function televisionState(viewer,now=Date.now()/1000){
 if(!viewer||typeof viewer.reported_at!=='number'||!Number.isFinite(viewer.reported_at))return '未连接';
 const age=now-viewer.reported_at;
 if(!Number.isFinite(age)||age<0||age>=12)return '未连接';
 const frameAge=viewer.server_frame_age_ms;
 return typeof viewer.frames==='number'&&Number.isFinite(viewer.frames)&&viewer.frames>0&&
  typeof viewer.fps==='number'&&Number.isFinite(viewer.fps)&&viewer.fps>0&&
  typeof frameAge==='number'&&Number.isFinite(frameAge)&&frameAge>=0&&frameAge<2000?'画面同步':'等待画面';
}

export function mountRecognitionDebug(root,{go}={}){
 let disposed=false,opened=false,sequence=0,refreshing=false;
 const panel=node('section',null,'recognition-debug'),head=node('div',null,'recognition-debug-head');
 const copy=node('div'),state=node('p','正在检查','muted'),tv=node('p','电视 · 待确认','muted');
 copy.append(node('h2','识别调试'),node('p','查看主镜头的人体与手势','muted'),state,tv);
 const actions=node('div',null,'product-actions'),button=(text,run)=>{const b=node('button',text);b.type='button';b.onclick=run;return b;};
 const open=button('查看识别',async()=>{
  if(opened)return;const current=++sequence;open.disabled=true;state.textContent='正在加载';
  try{
   const value=await read('/api/games/werewolf/open');
   if(disposed||current!==sequence)return;
   const url=new URL(value.url);
   if(url.protocol!=='http:'||url.hostname!=='127.0.0.1')throw Error('invalid_viewer');
   url.searchParams.set('debug','1');
   const frame=node('iframe',null,'recognition-debug-frame');frame.title='人物与手势实时识别';
   frame.setAttribute('referrerpolicy','no-referrer');frame.setAttribute('allow','fullscreen');frame.src=url.href;
   stage.replaceChildren(frame);opened=true;close.hidden=false;
   state.textContent='等待实时画面';
  }catch{if(!disposed&&current===sequence)state.textContent='暂不可用 · 可重新查看';}
  finally{if(!disposed&&current===sequence)open.disabled=opened;}
 });
 const close=button('关闭查看',()=>{sequence++;stage.replaceChildren();opened=false;open.disabled=false;close.hidden=true;void refresh();});close.hidden=true;
 actions.append(open,close,button('人物管理',()=>go?.('People')),button('互动控制',()=>go?.('Interaction')));
 head.append(copy,actions);panel.append(head);const stage=node('div');panel.append(stage);root.append(panel);
 async function refresh(){
  if(disposed||refreshing)return;refreshing=true;
  try{
   const report=await read('/api/embodied/status');if(disposed)return;
   const names={live:'识别运行',stale:'画面延迟',shutter_closed:'镜头遮蔽',connecting:'正在连接',stopped:'服务暂停',not_configured:'尚未配置',unavailable:'正在重连'};
   state.textContent=names[report.state]||'等待画面';
   const value=televisionState(report.tv_viewer);tv.textContent='电视 · '+value;
   tv.title=value==='未连接'?'在客厅电视打开 Friday 后检查连接':'仅表示电视端最近上报的状态';
   if(!report.owned){sequence++;stage.replaceChildren();opened=false;close.hidden=true;}
   open.disabled=!report.owned||opened;
  }catch{if(!disposed){state.textContent='状态未知 · 正在重连';tv.textContent='电视 · 待确认';}}
  finally{refreshing=false;}
 }
 void refresh();const timer=setInterval(refresh,3000);
 return{dispose(){disposed=true;sequence++;clearInterval(timer);panel.remove();}};
}
