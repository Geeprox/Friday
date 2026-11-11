/* Distant artwork stays still; every point on the snow plane shares the
 * simulation's three world units per distance unit. No independent motion timer,
 * camera roll, shake, input authority or extrapolation after a missing packet. */
function createSkiScenery(c,{scene,props,reducedMotion=()=>false}){
 const WORLD_TRAVEL=3,COURSE_WIDTH=1.9,HORIZON=335,FOCAL=350,EYE_HEIGHT=2.45;
 const BLEND_MS=120,UNSEEN_MS=1500;
 const mod=(n,size)=>(n%size+size)%size,clamp=(n,a,b)=>Math.max(a,Math.min(b,n));
 let current=null,rendered=null,snowPattern=null,textureSource='';
 const scope=g=>(g?.control_context||'')+':'+(g?.round_id??'');
 function pose(g,key){
  const input=g.players?.[0]?.lateral;
  const lateral=Number.isFinite(input)?clamp(input,-1,1):current?.key===key?current.target.lateral:0;
  return{distance:Number(g.ski?.distance)||0,lateral};
 }
 function interpolate({at:received,from,target},at){
  if(from===target||at-received>=BLEND_MS)return target;
  const mix=clamp((at-received)/BLEND_MS,0,1);
  return{distance:from.distance+(target.distance-from.distance)*mix,lateral:from.lateral+(target.lateral-from.lateral)*mix};
 }
 function receive(g,at){
  if(g?.mode!=='ski'){current=rendered=null;return;}
  const key=scope(g),target=pose(g,key),playing=g.status==='playing';
  if(current?.key===key&&at<current.at)return;
  const continuous=current?.key===key&&target.distance>=current.target.distance;
  if(!continuous||rendered?.key!==key)rendered=null;
  let from=target;
  if(continuous&&playing&&at-current.at<=UNSEEN_MS&&(!rendered||at-rendered.at<=UNSEEN_MS)){
   // 新包从这一刻的可见位置接续。短包间隔不缩短补间，暂停后沿用最后画面。
   from=current.playing&&!rendered?.paused?interpolate(current,at):rendered?.pose??current.target;
  }
  current={key,at,target,from,playing};
 }
 function frame(g,at){
  const key=scope(g),target=pose(g,key);
  if(g.mode!=='ski'||g.status==='complete')return target;
  if(g.status!=='playing'&&rendered?.key===key&&target.distance>=rendered.pose.distance)return rendered.pose;
  if(g.status==='playing'&&current?.key===key&&current.playing&&current.target.distance===target.distance)return interpolate(current,at);
  return target;
 }
 function project(x,z,lateral){const scale=FOCAL/(z+1.8);return[800+(x-lateral*COURSE_WIDTH)*scale,HORIZON+scale*EYE_HEIGHT,scale];}
 function texture(){
  if(snowPattern&&textureSource===scene.src)return snowPattern;
  // Sample only the unmarked snow of the original artwork. Mirrored UV edges
  // make a continuous texture; the raster itself is neither rewritten nor saved.
  const tile=document.createElement('canvas');tile.width=tile.height=512;
  const t=tile.getContext('2d'),sw=scene.naturalWidth,sh=scene.naturalHeight;
  for(let y=0;y<2;y++)for(let x=0;x<2;x++){t.save();t.translate(x?512:0,y?512:0);t.scale(x?-1:1,y?-1:1);t.drawImage(scene,sw*.40,sh*.72,sw*.20,sh*.24,0,0,256,256);t.restore();}
  snowPattern=c.createPattern(tile,'repeat');textureSource=scene.src;return snowPattern;
 }
 function snow(travel,lateral){
  const pattern=texture();
  if(pattern){c.save();c.fillStyle=pattern;
   for(let y=384;y<900;y+=4){const scale=(y-HORIZON)/EYE_HEIGHT,z=FOCAL/scale-1.8;
    const uvY=mod((travel+z)/18,1)*512,uvScale=scale*12/512;
    // 每条带的两端都回投到雪地。近处纹理随透视展开，条带边缘连续。
    const bottomZ=FOCAL*EYE_HEIGHT/(y+4-HORIZON)-1.8;
    const yScale=4/((bottomZ-z)*512/18);
    pattern.setTransform(new DOMMatrix([uvScale,0,0,yScale,800-lateral*COURSE_WIDTH*scale,y-uvY*yScale]));
    c.globalAlpha=clamp((y-384)/140,0,.92);c.fillRect(0,y,1600,4);
   }c.restore();
  }
 }
 function routeX(route,distance){
  const next=route.findIndex(point=>point[0]>=distance);
  if(next<=0)return route[next===0?0:route.length-1][1]*COURSE_WIDTH;
  const [from,fromX]=route[next-1],[to,toX]=route[next];
  const t=clamp((distance-from)/(to-from),0,1),mix=t*t*(3-2*t);
  return (fromX+(toX-fromX)*mix)*COURSE_WIDTH;
 }
 function band(points,offset,width,color){
  c.beginPath();
  for(let i=0;i<points.length;i++){
   const[x,y,scale,taper]=points[i],edge=x+(offset-width*taper/2)*scale;
   if(i===0)c.moveTo(edge,y);else c.lineTo(edge,y);
  }
  for(let i=points.length-1;i>=0;i--){
   const[x,y,scale,taper]=points[i];c.lineTo(x+(offset+width*taper/2)*scale,y);
  }
  c.closePath();c.fillStyle=color;c.fill();
 }
 function tracks(route,travel,lateral){
  const near=route?Math.max(-.25,route[0][0]*WORLD_TRAVEL-travel):-.25;
  const far=route?Math.min(30,route.at(-1)[0]*WORLD_TRAVEL-travel):30;
  if(far<=near)return;
  const depths=[];
  for(let z=far;z>near;z-=.42)depths.push(z);
  depths.push(near);
  // 门的位置也是采样点，滑痕、木门与穿门判定使用同一条路线。
  if(route){
   for(const[at]of route){const z=at*WORLD_TRAVEL-travel;if(z>near&&z<far)depths.push(z);}
   depths.sort((a,b)=>b-a);
  }
  const lanes=route?[0]:[-1.45,1.45];
  for(const lane of lanes){
   const points=depths.filter((z,i)=>i===0||depths[i-1]-z>1e-6).map(z=>{
    const world=travel+z;
    const x=route?routeX(route,world/WORLD_TRAVEL):lane+Math.sin(world*.22)*.42;
    return [...project(x,z,lateral),.82+.18*Math.cos(world*1.07)];
   });
   band(points,0,.34,'rgba(255,248,240,.24)');
   for(const offset of[-.14,.14]){
    band(points,offset,.050,'rgba(110,102,106,.24)');
    band(points,offset+.024,.016,'rgba(255,252,244,.72)');
   }
  }
 }
 function snowScuffs(route,travel,lateral){
  const spacing=1.7,start=Math.floor(travel/spacing);
  c.save();
  for(let i=start;i<start+16;i++){
   const world=i*spacing+.43*Math.sin(i*2.47),z=world-travel;
   if(z<-.25||z>=24)continue;
   const x=route&&i%3===0?routeX(route,world/WORLD_TRAVEL)+.64*Math.cos(i*1.9):3.6*Math.sin(i*2.39);
   const size=.095+.04*Math.cos(i*1.36);
   const [px,py,s]=project(x,z,lateral),length=size*s;
   c.globalAlpha=clamp((24-z)/9,0,1)*clamp((z+.25)/.65,0,1);
   c.beginPath();c.moveTo(px-length,py);
   c.quadraticCurveTo(px,py-length*.36,px+length,py-length*.10);
   c.quadraticCurveTo(px+length*.16,py+length*.13,px-length,py);
   c.fillStyle='rgba(113,102,107,.20)';c.fill();
   line([px-length*.67,py+1],[px+length*.58,py+1-length*.08],'rgba(255,252,242,.64)',Math.max(.35,s*.012));
  }
  c.restore();
 }
 function snowMark(x,z,halfWidth,lateral,depth,color){
  const corners=[[x-halfWidth,z],[x+halfWidth,z],[x+halfWidth,z+depth],[x-halfWidth,z+depth]];
  c.beginPath();
  corners.forEach(([at,d],i)=>{const [px,py]=project(at,d,lateral);if(i===0)c.moveTo(px,py);else c.lineTo(px,py);});
  c.closePath();c.fillStyle=color;c.fill();
 }
 function prop(index,x,z,lateral,multiplier=1,alpha=1){
  if(z<=-.30||z>38)return;const[px,py,s]=project(x,z,lateral),size=s*3.1*multiplier,sw=props.naturalWidth/4,sh=props.naturalHeight/2;
  c.globalAlpha=alpha;
  c.drawImage(props,(index%4)*sw,Math.floor(index/4)*sh,sw,sh,px-size/2,py-size+s*.08,size,size);
  c.globalAlpha=1;
 }
 function line(a,b,color,width){c.beginPath();c.moveTo(...a);c.lineTo(...b);c.strokeStyle=color;c.lineWidth=width;c.lineCap='round';c.stroke();}
 function draw(g,at){
  if(scene.complete&&scene.naturalWidth)c.drawImage(scene,0,0,1600,900);else{c.fillStyle='#f3f0e8';c.fillRect(0,0,1600,900);}
  if(!scene.complete||!scene.naturalWidth||!props.complete||!props.naturalWidth)return;
  const visible=frame(g,at),travel=visible.distance*WORLD_TRAVEL,items=[],ski=g.ski||{};
  const route=Array.isArray(ski.route)&&ski.route.length>1&&ski.route.length<=64&&
   ski.route.every((p,i)=>Array.isArray(p)&&p.length===3&&p.every(Number.isFinite)&&
    (i===0?p[0]===0:p[0]>ski.route[i-1][0])&&Math.abs(p[1])<=1&&p[2]>=0&&p[2]<=1)?ski.route:null;
  rendered=g.mode==='ski'?{pose:visible,key:scope(g),at,paused:g.status!=='playing'}:null;
  const reduced=reducedMotion();
  if(!reduced){
   snow(travel,visible.lateral);
   snowScuffs(route,travel,visible.lateral);
  }
  tracks(route,travel,visible.lateral);
  if(!reduced){
   const spacing=4.8,start=Math.floor(travel/spacing);
   for(let i=start;i<=start+7;i++){
    const z=(i+1)*spacing-travel;
    const alpha=clamp((36-z)/12,0,.85)*clamp((z-3.2)/7.5,0,1);
    if(alpha===0)continue;
    items.push({x:-4.7-Math.sin(i*1.9)*.55,z,index:mod(i,3),alpha},
     {x:4.7+Math.cos(i)*.55,z:z+.9,index:mod(i+1,3),alpha});
   }
  }
  const correction=((ski.distance||0)-visible.distance)*WORLD_TRAVEL;
  // 新雪道快照包含完整树位，和门、雪面一起按当前可见进度进出画面。
  const trees=route&&Array.isArray(ski.trees)&&ski.trees.length<=64&&ski.trees.every(p=>
   Array.isArray(p)&&p.length===2&&p.every(Number.isFinite)&&p[0]>0&&p[0]<route.at(-1)[0]&&Math.abs(p[1])<=1)?
   ski.trees.map(([distance,x])=>({x,z:distance*WORLD_TRAVEL-travel})):
   (ski.obstacles||[]).map(tree=>({...tree,z:tree.z+correction}));
  for(const {x,z} of trees){
   const alpha=clamp((30-z)/6,0,1)*clamp((z+.30)/.42,0,1);
   if(alpha>0)items.push({x:x*COURSE_WIDTH,z,index:0,alpha});
  }
  // The existing illustrated arch is the crossing target. Scale its opening to
  // the actual clearance plus the skis' half-width, on the same snow plane.
  const gates=route?route.filter(point=>point[2]>0).map(([distance,x,width])=>({x,width,z:distance*WORLD_TRAVEL-travel})):
   (ski.gates||[]).map(gate=>({...gate,z:gate.z+correction}));
  for(const gate of gates){
   const {z,width}=gate,x=gate.x*COURSE_WIDTH;
   if(z<=-.30||z>30)continue;
   snowMark(x,z,width*COURSE_WIDTH,visible.lateral,.19,'rgba(100,77,56,.38)');
   items.push({x,z,index:7,multiplier:(width+.10)/.43});
  }
  if(route){
   const [end]=route.at(-1),z=end*WORLD_TRAVEL-travel;
   if(z>=0&&z<=30){
    snowMark(0,z,COURSE_WIDTH,visible.lateral,.30,'rgba(94,78,65,.38)');
    for(let i=0;i<4;i++)snowMark((i-1.75)*COURSE_WIDTH/2,z,COURSE_WIDTH/8,visible.lateral,.30,'rgba(255,249,239,.92)');
    items.push({x:3.2,z:z+3,index:6,multiplier:1.1});
   }
  }else if(g.status==='complete')items.push({x:0,z:3,index:7});
  items.sort((a,b)=>b.z-a.z);for(const item of items)prop(item.index,item.x,item.z,visible.lateral,item.multiplier,item.alpha);
  // 雪板保留近处的稳定参照，镜头不会随摆臂上下晃动。
  for(const offset of[-1,1]){
   const x=800+offset*72+visible.lateral*26,tip=x-offset*5;
   c.beginPath();c.moveTo(x-22,900);c.lineTo(tip-14,794);
   c.quadraticCurveTo(tip-12,775,tip,772);c.quadraticCurveTo(tip+12,775,tip+14,794);
   c.lineTo(x+22,900);c.closePath();
   c.fillStyle='#b98c59';c.fill();c.strokeStyle='#6f5943';c.lineWidth=1.3;c.stroke();
   line([x-13,895],[tip-7,793],'#d9ad77',2);
   line([x+3,892],[tip+1,790],'#f2cf93',3.5);
  }
 }
 return{receive,draw};
}
