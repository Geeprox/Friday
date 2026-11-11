/* Original paper-and-clay scene plates/sprites. Prompts and hashes are adjacent.
 * One caller-owned 2D canvas/clock; no input authority, inference or device I/O.
 */
function questWorld(g){
 // Compatibility with already-running routes that predate the additive art fields.
 // Actions/counts still come from that route; this table never adds a station.
 const oldWorld={horizontal:['forest','gate'],overhead:['forest','canopy'],squat:['forest','branches'],
  side_step:['stream','stones'],high_knee:['stream','steps'],jack:['stream','flowers'],
  side_bend:['camp','boughs'],lunge:['camp','trail'],reach_up:['camp','flags'],arm_open:['camp','breeze']};
 const q=g.quest||{},legacy=[
  {action:'horizontal',hold:3,scene:'forest',landmark:'gate'},
  {action:'overhead',hold:3,scene:'forest',landmark:'canopy'},
  {action:'squat',reps:3,scene:'forest',landmark:'branches'}
 ];
 const stages=Array.isArray(q.stages)&&q.stages.length?q.stages:legacy;
 const level=Math.max(0,Math.min(stages.length,Number.isSafeInteger(q.level)?q.level:0));
 const stage=stages[Math.min(level,stages.length-1)],scenes=['forest','stream','camp'];
 const landmarks=['gate','canopy','branches','stones','steps','flowers','crossing','boughs','trail','flags','breeze','landing','camp_flowers','lanterns'];
 const requestedScene=stage?.scene??oldWorld[stage?.action]?.[0];
 const scene=scenes.includes(requestedScene)?requestedScene:'forest',values={};
 for(const[s,part]of stages.entries()){
  const name=part.landmark??oldWorld[part.action]?.[1];
  if(!landmarks.includes(name))continue;
  const reps=Number.isSafeInteger(part.reps)&&part.reps>0?part.reps:0;
  const required=Math.min(32,reps||Math.max(0,Number(part.hold)||0));
  const observed=reps?Math.floor(Number(q.reps)||0):Number(q.hold)||0;
  const count=s<level?required:s===level&&q.phase!=='transition'?Math.max(0,Math.min(required,observed)):0;
  values[name]={count,required,ratio:required?count/required:0,complete:s<level,visible:s<=level};
 }
 return{scene,level,done:level===stages.length,values};
}
function questPlacements(places,count){
 count=Math.max(0,Math.min(32,Math.floor(count)));if(count===places.length)return places;
 const groups=[places.filter(p=>p[0]<800),places.filter(p=>p[0]>=800)],both=groups.every(p=>p.length);
 const expand=(points,n)=>{if(!points.length||!n)return[];const scale=Math.min(1,Math.sqrt(Math.max(1,points.length-1)/Math.max(1,n-1)));return Array.from({length:n},(_,i)=>{const t=n===1?0:i*(points.length-1)/(n-1),a=Math.floor(t),b=Math.min(points.length-1,a+1),f=t-a;return points[a].map((value,k)=>(value+(points[b][k]-value)*f)*(k===2?scale:1));});};
 // Interpolate within each authored side column. Never run a reward row
 // across the source demonstration in the middle of the world.
 const left=expand(groups[0],both?Math.ceil(count/2):groups[0].length?count:0),right=expand(groups[1],both?Math.floor(count/2):groups[1].length?count:0),result=[];
 for(let i=0;i<Math.max(left.length,right.length);i++){if(left[i])result.push(left[i]);if(right[i])result.push(right[i]);}
 return result;
}
function createGameScenery(c,{base,onChange=()=>{}}){
 const images=new Map(),errors=new Set(),positions=[.5,.5];
 const tennisArt=['tennis-near-v1.png','tennis-far-v1.png'];
 const tennisCounts=[null,null],tennisPending=[null,null],tennisHitAt=[-10000,-10000],tennisPoses=[0,0];
 let opponentSequence=0;
 let tennisClock=0,tennisAt=null,tennisRunning=false,tennisPacketAt=null,tennisPacketPlaying=false,tennisPhase=null;
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 let lastAt=0,celebrateAt=-10000,lastLevel=0,portrait=false,lastRound=null,lastContext=null,lastTennisEvent=null,ballFrames=null,motionAt=null,shownBall=null;
 let questClock=0,questAt=null,questRunning=false,questShown=null,questPrevious=null,chapterAt=0;
 const questReveals=new Map();
 function resetQuest(){questClock=0;questAt=null;questRunning=false;questShown=questPrevious=null;questReveals.clear();}
 function resetTennis(){
  tennisClock=0;tennisAt=tennisPacketAt=null;tennisRunning=tennisPacketPlaying=false;tennisPhase=null;
  opponentSequence=0;
  tennisCounts.fill(null);tennisPending.fill(null);tennisHitAt.fill(-10000);tennisPoses.fill(0);
 }
 function receiveTennis(g,at){
  const running=g.mode==='tennis'&&g.status==='playing';
  const continuous=running&&tennisPacketPlaying&&tennisPacketAt!==null&&at>tennisPacketAt&&at-tennisPacketAt<=250;
  const served=tennisPhase==='serve'&&g.tennis?.phase==='rally';
  const feed=g.tennis?.event;
  if(continuous&&g.tennis?.practice&&['ready','point'].includes(tennisPhase)&&g.tennis.phase==='rally'&&
     feed?.kind==='serve'&&feed.player===1&&feed.age>=0&&feed.age<=.25)tennisPending[1]=at;
  if(g.tennis?.opponent&&Number.isSafeInteger(feed?.sequence)&&feed.sequence>opponentSequence){
   if(continuous&&feed.sequence===opponentSequence+1&&feed.player===1&&
      ['serve','hit'].includes(feed.kind)&&feed.age>=0&&feed.age<=.25)tennisPending[1]=at;
   opponentSequence=feed.sequence;
  }
  for(let i=0;i<2;i++){
   const player=g.players?.[i],count=player?.swings;
   if(!Number.isSafeInteger(count)||count<0)continue;
   const currentSwing=served?g.tennis.event?.kind==='serve'&&g.tennis.event.player===i:
    g.tennis?.phase==='rally'||g.tennis?.phase==='point'&&tennisPhase==='rally';
   if(continuous&&player.observation==='fresh'&&tennisCounts[i]!==null&&count===tennisCounts[i]+1
     &&currentSwing)tennisPending[i]=at;
   if(tennisCounts[i]!==null&&count<tennisCounts[i]){tennisPending[i]=null;tennisHitAt[i]=-10000;}
   tennisCounts[i]=count;
  }
  if(!running){tennisPending.fill(null);tennisRunning=false;}
  tennisPacketAt=at;tennisPacketPlaying=running;tennisPhase=g.tennis?.phase;
 }
 function receive(g,at){
  if(!g)return;
  if(g.round_id!==lastRound||g.control_context!==lastContext){
   lastRound=g.round_id;lastContext=g.control_context;lastTennisEvent=null;celebrateAt=-10000;lastLevel=0;
   positions.fill(.5);ballFrames=null;shownBall=null;motionAt=null;lastAt=at;
   resetQuest();resetTennis();
  }
  receiveTennis(g,at);
  const old=ballFrames;
  const continuous=g.mode==='tennis'&&g.status==='playing'&&g.tennis?.phase==='rally'
   &&old?.game.status==='playing'&&old.game.tennis?.phase==='rally'
   &&old.game.round_id===g.round_id&&old.game.tennis.flight===g.tennis.flight;
  ballFrames={game:g,at,from:continuous?old.game.tennis.ball:null,duration:old?Math.max(16,Math.min(120,at-old.at)):100};
 }
 function ballFrame(g,at){
  const ball=g.tennis?.ball||{x:.5,z:.5};
  if(reduced.matches||g.status!=='playing'||g.tennis?.phase!=='rally'||ballFrames?.game!==g||!ballFrames.from)return ball;
  const t=Math.max(0,Math.min(1,(at-ballFrames.at)/ballFrames.duration)),from=ballFrames.from;
  return {...ball,x:from.x+(ball.x-from.x)*t,z:from.z+(ball.z-from.z)*t};
 }
 function image(name){
  if(!images.has(name)){const item=new Image();images.set(name,item);item.onload=()=>{errors.delete(name);onChange();};item.onerror=()=>{errors.add(name);onChange();};item.src=new URL(name,base).href;}
  return images.get(name);
 }
 const complete=im=>im?.complete&&im.naturalWidth>0;
 const assets=mode=>mode==='quest'?['summer-scene.png','quest-stream-scene.png','quest-camp-scene.png','quest-props.png','quest-lantern.webp']:
  mode==='ski'?['winter-scene.png','winter-props.png']:
  mode==='tennis'?['summer-scene.png','summer-props.png',...tennisArt]:['summer-scene.png','summer-props.png'];
 function prepare(mode){for(const name of assets(mode))image(name);}
 function ready(mode){return assets(mode).every(name=>complete(images.get(name)));}
 function failed(mode){return assets(mode).some(name=>errors.has(name));}
 function reload(mode){for(const name of assets(mode)){errors.delete(name);const im=image(name);im.src=new URL(name+'?retry='+Date.now(),base).href;}}
 function background(name='summer-scene.png'){const im=image(name);if(complete(im))c.drawImage(im,0,0,1600,900);else{c.fillStyle='#f1e6d4';c.fillRect(0,0,1600,900);}}
 function sprite(index,x,y,size,{angle=0,mirror=false}={}){
  const im=image('summer-props.png');if(!complete(im))return;
  c.save();c.translate(x,y);c.rotate(angle);if(mirror)c.scale(-1,1);
  const w=im.naturalWidth/4,h=im.naturalHeight/2;
  c.drawImage(im,(index%4)*w,Math.floor(index/4)*h,w,h,-size/2,-size,size,size);c.restore();
 }
 function line(a,b,color,width=3){c.beginPath();c.moveTo(...a);c.lineTo(...b);c.strokeStyle=color;c.lineWidth=width;c.lineCap='round';c.stroke();}
 function paperCelebration(at){
  if(reduced.matches||at-celebrateAt>1000)return;
  const t=(at-celebrateAt)/1000;
  c.save();c.globalAlpha=1-t;
  for(let i=0;i<16;i++){const x=800+Math.sin(i*2.7)*440*t,y=430+Math.cos(i*1.7)*160*t+180*t*t;c.save();c.translate(x,y);c.rotate(i+t*3);c.fillStyle=['#efb24f','#f1ddd2','#507b65','#c66e4b'][i%4];c.fillRect(-4,-7,8,14);c.restore();}
  c.restore();
 }
 // Measured alpha bounds +8px padding, in the original 1774x887 atlas.
 // Canvas cropping preserves the original PNG; no generated figure or input is used.
 const questRects=[[49,185,352,176],[472,192,390,168],[915,117,394,246],[1341,100,421,242],
  [53,511,340,321],[498,508,337,320],[964,470,265,372],[1373,500,350,333]];
 function questSprite(index,x,y,width,{alpha=1,angle=0,mirror=false,top=false}={}){
  const lantern=index==='lantern',im=image(lantern?'quest-lantern.webp':'quest-props.png');if(!complete(im))return;
  const[sx,sy,sw,sh]=lantern?[59,13,221,298]:questRects[index],height=width*sh/sw;
  const aw=lantern?320:1774,ah=lantern?320:887;
  c.save();c.globalAlpha*=alpha;c.translate(x,y);c.rotate(angle);if(mirror)c.scale(-1,1);
  c.drawImage(im,sx*im.naturalWidth/aw,sy*im.naturalHeight/ah,sw*im.naturalWidth/aw,sh*im.naturalHeight/ah,
   -width/2,top?0:-height,width,height);c.restore();
 }
 const ease=t=>1-Math.pow(1-Math.max(0,Math.min(1,t)),3);
 function settled(id){
  const start=questReveals.get(id);if(start===undefined)return 1;
  const p=reduced.matches?1:ease((questClock-start)/380);
  if(p===1)questReveals.delete(id);return p;
 }
 function questSlot(id,index,x,y,width,earned,{top=false,marker=true}={}){
  if(!earned){
   if(marker){c.save();c.beginPath();c.ellipse(x,y,width*.29,width*.065,0,0,Math.PI*2);c.strokeStyle='#765b3b88';c.lineWidth=2;c.setLineDash([5,7]);c.stroke();c.restore();}
   return;
  }
  const p=settled(id);questSprite(index,x,y+(reduced.matches?0:6*(1-p)),width,{top,alpha:.4+.6*p});
 }
 function questWorldPaint(world){
  background({forest:'summer-scene.png',stream:'quest-stream-scene.png',camp:'quest-camp-scene.png'}[world.scene]);
  const parts=world.values;
  function row(name,index,places,{clear=false,marker=true,top=false}={}){
   const part=parts[name];if(!part?.visible)return;
   for(const[i,[x,y,w]]of questPlacements(places,part.required).entries()){
    const id=name+':'+i,earned=i<part.count;
    if(clear){const p=earned?settled(id):0;questSprite(index,x,y,w,{alpha:1-p});if(earned)questSprite(5,x,y,w*.55,{alpha:.4+.6*p});}
    else questSlot(id,index,x,y,w,earned,{marker,top});
   }
  }
  if(world.scene==='forest'){
   const value=name=>reduced.matches?(parts[name]?.complete?1:0):(parts[name]?.ratio||0),open=value('gate'),lift=value('canopy');
   questSprite(3,560-open*80,460-lift*65,300-open*40,{mirror:true,angle:-open*.12});
   questSprite(3,1040+open*80,460-lift*65,300-open*40,{angle:open*.12});
   row('branches',2,[[490,815,250],[545,665,225],[475,535,195]],{clear:true});
  }else if(world.scene==='stream'){
   row('stones',0,[[470,810,170],[540,700,150],[470,600,130],[545,510,115]]);
   row('steps',1,[[1100,825,170],[1150,750,160],[1070,675,150],[1140,605,140],[1080,540,130],[1150,485,120]]);
   row('flowers',5,[[585,860,120],[1040,865,120],[565,460,100],[1040,455,100]]);
   row('crossing',0,[[615,793,110],[985,793,110],[620,724,96],[980,724,96]]);
  }else if(world.scene==='camp'){
   row('boughs',2,[[470,825,235],[550,700,210],[470,590,185],[550,500,160]],{clear:true});
   row('trail',0,[[1130,820,175],[1060,710,155],[1140,600,135],[1070,510,115]]);
   if(parts.flags?.visible){
    c.save();c.beginPath();c.moveTo(450,230);c.bezierCurveTo(630,295,970,295,1150,230);c.strokeStyle='#826747';c.lineWidth=3;c.stroke();c.restore();
    row('flags',4,[[470,237,80],[1130,237,80],[615,271,80],[985,271,80]],{top:true,marker:false});
   }
   row('breeze',6,[[460,850,85],[1140,850,85],[391,882,88],[1209,882,88]]);
   row('landing',1,[[610,802,110],[990,802,110],[625,736,96],[975,736,96]]);
   row('camp_flowers',5,[[455,730,98],[1210,742,94],[510,440,74],[1090,438,74]]);
   row('lanterns','lantern',[[615,650,92],[985,650,92],[555,510,70],[930,515,70]]);
   if(world.done)questSlot('picnic',7,1120,690,170,true,{marker:false});
  }
 }
 function adventure(g,at){
  const running=g.mode==='quest'&&['playing','complete'].includes(g.status),dt=questAt===null?0:at-questAt;
  // Hidden frames, paused/recovering input and a stale caller clock earn no time.
  if(running&&questRunning&&dt>=0&&dt<=250)questClock+=dt;
  questAt=at;questRunning=running;
  if(!questShown||running||g.mode!=='quest'){
   const next=questWorld(g),old=questShown;
   const unseenChange=dt>250&&old&&(next.scene!==old.scene||next.level!==old.level||
    Object.entries(next.values).some(([name,part])=>part.count!==old.values[name]?.count));
   if(!running||unseenChange){questReveals.clear();questPrevious=null;}
   else if(old){
    for(const[name,part]of Object.entries(next.values)){
     const before=old.values[name]?.count||0;
     for(let i=Math.floor(before);i<Math.floor(part.count);i++)questReveals.set(name+':'+i,questClock);
    }
    if(next.done&&!old.done)questReveals.set('picnic',questClock);
    if(next.scene!==old.scene){questPrevious={...next,scene:old.scene,done:false};chapterAt=questClock;}
   }
   questShown=next;
  }
  const p=reduced.matches?1:ease((questClock-chapterAt)/360);
  if(questPrevious&&p<1){questWorldPaint(questPrevious);c.save();c.globalAlpha*=p;questWorldPaint(questShown);c.restore();}
  else{questPrevious=null;questWorldPaint(questShown);}
 }
 function training(g,at){
  background();sprite(1,130,700,370);sprite(0,1480,750,470);sprite(2,250,840,220);
  sprite(7,800,860,650);
  const h=g.hiit||{phase:'work',level:0,reps:0,required_reps:3};
  if(h.level>lastLevel){celebrateAt=at;lastLevel=h.level;}else if(h.level<lastLevel)lastLevel=h.level;
  paperCelebration(at);
 }
 function tennisPose(i,g,at){
  if(g.status!=='playing')return tennisPoses[i];
  const pending=tennisPending[i];
  if(pending!==null){
   if(at>=pending&&at-pending<=250)tennisHitAt[i]=tennisClock;
   tennisPending[i]=null;
  }
  const age=tennisClock-tennisHitAt[i],guide=g.tennis?.guide;
  // These are illustrations of an observed swing, never a hit/score decision.
  if(age>=0&&age<(reduced.matches?120:420))tennisPoses[i]=reduced.matches?5:5+Math.floor(age/60);
  else if(!reduced.matches&&g.tennis?.serve?.ready&&g.tennis.serve.player===i)tennisPoses[i]=3;
  else if(!reduced.matches&&g.tennis?.phase==='rally'&&guide?.active&&guide.receiver===i&&guide.in_position
    &&g.players?.[i]?.observation==='fresh'&&Number.isFinite(guide.seconds)&&guide.seconds>=0&&guide.seconds<=.65)
   tennisPoses[i]=1+Math.min(3,Math.floor((.65-guide.seconds)/.65*4));
  else tennisPoses[i]=0;
  return tennisPoses[i];
 }
 function tennisActor(i,frame,x,y,size){
  const im=image(tennisArt[i]);
  if(!complete(im)){sprite(i===0?4:5,x,y,size);return;}
  const w=im.naturalWidth/4,h=im.naturalHeight/3,extent=size*1.08;
  c.save();c.fillStyle='#59391d24';c.beginPath();c.ellipse(x,y+3,size*.26,size*.047,0,0,Math.PI*2);c.fill();
  c.translate(x,y);
  // Register every cel to the same source-cell foot plane, not its changing arm bounds.
  c.drawImage(im,(frame%4)*w,Math.floor(frame/4)*h,w,h,-extent/2,-extent*(i===0?334:340)/362,extent,extent);
  c.restore();
 }
 function tennis(g,at){
  background();const t=g.tennis||{ball:{x:.5,z:.5},score:[0,0]},players=g.players||[];
  const suspended=!['playing','complete'].includes(g.status);
  const running=g.status==='playing',step=tennisAt===null?0:at-tennisAt;
  if(running&&tennisRunning){
   if(step>=0&&step<=250)tennisClock+=step;
   else{tennisPending.fill(null);tennisHitAt.fill(-10000);}
  }
  tennisAt=at;tennisRunning=running;motionAt=tennisClock;
  const project=(x,z)=>[800+(x-.5)*(portrait?740-z*360:1120-z*580),840-z*460];
  c.save();c.beginPath();[[0,0],[1,0],[1,1],[0,1]].forEach(([x,z],i)=>{const p=project(x,z);i?c.lineTo(...p):c.moveTo(...p);});c.closePath();c.fillStyle='#bc64432c';c.fill();c.strokeStyle='#fff6d9';c.lineWidth=5;c.stroke();
  for(const x of [.11,.89])line(project(x,0),project(x,1),'#fff6d9',3);
  for(const z of [.25,.75])line(project(.11,z),project(.89,z),'#fff6d9',3);
  line(project(.5,.25),project(.5,.75),'#fff6d9',3);c.restore();
  const guide=t.guide;
  let cue;
  if(running&&guide?.active&&Number.isFinite(guide.target_x)&&Number.isFinite(guide.reach)){
   const window=guide.window>0&&guide.window<.5?guide.window:.1,z=guide.receiver===0?window:1-window;
   const p=project(guide.target_x,z),left=project(guide.target_x-guide.reach,z),right=project(guide.target_x+guide.reach,z);
   const rx=Math.abs(right[0]-left[0])/2,ry=rx*.21,ink=guide.receiver===0?'#285e53':'#8b3c29';
   const swing=t.phase==='rally'&&guide.cue==='swing';
   c.save();c.beginPath();c.ellipse(p[0],p[1],rx,ry,0,0,Math.PI*2);c.fillStyle=swing?'#fff6d9bd':guide.in_position?'#fff6d94d':'#fff6d91f';c.fill();c.strokeStyle=ink;c.lineWidth=swing?7:4;c.stroke();
   const approach=1+Math.max(0,Math.min(1,guide.seconds/.8))*.42;
   if(!reduced.matches&&t.phase==='rally'&&!swing){c.beginPath();c.ellipse(p[0],p[1],rx*approach,ry*approach,0,0,Math.PI*2);c.strokeStyle='#fff6d9';c.lineWidth=2;c.stroke();}
   c.restore();
   const number=(guide.receiver+1)+' 号';
   const label=t.practice||t.opponent?(swing?'挥拍':'落点'):number+(swing?' · 挥拍':'落点');
   cue={label,p,rx,ry,ink,swing};
  }else if(running&&t.serve?.ready){
   const p=project(t.ball.x,t.ball.z),rx=portrait?50:44,ry=rx*.25,ink=t.serve.player===0?'#285e53':'#8b3c29';
   c.save();c.beginPath();c.ellipse(p[0],p[1],rx,ry,0,0,Math.PI*2);c.fillStyle='#fff6d9bd';c.fill();
   c.strokeStyle=ink;c.lineWidth=5;c.stroke();c.restore();
   const label=t.opponent?(t.serve.player===0?'挥拍发球':'对手发球'):(t.serve.player+1)+' 号 · 发球';
   cue={label,p,rx,ry,ink,swing:true,side:t.serve.player===0?-1:1};
  }
  const dt=Math.min(.1,Math.max(0,(at-lastAt)/1000));lastAt=at;
  function player(i){
   const p=players[i],far=i===1?(t.opponent?.x??t.practice?.feed_x):null;
   const feed=Number.isFinite(far)?far:null;
   const wanted=feed??(.5+Math.max(-1,Math.min(1,p?.lateral||0))*.39);
   if(g.status==='playing'&&(feed!==null||Number.isFinite(p?.lateral)))positions[i]+=(wanted-positions[i])*(reduced.matches?1:1-Math.exp(-dt/.075));
   const point=project(positions[i],i===0?.025:.98),size=portrait?(i===0?300:205):(i===0?245:160);
   tennisActor(i,tennisPose(i,g,at),point[0],point[1],size);
   c.save();c.textAlign='center';c.font=`600 ${portrait?34:20}px -apple-system, sans-serif`;
   const labelWidth=portrait?90:60;c.fillStyle='#faf9f2';c.fillRect(point[0]-labelWidth/2,point[1]+8,labelWidth,portrait?44:27);
   const label=t.opponent?(i?'对手':'你'):t.practice?(i?'陪练':'你'):(i+1)+' 号';
   c.fillStyle=i?'#8a4030':'#234f49';c.fillText(label,point[0],point[1]+(portrait?42:29));
   c.restore();
  }
  player(1);
  const netLeft=project(0,.5),netRight=project(1,.5),netHeight=55;
  c.save();c.strokeStyle='#395f55aa';c.lineWidth=1;
  for(let i=0;i<=32;i++){const x=netLeft[0]+(netRight[0]-netLeft[0])*i/32;line([x,netLeft[1]-netHeight],[x,netLeft[1]],'#395f55aa',1);}
  for(let y=0;y<=5;y++)line([netLeft[0],netLeft[1]-y*netHeight/5],[netRight[0],netRight[1]-y*netHeight/5],'#395f55aa',1);
  line([netLeft[0],netLeft[1]-netHeight],[netRight[0],netRight[1]-netHeight],'#fff6d9',6);
  for(const p of [netLeft,netRight])line([p[0],p[1]+5],[p[0],p[1]-netHeight-7],'#2f6053',9);c.restore();
  player(0);
  if(!['ready','point','complete'].includes(t.phase)){
   const ball=suspended&&t.phase==='rally'&&shownBall?shownBall:ballFrame(g,at),p=project(ball.x,ball.z),lift=Math.sin(Math.max(0,Math.min(1,ball.z))*Math.PI)*90+18;
   if(!suspended)shownBall={x:ball.x,z:ball.z};
   c.save();c.fillStyle='#59391d40';c.beginPath();c.ellipse(p[0],p[1],13,5,0,0,Math.PI*2);c.fill();c.restore();sprite(6,p[0],p[1]-lift+14,portrait?52:42);
  }
  if(!suspended&&t.event?.kind==='point'&&t.event.sequence!==lastTennisEvent){celebrateAt=motionAt;lastTennisEvent=t.event.sequence;}
  paperCelebration(motionAt);
  if(cue){
   const {label,p,rx,ry,ink,swing}=cue,font=portrait?36:24;
   c.save();c.textAlign='center';c.textBaseline='middle';c.font=`650 ${font}px -apple-system, sans-serif`;
   const halfWidth=c.measureText(label).width/2+font*.48,height=font*1.52;
   const side=cue.side??(p[0]>800?-1:1),margin=portrait?340:24,offset=rx+halfWidth+12;
   const minX=margin+halfWidth,maxX=1600-margin-halfWidth,preferred=p[0]+side*offset;
   const beside=cue.side&&(preferred<minX||preferred>maxX)?p[0]-side*offset:preferred;
   const x=Math.max(minX,Math.min(maxX,beside)),y=p[1]-ry;
   c.beginPath();c.roundRect(x-halfWidth,y-height/2,halfWidth*2,height,font*.32);
   c.fillStyle=swing?ink:'#faf9f2';c.fill();c.fillStyle=swing?'#fffaf2':ink;c.fillText(label,x,y+font*.05);c.restore();
  }
 }
 return {prepare,ready,failed,reload,receive,adventure,training,tennis,
  teachingAnchor(mode){return mode==='hiit'?{x:800,y:535}:{x:800,y:790};},
  setPortrait(value){portrait=value;}};
}
