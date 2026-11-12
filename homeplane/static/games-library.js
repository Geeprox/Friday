import {motionTile} from './motion-guide.js';
import {mountPractice} from './recognition-practice.js';
import {mountWerewolf} from './werewolf-host.js';
import {mountKtv} from './ktv-console/app.js';
const h=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const b=h('button',text,cls);b.type='button';b.onclick=fn;return b;};
const art=id=>id==='beam'?'/static/games-art/beam.webp':['ski','tennis','quest','hiit','werewolf','ktv'].includes(id)?'/static/games-art/'+id+'.png':null;
const moves={ski:['body.arm_effort','body.lateral'],tennis:['body.lateral','body.swing'],quest:['pose.horizontal','pose.overhead','exercise.squat','exercise.side_tap','exercise.side_step','exercise.high_knee','exercise.jack','exercise.side_bend','exercise.side_lunge','exercise.reach_up','exercise.arm_open','exercise.balance','exercise.cross_knee','exercise.side_raise','exercise.overhead_clap'],hiit:['exercise.squat','exercise.jack','exercise.high_knee','exercise.side_lunge','exercise.side_step','exercise.side_bend','exercise.reach_up','exercise.arm_open','exercise.balance','exercise.cross_knee','exercise.side_raise','exercise.overhead_clap']};
const tennisVariants=[
 ['practice','单人练习','1 人 · 9 球'],
 ['solo','单人挑战','1 人 · 抢七'],
 ['match','双人对打','2 人 · 抢七']
];
const circuitModes=[['quest','山径冒险'],['hiit','运动训练']];
const isCircuit=app=>circuitModes.some(([id])=>id===app.id);
const displayName=app=>isCircuit(app)?'体感闯关':app.name;
const modeTitle=app=>circuitModes.find(([id])=>id===app.id)?.[1]||app.name;
const ktvEntry={id:'ktv',name:'家庭 KTV',description:'搜歌、排歌单，和家人一起唱。',searchText:'唱歌 点歌 K歌 卡拉OK 曲库',kind:'party',players_label:'一起唱'};
const storeEntries=apps=>[...apps.filter(a=>a.id!=='ktv'),ktvEntry].filter(a=>a.id!=='hiit'||!apps.some(b=>b.id==='quest')).map(a=>a.id==='quest'?{...a,name:'体感闯关',description:'沿山径完成动作，也可选择运动训练。',searchText:'山径冒险 动作闯关 运动闯关 深蹲 抬膝 上伸 胸前开合 单腿站立 交叉触膝 交替侧抬腿 头顶合掌'}:a);
const category=app=>app.id==='ktv'?'音乐聚会':app.kind==='party'?'聚会游戏':isCircuit(app)?'轻运动':'体感游戏';
const players=app=>app.players_label||(app.players?.[0]===app.players?.[1]?app.players[0]===1?'单人':app.players[0]+' 人同玩':app.players[0]+'–'+app.players[1]+' 人');
function skiRoutePreview(trail){
 const canvas=h('canvas',null,'ski-route-preview');
 canvas.width=216;canvas.height=58;canvas.setAttribute('aria-hidden','true');
 const ctx=canvas.getContext?.('2d'),path=trail.path;
 if(!ctx||!Array.isArray(path)||path.length<2||
    !path.every(point=>Array.isArray(point)&&Number.isFinite(point[0])&&Number.isFinite(point[1])))return canvas;
 const points=path.map(([distance,lateral])=>[8+200*distance/trail.distance,29-32*lateral]);
 ctx.strokeStyle=ctx.fillStyle='#3d594f';
 ctx.lineWidth=3;ctx.lineJoin='round';ctx.lineCap='round';ctx.beginPath();
 for(const [index,[x,y]] of points.entries()){if(index)ctx.lineTo(x,y);else ctx.moveTo(x,y);}
 ctx.stroke();
 for(const [x,y] of points.slice(1,-1)){ctx.beginPath();ctx.arc(x,y,3.5,0,Math.PI*2);ctx.fill();}
 return canvas;
}
export function mountGames(root,{go=()=>{}}={}){
 const difficultySelections=new Map();
 let alive=true,report=null,cameras=[],selection=null,filter='all',query='',busy=false,practice=null,party=null,ktv=null,diagrams=[],generation=0,ui={},lastError='',readOwner=null,readEpoch=0,readAt=null,stateFresh=false,readNotice='',catalogLoaded=false,pendingSkiTrail=null,pendingTennisVariant=null,pendingQuestRoute=null;
 const wrap=h('section',null,'games-library'),status=h('p','正在读取游戏库…','games-connection'),screen=h('div',null,'games-screen');status.setAttribute('role','status');wrap.append(status,screen);root.append(wrap);
 const abort=new AbortController();
 async function api(path,body,signal,headers={}){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...headers},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.any([abort.signal,AbortSignal.timeout(12000),...(signal?[signal]:[])])});const data=await r.json();if(!r.ok)throw Error(data.detail||'unavailable');return data;}
 function controlsCurrent(){const age=Date.now()-readAt;return stateFresh&&readAt!==null&&age>=0&&age<=8000;}
 function gameIntent(service=report?.service){const value=service?.game_intent;return typeof value==='string'&&/^[a-f0-9]{64}$/.test(value)?value:null;}
 function gameDifficulties(app){return ['quest','hiit','ski','tennis'].includes(app?.id)&&Array.isArray(report?.difficulties)?report.difficulties.filter(d=>['easy','standard','challenge'].includes(d?.id)&&typeof d.label==='string'&&d.label.length<=12):[];}
 function chosenDifficulty(app){const options=gameDifficulties(app),current=report?.service?.game;return options.find(d=>d.id===(difficultySelections.get(app.id)||(current?.selected_mode===app.mode?current.difficulty:null)))||options.find(d=>d.id==='standard');}
 function currentTennisVariant(){
  const game=report?.service?.game;
  return game?.selected_mode==='tennis'&&tennisVariants.some(([id])=>id===game.tennis_variant)
   ?game.tennis_variant:null;
 }
 function chosenTennisVariant(){return pendingTennisVariant||currentTennisVariant()||'match';}
 function tennisVariantPicker(){
  const field=h('fieldset',null,'game-tennis-variants'),options=h('div',null,'game-tennis-variant-list');
  field.append(h('legend','玩法'),options);
  for(const [id,label,details]of tennisVariants){
   const choice=h('label',null,'game-tennis-variant'),input=h('input'),copy=h('span');
   input.type='radio';input.name='tennis-variant';input.value=id;
   input.onchange=()=>{if(!busy&&input.checked){pendingTennisVariant=id;update();}};
   copy.append(h('strong',label),h('small',details));choice.append(input,copy);options.append(choice);
  }
  return field;
 }
 function questRoutes(app){
  if(app?.id!=='quest'||!Array.isArray(report?.quest_routes))return[];
  return report.quest_routes.filter(route=>['full','forest','stream','camp'].includes(route?.id)
   &&typeof route.name==='string'&&route.name.length<=16
   &&Number.isSafeInteger(route.stations)&&route.stations>0
   &&Array.isArray(route.teaching)&&route.teaching.length===route.stations
   &&route.teaching.every(key=>moves.quest.includes(key)));
 }
 function currentQuestRoute(app){
  const game=report?.service?.game;
  return game?.selected_mode==='quest'?questRoutes(app).find(route=>route.id===game.quest_route):null;
 }
 function chosenQuestRoute(app){
  const routes=questRoutes(app);
  return routes.find(route=>route.id===pendingQuestRoute)||currentQuestRoute(app)||routes.find(route=>route.id==='full');
 }
 function questRoutePicker(app){
  const routes=questRoutes(app);
  if(!routes.length)return null;
  const field=h('fieldset',null,'game-quest-routes'),list=h('div',null,'game-quest-route-list');
  field.append(h('legend','路线'),list);
  for(const route of routes){
   const choice=h('label',null,'game-quest-route'),input=h('input'),copy=h('span');
   input.type='radio';input.name='quest-route';input.value=route.id;
   input.onchange=()=>{if(!busy&&input.checked){pendingQuestRoute=route.id;update();}};
   copy.append(h('strong',route.name),h('small',route.stations+' 站'));
   choice.append(input,copy);list.append(choice);
  }
  return field;
 }
 function skiTrails(app){
  if(app?.id!=='ski'||!Array.isArray(report?.ski_trails))return[];
  return report.ski_trails.filter(trail=>typeof trail?.id==='string'&&/^[a-z][a-z-]{1,23}$/.test(trail.id)&&
   typeof trail.name==='string'&&trail.name.length<=16&&Number.isSafeInteger(trail.distance)&&trail.distance>0&&
   Number.isSafeInteger(trail.gate_count)&&trail.gate_count>0);
 }
 function chosenSkiTrail(app){
  const options=skiTrails(app),id=pendingSkiTrail||report?.service?.game?.ski_trail;
  return options.find(trail=>trail.id===id)||options.find(trail=>trail.id==='summit')||options[0];
 }
 function skiTrailPicker(app){
  const trails=skiTrails(app);
  if(!trails.length)return null;
  const group=h('div',null,'game-ski-trails'),label=h('h3','雪道'),list=h('div',null,'game-ski-trail-list');
  label.id='game-ski-trail-label';group.setAttribute('role','group');group.setAttribute('aria-labelledby',label.id);
  for(const trail of trails){
   const control=button('',()=>{if(!busy){pendingSkiTrail=trail.id;update();}},'game-ski-trail');
   control.dataset.skiTrail=trail.id;
   control.append(h('span',trail.name,'ski-route-name'),h('span',trail.distance+' 米 · '+trail.gate_count+' 道门','ski-route-length'),skiRoutePreview(trail));
   list.append(control);
  }
  group.append(label,list);
  return group;
 }
 function retireRead(){readEpoch++;const previous=readOwner;readOwner=null;previous?.controller.abort();}
 function clean(){ktv?.dispose();ktv=null;practice?.dispose();practice=null;party?.dispose();party=null;diagrams.forEach(d=>d.dispose());diagrams=[];retireStage();ui={};}
 function cover(app,cls='game-cover'){const src=art(app.id);if(src){const image=h('img',null,cls);image.src=src;image.alt=app.name+'封面插画';image.width=1672;image.height=941;image.decoding='async';return image;}const fallback=h('div',null,'game-tool-cover'),icon=h('span',null,'icon icon-users');icon.setAttribute('aria-hidden','true');fallback.append(icon,h('span',app.name));return fallback;}
 function select(app){if(busy)return;selection=app;lastError='';generation++;draw();screen.scrollIntoView?.({block:'start',behavior:'instant'});const title=screen.querySelector('h2');title?.setAttribute('tabindex','-1');title?.focus?.({preventScroll:true});}
 function focusStoreGame(id){
  const gameId=id==='hiit'&&report.apps.some(app=>app.id==='quest')?'quest':id;
  const target=[...screen.querySelectorAll('.game-cover-button')].find(node=>node.dataset.game===gameId)
    ||screen.querySelector('.games-feature-image')||screen.querySelector('input');
  target?.focus();
 }
 function returnToStore(app){selection=null;generation++;draw();focusStoreGame(app.id);}
 function preparingGame(){const g=report?.service?.game;return g?.mode==='off'&&g.game_input_enabled===true;}
 function currentGame(){const g=report?.service?.game,mode=preparingGame()?g.selected_mode:g?.mode;return report?.apps.find(a=>a.mode===mode&&a.kind==='game');}
 function serviceStatus(){
  if(!report)return;
  const service=report.service,error=lastError&&!ui.notice;
  const message=error?lastError:!controlsCurrent()?(readNotice||'正在更新游戏状态'):
    !service.configured?'接入摄像头后，即可用身体动作玩游戏':
    service.capture_enabled===false?'摄像头已暂停':!gameIntent()?'游戏状态暂时不可用':'';
  status.hidden=selection?.id==='ktv'||!message;
  if(status.textContent!==message)status.textContent=message;
  status.dataset.state=error?'changed':'waiting';
 }
 function roundVariant(app){
  const game=report.service.game;
  if(app.id==='ski')return skiTrails(app).find(trail=>trail.id===game.ski_trail)?.name;
  if(app.id==='tennis')return tennisVariants.find(([id])=>id===game.tennis_variant)?.[1];
  if(app.id==='quest')return currentQuestRoute(app)?.name;
 }
 function roundStatus(){
  if(!controlsCurrent())return '正在更新';
  const game=report.service.game;
  if(preparingGame())return '准备中';
  return {complete:'已完成',paused:'已暂停',waiting:'等待玩家',calibrating:'准备中',playing:'进行中'}[game.status]
    ||'等待游戏状态';
 }
 function makeRoundSummary(resume,stop,artwork){
  const element=h('div',null,'game-round-summary'),copy=h('div',null,'game-round-copy');
  const title=h('strong'),state=h('p'),actions=h('div',null,'game-round-actions');
  element.setAttribute('role','group');
  element.setAttribute('aria-label','当前游戏');
  state.setAttribute('role','status');
  copy.append(title,state);
  actions.append(resume,stop);
  if(artwork)element.append(artwork);
  element.append(copy,actions);
  return {element,title,state,resume,stop};
 }
 function updateRunning(){
  if(!ui.running)return;const live=report.service.owned?currentGame():null,id=live?.id||'';
  if(ui.running.dataset.game!==id){
   const previous=ui.running.dataset.game,restoreFocus=ui.running.contains(document.activeElement);
   ui.running.dataset.game=id;
   ui.running.replaceChildren();
   ui.runningRound=null;
   if(live){
    const resume=button('继续游戏',()=>{
     select(live);
     return perform(openStage,ui.roundNotice);
    },'primary');
    const stop=button('结束游戏',()=>perform(stopGame));
    const artwork=cover(live,'game-round-art');artwork.alt='';
    const summary=makeRoundSummary(resume,stop,artwork);
    summary.element.className+=' game-running-banner';
    summary.title.textContent=modeTitle(live);
    ui.running.append(summary.element);
    ui.runningRound=summary;
   }
   if(restoreFocus&&previous)focusStoreGame(previous);
  }
  const summary=ui.runningRound;
  if(!summary)return;
  const completed=report.service.game.status==='complete';
  const state=[roundVariant(live),roundStatus()].filter(Boolean).join(' · ');
  if(summary.state.textContent!==state)summary.state.textContent=state;
  summary.resume.textContent=completed?'查看成绩':preparingGame()?'进入游戏':'继续游戏';
  summary.stop.textContent=completed?'退出游戏':'结束游戏';
  summary.resume.disabled=summary.stop.disabled=busy||!controlsCurrent()||!gameIntent();
 }
 function draw(){if(!alive||!report)return;clean();screen.replaceChildren();serviceStatus();if(selection)drawDetail();else drawStore();}
 function drawStore(){
  ui.running=h('div',null,'game-running-slot');screen.append(ui.running);updateRunning();
  const games=storeEntries(report.apps).filter(a=>a.kind==='game'),featured=games.find(a=>a.id==='ski')||games[0];
  if(featured){const feature=h('article',null,'games-feature'),imageButton=button('',()=>select(featured),'games-feature-image');imageButton.setAttribute('aria-label','查看'+featured.name);imageButton.append(cover(featured));const copy=h('div',null,'games-feature-copy'),meta=h('p',players(featured)+' · '+category(featured),'game-meta');copy.append(h('h2',featured.name),meta,h('p',featured.description,'game-feature-description'),button('查看游戏',()=>select(featured),'primary'));feature.append(imageButton,copy);screen.append(feature);}
  const toolbar=h('div',null,'games-toolbar'),filters=h('div',null,'games-filters');filters.setAttribute('aria-label','游戏类型');for(const[key,label]of [['all','全部游戏'],['multi','一起玩'],['exercise','轻运动'],['tools','聚会游戏']]){const b=button(label,()=>{filter=key;drawResults();for(const item of filters.children)item.setAttribute('aria-pressed',String(item===b));});b.setAttribute('aria-pressed',String(filter===key));filters.append(b);}const search=h('input');search.type='search';search.placeholder='搜索游戏';search.value=query;search.setAttribute('aria-label','搜索游戏');search.oninput=()=>{query=search.value;drawResults();};toolbar.append(filters,search);screen.append(toolbar);
  const results=h('div',null,'games-grid');results.setAttribute('aria-live','polite');screen.append(results);
  function drawResults(){results.replaceChildren();const apps=storeEntries(report.apps).filter(a=>a.kind!=='interaction'&&(filter!=='multi'||a.id==='ktv'||a.players[1]>1)&&(filter!=='exercise'||isCircuit(a))&&(filter!=='tools'||a.kind==='party')&&(!query||(a.name+a.description+(a.searchText||'')).includes(query.trim())));for(const app of apps){const card=h('article',null,'game-card'),coverButton=button('',()=>select(app),'game-cover-button');coverButton.dataset.game=app.id;coverButton.setAttribute('aria-label','查看'+app.name);coverButton.append(cover(app));const text=button(app.name,()=>select(app),'game-title-button');const meta=h('p',players(app)+' · '+category(app),'game-meta');card.append(coverButton,text,meta);if(app.status==='prototype')card.append(h('span','开发预览','game-preview-label'));results.append(card);}if(!apps.length){const empty=h('div',null,'games-empty');empty.append(h('h3','没有找到这个游戏'),h('p','换个名称，或查看全部游戏。'),button('显示全部游戏',()=>{query='';filter='all';draw();}));results.append(empty);}}
  drawResults();const footer=h('div',null,'games-footer');footer.append(h('p','体感游戏无需手柄，聚会游戏用手机加入。'),button('认识游戏动作',()=>{try{sessionStorage.setItem('Friday.interactionMode','gestures');}catch{}go('Interaction');}),button('全屋互动控制',()=>go('Interaction')));screen.append(footer);
 }
 function drawDetail(){
  if(selection.id==='ktv'){void openKtv();return;}
  const app=selection.id==='quest'&&selection.name==='体感闯关'?report.apps.find(a=>a.id==='quest')||selection:selection,back=button('全部游戏',()=>returnToStore(app),'game-back');const arrow=h('span',null,'icon icon-arrow-right');arrow.setAttribute('aria-hidden','true');back.prepend(arrow);screen.append(back);
  if(app.id==='werewolf'){party=mountWerewolf(screen,{go});return;}
  const top=h('div',null,'game-detail-layout'),visual=h('div',null,'game-detail-art');top.dataset.game=app.id;visual.append(cover(app));const info=h('section',null,'game-detail-info');info.append(h('h2',displayName(app)));if(app.id!=='tennis')info.append(h('p',players(app)+' · '+category(app),'game-meta'),h('p',app.description,'game-description'));if(isCircuit(app)){const modes=h('div',null,'game-mode-picker');modes.setAttribute('aria-label','闯关玩法');for(const[id,label]of circuitModes){const next=report.apps.find(a=>a.id===id);if(!next)continue;const b=button(label,()=>select(next));b.setAttribute('aria-pressed',String(app.id===id));modes.append(b);}info.append(modes);}
  if(app.status==='prototype')info.append(h('p','开发预览 · 座位追踪与指认工具','game-preview-label'));
  const roundNotice=h('p',null,'game-notice');
  roundNotice.setAttribute('role','status');
  const resume=button('继续游戏',()=>perform(openStage,roundNotice));
  const stop=button('结束游戏',()=>perform(stopGame,roundNotice));
  const round=makeRoundSummary(resume,stop);
  round.element.className+=' game-detail-round';
  round.element.hidden=true;
  round.element.append(roundNotice);
  info.append(round.element);
  const trailPicker=skiTrailPicker(app),tennisPicker=app.id==='tennis'?tennisVariantPicker():null,questPicker=questRoutePicker(app);
  if(trailPicker)info.append(trailPicker);
  if(tennisPicker)info.append(tennisPicker);
  if(questPicker)info.append(questPicker);
  let difficulty=null,difficultyNote=null;
  const options=gameDifficulties(app);
  if(options.length){const field=h('div',null,'game-difficulty'),label=h('label','难度');label.setAttribute('for','game-difficulty');difficulty=h('select');difficulty.id='game-difficulty';difficulty.setAttribute('aria-describedby','game-difficulty-note');for(const option of options){const item=h('option',option.label);item.value=option.id;difficulty.append(item);}difficulty.value=chosenDifficulty(app)?.id||'standard';difficultyNote=h('p',null,'game-difficulty-note');difficultyNote.id='game-difficulty-note';difficulty.onchange=()=>{if(busy){difficulty.value=chosenDifficulty(app)?.id||'standard';return;}if(options.some(d=>d.id===difficulty.value))difficultySelections.set(app.id,difficulty.value);update();};field.append(label,difficulty,difficultyNote);info.append(field);}
  const readiness=h('p',null,'game-readiness');info.append(readiness);const notice=h('p',lastError,'game-notice');notice.setAttribute('role','status');const actions=h('div',null,'game-actions'),start=button(app.kind==='seating'?'打开聚会工具':'开始游戏',()=>perform(intent=>startGame(app,intent)),'primary');actions.append(start);info.append(actions,notice);top.append(visual,info);screen.append(top);
  const stage=h('dialog',null,'game-stage-slot');
  stage.setAttribute('aria-labelledby','game-stage-title');
  stage.oncancel=event=>{
   event.preventDefault();
   if(ui.playView&&document.fullscreenElement===ui.playView)void document.exitFullscreen().catch(()=>{});
   else closeStage();
  };
  screen.append(stage);
  const teaching=h('section',null,'game-how-to'),tennisTip=tennisPicker?h('p'):null;
  if(questPicker){
   teaching.append(h('h3','动作示范'),h('p','留出活动空间，让双手和双脚完整入镜。','game-motion-instruction'));
  }else if(app.id==='beam'){
   const guide=h('figure',null,'game-beam-guide'),diagram=h('img'),caption=h('figcaption');
   diagram.src='/static/games-art/beam-pose.svg';
   diagram.alt='双臂从放下到在胸前摆成 L 的动作示范';
   caption.append(h('strong','一竖一横，在胸前摆出 L'),h('p','用小幅转身和抬臂瞄准。保持到光线发出，放下双臂再瞄下一只。'));
   guide.append(diagram,caption);
   teaching.append(h('h3','怎么玩'),guide);
  }else{
   teaching.append(h('h3','怎么玩'));
   const steps=h('div',null,'game-play-steps');
   steps.append(h('p','留出活动空间，让全身入镜'));
   if(tennisTip)steps.append(tennisTip);
   else steps.append(h('p','比出 OK 选择玩家，放下手后开始'),h('p',app.players?.[0]===2?'两位玩家都入镜后一起参与':'跟着画面，用动作参与'));
   teaching.append(steps);
  }
  const tutorial=h('div',null,'motion-mini-list'),practiceSlot=h('div');
  teaching.append(tutorial,practiceSlot);screen.append(teaching);
  if(app.kind==='seating')teaching.append(h('p','此工具提供座位追踪和指认，不含狼人杀角色或裁判规则。','game-meta'));
  const tv=h('p',null,'game-tv-status');tv.setAttribute('role','status');screen.append(tv);
  ui={start,resume,stop,round,roundNotice,readiness,notice,stage,tv,difficulty,difficultyNote,trailPicker,tennisPicker,tennisTip,questPicker,tutorial,practiceSlot};
  update();
 }
 function updateTeaching(app,route){
  const {tutorial,practiceSlot}=ui;
  const keys=app.id==='beam'?[]:route?.teaching||moves[app.id]||['hand.point','hand.ok'],signature=keys.join(',');
  if(tutorial.dataset.moves===signature)return;
  practice?.dispose();practice=null;practiceSlot.replaceChildren();
  diagrams.forEach(diagram=>diagram.dispose());
  diagrams=keys.map(key=>motionTile(key,gesture=>{
   practice?.dispose();
   practice=mountPractice(practiceSlot,{gesture,go,cameras,onClose:()=>{practice=null;practiceSlot.replaceChildren();}});
   practiceSlot.scrollIntoView?.({block:'nearest',behavior:'instant'});
  }));
  tutorial.dataset.moves=signature;
  tutorial.replaceChildren(...diagrams);
 }
 async function startGame(app,intent){
  practice?.dispose();practice=null;
  const id=cameras.find(c=>c.id===report.service.camera)?.id||cameras[0]?.id;
  if(!id)throw Error('camera_missing');
  const options={},difficulty=chosenDifficulty(app),trail=chosenSkiTrail(app),route=chosenQuestRoute(app);
  if(difficulty)options.difficulty=difficulty.id;
  if(trail)options.ski_trail=trail.id;
  if(app.id==='tennis')options.tennis_variant=chosenTennisVariant();
  if(route)options.quest_route=route.id;
  const result=await api('/api/embodied/start',{camera:id,app:app.id,expected_intent:intent,...options});
  const confirmed=gameIntent(result);
  if(!confirmed||!result.owned||result.game?.selected_mode!==app.mode||
     Object.entries(options).some(([key,value])=>result.game?.[key]!==value))throw Error('game_state_changed');
  if(alive){
   await openStage(confirmed);
   difficultySelections.delete(app.id);
   if(trail)pendingSkiTrail=null;
   if(options.tennis_variant)pendingTennisVariant=null;
   if(route)pendingQuestRoute=null;
  }
 }
 async function openKtv(){
  const stamp=generation;screen.append(h('p','正在连接家庭 KTV…','games-connection'));
  try{
   if(!document.querySelector('link[data-ktv-styles]')){const css=h('link');css.rel='stylesheet';css.href='/static/ktv-console/style.css';css.setAttribute('data-ktv-styles','');document.head.append(css);}
   if(!globalThis.FridayPartyAudio)await import('/api/games/ktv/party-audio.js');
   if(!alive||stamp!==generation||selection?.id!=='ktv')return;screen.replaceChildren();
   ktv=mountKtv(screen,{endpoint:'/api/games/ktv',embedded:true,partyAudio:globalThis.FridayPartyAudio,readOnly:document.body.dataset.consoleReadOnly==='true',onBack:()=>{selection=null;generation++;draw();screen.querySelector('button[aria-label="查看家庭 KTV"]')?.focus?.();}});
  }catch{if(alive&&stamp===generation){screen.replaceChildren(h('h2','家庭 KTV'),h('p','页面暂时无法载入，请重试。','games-empty'),button('重新载入',()=>{screen.replaceChildren();void openKtv();}),button('返回游戏库',()=>{selection=null;generation++;draw();}));}}
 }
 function retireStage(){
  if(ui.stage?.open)ui.stage.close();
  ui.stage?.replaceChildren();
  ui.playView=ui.playTitle=ui.frame=null;
  ui.stageEnded=false;
 }
 function closeStage(){
  generation++;
  retireStage();
  if(!busy){stateFresh=false;retireRead();update();void refresh(true);}
  const heading=screen.querySelector('h2');
  heading?.setAttribute('tabindex','-1');heading?.focus({preventScroll:true});
 }
 async function openStage(intent){
  const round=generation,app=selection;
  if(!app)return;
  const current=()=>alive&&round===generation&&selection?.id===app.id;
  const lookup=version=>api('/api/games/'+encodeURIComponent(app.id)+'/open',undefined,undefined,{'X-Friday-Game-Intent':version});
  const before=gameIntent()===intent?report.service.game:null;
  let result;
  try{result=await lookup(intent);}
  catch(error){
   if(!current()||error.message!=='game_state_changed'||!before?.control_context||!Number.isSafeInteger(before.round_id))throw error;
   // 关闭最后一个舞台可能让原局暂停并更新版本。同一局的只读入口可重新读取一次。
   if(!await refresh(true)||!current()||!report.service.owned||!gameIntent())throw error;
   const after=report.service.game,fields=['control_context','round_id','mode','selected_mode','game_input_enabled',
    'difficulty','ski_trail','tennis_variant','quest_route'];
   if(!after||fields.some(field=>before[field]!==after[field]))throw error;
   result=await lookup(gameIntent());
  }
  if(!current())return;
  const url=new URL(result.url);
  if(url.hostname!=='127.0.0.1'||url.protocol!=='http:'||url.username||url.password)throw Error('invalid_stage');
  url.searchParams.set('embed','1');
  const frame=h('iframe',null,'game-stage'),view=h('div',null,'game-play-view');
  frame.src=url.href;frame.title=modeTitle(app)+'实时画面';frame.setAttribute('referrerpolicy','no-referrer');
  const heading=h('div',null,'game-stage-heading'),back=button('返回详情',closeStage),title=h('h2',modeTitle(app));
  title.id='game-stage-title';
  const arrow=h('span',null,'icon icon-arrow-right');arrow.setAttribute('aria-hidden','true');back.prepend(arrow);
  const fullscreen=button('全屏',async()=>{
   try{
    if(document.fullscreenElement===view)await document.exitFullscreen();
    else await view.requestFullscreen();
   }catch{if(document.fullscreenElement!==view)fullscreen.textContent='重试全屏';}
  });
  fullscreen.hidden=!document.fullscreenEnabled||typeof view.requestFullscreen!=='function';
  view.onfullscreenchange=()=>{fullscreen.textContent=document.fullscreenElement===view?'退出全屏':'全屏';};
  heading.append(back,title,fullscreen);view.append(heading,frame);
  ui.stage.replaceChildren(view);
  ui.playView=view;ui.playTitle=title;ui.frame=frame;ui.stageEnded=false;
  ui.stage.showModal();
  back.focus({preventScroll:true});
 }
 function showStoppedStage(){
  const panel=h('section',null,'game-stage-ended'),title=h('h2','游戏已停止'),back=button('返回游戏详情',closeStage,'primary');
  title.id='game-stage-title';panel.append(title,back);
  ui.playView.replaceChildren(panel);ui.playTitle=ui.frame=null;ui.stageEnded=true;
  back.focus({preventScroll:true});
 }
 async function stopGame(intent){
  const id=ui.running?.dataset.game,completed=report.service.game?.status==='complete';
  const result=await api('/api/embodied/game/stop',{expected_intent:intent});
  if(result.mode!=='off')throw Error('invalid_game_response');
  if(!alive)return;
  if(ui.stage){
   closeStage();
   ui.notice.textContent=completed?'已退出游戏':'本局已结束';
  }else if(id&&(!document.activeElement||document.activeElement===document.body||ui.running.contains(document.activeElement))){
   focusStoreGame(id);
  }
 }
 async function perform(fn,notice=ui.notice){
  if(busy||!alive)return;
  const intent=gameIntent();
  if(!controlsCurrent()||!intent){stateFresh=false;update();await refresh(true);return;}
  busy=true;stateFresh=false;lastError='';
  if(ui.notice)ui.notice.textContent='';
  if(ui.roundNotice)ui.roundNotice.textContent='';
  retireRead();update();
  try{
   await fn(intent);
   if(alive)await refresh(true);
  }catch(error){
   if(alive){
    const changed=['game_state_changed','embodied_not_running'].includes(error.message);
    lastError=changed?'游戏状态已变化，请确认后再操作。':
      error.message==='camera_missing'?'还没有接入摄像头，请先完成设置。':'暂时无法完成，请重试。';
    if(changed)await refresh(true);
    const target=notice&&notice===ui.roundNotice&&ui.round.element.hidden?ui.notice:notice;
    if(target)target.textContent=lastError;
   }
  }finally{busy=false;if(alive)update();}
 }
 function updateSkiTrails(same,completed){
  if(!ui.trailPicker)return;
  const trail=chosenSkiTrail(selection);
  for(const control of ui.trailPicker.querySelectorAll('button')){
   control.setAttribute('aria-pressed',String(control.dataset.skiTrail===trail?.id));
   control.disabled=busy;
  }
  const live=same&&skiTrails(selection).find(item=>item.id===report.service.game?.ski_trail);
  if(completed&&trail){ui.start.textContent=(live?.id===trail.id?'重滑':'滑')+trail.name;return;}
  if(!live)return;
  ui.resume.textContent+=' · '+live.name;
  if(trail?.id!==live.id)ui.start.textContent='换道并开始';
 }
 function updateTennisVariants(same,completed){
  if(!ui.tennisPicker)return;
  const current=currentTennisVariant();
  const choice=chosenTennisVariant();
  const label=tennisVariants.find(([id])=>id===choice)[1];
  for(const input of ui.tennisPicker.querySelectorAll('input')){
   input.checked=input.value===choice;
   input.disabled=busy;
  }
  ui.tennisTip.textContent={
   practice:'跟着落点移动，在击球圈内挥拍。',
   solo:'和球场对手轮流发球，先得 7 分并领先 2 分获胜。',
   match:'两人站稳后，由发球者挥拍开球。'
  }[choice];
  if(!same||!current){ui.start.textContent='进入'+label;return;}
  if(completed){
   ui.start.textContent='再开'+label;
   ui.start.className='primary';
   ui.resume.textContent='查看本局成绩';
   ui.resume.className='';
   return;
  }
  const currentLabel=tennisVariants.find(([id])=>id===current)[1];
  ui.resume.textContent=(preparingGame()?'进入':'继续')+currentLabel;
  ui.start.textContent=choice===current?'重新开始':'换玩法并开局';
 }
 function updateQuestRoutes(choice,same,game){
  if(!ui.questPicker||!choice)return;
  for(const input of ui.questPicker.querySelectorAll('input')){
   input.checked=input.value===choice.id;
   input.disabled=busy;
  }
  const current=same&&currentQuestRoute(selection);
  if(!current){ui.start.textContent='进入'+choice.name;return;}
  const changed=current.id!==choice.id;
  if(game.status==='complete'){
   ui.resume.textContent='查看本局成绩';
   ui.resume.className='';
   ui.start.textContent=changed?'进入'+choice.name:'重玩'+current.name;
   ui.start.className='primary';
   return;
  }
  const preparing=preparingGame();
  ui.resume.textContent=(preparing?'进入':'继续')+current.name;
  ui.start.textContent=changed?'更换路线并开始':'重新开始';
  ui.start.hidden=preparing&&!changed&&(!ui.difficulty||ui.difficulty.value===game.difficulty);
 }
 function update(){
  serviceStatus();
  if(!report)return;
  updateRunning();
  if(!ui.start)return;
  const s=report.service,live=s.owned?currentGame():null,same=live?.id===selection?.id;
  const completed=same&&s.game?.status==='complete';
  const questRoute=chosenQuestRoute(selection);
  const configured=Boolean(s.configured&&cameras.length),disabled=busy||!controlsCurrent()||!gameIntent();
  ui.start.disabled=disabled||!configured||s.capture_enabled===false;
  ui.resume.hidden=!same;
  ui.resume.disabled=disabled;
  ui.resume.textContent=completed?'查看本局成绩':preparingGame()?'进入游戏':'继续游戏';
  if(ui.difficulty){
   ui.difficulty.disabled=disabled;
   ui.difficulty.value=chosenDifficulty(selection)?.id||'standard';
   ui.difficultyNote.textContent=chosenDifficulty(selection)?.description?.[selection.id]||'';
   ui.difficultyNote.hidden=Boolean(questRoute&&!questRoute.teaching.some(key=>key.startsWith('pose.')));
   const current=gameDifficulties(selection).find(d=>d.id===s.game?.difficulty);
   if(same&&!completed&&current&&!ui.trailPicker&&!ui.tennisPicker&&!ui.questPicker)ui.resume.textContent+=' · '+current.label;
  }
  ui.start.textContent=completed?(selection.id==='hiit'?'再练一轮':selection.id==='ski'?'再滑一局':'再玩一局'):same?'重新开始':'开始游戏';
  ui.start.className=same&&!completed?'':'primary';
  ui.start.hidden=false;
  ui.resume.className=completed?'':'primary';
  updateSkiTrails(same,completed);
  updateTennisVariants(same,completed);
  updateQuestRoutes(questRoute,same,s.game);
  updateTeaching(selection,questRoute);
  if(ui.frame&&live){ui.playTitle.textContent=modeTitle(live);ui.frame.title=modeTitle(live)+'实时画面';}
  ui.round.element.hidden=!live;
  if(live){
   const variant=roundVariant(live),status=roundStatus();
   ui.round.title.textContent=same?(completed&&variant?variant:'本局'):modeTitle(live);
   const state=!same&&variant?variant+' · '+status:status;
   if(ui.round.state.textContent!==state)ui.round.state.textContent=state;
  }
  ui.stop.hidden=!live;
  ui.stop.textContent=s.game?.status==='complete'?'退出游戏':'结束游戏';
  ui.stop.disabled=disabled;
  const camera=cameras.find(c=>c.id===s.camera);
  ui.readiness.textContent=!configured?'还没有接入可用于体感识别的摄像头':s.capture_enabled===false
   ?'摄像头已暂停':camera?camera.room+' · '+camera.name:'开始后使用已配置的游戏镜头';
  if(!configured&&!ui.recovery){ui.recovery=button('设置摄像头',()=>go('Settings'));ui.readiness.after(ui.recovery);}
  const tvReady=Boolean(controlsCurrent()&&s.owned&&s.game?.selected_mode===selection.mode&&
   s.game.presentation?.television_ready===true);
  const tvMessage=tvReady?'电视游戏画面已就绪':'在电视的 Friday 打开「家庭游戏」即可同屏游玩';
  if(ui.tv.textContent!==tvMessage)ui.tv.textContent=tvMessage;
  if(ui.stage?.open&&!ui.stageEnded&&!busy&&controlsCurrent()&&!s.owned)showStoppedStage();
 }
 async function refresh(replace=false){
  if(!alive)return false;update();if(readOwner){if(!replace)return readOwner.promise;retireRead();}
  const owner={epoch:++readEpoch,controller:new AbortController(),promise:null};readOwner=owner;readNotice='';
  const current=()=>alive&&readOwner===owner&&owner.epoch===readEpoch;
  owner.promise=(async()=>{try{
   const[next,inventory]=await Promise.all([api('/api/games',undefined,owner.controller.signal),api('/api/embodied/cameras',undefined,owner.controller.signal)]);
   owner.finished=true;
   if(!current())return false;const first=!catalogLoaded;report=next;catalogLoaded=true;cameras=inventory.cameras||[];stateFresh=true;readAt=Date.now();readNotice='';if(first&&selection?.id!=='ktv')draw();else update();return true;
  }catch{if(!current())return false;stateFresh=false;readNotice='暂时无法连接游戏，正在重试';if(!report){report={apps:[],service:{configured:false,owned:false}};draw();}update();return false;
  }finally{if(!owner.finished)owner.controller.abort();if(readOwner===owner)readOwner=null;}})();return owner.promise;
 }
 void refresh();const timer=setInterval(()=>{if(!busy)void refresh();},3000);return{dispose(){alive=false;generation++;retireRead();abort.abort();clearInterval(timer);clean();wrap.remove();}};
}
