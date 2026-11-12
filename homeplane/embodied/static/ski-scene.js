/* Distant artwork stays still; every point on the snow plane shares the
 * simulation's three world units per distance unit. No independent motion timer,
 * camera roll, shake, input authority or extrapolation after a missing packet. */
function createSkiScenery(c,{scene,props}){
 const WORLD_TRAVEL=3,HORIZON=335,FOCAL=350,EYE_HEIGHT=2.45;
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
 function project(x,z,lateral){const scale=FOCAL/(z+1.8);return[800+(x-lateral*1.9)*scale,HORIZON+scale*EYE_HEIGHT,scale];}
 function texture(){
  if(snowPattern&&textureSource===scene.src)return snowPattern;
  // Sample only the unmarked snow of the original artwork. Mirrored UV edges
  // make a continuous texture; the raster itself is neither rewritten nor saved.
  const tile=document.createElement('canvas');tile.width=tile.height=512;
  const t=tile.getContext('2d'),sw=scene.naturalWidth,sh=scene.naturalHeight;
  for(let y=0;y<2;y++)for(let x=0;x<2;x++){t.save();t.translate(x?512:0,y?512:0);t.scale(x?-1:1,y?-1:1);t.drawImage(scene,sw*.40,sh*.72,sw*.20,sh*.24,0,0,256,256);t.restore();}
  snowPattern=c.createPattern(tile,'repeat');textureSource=scene.src;return snowPattern;
 }
 function snow(distance,lateral){
  const pattern=texture(),travel=distance*WORLD_TRAVEL;
  if(pattern){c.save();c.fillStyle=pattern;
   for(let y=384;y<900;y+=3){const scale=(y-HORIZON)/EYE_HEIGHT,z=FOCAL/scale-1.8;
    const uvY=mod((travel+z)/18,1)*512,uvScale=scale*12/512;
    pattern.setTransform(new DOMMatrix([uvScale,0,0,1,800-lateral*1.9*scale,y-uvY]));
    c.globalAlpha=clamp((y-384)/140,0,.92);c.fillRect(0,y,1600,3);
   }c.restore();
  }
  // Real grooves on the same world plane, rather than marks baked into a sky
  // plate or speed lines driven by wall time. The complete curve passes us.
  c.save();c.lineCap='round';
  for(const lane of[-1.45,1.45])for(const pair of[-.08,.08]){
   let last=null;
   for(let z=30;z>=-.25;z-=.28){const worldZ=travel+z,x=lane+pair+Math.sin(worldZ*.22)*.42,p=project(x,z,lateral);
    if(last){const alpha=clamp((p[1]-380)/160,0,1),width=Math.max(.6,p[2]*.018);
     c.beginPath();c.moveTo(last[0],last[1]);c.lineTo(p[0],p[1]);c.strokeStyle='rgba(132,142,151,'+(.24*alpha)+')';c.lineWidth=width;c.stroke();
     c.beginPath();c.moveTo(last[0]+width,last[1]+1);c.lineTo(p[0]+width,p[1]+1);c.strokeStyle='rgba(255,250,238,'+(.65*alpha)+')';c.lineWidth=width*.7;c.stroke();
    }last=p;
   }
  }c.restore();
 }
 function prop(index,x,z,lateral,multiplier=1){
  if(z<=-.30||z>38)return;const[px,py,s]=project(x,z,lateral),size=s*3.1*multiplier,sw=props.naturalWidth/4,sh=props.naturalHeight/2;
  c.drawImage(props,(index%4)*sw,Math.floor(index/4)*sh,sw,sh,px-size/2,py-size+s*.08,size,size);
 }
 function line(a,b,color,width){c.beginPath();c.moveTo(...a);c.lineTo(...b);c.strokeStyle=color;c.lineWidth=width;c.lineCap='round';c.stroke();}
 function draw(g,at){
  if(scene.complete&&scene.naturalWidth)c.drawImage(scene,0,0,1600,900);else{c.fillStyle='#f3f0e8';c.fillRect(0,0,1600,900);}
  if(!scene.complete||!scene.naturalWidth||!props.complete||!props.naturalWidth)return;
  const visible=frame(g,at),travel=visible.distance*WORLD_TRAVEL,items=[];
  rendered=g.mode==='ski'?{...visible,key:scope(g)}:null;
  snow(visible.distance,visible.lateral);
  const start=Math.floor(travel/3.6);
  for(let i=start;i<=start+10;i++){const z=i*3.6+3.6-travel;items.push({x:-4.2-Math.sin(i*1.9)*.55,z,index:mod(i,3)},{x:4.2+Math.cos(i)*.55,z,index:mod(i+1,3)});}
  const correction=((g.ski?.distance||0)-visible.distance)*WORLD_TRAVEL;
  for(const o of g.ski?.obstacles||[])items.push({x:o.x*1.9,z:o.z+correction,index:0});
  // The existing illustrated arch is the crossing target. Scale its opening to
  // the actual clearance plus the skis' half-width, on the same snow plane.
  for(const gate of g.ski?.gates||[])items.push({x:gate.x*1.9,z:gate.z+correction,index:7,multiplier:(gate.width+.10)/.43});
  if(g.status==='complete')items.push({x:0,z:3,index:7});
  items.sort((a,b)=>b.z-a.z);for(const item of items)prop(item.index,item.x,item.z,visible.lateral,item.multiplier);
  // Skis and poles are a stable foreground reference; no cosmetic bob or roll.
  for(const offset of[-1,1]){const x=800+offset*72+visible.lateral*30;c.beginPath();[[x-20,900],[x+20,900],[x+offset*5+15,777],[x+offset*5-7,777]].forEach((p,i)=>i?c.lineTo(...p):c.moveTo(...p));c.closePath();c.fillStyle='#b48753';c.fill();c.strokeStyle='#74634d';c.lineWidth=1;c.stroke();line([x-10,886],[x+offset*5+1,789],'#e9c78c',3);}
  line([455,900],[640+visible.lateral*30,746],'#3d4242',7);line([1145,900],[960+visible.lateral*30,746],'#3d4242',7);
 }
 return{receive,draw};
}
