
const $=id=>document.getElementById(id),key=new URLSearchParams(location.search).get('k')||sessionStorage.getItem('friday-view-key')||'',url=p=>p+'?k='+encodeURIComponent(key),canvas=$('world'),c=canvas.getContext('2d');$('back').href=url('/');let selected='quest',meta=null,received=null,active=false,firstPacket=true,lastBackendMode=null,lastBackendRound=null,debugFrame=null,debugMeta=null,decoding=false;const dc=$('debugVideo').getContext('2d');
let gameSocket=null,streamOpen=false,streamGeneration=0,reconnectTimer=null,stageClosed=false,commandPending=null,lastConnectionFresh=null,lastAcceptedAt=null,socketAttemptAt=0,debugReceived=null,decodeOwner=0;
let teachingWaitSince=null;
const tennisModes={
 practice:['单人练习','陪练发 9 球，跟着落点提示挥拍。'],
 solo:['单人挑战','与球场对手轮流发球。第一球由你开出。'],
 match:['双人对打','两位玩家并排站好，双脚入镜。左侧先发球。']
};
const skiWarmupSteps={
 left:['向左迈一步','在左侧稍停。','ski.side_step'],
 right:['向右迈一步','越过起点，在右侧稍停。','ski.side_step'],
 arms:['交替摆臂','回到中间，双臂前后交替摆动。','body.arm_effort'],
 ready:['动作已就绪','站在中间，双脚保持入镜。','body.arm_effort']
};
function skiWarmup(g){
 const step=g?.ski?.warmup?.step;
 return g?.mode==='ski'&&Object.hasOwn(skiWarmupSteps,step)?g.ski.warmup:null;
}
function validGameState(g){return !!g&&typeof g.control_context==='string'&&/^[a-f0-9]{32}$/.test(g.control_context)&&Number.isSafeInteger(g.control_revision)&&g.control_revision>=0&&Number.isSafeInteger(g.round_id)&&g.round_id>0&&['quest','ski','tennis','hiit','home'].includes(g.selected_mode)&&['off','quest','ski','tennis','hiit','home'].includes(g.mode)&&(g.difficulty===undefined||['easy','standard','challenge'].includes(g.difficulty))&&(g.tennis_variant==null||g.selected_mode==='tennis'&&typeof g.tennis_variant==='string'&&Object.hasOwn(tennisModes,g.tennis_variant))&&(g.presentation===undefined||g.presentation&&typeof g.presentation.required==='boolean'&&typeof g.presentation.ready==='boolean'&&(!g.presentation.required||/^[a-f0-9]{32}$/.test(g.presentation.context)&&/^[a-f0-9]{32}$/.test(g.presentation.window)));}
function gameViewCurrent(at=performance.now()){return !stageClosed&&streamOpen&&received!==null&&at-received>=0&&at-received<=1500&&validGameState(meta?.game);}
function presentedGame(){const g=meta?.game||{};return gameViewCurrent()||g.mode==='off'||g.status==='complete'?g:{...g,status:'paused',gesture_control:{}};}
function syncGameDifficulty(disabled){const setting=$('difficultySetting'),control=$('difficultySelect'),note=$('difficultyNote');if(!setting||!control)return;const g=meta?.game,options=Array.isArray(g?.difficulty_options)?g.difficulty_options.filter(d=>['easy','standard','challenge'].includes(d?.id)&&typeof d.label==='string'&&d.label.length<=12):[],current=options.find(d=>d.id===g?.difficulty),supported=!!current&&selected!=='home';setting.hidden=!supported;control.disabled=disabled||!supported||g.mode!=='off'&&g.status!=='complete';if(!supported)return;const signature=JSON.stringify(options.map(d=>[d.id,d.label]));if(control.dataset.options!==signature){control.replaceChildren();for(const option of options){const item=document.createElement('option');item.value=option.id;item.textContent=option.label;control.append(item);}control.dataset.options=signature;}control.value=current.id;if(note)note.textContent=g.mode!=='off'&&g.status!=='complete'?'结束本局后可更改':current.description?.[selected]||'';}
function gameTeachingKey(g){return selected==='ski'?skiWarmupSteps[skiWarmup(g)?.step]?.[2]||null:selected==='quest'?questPresentation(g).teaching:selected==='hiit'?({squat:'exercise.squat',jack:'exercise.jack',high_knee:'exercise.high_knee',lunge:'exercise.side_lunge',side_step:'exercise.side_step',side_bend:'exercise.side_bend',reach_up:'exercise.reach_up',arm_open:'exercise.arm_open',balance:'exercise.balance',cross_knee:'exercise.cross_knee',side_raise:'exercise.side_raise',overhead_clap:'exercise.overhead_clap'}[g.hiit?.action||'squat']):null;}
function gameSceneReady(){const scene=selected==='home'||(selected==='ski'?winterReady():scenery.ready(selected));if(!scene)return false;const g=meta?.game;if(g?.presentation?.required===true&&g.status!=='complete'&&['quest','hiit'].includes(selected)){const key=gameTeachingKey(g),diagram=document.querySelector('#gameTutorial canvas');return Boolean(key&&$('gameTutorial')?.dataset.teachingKey===key&&['ready','static'].includes(diagram?.dataset.motion));}return true;}
function gameDisplayReady(){return gameSceneReady()&&(meta?.game?.presentation?.required!==true||presentationClient.ready());}
function resumeInputReady(g=meta?.game){return g?.mode===selected&&g.manually_paused===true&&(g.source_recovery?.required!==true||g.source_recovery.ready===true);}
function syncGameControls(){
 const current=gameViewCurrent(),disabled=!current||!!commandPending;
 const controls=['startGame','startSkiWarmup','stopGame','pauseGame','resumeGame','toggleSkiChooser','chooseSkiTrail',
  'chooseTennisVariant','toggleQuestChooser','chooseQuestRoute'];
 for(const n of document.querySelectorAll('[data-mode],'+controls.map(name=>'[data-click="'+name+'()"]').join(','))){
  const call=n.getAttribute('data-click'),starts=['startGame()','startSkiWarmup()'].includes(call);
  n.disabled=disabled||starts&&!gameDisplayReady()||call==='resumeGame()'&&(!resumeInputReady()||!gameDisplayReady());
 }
 document.querySelector('.actions')?.setAttribute('aria-busy',String(!!commandPending));
 syncGameDifficulty(disabled);
 const record=$('recordToggle');if(record)record.disabled=disabled||typeof meta?.recording?.armed!=='boolean';
 const label=!current?(meta?'正在重新连接':'正在连接游戏'):commandPending?commandPending.label:
  meta?.status==='live'?'已连接 · '+(meta.people?.length||0)+' 位玩家':'等待摄像头';
 if($('connection').textContent!==label)$('connection').textContent=label;
}
if(new URLSearchParams(location.search).get('embed')==='1')document.body?.classList?.add?.('embedded');
let renderedOverlay=null;
function showOverlay(markup){
 if(renderedOverlay===markup)return;
 renderedOverlay=markup;
 const overlay=$('overlay');overlay.innerHTML=markup;overlay.querySelector('h2')?.setAttribute('tabindex','-1');
}
function restoreStageFocus(previous){
 if(!previous)return;
 const focused=document.activeElement;
 if(focused&&focused!==previous&&focused!==document.body)return;
 const available=node=>node?.isConnected&&!node.disabled&&!node.hidden&&!node.closest?.('[hidden]');
 if(available(previous)){if(focused!==previous)previous.focus({preventScroll:true});return;}
 const overlay=$('overlay'),pause=$('pauseToggle');
 const warmupTitle=$('skiWarmupTitle');
 const next=(available(warmupTitle)?warmupTitle:null)||overlay.querySelector(':is([data-ski-trail],[data-tennis-variant],[data-quest-route])[aria-pressed="true"]:not([disabled])')||
  overlay.querySelector('button:not([disabled])')||(available(pause)?pause:null)||overlay.querySelector('h2')||document.querySelector('[data-mode="'+selected+'"]');
 next?.focus?.({preventScroll:true});
}
function updateSourceMode(packet){
 const label=$('sourceMode');
 if(packet?.simulated===true){label.hidden=false;document.body.dataset.simulated='true';}
 else if(packet?.simulated===false){label.hidden=true;delete document.body.dataset.simulated;}
}
function completedGame(g){
 if(selected==='ski'){
  const s=g.ski||{},trail=skiTrails(g).find(item=>item.id===g.ski_trail);
  return[trail?escapeStageText(trail.name)+' · 成绩':'三段雪道已完成',Math.max(0,s.points||0)+' 分 · '+(s.gate_count?'穿门 '+(s.gates_passed||0)+' / '+s.gate_count:'避开 '+(s.passed||0)+' 处障碍')+' · '+(s.stars||1)+' / 3 星','再滑一程'];
 }
 if(selected==='tennis'){
  const t=g.tennis||{};
  if(g.tennis_variant==='practice')return['练习完成','最长连击 '+(t.best_rally||0)+' 球','再练一轮'];
  if(g.tennis_variant==='solo')return[t.winner===0?'你赢了':'对手获胜',
   (t.score||[0,0]).join(' : ')+' · 最长回合 '+(t.best_rally||0)+' 拍','再打一场'];
  return[Number.isInteger(t.winner)?(t.winner+1)+' 号玩家获胜':'本局已结束',(t.score||[0,0]).join(' : ')+' · 最长连续回合 '+(t.best_rally||0),'再比一局'];
 }
 return[selected==='hiit'?'本轮训练已完成':'山径已通关',selected==='hiit'?'完成 '+(g.hiit?.total_reps||0)+' 次动作':'走完 '+(g.quest?.total_levels||g.quest?.targets?.length||3)+' 站山径 · 完成 '+(g.quest?.total_reps??g.quest?.reps??0)+' 次动作','再挑战一次'];
}
const legacyQuestStages=[
 {action:'horizontal',name:'双臂平举',title:'双臂平举，打开林间入口',instruction:'双臂抬至肩高，保持 3 秒。',hold:3,reps:0,teaching:'pose.horizontal'},
 {action:'overhead',name:'双手上举',title:'双手举高，穿过树冠',instruction:'双手举过头顶，保持 3 秒。',hold:3,reps:0,teaching:'pose.overhead'},
 {action:'squat',name:'浅蹲起立',title:'浅蹲再站起，穿过矮枝',instruction:'浅蹲后站直，重复 3 次。',hold:0,reps:3,teaching:'exercise.squat'}
];
function questPresentation(g){
 const q=g.quest||{},stages=Array.isArray(q.stages)&&q.stages.length?q.stages:legacyQuestStages,total=stages.length,level=Math.max(0,Math.min(total,q.level||0)),stage=stages[level],done=g.status==='complete'||level===total,preparing=!done&&q.phase==='transition';
 const fraction=stage?.hold?Math.min(1,(q.hold||0)/stage.hold):stage?.reps?Math.min(1,(q.reps||0)/stage.reps):0;
 const live=done?'':preparing?Math.ceil(q.transition_seconds||0)+' 秒后开始':stage?.hold?'已保持 '+Math.min(stage.hold,q.hold||0).toFixed(1)+' / '+stage.hold+' 秒':'已完成 '+(q.reps||0)+' / '+(stage?.reps||0)+' 次';
 return{title:done?'山径已通关':preparing?'下一站 · '+stage.name:stage.title,instruction:done?'':stage.instruction,live,level,total,stages,chapter:stage?.chapter||stages.at(-1)?.chapter||'',
  progress:done?1:(level+fraction)/total,teaching:done?null:stage.teaching,
  announcement:done?total+' 站山径已完成':(preparing?'下一站：':'第 '+(level+1)+' 站：')+stage.name+'。'+stage.instruction};
}
function updateQuestHUD(g){
 const view=questPresentation(g),trail=$('questTrail');
 $('mission').textContent=view.title;$('instruction').textContent=view.instruction;$('liveProgress').textContent=view.live;
 $('score').innerHTML=view.level+' / '+view.total+'<small>完成站点</small>';$('progress').style.width=(view.progress*100)+'%';
 trail.setAttribute('aria-label',view.chapter?'山径路线，当前：'+view.chapter:'山径路线');
 const stamp=g.control_context+':'+g.round_id+':'+view.level+':'+view.stages.map(s=>[s.action,s.name,s.chapter].join('/')).join(',');
 if(trail.dataset.route!==stamp){
  trail.dataset.route=stamp;trail.replaceChildren(...view.stages.map((stage,i)=>{const li=document.createElement('li'),state=i<view.level?'complete':i===view.level?'current':'upcoming';li.dataset.state=state;li.dataset.chapterStart=String(i>0&&!!stage.chapter&&stage.chapter!==view.stages[i-1].chapter);li.textContent=String(i+1);li.setAttribute('aria-label',(stage.chapter?stage.chapter+'，':'')+'第 '+(i+1)+' 站：'+stage.name+'，'+({complete:'已完成',current:'当前站点',upcoming:'待完成'})[state]);if(state==='current')li.setAttribute('aria-current','step');return li;}));
 }
 const announcement=$('questAnnouncement'),notice=g.control_context+':'+g.round_id+':'+view.level+':'+(g.quest?.phase||'work');
 if(announcement.dataset.notice!==notice){announcement.dataset.notice=notice;announcement.textContent=view.announcement;}
 return view;
}
function gamePhaseMessage(g){
 const r=g.source_recovery,doubles=selected==='tennis'&&!['practice','solo'].includes(g.tennis_variant);
 if(r?.required===true&&r.phase==='display')return'本局进度已保留，等待游戏画面恢复。';
 if(r?.required===true){
  if(meta?.status!=='live'||r.phase==='camera')return'本局进度已保留，等待画面恢复。';
  if(r.ready===true)return r.accepts_ok&&selected==='tennis'?'比分与球路已保留。':'本局进度已保留。';
  if(r.accepts_ok)return doubles?'两位玩家回到原站位，双脚入镜后站定。':'回到原站位，双脚入镜后站定。';
  return r.phase==='players'?(doubles?'请让两位玩家回到原来的站位，双脚完整入镜。':'请让一位玩家完整入镜。'):'自然站定片刻，重新确认站位。';
 }
 if(g.manually_paused)return'';
 if(g.resume_seconds>0)return'准备继续本局';
 if(g.status==='calibrating')return'自然站定片刻，再开始动作。';
 if(g.status==='waiting')return doubles?'两位玩家一起入镜，让双脚完整可见。':'站到镜头前，让双脚完整可见。';
 if(g.status==='paused'){
  if(g.reason==='请让手和双脚入镜')return'让手和双脚入镜，游戏会自动继续。';
  return g.reason?.includes('双脚')?'让双脚完整入镜，游戏会自动继续。':'镜头正在调整，稍候继续。';
 }
 return'正在连接游戏画面。';
}
const artRoot=new URL('.',document.currentScript?.src||new URL('/assets/games.js',location.href)),winterProps=new Image(),winterScene=new Image();
let artError=false;
function sceneRefreshed(){if(!meta)return;if(meta.game?.mode!==selected)selectMode(selected);updateHUD();}
const scenery=createGameScenery(c,{base:artRoot,onChange:sceneRefreshed});
const skiMotion=matchMedia('(prefers-reduced-motion: reduce)');
const skiScenery=createSkiScenery(c,{scene:winterScene,props:winterProps,reducedMotion:()=>skiMotion.matches});
const presentationClient=createGamePresentation({state:()=>meta?.game,current:gameViewCurrent,visible:()=>!document.hidden,assetsReady:gameSceneReady,onChange:()=>{if(!stageClosed)syncGameControls();},send:async(body,options)=>{const response=await fetch(url('/api/game/presentation'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),...options});if(!response.ok)throw Error('game_view_changed');return response.json();}});
const gameSound=createGameSound({state:()=>meta?.game,current:()=>!commandPending&&gameViewCurrent()&&meta?.status==='live'&&gameDisplayReady(),visible:()=>!document.hidden,onChange:updateGameSound});
function updateGameSound(value){const button=$('soundToggle');if(!button)return;button.textContent=value.pending?'取消开启音效':value.enabled?'关闭音效':value.state==='blocked'?'重试开启音效':'开启音效';button.setAttribute('aria-pressed',String(value.enabled));button.setAttribute('aria-busy',String(value.pending));}
async function toggleGameSound(){const value=gameSound.status();if(value.enabled||value.pending){gameSound.disable();return;}const enabled=await gameSound.enable();if(!enabled&&!stageClosed&&gameSound.status().state==='blocked')$('gameNotice').textContent='音效暂不可用，可以重试开启。';else if(enabled&&$('gameNotice').textContent==='音效暂不可用，可以重试开启。')$('gameNotice').textContent='';}
function winterReady(){return winterProps.complete&&winterProps.naturalWidth>0&&winterScene.complete&&winterScene.naturalWidth>0;}
function reloadTeaching(){location.reload();}
function reloadScene(){if(selected!=='ski'){scenery.reload(selected);return;}artError=false;for(const[image,name]of [[winterProps,'winter-props.png'],[winterScene,'winter-scene.png']]){image.onload=sceneRefreshed;image.onerror=()=>{artError=true;if(meta)updateHUD();};image.src=new URL(name+(image.src?'?retry='+Date.now():''),artRoot).href;}}
async function pauseGame(){if(meta?.game?.manually_paused)return false;return commandGame({mode:selected,action:'pause'},'暂时无法暂停，请重试。');}
async function resumeGame(){if(!resumeInputReady()||!gameDisplayReady())return false;return commandGame({mode:selected,action:'resume'},'暂时无法继续本局，请重试。');}
function togglePlayerView(){const panel=$('playerView');panel.hidden=!panel.hidden;const control=$('previewToggle');control.setAttribute('aria-pressed',String(!panel.hidden));control.textContent=panel.hidden?'查看我的动作':'收起动作画面';}
if(key){sessionStorage.setItem('friday-view-key',key);history.replaceState(null,'',location.pathname+(new URLSearchParams(location.search).get('embed')==='1'?'?embed=1':''));}

