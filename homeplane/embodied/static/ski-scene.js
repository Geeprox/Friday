/* Distant artwork stays still; every point on the snow plane shares the
 * simulation's three world units per distance unit. No independent motion timer,
 * camera roll, shake, input authority or extrapolation after a missing packet. */
function createSkiScenery(c,{scene,props}){
 const WORLD_TRAVEL=3,COURSE_WIDTH=1.9,HORIZON=335,FOCAL=350,EYE_HEIGHT=2.45;
 const mod=(n,size)=>(n%size+size)%size,clamp=(n,a,b)=>Math.max(a,Math.min(b,n));
 let previous=null,current=null,rendered=null,snowPattern=null,textureSource='';
 const scope=g=>(g?.control_context||'')+':'+(g?.round_id??'');
 function receive(g,at){
  if(g?.mode!=='ski'){previous=current=rendered=null;return;}
  const key=scope(g),distance=Number(g.ski?.distance)||0;
  if(current&&(current.key!==key||distance<current.distance))previous=current=rendered=null;
  const input=g.players?.[0]?.lateral;
  const next={key,at,distance,lateral:Number.isFinite(input)?clamp(input,-1,1):current?.lateral??0,playing:g.status==='playing'};
  previous=current&&next.distance>=current.distance?current:next;current=next;
 }
 function frame(g,at){
  const key=scope(g),distance=Number(g.ski?.distance)||0,input=g.players?.[0]?.lateral;
  const lateral=Number.isFinite(input)?clamp(input,-1,1):current?.key===key?current.lateral:0;
  if(g.mode==='ski'&&!['playing','complete'].includes(g.status)&&rendered?.key===key&&distance>=rendered.distance)return{distance:rendered.distance,lateral:rendered.lateral};
  if(!current||current.key!==key||!previous||!current.playing||!previous.playing||g.status!=='playing'||at-current.at>1500)return{distance,lateral};
  // One observation behind: interpolate known positions, never invent travel.
  const mix=clamp((at-current.at)/Math.max(1,current.at-previous.at),0,1);
  return{distance:previous.distance+(current.distance-previous.distance)*mix,lateral:previous.lateral+(current.lateral-previous.lateral)*mix};
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
   for(let y=384;y<900;y+=3){const scale=(y-HORIZON)/EYE_HEIGHT,z=FOCAL/scale-1.8;
    const uvY=mod((travel+z)/18,1)*512,uvScale=scale*12/512;
    pattern.setTransform(new DOMMatrix([uvScale,0,0,1,800-lateral*COURSE_WIDTH*scale,y-uvY]));
    c.globalAlpha=clamp((y-384)/140,0,.92);c.fillRect(0,y,1600,3);
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
   const[x,y,scale]=points[i],edge=x+(offset-width/2)*scale;
   if(i===0)c.moveTo(edge,y);else c.lineTo(edge,y);
  }
  for(let i=points.length-1;i>=0;i--){
   const[x,y,scale]=points[i];c.lineTo(x+(offset+width/2)*scale,y);
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
    return project(x,z,lateral);
   });
   band(points,0,.25,'rgba(255,248,240,.24)');
   for(const offset of[-.092,.092]){
    band(points,offset,.024,'rgba(88,108,119,.30)');
    band(points,offset+.019,.014,'rgba(255,252,244,.85)');
   }
  }
  if(route){
   const spacing=2.7;
   for(let at=Math.ceil((travel+near)/spacing)*spacing;at<travel+far;at+=spacing){
    const x=routeX(route,at/WORLD_TRAVEL),z=at-travel;
    snowMark(x,z,.11,lateral,.075,'rgba(88,108,119,.23)');
   }
  }
 }
 function snowMark(x,z,halfWidth,lateral,depth,color){
  const corners=[[x-halfWidth,z],[x+halfWidth,z],[x+halfWidth,z+depth],[x-halfWidth,z+depth]];
  c.beginPath();
  corners.forEach(([at,d],i)=>{const [px,py]=project(at,d,lateral);if(i===0)c.moveTo(px,py);else c.lineTo(px,py);});
  c.closePath();c.fillStyle=color;c.fill();
 }
 function prop(index,x,z,lateral,multiplier=1){
  if(z<=-.30||z>38)return;const[px,py,s]=project(x,z,lateral),size=s*3.1*multiplier,sw=props.naturalWidth/4,sh=props.naturalHeight/2;
  c.drawImage(props,(index%4)*sw,Math.floor(index/4)*sh,sw,sh,px-size/2,py-size+s*.08,size,size);
 }
 function line(a,b,color,width){c.beginPath();c.moveTo(...a);c.lineTo(...b);c.strokeStyle=color;c.lineWidth=width;c.lineCap='round';c.stroke();}
 function draw(g,at){
  if(scene.complete&&scene.naturalWidth)c.drawImage(scene,0,0,1600,900);else{c.fillStyle='#f3f0e8';c.fillRect(0,0,1600,900);}
  if(!scene.complete||!scene.naturalWidth||!props.complete||!props.naturalWidth)return;
  const visible=frame(g,at),travel=visible.distance*WORLD_TRAVEL,items=[],ski=g.ski||{};
  const route=Array.isArray(ski.route)&&ski.route.length>1&&ski.route.length<=64&&
   ski.route.every((p,i)=>Array.isArray(p)&&p.length===3&&p.every(Number.isFinite)&&
    (i===0?p[0]===0:p[0]>ski.route[i-1][0])&&Math.abs(p[1])<=1&&p[2]>=0&&p[2]<=1)?ski.route:null;
  rendered=g.mode==='ski'?{...visible,key:scope(g)}:null;
  snow(travel,visible.lateral);
  tracks(route,travel,visible.lateral);
  const start=Math.floor(travel/3.6);
  for(let i=start;i<=start+10;i++){const z=i*3.6+3.6-travel;items.push({x:-4.2-Math.sin(i*1.9)*.55,z,index:mod(i,3)},{x:4.2+Math.cos(i)*.55,z,index:mod(i+1,3)});}
  const correction=((ski.distance||0)-visible.distance)*WORLD_TRAVEL;
  for(const o of ski.obstacles||[])items.push({x:o.x*COURSE_WIDTH,z:o.z+correction,index:0});
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
  items.sort((a,b)=>b.z-a.z);for(const item of items)prop(item.index,item.x,item.z,visible.lateral,item.multiplier);
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
