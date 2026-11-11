import {motionTile} from './motion-guide.js';
import {mountPractice} from './recognition-practice.js';
import {mountWerewolf} from './werewolf-host.js';
import {mountKtv} from './ktv-console/app.js';
const h=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const button=(text,fn,cls)=>{const b=h('button',text,cls);b.type='button';b.onclick=fn;return b;};
const art=id=>['ski','tennis','quest','hiit','werewolf','ktv'].includes(id)?'/static/games-art/'+id+'.png':null;
const moves={ski:['body.arm_effort','body.lateral'],tennis:['body.lateral','body.swing'],quest:['pose.horizontal','pose.overhead','exercise.squat','exercise.side_step','exercise.high_knee','exercise.jack','exercise.side_bend','exercise.side_lunge','exercise.reach_up','exercise.arm_open','exercise.balance','exercise.cross_knee','exercise.side_raise','exercise.overhead_clap'],hiit:['exercise.squat','exercise.jack','exercise.high_knee','exercise.side_lunge','exercise.side_step','exercise.side_bend','exercise.reach_up','exercise.arm_open','exercise.balance','exercise.cross_knee','exercise.side_raise','exercise.overhead_clap']};
const tennisVariants=[
 ['practice','单人练习','1 人 · 9 球'],
 ['solo','单人挑战','1 人 · 抢七'],
 ['match','双人对打','2 人 · 抢七']
];
const isCircuit=app=>['quest','hiit'].includes(app.id);
const displayName=app=>isCircuit(app)?'体感闯关':app.name;
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
 let alive=true,report=null,cameras=[],selection=null,filter='all',query='',busy=false,stageApp=null,frame=null,practice=null,party=null,ktv=null,diagrams=[],generation=0,ui={},lastError='',readOwner=null,readEpoch=0,readAt=null,stateFresh=false,readNotice='',catalogLoaded=false,pendingSkiTrail=null,pendingTennisVariant=null;
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
 function clean(){ktv?.dispose();ktv=null;practice?.dispose();practice=null;party?.dispose();party=null;diagrams.forEach(d=>d.dispose());diagrams=[];ui={};frame=null;stageApp=null;wrap.dataset.playing='false';}
 function cover(app,cls='game-cover'){const src=art(app.id);if(src){const image=h('img',null,cls);image.src=src;image.alt=app.name+'封面插画';image.width=1672;image.height=941;image.decoding='async';return image;}const fallback=h('div',null,'game-tool-cover'),icon=h('span',null,'icon icon-users');icon.setAttribute('aria-hidden','true');fallback.append(icon,h('span',app.name));return fallback;}
 function select(app){if(busy)return;selection=app;lastError='';generation++;draw();screen.scrollIntoView?.({block:'start',behavior:'instant'});const title=screen.querySelector('h2');title?.setAttribute('tabindex','-1');title?.focus?.({preventScroll:true});}
 function returnToStore(app){const id=isCircuit(app)&&report.apps.some(a=>a.id==='quest')?'quest':app.id;selection=null;generation++;draw();const target=[...screen.querySelectorAll('.game-cover-button')].find(n=>n.dataset.game===id)||screen.querySelector('.games-feature-image')||screen.querySelector('input');target?.focus();}
 function preparingGame(){const g=report?.service?.game;return g?.mode==='off'&&g.game_input_enabled===true;}
 function currentGame(){const g=report?.service?.game,mode=preparingGame()?g.selected_mode:g?.mode;return report?.apps.find(a=>a.mode===mode&&a.kind==='game');}
 function serviceStatus(){status.hidden=selection?.id==='ktv';if(!report)return;const s=report.service;status.textContent=lastError&&!ui.notice?lastError:!controlsCurrent()?(readNotice||'正在更新游戏状态'):!s.configured?'接入摄像头后，即可用身体动作玩游戏':s.capture_enabled===false?'摄像头已暂停':!gameIntent()?'游戏状态暂时不可用':s.owned?'体感服务已启动':'选择游戏后启动体感识别';status.dataset.state=lastError&&!ui.notice?'changed':controlsCurrent()&&gameIntent()&&s.owned?'ready':'waiting';}
 function updateRunning(){
  if(!ui.running)return;const live=report.service.owned?currentGame():null,id=live?.id||'';
  if(ui.running.dataset.game!==id){ui.running.dataset.game=id;ui.running.replaceChildren();ui.runningButtons=[];ui.runningLabel=null;ui.runningResume=null;if(live){const banner=h('div',null,'game-running-banner'),label=h('strong'),resume=button('继续游戏',()=>{select(live);void perform(openStage);}),stop=button('结束游戏',()=>perform(stopGame));banner.append(label,resume,stop);ui.running.append(banner);ui.runningButtons=[resume,stop];ui.runningLabel=label;ui.runningResume=resume;}}
  if(ui.runningLabel)ui.runningLabel.textContent=displayName(live)+(!controlsCurrent()?' · 正在更新':preparingGame()?' · 准备中':' · 进行中');
  if(ui.runningResume)ui.runningResume.textContent=preparingGame()?'进入游戏':'继续游戏';
  for(const control of ui.runningButtons||[])control.disabled=busy||!controlsCurrent()||!gameIntent();
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
  const top=h('div',null,'game-detail-layout'),visual=h('div',null,'game-detail-art');top.dataset.game=app.id;visual.append(cover(app));const info=h('section',null,'game-detail-info');info.append(h('h2',displayName(app)));if(app.id!=='tennis')info.append(h('p',players(app)+' · '+category(app),'game-meta'),h('p',app.description,'game-description'));if(isCircuit(app)){const modes=h('div',null,'game-mode-picker');modes.setAttribute('aria-label','闯关玩法');for(const[id,label]of [['quest','山径冒险'],['hiit','运动训练']]){const next=report.apps.find(a=>a.id===id);if(!next)continue;const b=button(label,()=>select(next));b.setAttribute('aria-pressed',String(app.id===id));modes.append(b);}info.append(modes);}
  if(app.status==='prototype')info.append(h('p','开发预览 · 座位追踪与指认工具','game-preview-label'));
  const trailPicker=skiTrailPicker(app),tennisPicker=app.id==='tennis'?tennisVariantPicker():null;
  if(trailPicker)info.append(trailPicker);
  if(tennisPicker)info.append(tennisPicker);
  let difficulty=null,difficultyNote=null;
  const options=gameDifficulties(app);
  if(options.length){const field=h('div',null,'game-difficulty'),label=h('label','难度');label.setAttribute('for','game-difficulty');difficulty=h('select');difficulty.id='game-difficulty';difficulty.setAttribute('aria-describedby','game-difficulty-note');for(const option of options){const item=h('option',option.label);item.value=option.id;difficulty.append(item);}difficulty.value=chosenDifficulty(app)?.id||'standard';difficultyNote=h('p',null,'game-difficulty-note');difficultyNote.id='game-difficulty-note';difficulty.onchange=()=>{if(busy){difficulty.value=chosenDifficulty(app)?.id||'standard';return;}if(options.some(d=>d.id===difficulty.value))difficultySelections.set(app.id,difficulty.value);update();};field.append(label,difficulty,difficultyNote);info.append(field);}
  const readiness=h('p',null,'game-readiness');info.append(readiness);const notice=h('p',lastError,'game-notice');notice.setAttribute('role','status');const actions=h('div',null,'game-actions'),start=button(app.kind==='seating'?'打开聚会工具':'开始游戏',()=>perform(intent=>startGame(app,intent)),'primary'),resume=button('继续游戏',()=>perform(openStage)),stop=button('结束游戏',()=>perform(stopGame));actions.append(start,resume,stop);info.append(actions,notice);top.append(visual,info);screen.append(top);
  const stage=h('div',null,'game-stage-slot');stage.hidden=true;screen.append(stage);const teaching=h('section',null,'game-how-to');teaching.append(h('h3','怎么玩'));const steps=h('div',null,'game-play-steps'),tennisTip=tennisPicker?h('p'):null;
  steps.append(h('p','留出活动空间，让全身入镜'));
  if(tennisTip)steps.append(tennisTip);
  else steps.append(h('p','比出 OK 选择玩家，放下手后开始'),h('p',app.players?.[0]===2?'两位玩家都入镜后一起参与':'跟着画面，用动作参与'));
  teaching.append(steps);
  const tutorial=h('div',null,'motion-mini-list'),practiceSlot=h('div');for(const key of moves[app.id]||['hand.point','hand.ok']){const tile=motionTile(key,gesture=>{practice?.dispose();practice=mountPractice(practiceSlot,{gesture,go,cameras,onClose:()=>{practice=null;practiceSlot.replaceChildren();}});practiceSlot.scrollIntoView?.({block:'nearest',behavior:'instant'});});diagrams.push(tile);tutorial.append(tile);}teaching.append(tutorial,practiceSlot);screen.append(teaching);
  if(app.kind==='seating')teaching.append(h('p','此工具提供座位追踪和指认，不含狼人杀角色或裁判规则。','game-meta'));
  const tv=h('p',null,'game-tv-status');screen.append(tv);ui={start,resume,stop,readiness,notice,stage,tv,difficulty,difficultyNote,trailPicker,tennisPicker,tennisTip};update();
 }
 async function startGame(app,intent){
  practice?.dispose();practice=null;
  const id=cameras.find(c=>c.id===report.service.camera)?.id||cameras[0]?.id;
  if(!id)throw Error('camera_missing');
  const options={},difficulty=chosenDifficulty(app),trail=chosenSkiTrail(app);
  if(difficulty)options.difficulty=difficulty.id;
  if(trail)options.ski_trail=trail.id;
  if(app.id==='tennis')options.tennis_variant=chosenTennisVariant();
  const result=await api('/api/embodied/start',{camera:id,app:app.id,expected_intent:intent,...options});
  const confirmed=gameIntent(result);
  if(!confirmed||!result.owned||result.game?.selected_mode!==app.mode||
     Object.entries(options).some(([key,value])=>result.game?.[key]!==value))throw Error('game_state_changed');
  if(alive){
   await openStage(confirmed);
   difficultySelections.delete(app.id);
   if(trail)pendingSkiTrail=null;
   if(options.tennis_variant)pendingTennisVariant=null;
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
 function closeStage(){generation++;wrap.dataset.playing='false';ui.stageEnded=false;ui.stage?.replaceChildren();if(ui.stage)ui.stage.hidden=true;frame=null;stageApp=null;ui.playTitle=null;if(!busy){stateFresh=false;retireRead();update();void refresh(true);}const heading=screen.querySelector('h2');heading?.setAttribute('tabindex','-1');heading?.focus?.({preventScroll:true});}
 async function openStage(intent){const round=generation,app=selection;if(!app)return;const result=await api('/api/games/'+encodeURIComponent(app.id)+'/open',undefined,undefined,{'X-Friday-Game-Intent':intent});if(!alive||round!==generation||selection?.id!==app.id)return;const url=new URL(result.url);if(url.hostname!=='127.0.0.1'||url.protocol!=='http:'||url.username||url.password)throw Error('invalid_stage');url.searchParams.set('embed','1');frame=h('iframe',null,'game-stage');frame.src=url.href;frame.title=app.name+'实时画面';frame.setAttribute('allow','fullscreen');frame.setAttribute('referrerpolicy','no-referrer');const heading=h('div',null,'game-stage-heading'),back=button('返回详情',closeStage),title=h('h2',displayName(app));heading.append(back,title);ui.playTitle=title;ui.stageEnded=false;ui.stage.hidden=false;ui.stage.replaceChildren(heading,frame);stageApp=app.id;wrap.dataset.playing='true';ui.stage.scrollIntoView?.({block:'start',behavior:'instant'});back.focus?.({preventScroll:true});}
 function showStoppedStage(){
  const restoreFocus=ui.stage.contains(document.activeElement),panel=h('section',null,'game-stage-ended'),title=h('h2','游戏已停止'),back=button('返回游戏详情',closeStage,'primary');
  title.id='game-stage-ended-title';panel.setAttribute('aria-labelledby',title.id);panel.append(title,back);
  ui.stage.replaceChildren(panel);ui.playTitle=null;ui.stageEnded=true;frame=null;
  if(restoreFocus)back.focus?.({preventScroll:true});
 }
 async function stopGame(intent){const result=await api('/api/embodied/game/stop',{expected_intent:intent});if(result.mode!=='off')throw Error('invalid_game_response');if(alive){closeStage();if(ui.notice)ui.notice.textContent='本局已结束';}}
 async function perform(fn){if(busy||!alive)return;const intent=gameIntent();if(!controlsCurrent()||!intent){stateFresh=false;update();await refresh(true);return;}busy=true;stateFresh=false;lastError='';if(ui.notice)ui.notice.textContent='';retireRead();update();try{await fn(intent);if(alive)await refresh(true);}catch(e){if(alive){const changed=e.message==='game_state_changed';lastError=changed?'游戏状态已变化，请确认后再操作。':e.message==='camera_missing'?'还没有接入摄像头，请先完成设置。':'暂时无法完成，请重试。';if(changed)await refresh(true);if(ui.notice)ui.notice.textContent=lastError;}}finally{busy=false;if(alive)update();}}
 function updateSkiTrails(same){
  if(!ui.trailPicker)return;
  const trail=chosenSkiTrail(selection);
  for(const control of ui.trailPicker.querySelectorAll('button')){
   control.setAttribute('aria-pressed',String(control.dataset.skiTrail===trail?.id));
   control.disabled=busy;
  }
  const live=same&&skiTrails(selection).find(item=>item.id===report.service.game?.ski_trail);
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
 function update(){
  serviceStatus();
  if(!report)return;
  updateRunning();
  if(!ui.start)return;
  const s=report.service,live=currentGame(),same=s.owned&&live?.id===selection?.id;
  const configured=Boolean(s.configured&&cameras.length),disabled=busy||!controlsCurrent()||!gameIntent();
  ui.start.disabled=disabled||!configured||s.capture_enabled===false;
  ui.resume.hidden=!same;
  ui.resume.disabled=disabled;
  ui.resume.textContent=preparingGame()?'进入游戏':'继续游戏';
  if(ui.difficulty){
   ui.difficulty.disabled=disabled;
   ui.difficulty.value=chosenDifficulty(selection)?.id||'standard';
   ui.difficultyNote.textContent=chosenDifficulty(selection)?.description?.[selection.id]||'';
   const current=gameDifficulties(selection).find(d=>d.id===s.game?.difficulty);
   if(same&&current&&!ui.trailPicker&&!ui.tennisPicker)ui.resume.textContent+=' · '+current.label;
  }
  ui.start.textContent=same?'重新开始':'开始游戏';
  ui.start.className=same?'':'primary';
  ui.resume.className='primary';
  updateSkiTrails(same);
  updateTennisVariants(same,s.game?.status==='complete');
  if(frame&&ui.playTitle&&live){ui.playTitle.textContent=displayName(live);frame.title=live.name+'实时画面';}
  ui.stop.hidden=!s.owned||!live;
  ui.stop.disabled=disabled;
  const camera=cameras.find(c=>c.id===s.camera);
  ui.readiness.textContent=!configured?'还没有接入可用于体感识别的摄像头':s.capture_enabled===false
   ?'摄像头已暂停':camera?camera.room+' · '+camera.name:'开始后使用已配置的游戏镜头';
  if(!configured&&!ui.recovery){ui.recovery=button('设置摄像头',()=>go('Settings'));ui.readiness.after(ui.recovery);}
  const tv=s.tv_viewer,fresh=tv&&Date.now()/1000-tv.reported_at<12;
  ui.tv.textContent=fresh?'电视已连接，正在同步游戏画面':'大屏同玩：在电视上打开 Friday';
  if(stageApp&&!ui.stageEnded&&!busy&&controlsCurrent()&&!s.owned)showStoppedStage();
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
