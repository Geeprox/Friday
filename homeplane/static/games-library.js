const h=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(!r.ok)throw Error('unavailable');return r.json();}
const states={not_configured:'识别服务待配置',stopped:'准备开始',live:'识别中',connecting:'正在连接镜头',unavailable:'镜头正在重连'};
export function mountGames(root,{go}={}){
 let disposed=false,selection=null,report=null,cameras=[],busy=false,chosenCamera=null,stageApp=null,ui={};
 const wrap=h('section',null,'games-library'),list=h('nav',null,'games-list'),detail=h('section',null,'game-detail');list.setAttribute('aria-label','游戏与互动');wrap.append(list,detail);root.append(wrap);
 const notice=h('p','正在读取游戏库…','muted');notice.setAttribute('role','status');detail.append(notice);
 const button=(text,fn,cls)=>{const b=h('button',text,cls);b.type='button';b.onclick=fn;return b;};
 async function perform(fn){if(busy)return;busy=true;update();try{await fn();await refresh();}catch{notice.textContent='暂时无法完成，请重试。';}finally{busy=false;update();}}
 async function openStage(){const result=await api('/api/games/'+selection.id+'/open');const url=new URL(result.url);if(url.hostname!=='127.0.0.1')throw Error('invalid_stage');
  const frame=h('iframe',null,'game-stage');frame.src=result.url;frame.title=selection.name+'实时画面';frame.setAttribute('allow','fullscreen');frame.setAttribute('referrerpolicy','no-referrer');ui.stage.replaceChildren(frame);stageApp=selection.id;notice.textContent='在画面前比 OK 绑定玩家，放下手后开始。';}
 function draw(){if(disposed||!selection)return;stageApp=null;ui={};list.replaceChildren();for(const app of report.apps){const row=button('',()=>{if(!busy){selection=app;draw();}},'game-row');row.setAttribute('aria-pressed',String(selection.id===app.id));row.append(h('span',app.name),h('small',app.kind==='interaction'?'全屋':app.players[0]===app.players[1]?app.players[0]+' 人':app.players[0]+'–'+app.players[1]+' 人'));list.append(row);}
  detail.replaceChildren();detail.append(h('p',selection.kind==='seating'?'聚会工具':selection.kind==='interaction'?'家庭互动':'一起动起来','game-kicker'),h('h2',selection.name),h('p',selection.description,'game-description'));
  if(selection.kind==='interaction'){detail.append(h('p','在互动控制中设置动作与设备，全屋摄像头共同响应。','muted'),button('前往互动控制',()=>{if(go)go('Interaction');else location.href='/#interaction';},'primary'));return;}
  if(selection.kind==='seating')detail.append(h('p','支持座位追踪和指认；当前提供聚会工具，不包含狼人杀角色与裁判规则。','muted'));
  const camera=h('select');camera.setAttribute('aria-label','游戏画面镜头');for(const c of cameras.filter(c=>!report.service.camera||c.id===report.service.camera)){const option=h('option',c.room+' · '+c.name);option.value=c.id;camera.append(option);}camera.value=chosenCamera||report.service.camera||cameras[0]?.id||'';camera.onchange=()=>{chosenCamera=camera.value;};
  const status=h('p',null,'muted'),tv=h('p',null,'game-tv-status'),actions=h('div',null,'game-actions');
  const start=button('开始游戏',()=>perform(async()=>{await api('/api/embodied/start',{camera:camera.value,app:selection.id});await openStage();}),'primary');
  const resume=button('显示当前画面',()=>perform(openStage)),stop=button('结束游戏',()=>perform(async()=>{await api('/api/embodied/game/stop',{});ui.stage.replaceChildren(h('p','本局已结束，家庭识别继续运行。','game-stage-empty'));stageApp=null;}));actions.append(start,resume,stop);
  const label=h('label',null,'game-camera-label');label.append(h('span','游戏画面镜头'),camera);detail.append(label,status,actions);
  const stage=h('div',null,'game-stage-slot');stage.append(h('p','开始游戏后，实时画面会显示在这里。','game-stage-empty'));detail.append(stage,tv,notice);
  detail.append(h('p','电视进入 Friday 后会同步当前游戏。切换游戏不会打断电视正在播放的其他内容。','muted'));
  ui={camera,status,tv,start,resume,stop,stage};update();
 }
 function update(){if(!ui.status||!report)return;const service=report.service;ui.status.textContent=states[service.state]||'等待连接';ui.start.disabled=busy||!service.configured;ui.resume.disabled=busy||!service.owned;ui.stop.disabled=busy||!service.owned;ui.camera.disabled=true;
  const tv=service.tv_viewer,fresh=tv&&Date.now()/1000-tv.reported_at<12;ui.tv.textContent=fresh?'电视已连接 · 正在同步画面':'电视待连接 · 可在电视打开 Friday';
  if(stageApp&&!service.owned){ui.stage.replaceChildren(h('p','游戏已停止，可以重新开始。','game-stage-empty'));stageApp=null;}
 }
 async function refresh(){try{const [next,inventory]=await Promise.all([api('/api/games'),api('/api/embodied/cameras')]);if(disposed)return;const first=!report;report=next;cameras=inventory.cameras;selection=report.apps.find(a=>a.id===selection?.id)||report.apps[0];if(first){notice.textContent='OK 绑定玩家 · Yeah 切换游戏';draw();}else update();}catch{if(!disposed)notice.textContent='连接中断，正在重试。';}}
 void refresh();const timer=setInterval(()=>{if(!busy)void refresh();},2500);return{dispose(){disposed=true;clearInterval(timer);wrap.remove();}};
}