const descriptions={home:['镜头联动 / 05','双臂开合，控制梦幻帘','OK 绑定操作者 · 双手在胸前合拢后向两侧打开 · 眼部注视确认后可直接说 · 看不清眼睛时先说 Friday'],quest:['山径训练 / 01','山径冒险','沿着山径，跟随示范完成动作，抵达山顶营地。'],ski:['山地滑行 / 02','林间滑雪','左右移动躲树 · 双臂同向或交替摆动前进 · 停止摆臂会减速'],hiit:['循环训练 / 04','运动训练','跟随示范完成一轮站姿动作。'],tennis:['客厅球场 / 03','双人网球','两位玩家分别站定 · 横移接球 · 快速挥动手臂触发击球']};
async function commandGame(body,failure){
 if(commandPending)return false;
 if(!gameViewCurrent()){$('gameNotice').textContent='正在连接游戏，请稍后重试。';return false;}
 const before=meta.game,lease={generation:streamGeneration,context:before.control_context,revision:before.control_revision,controller:new AbortController(),focus:document.activeElement?.closest?.('button,select'),
  label:body.quest_route?'正在准备路线':body.ski_warmup?'正在准备试滑':body.tennis_variant?'正在切换玩法':body.ski_trail?'正在准备雪道':
   body.difficulty?'正在设置难度':({select:'正在切换游戏',start:'正在开始本局',pause:'正在暂停',resume:'正在继续'})[body.action]||'正在结束本局'};
 const notice=(body.action==='resume'&&$('resumeNotice'))||$('gameNotice');
 const reportNotice=message=>{(notice.isConnected===false?$('gameNotice'):notice).textContent=message;};
 commandPending=lease;gameSound.quiet();$('gameNotice').textContent='';notice.textContent='';syncGameControls();
 const timeout=setTimeout(()=>lease.controller.abort(),5000);
 const current=()=>!stageClosed&&streamGeneration===lease.generation&&meta?.game?.control_context===lease.context;
 try{
  const r=await fetch(url('/api/game'),{method:'POST',headers:{'Content-Type':'application/json'},signal:lease.controller.signal,body:JSON.stringify({...body,expected_context:lease.context,expected_revision:lease.revision})});
  if(!current())return false;
  if(!r.ok){if(r.status===409){let info={};try{if(typeof r.json==='function')info=await r.json();}catch{}if(!current())return false;reportNotice(info?.detail==='game_input_not_ready'?'请先回到镜头前站定，再继续。':info?.detail==='game_presentation_not_ready'?'游戏画面正在准备，请稍候。':'本局状态已变化，请稍后重试。');return false;}throw Error('request_failed');}
  const result=await r.json();if(!current())return false;
  const matches=(body.mode==='off'?result.mode==='off':body.action==='select'?result.mode==='off'&&result.selected_mode===body.mode:result.mode===body.mode&&result.selected_mode===body.mode)
   &&(body.action!=='pause'||result.manually_paused===true)&&(body.action!=='resume'||result.manually_paused===false)
   &&(body.difficulty===undefined||result.difficulty===body.difficulty);
  const expectedTrail=body.ski_trail||(body.action==='start'&&body.mode==='ski'?before.ski_trail:null);
  const expectedVariant=body.tennis_variant||(body.mode==='tennis'?before.tennis_variant:null);
  const expectedRoute=body.quest_route||(body.mode==='quest'?before.quest_route:null);
  const matchesWarmup=body.mode!=='ski'||body.action!=='start'||
   (body.ski_warmup?Boolean(skiWarmup(result)):result.ski?.warmup==null);
  if(!validGameState(result)||result.control_context!==lease.context||result.control_revision<=lease.revision||
     !matches||!matchesWarmup||expectedTrail&&result.ski_trail!==expectedTrail||
     expectedVariant&&result.tennis_variant!==expectedVariant||expectedRoute&&result.quest_route!==expectedRoute)throw Error('game_reply_invalid');
  if(meta.game.control_revision>result.control_revision)return false;
  // A same-revision stream packet can already contain newer motion/score state.
  if(meta.game.control_revision<result.control_revision)meta={...meta,game:result};
  selectMode(meta.game.selected_mode);updateHUD();return true;
 }catch{if(current())reportNotice(failure);return false;}
 finally{clearTimeout(timeout);if(commandPending===lease){commandPending=null;syncGameControls();
  if(lease.focus&&current()&&gameViewCurrent())restoreStageFocus(lease.focus);
 }}
}
async function chooseMode(mode){return commandGame({action:'select',mode},'暂时无法切换游戏，请重试。');}
function modeDescription(mode,g){const d=[...descriptions[mode]];if(mode==='ski'&&g?.ski?.gate_count)d[2]='摆动双臂前进，左右穿过 '+g.ski.gate_count+' 道木门，避开树木。';return d;}
function selectMode(mode){selected=mode;if(mode==='ski'){if(!winterProps.src)reloadScene();}else if(mode!=='home')scenery.prepare(mode);document.body.dataset.mode=mode;active=meta?.game?.mode===mode&&meta?.game?.mode!=='off';for(const b of document.querySelectorAll('[data-mode]'))b.setAttribute('aria-pressed',String(b.dataset.mode===mode));const d=modeDescription(mode,meta?.game);$('chapter').textContent=d[0];$('mission').textContent=d[1];$('instruction').textContent=d[2];}
async function startGame(warmup=false){
 if(warmup&&(selected!=='ski'||meta?.game?.mode!=='off'||meta.game.ski?.warmup!==null))return false;
 if(!gameDisplayReady()){$('gameNotice').textContent='游戏画面正在准备，请稍候。';return false;}
 const body={mode:selected,action:'start'};
 if(warmup)body.ski_warmup=true;
 return commandGame(body,warmup?'暂时无法试滑，请重试。':'暂时无法开始本局，请重试。');
}
async function chooseTennisVariant(variant){
 const g=meta?.game;
 if(!g||selected!=='tennis'||g.mode!=='off'&&g.status!=='complete'||!Object.hasOwn(tennisModes,variant)||
    variant===(g.tennis_variant||'match'))return false;
 return commandGame({mode:'tennis',action:'select',tennis_variant:variant},'暂时无法切换玩法，请重试。');
}
async function changeDifficulty(){const g=meta?.game,value=$('difficultySelect')?.value;if(!g||g.mode!=='off'&&g.status!=='complete'||!g.difficulty_options?.some(d=>d.id===value)){syncGameControls();return false;}const changed=await commandGame({mode:selected,action:'select',difficulty:value},'暂时无法更改难度，请重试。');syncGameControls();return changed;}
async function curtainCommand(action){await fetch(url('/api/home/curtain'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})})}
async function stopGame(){return commandGame({mode:'off'},'暂时无法结束本局，请重试。');}
let gamePackets=0,gameWindow=performance.now(),gameReport=0;function reportViewer(){if(location.hostname==='127.0.0.1')return;const now=performance.now();if(now-gameReport<4000)return;gameReport=now;fetch(url('/api/viewer'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:'0.5.0',mode:'games',frames:gamePackets,fps:Math.round(gamePackets*10000/Math.max(1,now-gameWindow))/10,server_frame_age_ms:meta?.frame_age_ms,event:'metadata_received'})}).catch(()=>{})}
function tennisPresentation(g){
 const t=g.tennis||{},guide=t.guide||{},receiver=(guide.receiver===1?2:1),event=t.event;
 const cues={left:'向左移动到光圈处。',right:'向右移动到光圈处。',wait:'站位就绪，等挥拍提示。',swing:'现在挥拍！'};
 if(g.tennis_variant==='practice'){
  const {results,balls}=tennisPracticeStats(t);
  const live=g.status==='playing',returning=live&&t.phase==='rally'&&t.ball?.toward===1;
  const attempt=Math.min(balls,results.length+(returning?0:1));
  const title=live&&attempt>0?['定点回球','左右回球','快速回球'][Math.floor((attempt-1)/3)]:'单人练习';
  const instruction=!live?'':guide.active?cues[guide.cue]||'看准来球，准备挥拍。':returning?'':'准备接球。';
  const fresh=live&&event?.age>=0&&event.age<1.4;
  const feedback=fresh&&event.kind==='hit'?(event.quality==='perfect'?'漂亮回击':'回球成功'):
   fresh&&event.kind==='miss'?(event.next_focus==='position'?'下一球先站进光圈':'下一球看提示再挥拍'):'';
  return {title,instruction,event:feedback,eventKey:feedback?g.control_context+':'+g.round_id+':'+event.sequence:'',
   rules:'先站进光圈，看到提示再挥拍。'};
 }
 const serve=[0,1].includes(t.serve?.player)?t.serve:null;
 const live=g.status==='playing',seconds=Math.max(1,Math.ceil(t.ready_seconds||0));
 const solo=g.tennis_variant==='solo';
 const player=i=>solo?['你','对手'][i]:(i+1)+' 号';
 let title=solo?'单人挑战':'双人网球',instruction='';
 if(live&&serve){
  const server=player(serve.player),opponentServing=solo&&serve.player===1;
  if(t.phase==='point'){title=event?.kind==='point'?player(event.player)+'得分':'这一球结束';instruction=server+'准备发球 · '+seconds+' 秒';}
  else if(t.phase==='ready'){title=server+'准备发球';instruction=seconds+' 秒后就绪';}
  else{title=server+'发球';instruction=opponentServing?'准备接球。':serve.ready?'现在挥拍！':'双脚入镜，发球手先停稳';}
 }else if(live&&guide.active){
  title=player(receiver-1)+'接球';
  instruction=cues[guide.cue]||'看准来球，准备挥拍。';
 }else if(live&&solo&&t.phase==='rally'){
  title='对手接球';
 }
 const shot=live&&['serve','hit'].includes(event?.kind)&&event.age>=0&&event.age<1.4;
 const focus=live&&t.phase==='point'&&event?.kind==='point'?({position:'先站进光圈，再挥拍',timing:'看到挥拍提示再出手'})[event.next_focus]:null;
 const feedback=shot&&(!solo||event.player===0)?(solo?'':player(event.player)+' · ')+(event.kind==='serve'?'发球成功':event.quality==='perfect'?'漂亮回击':'回球成功'):
  focus?(solo?'':player(1-event.player)+' · ')+focus:'';
 return{title,instruction,event:feedback,
  eventKey:feedback?g.control_context+':'+g.round_id+':'+event.sequence:'',rules:'先得 '+(t.target||7)+' 分并领先 2 分获胜，'+(t.cap||15)+' 分封顶。'};
}
function tennisPracticeStats(t){
 const balls=t.practice?.balls===9?9:0,raw=t.practice?.results;
 const results=Array.isArray(raw)&&raw.length<=balls&&raw.every(result=>typeof result==='boolean')?raw:[];
 return {balls,results,hits:results.filter(Boolean).length};
}
function tennisPracticeScore(t,g,scoreboard){
 const {balls,results,hits}=tennisPracticeStats(t);
 const current=g.status==='playing'&&t.guide?.active===true?results.length:-1;
 const signature=['practice',balls,current,...results].join('/');
 if(scoreboard.dataset.tennis===signature)return;
 scoreboard.dataset.tennis=signature;
 if(!balls){scoreboard.textContent='';return;}
 const markers=Array.from({length:balls},(_,i)=>{
  const result=results[i],state=result===true?'hit':result===false?'miss':i===current?'current':'next';
  const status={hit:'命中',miss:'未命中',current:'接球中',next:'待接球'}[state];
  return '<li data-state="'+state+'"><span class="sr-only">第 '+(i+1)+' 球，'+status+'</span></li>';
 }).join('');
 scoreboard.innerHTML='<div class="tennis-drill-count"><small>命中</small><b>'+hits+'<span> / '+balls+'</span></b></div>'+
  '<ol class="tennis-drill-balls" aria-label="回球进度">'+markers+'</ol>';
}
function skiPresentation(g){
 const s=g.ski||{},route=s.gate_count>0,event=s.event;
 const instruction=g.status==='complete'?'':(s.speed||0)<=.1?(route?'摆动双臂出发，左右移动穿过木门。':'摆动双臂出发，左右移动避开障碍。'):route?({left:'向左穿过前方木门。',right:'向右穿过前方木门。',center:'沿门中央穿过，保持摆臂。',finish:'穿门完成，继续滑向终点。'}[s.guide?.cue]||'穿过木门，避开树木。'):'保持摆臂，观察前方障碍。';
 const fresh=g.status==='playing'&&event&&event.age>=0&&event.age<1.6;
 const message=fresh?(event.kind==='gate'?(event.combo>1?'连续穿门 '+event.combo+' 道':'穿门成功')+' · +'+event.points:event.kind==='miss'?'漏过这道门，继续下一道。':event.kind==='collision'?'碰到树木，继续摆臂。':''):'';
 return{instruction,event:message,detail:route?'穿门 '+(s.gates_passed||0)+' / '+s.gate_count:'得分 · 避开 '+(s.passed||0)+' 处'};
}
function updateTennisHUD(g){
 const chosen=selected==='tennis',event=$('tennisEvent'),rules=$('tennisRules'),scoreboard=$('score');
 event.hidden=!chosen;rules.hidden=!chosen;scoreboard.classList.toggle('tennis-score',chosen);
 $('progress').parentElement.hidden=chosen;
 if(!chosen){delete scoreboard.dataset.tennis;return;}
 const view=tennisPresentation(g),t=g.tennis||{},scores=t.score||[0,0];
 for(const[id,text]of[['mission',view.title],['instruction',view.instruction],['tennisRules',view.rules]])if($(id).textContent!==text)$(id).textContent=text;
 if(view.eventKey&&event.dataset.event!==view.eventKey){event.textContent=view.event;event.dataset.event=view.eventKey;}
 if(!view.eventKey&&g.tennis?.event==null){event.textContent='';event.dataset.event='';}
 event.hidden=!view.event;
 if(g.tennis_variant==='practice'){tennisPracticeScore(t,g,scoreboard);return;}
 const solo=g.tennis_variant==='solo';
 const receiver=g.status!=='playing'?null:t.guide?.active?t.guide.receiver:solo&&t.phase==='rally'?t.ball?.toward:null;
 const server=g.status==='playing'?t.serve?.player:null;
 const signature=[solo,...scores,t.rally||0,t.best_rally||0,receiver,server].join('/');
 if(scoreboard.dataset.tennis!==signature){
  scoreboard.dataset.tennis=signature;
  scoreboard.innerHTML='<div class="tennis-scoreline">'+scores.map((score,i)=>'<span class="tennis-player" data-player="'+i+'"'+(server===i?' data-serving="true"':receiver===i?' data-receiving="true"':'')+'><small>'+(solo?['你','对手'][i]:(i+1)+' 号')+'</small><b>'+score+'</b></span>').join('<span class="tennis-score-separator" aria-hidden="true">:</span>')+'</div><small>回合 '+(t.rally||0)+' · 最长 '+(t.best_rally||0)+'</small>';
 }
}
function streamLost(ws){
 if(stageClosed||gameSocket!==ws)return;
 gameSound.quiet();
 gameSocket=null;streamOpen=false;streamGeneration++;commandPending?.controller.abort();commandPending=null;
 debugFrame=debugMeta=null;debugReceived=null;decodeOwner++;decoding=false;
 try{ws.close();}catch{}
 updateHUD();if(reconnectTimer===null)reconnectTimer=setTimeout(()=>{reconnectTimer=null;connect();},1500);
}
function receiveGamePacket(ws,generation,data){
 if(stageClosed||gameSocket!==ws||streamGeneration!==generation)return;
 try{
  if(!(data instanceof ArrayBuffer)||data.byteLength<8)throw Error();
  const view=new DataView(data),n=view.getUint32(0),size=view.getUint32(4);
  if(n<2||n>1500000||size>6000000||8+n+size!==data.byteLength)throw Error();
  const packet=JSON.parse(new TextDecoder().decode(new Uint8Array(data,8,n)));
  if(!packet||!validGameState(packet.game))throw Error();
  if(meta?.game?.control_context===packet.game.control_context&&meta.game.control_revision>packet.game.control_revision)return;
  meta=packet;received=lastAcceptedAt=performance.now();lastConnectionFresh=true;
  skiScenery.receive(meta.game,received);scenery.receive(meta.game,received);gamePackets++;reportViewer();
  if(firstPacket||lastBackendMode!==meta.game.selected_mode||lastBackendRound!==meta.game.mode){firstPacket=false;lastBackendMode=meta.game.selected_mode;lastBackendRound=meta.game.mode;selectMode(meta.game.selected_mode);}
  updateHUD();if(typeof updateRecording==='function')updateRecording(meta.recording);syncGameControls();
  if(size&&!decoding){
   decoding=true;const decode=++decodeOwner,snapshot=meta,at=received,img=new Image(),done=()=>{if(decodeOwner===decode)decoding=false;};
   img.onload=()=>{if(gameSocket===ws&&streamGeneration===generation&&decodeOwner===decode&&performance.now()-at<=1500){debugFrame=img;debugMeta=snapshot;debugReceived=at;}done();};
   img.onerror=done;const reader=new FileReader();
   reader.onload=()=>{if(gameSocket===ws&&streamGeneration===generation&&decodeOwner===decode)img.src=reader.result;else done();};reader.onerror=done;
   reader.readAsDataURL(new Blob([new Uint8Array(data,8+n,size)],{type:'image/jpeg'}));
  }
 }catch{streamLost(ws);}
}
function connect(){
 if(stageClosed)return;
 received=null;streamOpen=false;debugFrame=debugMeta=null;debugReceived=null;decodeOwner++;decoding=false;socketAttemptAt=performance.now();const generation=++streamGeneration;
 try{const ws=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/ws?view=game&k=${encodeURIComponent(key)}`);gameSocket=ws;ws.binaryType='arraybuffer';
  ws.onopen=()=>{if(gameSocket===ws&&streamGeneration===generation){streamOpen=true;updateHUD();}};
  ws.onmessage=e=>receiveGamePacket(ws,generation,e.data);ws.onclose=()=>streamLost(ws);ws.onerror=()=>streamLost(ws);
 }catch{reconnectTimer=setTimeout(()=>{reconnectTimer=null;connect();},1500);}
 updateHUD();
}
function reconnectGame(){if(stageClosed)return;clearTimeout(reconnectTimer);reconnectTimer=null;const old=gameSocket;gameSocket=null;streamOpen=false;commandPending?.controller.abort();commandPending=null;if(old){old.onclose=null;try{old.close();}catch{}}connect();}
function watchGameConnection(now){const fresh=gameViewCurrent(now);if(fresh!==lastConnectionFresh){lastConnectionFresh=fresh;updateHUD();}if(gameSocket&&!fresh&&now-(received??socketAttemptAt)>5000)reconnectGame();}
let skiChooserRound=null;
function stageRound(g){return g.control_context+':'+g.round_id;}
function skiTrails(g){
 return Array.isArray(g?.ski_trails)?g.ski_trails.filter(trail=>typeof trail?.id==='string'&&/^[a-z][a-z-]{1,23}$/.test(trail.id)&&
  typeof trail.name==='string'&&trail.name.length<=16&&Number.isSafeInteger(trail.distance)&&trail.distance>0&&
  Number.isSafeInteger(trail.gate_count)&&trail.gate_count>0):[];
}
function escapeStageText(text){return text.replace(/[&<>]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[char]));}
function skiTrailPicker(g){
 const trails=skiTrails(g);
 if(!trails.length)return'';
 return '<div class="ski-trail-picker" role="group" aria-label="雪道">'+trails.map(trail=>
  '<button type="button" data-click="chooseSkiTrail()" data-ski-trail="'+trail.id+'" aria-pressed="'+(trail.id===g.ski_trail)+
  '"><strong>'+escapeStageText(trail.name)+'</strong><span>'+trail.distance+' 米 · '+trail.gate_count+' 门</span></button>').join('')+'</div>';
}
function showSkiWarmup(g,current){
 return current&&selected==='ski'&&Boolean(skiWarmup(g))&&winterReady()&&
  !g.manually_paused&&!g.source_recovery?.required&&!(g.resume_seconds>0);
}
function updateSkiWarmup(g,visible){
 const warmup=selected==='ski'?skiWarmup(g):null;
 const panel=$('skiWarmup');
 if(warmup)document.body.dataset.skiWarmup='true';else delete document.body.dataset.skiWarmup;
 panel.hidden=!visible;
 if(!visible)return;
 const {step,fraction}=warmup,ready=step==='ready';
 const [stepTitle,stepInstruction]=skiWarmupSteps[step];
 const observing=g.status==='playing';
 const title=observing||ready?stepTitle:'准备试滑';
 const instruction=observing?stepInstruction:g.status==='calibrating'?'自然站定片刻。':
  meta?.room?.registration?.status==='locked'?'双手和双脚完整入镜。':'等待镜头就绪。';
 for(const [id,label] of [['skiWarmupTitle',title],['skiWarmupInstruction',instruction],
  ['skiWarmupContinue',ready?'开始滑行':'直接开滑']]){
  if($(id).textContent!==label)$(id).textContent=label;
 }
 $('skiWarmupContinue').classList.toggle('primary',ready);
 panel.dataset.step=step;
 const steps=$('skiWarmupSteps').children,index=Object.keys(skiWarmupSteps).indexOf(step);
 for(let i=0;i<steps.length;i++){
  const item=steps[i],state=i<index?'done':i===index?'current':'next';
  if(item.dataset.state!==state){
   item.dataset.state=state;
   item.setAttribute('aria-label',item.textContent+(state==='done'?'，已识别':''));
   if(state==='current')item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');
  }
  const amount=state==='done'?1:state==='current'&&Number.isFinite(fraction)?Math.max(0,Math.min(1,fraction)):0;
  item.style.setProperty('--ski-hold',amount*100+'%');
 }
}
function toggleSkiChooser(){
 const g=meta?.game;
 if(commandPending||!gameViewCurrent()||selected!=='ski'||g.status!=='complete'||skiTrails(g).length<2)return;
 skiChooserRound=skiChooserRound===stageRound(g)?null:stageRound(g);
 updateHUD();
}
async function chooseSkiTrail(id){
 const g=meta?.game;
 if(!gameViewCurrent()||selected!=='ski'||!skiTrails(g).some(trail=>trail.id===id))return false;
 if(g.mode==='off'?g.ski_trail===id:g.status!=='complete'||skiChooserRound!==stageRound(g))return false;
 return commandGame({mode:'ski',action:'select',ski_trail:id},'暂时无法更换雪道，请重试。');
}
function skiResult(ski){
 const sections=ski?.section_results;
 if(!Array.isArray(sections)||sections.length<1||sections.length!==ski.course_count||
    !Number.isSafeInteger(ski.points)||ski.points<0||![1,2,3].includes(ski.stars)||
    !sections.every(row=>typeof row?.name==='string'&&Number.isSafeInteger(row.gates_passed)&&
     Number.isSafeInteger(row.gate_count)&&row.gates_passed>=0&&row.gate_count>=row.gates_passed))return'';
 const rows=sections.map(row=>'<div><dt>'+(sections.length===1?'穿门':escapeStageText(row.name))+'</dt><dd>'+row.gates_passed+' / '+row.gate_count+'</dd></div>');
 let time='';
 if(Number.isFinite(ski.time_seconds)&&ski.time_seconds>0){
  const tenths=Math.round(ski.time_seconds*10),minutes=Math.floor(tenths/600),seconds=(tenths%600)/10;
  time=' · '+(minutes?minutes+' 分 ':'')+seconds.toFixed(1)+' 秒';
 }
 return '<p class="ski-result-summary"><strong>'+ski.points+' 分</strong> · '+ski.stars+' 星'+time+'</p>'+
  '<dl class="ski-splits" aria-label="各段穿门成绩">'+rows.join('')+'</dl>';
}
function tennisLineupMarkup(states){
 const labels=new Map([['missing','请回到画面'],['feet','露出双脚'],['calibrating','站定片刻'],['ready','已就位']]);
 const rows=states.map((state,i)=>'<div data-player="'+i+'"><dt>'+(i+1)+' 号玩家</dt><dd>'+labels.get(state)+'</dd></div>');
 return '<dl class="tennis-lineup" aria-label="双人站位">'+rows.join('')+'</dl>';
}
function tennisVariantButton(id,pressed){
 const [label]=tennisModes[id];
 return '<button type="button" data-click="chooseTennisVariant()" data-tennis-variant="'+id+'"'+
  (pressed===undefined?'':' aria-pressed="'+pressed+'"')+'>'+label+'</button>';
}
function tennisLobby(g){
 const current=Object.hasOwn(tennisModes,g.tennis_variant)?g.tennis_variant:'match';
 const variants='<div class="tennis-variants" role="group" aria-label="网球玩法">'+
  Object.keys(tennisModes).map(id=>tennisVariantButton(id,id===current)).join('')+'</div>';
 const detail=tennisModes[current][1];
 return '<h2>准备开球</h2>'+variants+'<p>'+detail+'</p><button class="primary" data-click="startGame()">开始本局</button>';
}
let questChooserRound=null;
function questRoutes(g){
 return Array.isArray(g.quest_routes)?g.quest_routes.filter(route=>['full','forest','stream','camp'].includes(route?.id)
  &&typeof route.name==='string'&&route.name.length<=16
  &&Number.isSafeInteger(route.stations)&&route.stations>0):[];
}
function toggleQuestChooser(){
 const g=meta?.game;
 if(commandPending||!gameViewCurrent()||selected!=='quest'||g.status!=='complete')return;
 questChooserRound=questChooserRound===stageRound(g)?null:stageRound(g);
 updateHUD();
}
async function chooseQuestRoute(id){
 const g=meta?.game;
 if(!gameViewCurrent()||selected!=='quest'||g.mode!=='off'&&g.status!=='complete'||
    id===g.quest_route||!questRoutes(g).some(route=>route.id===id))return false;
 return commandGame({mode:'quest',action:'select',quest_route:id},'暂时无法更换路线，请重试。');
}
function questRoutePicker(routes,current){
 const choices=routes.map(route=>'<button type="button" data-click="chooseQuestRoute()" data-quest-route="'+route.id+
  '" aria-pressed="'+(route.id===current.id)+'"><strong>'+escapeStageText(route.name)+'</strong><small>'+route.stations+' 站</small></button>');
 return '<div class="quest-route-options" role="group" aria-label="路线">'+choices.join('')+'</div>';
}
function questOverlay(g,finished){
 const routes=questRoutes(g),current=routes.find(route=>route.id===g.quest_route);
 if(!current)return '';
 if(!finished||questChooserRound===stageRound(g)){
  const title=finished?'更换路线':'选择路线';
  const action=finished?'<button data-click="toggleQuestChooser()">返回成绩</button>':
   '<button class="primary" data-click="startGame()">'+(current.id==='full'?'开始全程':'开始本段')+'</button>';
  return '<h2>'+title+'</h2>'+questRoutePicker(routes,current)+action;
 }
 const next=routes.find(route=>route.id===({forest:'stream',stream:'camp'})[current.id]);
 const view=questPresentation(g),title=escapeStageText(current.id==='full'?'山径已通关':current.name+'完成');
 const reps=g.quest?.total_reps;
 const replay='<button'+(next?'':' class="primary"')+' data-click="startGame()">'+
  (current.id==='full'?'再走一遍':'重玩本段')+'</button>';
 const advance=next?'<button class="primary" data-click="chooseQuestRoute()" data-quest-route="'+next.id+'">下一段 · '+
  escapeStageText(next.name)+'</button>':'';
 return '<h2>'+title+'</h2><p class="quest-result-stations">'+view.level+' / '+view.total+' 站'+
  (Number.isSafeInteger(reps)&&reps>0?' · '+reps+' 次动作':'')+'</p>'+
  '<div class="quest-result-actions">'+advance+replay+'<button data-click="toggleQuestChooser()">换路线</button></div>';
}
function stageOverlayMarkup(g,current,playing){
 const bodyGame=selected==='quest'||selected==='hiit',finished=playing&&g.status==='complete';
 const teachingPending=current&&!finished&&bodyGame&&g.presentation?.required===true&&scenery.ready(selected)&&!gameSceneReady();
 if(!teachingPending)teachingWaitSince=null;
 if(!current)return'<h2>'+(meta?'连接已中断':'正在连接游戏')+'</h2><p>连接恢复后会同步本局状态。</p><button data-click="reconnectGame()">重新连接</button>';
 if(!finished&&['quest','hiit','tennis'].includes(selected)&&!scenery.ready(selected))return scenery.failed(selected)?'<h2>游戏画面暂不可用</h2><button data-click="reloadScene()">重新载入画面</button>':'<h2>正在准备游戏画面</h2>';
 if(!finished&&selected==='ski'&&!winterReady())return artError?'<h2>雪道画面暂不可用</h2><button data-click="reloadScene()">重新载入画面</button>':'<h2>正在准备雪道</h2>';
 if(showSkiWarmup(g,current))return'';
 if(teachingPending){teachingWaitSince??=performance.now();return performance.now()-teachingWaitSince>5000?'<h2>动作示范暂不可用</h2><button data-click="reloadTeaching()">重新载入动作示范</button>':'<h2>正在准备动作示范</h2>';}
 const control=g.gesture_control||{};
 if((!playing||finished||control.gesture==='yeah')&&control.progress>0&&!control.latched)return'<h2>'+(control.gesture==='yeah'?'保持剪刀手，切换游戏':'保持 OK，开始本局')+'</h2><p>'+Math.round(control.progress*100)+'%</p>';
 if(selected==='home'||playing&&g.status==='playing')return'';
 if(!playing){
  if(selected==='tennis')return tennisLobby(g);
  const routePicker=selected==='quest'?questOverlay(g,false):'';
  if(routePicker)return routePicker;
  const trailPicker=selected==='ski'?skiTrailPicker(g):'';
  if(trailPicker)return '<h2>选择雪道</h2>'+trailPicker+'<div class="ski-lobby-actions"><button class="primary" data-click="startGame()">开始本局</button>'+
   (g.ski?.warmup===null?'<button data-click="startSkiWarmup()">先试滑</button>':'')+'</div>';
  const [,title,detail]=modeDescription(selected,g);
  return '<h2>'+title+'</h2><p>'+detail+'</p><button class="primary" data-click="startGame()">开始本局</button>';
 }
 if(finished){
  const routeResult=selected==='quest'?questOverlay(g,true):'';
  if(routeResult)return routeResult;
  const [title,detail,action]=completedGame(g),result=selected==='ski'?skiResult(g.ski):'';
  if(selected==='tennis')return '<h2>'+title+'</h2><p>'+detail+'</p><div class="tennis-result-actions">'+
   '<button class="primary" data-click="startGame()">'+action+'</button>'+
   Object.keys(tennisModes).filter(id=>id!==(g.tennis_variant||'match')).map(id=>tennisVariantButton(id)).join('')+'</div>';
  if(selected==='ski'&&skiTrails(g).length>1){
   if(skiChooserRound===stageRound(g))return '<h2>更换雪道</h2>'+skiTrailPicker(g)+'<button data-click="toggleSkiChooser()">返回成绩</button>';
   return '<h2>'+title+'</h2>'+(result||'<p>'+detail+'</p>')+'<div class="ski-result-actions"><button class="primary" data-click="startGame()">'+action+'</button><button data-click="toggleSkiChooser()">换雪道</button></div>';
  }
  return'<h2>'+title+'</h2>'+(result||'<p>'+detail+'</p>')+'<button class="primary" data-click="startGame()">'+action+'</button>';
 }
 if(g.status==='waiting'&&g.presentation?.required===true&&!g.presentation.ready&&(g.quest?.phase==='transition'||g.hiit?.phase==='rest'))return'<h2>正在准备下一站</h2>';
 const recovery=g.source_recovery?.required===true,countdown=g.resume_seconds>0&&!g.manually_paused;
 const lineup=g.tennis?.lineup;
 if(selected==='tennis'&&!recovery&&!g.manually_paused&&!countdown&&Array.isArray(lineup)&&lineup.length===2&&
    lineup.every(state=>['ready','calibrating','feet','missing'].includes(state))){
  const title=lineup.includes('missing')?'等待玩家回到画面':'确认双人站位';
  return '<h2>'+title+'</h2>'+tennisLineupMarkup(lineup);
 }
 const r=g.source_recovery;
 const title=recovery?(r.phase==='display'?'正在准备游戏画面':meta?.status!=='live'||r.phase==='camera'?'正在恢复画面':
  r.ready===true?(r.accepts_ok?'站位已确认':'玩家已就绪'):r.accepts_ok?'重新确认站位':r.phase==='players'?'请回到镜头前':'站定片刻'):
  ({waiting:'等待玩家',calibrating:'站定片刻',paused:'游戏已暂停'}[g.status]||'正在准备');
 const heading=countdown?'<h2 class="game-countdown" aria-label="'+Math.ceil(g.resume_seconds)+' 秒后继续">'+Math.ceil(g.resume_seconds)+'</h2>':'<h2>'+title+'</h2>';
 const detail=gamePhaseMessage(g),resume=resumeInputReady(g);
 const gestureHint=r?.accepts_ok?'<p id="resumeGestureHint" class="resume-gesture-hint">放下手，再保持 OK 继续</p>':'';
 return heading+(detail?'<p'+(recovery?' class="game-recovery-message"':'')+'>'+detail+'</p>':'')+
  (resume?'<button class="primary" data-click="resumeGame()">继续本局</button>'+gestureHint+'<p id="resumeNotice" class="resume-notice" role="status"></p>':'');
}
function updateHUD(){const overlay=$('overlay'),pause=$('pauseToggle'),previousFocus=document.activeElement,followStageFocus=previousFocus&&(previousFocus===pause||overlay.contains(previousFocus)||$('skiWarmup').contains(previousFocus));updateSourceMode(meta);const current=gameViewCurrent(),g=presentedGame(),playing=g.mode===selected&&g.mode!=='off';active=current&&playing&&g.status!=='complete';$('camera').textContent=({locked:'镜头已就绪',moving:'镜头正在调整，游戏暂停',settling:'镜头稳定中'})[meta?.room?.registration?.status]||'等待镜头就绪';$('camera').hidden=!current||meta?.room?.registration?.status==='locked';
document.body.dataset.questResult=String(current&&playing&&selected==='quest'&&g.status==='complete'&&questRoutes(g).length>0);
const liveProgress=$('liveProgress');liveProgress.hidden=!playing||g.status==='complete'||!['quest','hiit'].includes(selected);
const players=g.players||[],input=$('input');input.hidden=selected==='tennis';input.textContent=selected==='tennis'?'':players.length?players.map((p,i)=>(i+1)+' 号玩家 · '+(p.lateral<-.12?'左侧':p.lateral>.12?'右侧':'居中')).join('　 /　 '):'让双脚与双手完整入镜';
const feedbackText=selected==='home'?(meta?.home?.reason||''):selected==='hiit'?(g.hiit?.feedback||''):selected==='quest'?(g.quest?.feedback||''):selected==='ski'?skiPresentation(g).event:'';if($('feedback').textContent!==feedbackText)$('feedback').textContent=feedbackText;
if(selected==='quest')updateQuestHUD(g);
$('questTrail').hidden=selected!=='quest';$('questAnnouncement').hidden=selected!=='quest'||!playing;
if(selected==='ski'){const s=g.ski||{},view=skiPresentation(g),trail=skiTrails(g).find(item=>item.id===g.ski_trail);$('mission').textContent=!playing||g.status==='complete'?trail?.name||s.course_name||'林间滑雪':(s.course_name||'林间初雪')+(s.course_count>1?' · 第 '+((s.course||0)+1)+' / '+s.course_count+' 段':'');$('instruction').textContent=view.instruction;$('score').innerHTML=(s.points||0)+'<small>'+view.detail+'</small>';$('progress').style.width=(Math.max(0,Math.min(1,s.progress||0))*100)+'%'}
if(selected==='hiit'){const h=g.hiit||{level:0,name:'深蹲',reps:0,required_reps:3,phase:'work',total_levels:12};$('mission').textContent=h.phase==='rest'?'休息一下':h.phase==='complete'?'本轮训练已完成':h.name;$('instruction').textContent=h.phase==='rest'?'下一关：'+h.name:h.instruction||descriptions.hiit[2];liveProgress.textContent=h.phase==='rest'?Math.ceil(h.rest_seconds||0)+' 秒后继续':'已完成 '+h.reps+' / '+h.required_reps+' 次';$('score').innerHTML=Math.min(h.total_levels,h.level)+' / '+h.total_levels+'<small>动作关卡 · 共 '+(h.total_reps||0)+' 次</small>';$('progress').style.width=(h.phase==='complete'?100:h.phase==='rest'?(1-(h.rest_seconds||0)/5)*100:h.reps/h.required_reps*100)+'%'}
if(selected==='home'){const h=meta?.home||{},d=h.curtain||{},owner=h.owner_track;$('mission').textContent=d.status==='moving'?'已发出窗帘指令':'双臂开合，控制梦幻帘';$('score').innerHTML=(d.position==null?'—':d.position+'%')+'<small>梦幻帘 · '+({ready:'已读回',moving:'等待位置更新',connecting:'连接中',unconfirmed:'位置更新延迟',unavailable:'暂不可用',checking:'检查中',fault:'设备故障'}[d.status]||'等待设备')+'</small>';$('input').textContent=owner==null?'看向镜头，或 OK 选择操作者':'操作者 #'+owner+' · 双臂向两侧打开 → 开帘 / 合拢 → 关帘';$('instruction').textContent=descriptions.home[2];$('progress').style.width=((h.window_gesture?.progress||0)*100)+'%'}
updateTennisHUD(g);
if(pause)pause.hidden=!playing||g.status==='complete'||g.manually_paused===true||g.source_recovery?.required===true||selected==='home';
const stop=$('stopToggle'),warmup=selected==='ski'&&skiWarmup(g),warmupVisible=showSkiWarmup(g,current);
if(stop){stop.hidden=!playing;const label=warmup?'结束试滑':'结束本局';if(stop.textContent!==label)stop.textContent=label;}
updateSkiWarmup(g,warmupVisible);
const feedback=document.querySelector('.player-feedback');if(feedback)feedback.hidden=!current||g.status==='complete'||g.manually_paused||g.resume_seconds>0||(selected==='tennis'?$('camera').hidden&&$('tennisEvent').hidden:selected!=='home'&&(!playing||g.status!=='playing'));
if(g.manually_paused||g.source_recovery?.required===true||g.resume_seconds>0)$('instruction').textContent='';
$('feedback').hidden=!$('feedback').textContent||$('feedback').textContent===$('instruction').textContent;
const gestureHint=$('gestureHint');
if(gestureHint){
 const canStart=current&&g.game_input_enabled&&selected!=='home'&&!g.manually_paused&&(!playing||g.status==='complete');
 gestureHint.hidden=!canStart;
 gestureHint.textContent=(g.status==='complete'?'OK 重玩':'OK 开始')+' · 剪刀手切换游戏 · 触发后放下手';
}
const progress=Number.parseFloat($('progress').style.width);$('progress').parentElement?.setAttribute('aria-valuenow',String(Number.isFinite(progress)?Math.max(0,Math.min(100,progress)):0));
document.dispatchEvent(new CustomEvent('friday:game-teaching',{detail:{key:gameTeachingKey(g),mode:selected,visible:selected==='ski'?warmupVisible:current&&playing&&g.status==='playing'&&scenery.ready(selected),paused:!current||g.manually_paused===true||g.hiit?.phase==='rest'||warmup?.step==='ready'}}));
if(g.presentation?.required===true)presentationClient.tick();
showOverlay(stageOverlayMarkup(g,current,playing));syncGameControls();
const resumeGestureHint=$('resumeGestureHint'),hold=g.gesture_control;
if(resumeGestureHint){
 const text=hold?.progress>0&&!hold.latched?'保持 OK · '+Math.round(hold.progress*100)+'%':'放下手，再保持 OK 继续';
 if(resumeGestureHint.textContent!==text)resumeGestureHint.textContent=text;
}
if(followStageFocus&&!commandPending)restoreStageFocus(previousFocus);
gameSound.tick();}
function polygon(points,fill,stroke){c.beginPath();points.forEach((p,i)=>i?c.lineTo(...p):c.moveTo(...p));c.closePath();c.fillStyle=fill;c.fill();if(stroke){c.strokeStyle=stroke;c.lineWidth=1;c.stroke()}}
function line(a,b,color,width=2){c.beginPath();c.moveTo(...a);c.lineTo(...b);c.strokeStyle=color;c.lineWidth=width;c.lineCap='round';c.stroke()}
function world(at){const g=presentedGame();if(selected==='ski')skiScenery.draw(g,at);else scenery.adventure(g,at);}
function homeWorld(){c.fillStyle='#e6ece0';c.fillRect(0,0,1600,900);const h=meta?.home||{},d=h.curtain||{},position=Math.max(0,Math.min(1,(d.position??50)/100));const x=480,y=300,w=580,hh=300;c.fillStyle='#d1dfd5';c.fillRect(x,y,w,hh);polygon([[x,y+hh],[x+w*.55,y+hh*.3],[x+w,y+hh*.65],[x+w,y+hh]],'#b7cbb6');for(let i=0;i<18;i++){const xx=x+i*w/18;c.fillStyle=`rgba(242,244,231,${.94-position*.82})`;c.fillRect(xx,y,w/18-3,hh)}for(const xx of [x,x+w/2,x+w])line([xx,y],[xx,y+hh],'#69856c',7);line([x,y],[x+w,y],'#69856c',7);line([x,y+hh],[x+w,y+hh],'#69856c',7);c.fillStyle='#55705a';c.font='18px sans-serif';c.fillText('窗帘 · 设备读回 '+(d.position==null?'等待':d.position+'%'),x,y+hh+40);let ty=280;const info=(title,value)=>{c.fillStyle='#7a8d7c';c.font='14px sans-serif';c.fillText(title,1130,ty);c.fillStyle='#345b42';c.font='20px sans-serif';c.fillText(String(value||'—'),1130,ty+33,410);ty+=100};const a=(h.attention||[]).find(a=>a.person_id===h.owner_track);info('注视候选 · 眼部与朝向',a?.qualified?'已稳定（实验判定）':a?.state==='checking'?'正在确认':a?.head_facing&&!a?.eyes_clear?'眼部不清晰，请说 Friday':'未确认，请说 Friday');info('语音',h.voice==='ready'&&h.voice_activity?'听到人声，请继续':({ready:'注视确认后直说，或说 Friday',loading_local_models:'本地模型启动中',transcribing:'正在识别',not_started:'尚未启用',paused:'语音已暂停 · 安静模式',unavailable:'暂不可用'})[h.voice]);info('识别到的内容',h.last_transcript||'等待语音');info('指令处理',h.intent_provider==='local_rules'?'本地快速控制':h.intent_provider==='codex_app_server'?'ARL · Codex':'等待指令');const result=(h.last_failure?.at||0)>(h.last_result?.returned_at||0)?'上个任务暂未完成，请再试一次。':h.last_result?.text;if(result){c.font='16px sans-serif';c.fillStyle='#4d6c54';for(let i=0;i<Math.min(3,Math.ceil(result.length/40));i++)c.fillText(result.slice(i*40,(i+1)*40),470,685+i*27,1010)}}
function hiitWorld(at){scenery.training(presentedGame(),at);}
function tennis(at){scenery.tennis(presentedGame(),at);}
function realSkeleton(){const players=meta?.game?.players||[];if(!players.length)return;const links=[['leftShoulder','rightShoulder'],['leftShoulder','leftElbow'],['leftElbow','leftWrist'],['rightShoulder','rightElbow'],['rightElbow','rightWrist'],['leftShoulder','leftHip'],['rightShoulder','rightHip'],['leftHip','rightHip'],['leftHip','leftKnee'],['leftKnee','leftAnkle'],['rightHip','rightKnee'],['rightKnee','rightAnkle']];for(const [i,p]of players.entries()){const pts=Object.values(p.joints||{});if(!pts.length)continue;const xs=pts.map(q=>q.x),ys=pts.map(q=>q.y),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),scale=Math.min(160/Math.max(.01,maxX-minX),200/Math.max(.01,maxY-minY)),x=65+i*200,y=460;for(const [a,b]of links){const A=p.joints[a],B=p.joints[b];if(A&&B)line([x+(A.x-minX)*scale,y+(A.y-minY)*scale],[x+(B.x-minX)*scale,y+(B.y-minY)*scale],i?'#a78254':'#4a775c',4)}c.font='13px sans-serif';c.fillStyle='#6e856e';c.fillText((i+1)+'号 · 实时骨架',x,y+225)}}
let debugDrawAt=0;function drawDebug(){if(performance.now()-debugDrawAt<100)return;debugDrawAt=performance.now();
 const fresh=gameViewCurrent()&&meta?.status==='live'&&debugReceived!==null&&performance.now()-debugReceived<=1500;dc.clearRect(0,0,640,360);dc.fillStyle='#14251a';dc.fillRect(0,0,640,360);
 if(fresh&&debugFrame){dc.drawImage(debugFrame,0,0,640,360);dc.lineWidth=2.5;dc.strokeStyle='#b4f9c3';const links=[['leftShoulder','rightShoulder'],['leftShoulder','leftElbow'],['leftElbow','leftWrist'],['rightShoulder','rightElbow'],['rightElbow','rightWrist'],['leftShoulder','leftHip'],['rightShoulder','rightHip'],['leftHip','rightHip'],['leftHip','leftKnee'],['leftKnee','leftAnkle'],['rightHip','rightKnee'],['rightKnee','rightAnkle']];
 for(const p of debugMeta?.people||[]){for(const [a,b] of links){const A=p.joints?.[a],B=p.joints?.[b];if(A&&B){dc.beginPath();dc.moveTo(A.x*640,A.y*360);dc.lineTo(B.x*640,B.y*360);dc.stroke()}}for(const q of Object.values(p.joints||{})){dc.beginPath();dc.arc(q.x*640,q.y*360,2.8,0,7);dc.fillStyle='#ffffff';dc.fill()}}
 dc.strokeStyle='#ffe3a6';dc.lineWidth=1.5;for(const h of debugMeta?.hands||[]){for(const finger of [['wrist','thumbCMC','thumbMP','thumbIP','thumbTip'],['wrist','indexMCP','indexPIP','indexDIP','indexTip'],['wrist','middleMCP','middlePIP','middleDIP','middleTip'],['wrist','ringMCP','ringPIP','ringDIP','ringTip'],['wrist','littleMCP','littlePIP','littleDIP','littleTip']])for(let i=1;i<finger.length;i++){const A=h.joints?.[finger[i-1]],B=h.joints?.[finger[i]];if(A&&B){dc.beginPath();dc.moveTo(A.x*640,A.y*360);dc.lineTo(B.x*640,B.y*360);dc.stroke()}}}}
 const m=fresh?meta:null,ps=m?.game?.players||[];$('debugMetrics').textContent=m?`${m.people?.length||0} 人 / ${m.hands?.length||0} 手 · ${m.processed_fps||0} fps · 接收→结果 ${m.pipeline_ms||0} ms`+(ps.length?'\n'+ps.map((p,i)=>`${i+1}号 ${p.lateral<-.05?'左':p.lateral>.05?'右':'中'} ${Math.round(Math.abs(p.lateral)*100)}% · 动力 ${Math.round((p.arm_effort||0)*100)}% · 腕部 ${p.observed_wrists??0}/2 · ${({fresh:'双脚',torso_follow:'躯干接续',brief_hold:'短时保持',missing:'脚部丢失'})[p.foot_observation]||''} · 蹲 ${p.squat_depth==null?'未标定':Math.round(p.squat_depth*100)+'%'}`).join(' · '):' · '+(m.game?.reason||'等待玩家')):'实时画面已过期';
}
let lastScenePaint=-Infinity,stageFrame=null,teachingPlacement=null;
function placeTeaching(r,portrait){
 const figure=$('gameTutorial');if(!figure||!['quest','hiit'].includes(selected))return;
 const floor=scenery.teachingAnchor(selected),scale=Math.min(r.width/(portrait?960:1600),r.height/900);
 const x=canvas.offsetLeft+(r.width-1600*scale)/2+floor.x*scale,y=canvas.offsetTop+(r.height-900*scale)/2+floor.y*scale;
 const fit=portrait?Math.max(0,y-canvas.offsetTop-8)*8/9:420;
 const stamp=[selected,x.toFixed(2),y.toFixed(2),fit.toFixed(2)].join(':');
 if(stamp!==teachingPlacement){
  teachingPlacement=stamp;
  figure.style.setProperty('--teaching-x',x.toFixed(2)+'px');
  figure.style.setProperty('--teaching-y',y.toFixed(2)+'px');
  figure.style.setProperty('--teaching-fit',fit.toFixed(2)+'px');
 }
}
function scheduleStage(){if(stageFrame==null&&!document.hidden&&!stageClosed)stageFrame=requestAnimationFrame(draw);}
function draw(now=performance.now()){
 if(meta?.game?.presentation?.required===true)presentationClient.tick();
 gameSound.tick();stageFrame=null;
 if(document.hidden||stageClosed)return;
 watchGameConnection(now);
 const period=1000/(active?30:10);
 // 60 Hz 的两个刷新间隔可能被计为 33.3ms，留出半毫秒的时间戳误差。
 if(now-lastScenePaint+.5<period){scheduleStage();return;}
 lastScenePaint=now;
 const r=canvas.getBoundingClientRect(),ratio=Math.min(2,devicePixelRatio||1);
 if(canvas.width!==Math.round(r.width*ratio)||canvas.height!==Math.round(r.height*ratio)){
  canvas.width=Math.round(r.width*ratio);canvas.height=Math.round(r.height*ratio);
 }
 c.setTransform(1,0,0,1,0,0);c.fillStyle='#eef0e8';c.fillRect(0,0,canvas.width,canvas.height);
 const portrait=matchMedia('(max-width:760px) and (orientation:portrait)').matches;
 scenery.setPortrait(portrait);placeTeaching(r,portrait);
 const portraitQuestResult=portrait&&document.body.dataset.questResult==='true';
 const scale=portraitQuestResult?Math.max(canvas.width/1600,canvas.height/900):
  Math.min(canvas.width/(portrait?960:1600),canvas.height/900);
 c.setTransform(scale,0,0,scale,(canvas.width-1600*scale)/2,(canvas.height-900*scale)/2);
 const paintAt=gameViewCurrent(now)?now:(lastAcceptedAt??now);
 selected==='home'?homeWorld():selected==='tennis'?tennis(paintAt):selected==='hiit'?hiitWorld(paintAt):world(paintAt);
 drawDebug();if(typeof drawRecordPage==='function')drawRecordPage(performance.now());
 scheduleStage();
}
document.addEventListener('visibilitychange',()=>{if(document.hidden){gameSound.quiet();presentationClient.release();if(stageFrame!=null)cancelAnimationFrame(stageFrame);stageFrame=null;}else{updateHUD();scheduleStage();}});
function closeGameView(){gameSound.close();presentationClient.close();stageClosed=true;streamGeneration++;clearTimeout(reconnectTimer);reconnectTimer=null;commandPending?.controller.abort();commandPending=null;const old=gameSocket;gameSocket=null;streamOpen=false;if(old){old.onclose=null;old.close();}if(stageFrame!==null)cancelAnimationFrame(stageFrame);stageFrame=null;}
window.addEventListener('pagehide',closeGameView);
window.addEventListener('pageshow',event=>{if(event.persisted){gameSound.reopen();stageClosed=false;connect();scheduleStage();}});

/* Canvas-only recording: optional page sound is not added to recorded video. */
let localRecorder=null,recordUpload=Promise.resolve(),recordFailed=false,recordClaim=false,recordCanvas=null,recordDrawAt=0,presenceSince=0,recordIdentity=null,recordStopTimer=null;
const localRecordingHost=['127.0.0.1','localhost','[::1]'].includes(location.hostname)&&!navigator.userAgent.includes('; wv)');
async function recordRequest(data){const r=await fetch(url('/api/record'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw Error('recording_request_failed');return r.json()}
async function toggleRecording(){if(!gameViewCurrent()||commandPending)return;const current=meta?.recording||{};await recordRequest({action:'arm',enabled:!current.armed})}
function recordText(ctx,text,x,y,size=16,color='#314d3a',max=1100){ctx.font=`${size}px -apple-system, sans-serif`;ctx.fillStyle=color;ctx.textAlign='left';ctx.fillText(String(text||''),x,y,max)}
function drawRecordPage(at){if(!localRecorder||localRecorder.state!=='recording'||at-recordDrawAt<65)return;recordDrawAt=at;const x=recordCanvas.getContext('2d');x.fillStyle='#edf1e9';x.fillRect(0,0,1280,720);x.drawImage(canvas,0,70,1280,590);recordText(x,'山径 · 客厅运动',24,38,24);const modes=[['quest','Pose 闯关'],['ski','第一人称滑雪'],['tennis','双人网球'],['hiit','HIIT 闯关'],['home','智能家居']];modes.forEach(([m,name],i)=>{x.fillStyle=m===selected?'#3e6550':'#e3eade';x.fillRect(350+i*130,15,120,37);recordText(x,name,362+i*130,39,16,m===selected?'#ffffff':'#50614c',125)});recordText(x,$('connection').textContent,1030,38,14);recordText(x,$('chapter').textContent,24,100,13,'#748473');recordText(x,$('mission').textContent,24,138,27);recordText(x,$('instruction').textContent,24,169,14,'#667d69',1000);recordText(x,$('score').innerText.replace(/\n/g,' '),1030,115,24,'#314d3a',230);
 const overlay=$('overlay');if(overlay.textContent.trim()){x.fillStyle='#edf3e9ed';x.fillRect(340,260,620,150);recordText(x,overlay.querySelector('h2')?.textContent||'',370,310,28,'#314d3a',560);recordText(x,overlay.querySelector('p')?.textContent||'',370,355,15,'#667d69',560)}
 x.fillStyle='#f1f5ed';x.fillRect(16,420,300,220);x.drawImage($('debugVideo'),20,446,292,164);recordText(x,'实时识别 · 调试浮窗',26,439,13);recordText(x,$('debugMetrics').textContent,26,630,11,'#4f6e57',280);recordText(x,$('input').textContent,340,605,14,'#365d42',900);recordText(x,$('camera').textContent,340,634,13,'#667d69',900);recordText(x,'OK 保持 1 秒开始 · Yeah 保持 1 秒切换',24,694,14,'#667d69');x.fillStyle='#b4604e';x.beginPath();x.arc(1060,690,5,0,7);x.fill();recordText(x,'录制中 · '+Math.floor((Date.now()-recordIdentity.started_at*1000)/1000)+' 秒',1075,695,14,'#8f5446',190)}
async function beginPageRecording(){if(recordClaim||localRecorder)return;recordClaim=true;try{
 if(typeof MediaRecorder==='undefined'||!HTMLCanvasElement.prototype.captureStream)throw Error('页面录制能力不可用');
 const mime=['video/webm;codecs=vp8','video/mp4','video/mp4;codecs=avc1.42E01E','video/webm'].find(t=>MediaRecorder.isTypeSupported(t));if(!mime)throw Error('没有可用的视频编码器');
 recordCanvas=document.createElement('canvas');recordCanvas.width=1280;recordCanvas.height=720;const stream=recordCanvas.captureStream(15);const encoder=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:2600000});recordIdentity=await recordRequest({action:'start',mime});recordFailed=false;recordUpload=Promise.resolve();localRecorder=encoder;
 encoder.ondataavailable=e=>{if(!e.data.size)return;recordUpload=recordUpload.then(async()=>{const r=await fetch(url('/api/record/chunk')+'&id='+encodeURIComponent(recordIdentity.id),{method:'POST',body:e.data});if(!r.ok)throw Error('视频片段写入失败')}).catch(()=>{recordFailed=true})};
 encoder.onstop=async()=>{clearTimeout(recordStopTimer);stream.getTracks().forEach(t=>t.stop());await recordUpload;try{if(!recordFailed)await recordRequest({action:'finish',id:recordIdentity.id});else await recordRequest({action:'arm',enabled:false})}finally{localRecorder=null;recordClaim=false}};
 encoder.start(2000);recordStopTimer=setTimeout(()=>{if(encoder.state==='recording')encoder.stop()},180000);
 }catch(e){recordClaim=false;$('recordState').textContent=e.message;try{await recordRequest({action:'error',reason:String(e.message).slice(0,120)})}catch{}}}
function updateRecording(state){state=state||{};const button=$('recordToggle');if(!button)return;button.disabled=!gameViewCurrent()||!!commandPending||typeof state.armed!=='boolean';button.setAttribute('aria-pressed',String(!!state.armed));button.textContent=state.status==='recording'?'停止录制':state.armed?'本局录制已开启':'录制本局';$('recordState').textContent=({armed:'本局录制已开启',recording:'正在录制 · 不含声音',stopping:'正在保存视频',saved:'视频已保存',interrupted:'录制已中断',error:'录制暂不可用',off:''})[state.status]||'';
 if(localRecorder&&state.status==='stopping'&&localRecorder.state==='recording')localRecorder.stop();
 const visible=meta?.status==='live'&&(meta.people||[]).some(p=>Object.keys(p.joints||{}).length>=8);presenceSince=visible?(presenceSince||performance.now()):0;
 if(localRecordingHost&&state.armed&&state.status==='armed'&&presenceSince&&performance.now()-presenceSince>350)beginPageRecording();
 if(state.status==='saved'&&state.id){$('recordState').innerHTML='已保存 · <a href="'+url('/api/record/file')+'&id='+encodeURIComponent(state.id)+'" download>下载视频</a>'}}

function delegatedCall(node,call){switch(call){case "chooseMode('hiit')": chooseMode('hiit'); break;
case "chooseMode('home')": chooseMode('home'); break;
case "chooseMode('quest')": chooseMode('quest'); break;
case "chooseMode('ski')": chooseMode('ski'); break;
case "chooseMode('tennis')": chooseMode('tennis'); break;
case "curtainCommand('close')": curtainCommand('close'); break;
case "curtainCommand('open')": curtainCommand('open'); break;
case 'document.documentElement.requestFullscreen?.()': document.documentElement.requestFullscreen?.(); break;
case 'startGame()': startGame(); break;
case 'startSkiWarmup()': startGame(true); break;
case 'stopGame()': stopGame(); break;
case 'pauseGame()': pauseGame(); break;
case 'resumeGame()': resumeGame(); break;
case 'changeDifficulty()': changeDifficulty(); break;
case 'chooseTennisVariant()': chooseTennisVariant(node.dataset.tennisVariant); break;
case 'toggleSkiChooser()': toggleSkiChooser(); break;
case 'chooseSkiTrail()': chooseSkiTrail(node.dataset.skiTrail); break;
case 'chooseQuestRoute()': chooseQuestRoute(node.dataset.questRoute); break;
case 'toggleQuestChooser()': toggleQuestChooser(); break;
case 'reconnectGame()': reconnectGame(); break;
case 'togglePlayerView()': togglePlayerView(); break;
case 'reloadScene()': reloadScene(); break;
case 'reloadTeaching()': reloadTeaching(); break;
case 'toggleRecording()': toggleRecording(); break;
case 'toggleGameSound()': toggleGameSound(); break;}}
const stageOptions=document.querySelector('.stage-options');
for(const kind of ['click','change','input'])document.addEventListener(kind,e=>{if(kind==='click'&&stageOptions?.open&&!stageOptions.contains(e.target))stageOptions.open=false;const node=e.target.closest('[data-'+kind+']');if(node)delegatedCall(node,node.getAttribute('data-'+kind));});
stageOptions?.addEventListener('keydown',e=>{if(e.key==='Escape'&&stageOptions.open){e.preventDefault();stageOptions.open=false;stageOptions.querySelector('summary')?.focus();}});

// Start after all recorder and renderer state has been initialized.
connect();draw();
