// Geometry and stable identities come from the import. Friday owns the visual
// proportions, materials and device styles; missing fixture positions stay unset.
import * as T from './vendor/three.module.js';
import {modelPoint,sourcePoint,placedDevice,roomLighting,roomQuickControls,arrangeRoomControls,directPower} from './house-appearance.js';
import {createDeviceModel} from './device-models.js';
export const defaultRooms=[];
const WALL_HEIGHT=2.15,WALL_THICKNESS=.105,INITIAL_ANGLE=-.72;
const world=p=>{const[x,z]=modelPoint(p);return new T.Vector3(x,0,z);};
function center(points){let x=0,z=0,area=0;for(let i=0;i<points.length;i++){const a=points[i],b=points[(i+1)%points.length],cross=a[0]*b[1]-b[0]*a[1];area+=cross;x+=(a[0]+b[0])*cross;z+=(a[1]+b[1])*cross;}return Math.abs(area)>1e-8?[x/(3*area),z/(3*area)]:points[0];}
// A small room-distance texture gives light strips a soft edge instead of a
// rectangular overlay. ShapeGeometry itself clips concave rooms and holes.
function roomDistance(rings,bounds){
 const n=128,pixels=new Uint8Array(n*n),[x,y,w,h]=bounds;
 const edges=rings.flatMap(r=>r.map((a,i)=>[a,r[(i+1)%r.length]]));
 for(let row=0;row<n;row++)for(let col=0;col<n;col++){
  const px=x+(col+.5)/n*w,py=y+(row+.5)/n*h;let nearest=Infinity;
  for(const[a,b]of edges){const dx=b.x-a.x,dy=b.y-a.y,den=dx*dx+dy*dy,t=den?T.MathUtils.clamp(((px-a.x)*dx+(py-a.y)*dy)/den,0,1):0;nearest=Math.min(nearest,Math.hypot(px-a.x-t*dx,py-a.y-t*dy));}
  pixels[row*n+col]=Math.round(Math.min(1,nearest/1.5)*255);
 }
 const texture=new T.DataTexture(pixels,n,n,T.RedFormat);texture.minFilter=texture.magFilter=T.LinearFilter;texture.needsUpdate=true;return texture;
}
function floorMaterial(rings){
 const points=rings.flat(),xs=points.map(p=>p.x),ys=points.map(p=>p.y),bounds=[Math.min(...xs),Math.min(...ys),Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys)];
 const distance=roomDistance(rings,bounds),uniforms={fridayBounds:{value:new T.Vector4(...bounds)},fridayDistance:{value:distance},fridayEnergy:{value:new T.Vector4()},fridayAccent:{value:0},fridaySelected:{value:0},fridayTint:{value:new T.Color(0xffcd86)},fridayPoints:{value:Array.from({length:12},()=>new T.Vector4(0,0,1,0))}};
 const material=new T.MeshStandardMaterial({color:0xc9c5bc,roughness:.92});
 material.onBeforeCompile=shader=>{
  Object.assign(shader.uniforms,uniforms);
  shader.vertexShader='varying vec2 fridayPosition;\n'+shader.vertexShader.replace('#include <begin_vertex>','#include <begin_vertex>\nfridayPosition=position.xy;');
  shader.fragmentShader=`varying vec2 fridayPosition;
   uniform vec4 fridayBounds,fridayEnergy; uniform sampler2D fridayDistance;
   uniform float fridayAccent,fridaySelected; uniform vec3 fridayTint; uniform vec4 fridayPoints[12];
   float fridayLight(){
    float edge=texture2D(fridayDistance,(fridayPosition-fridayBounds.xy)/fridayBounds.zw).r*1.5;
    float strips=exp(-edge*3.8)*fridayEnergy.y;
    float pools=0.0;
    for(int i=0;i<12;i++){vec4 p=fridayPoints[i];float r=length(fridayPosition-p.xy)/max(.08,p.z);pools+=exp(-r*r*2.0)*p.w;}
    return clamp(fridayEnergy.x*.88+strips*.72+fridayEnergy.y*.20+fridayEnergy.z*.24+fridayEnergy.w*.20+fridayAccent*.16+pools*.36,0.0,1.0);
   }
  `+shader.fragmentShader;
  shader.fragmentShader=shader.fragmentShader.replace('#include <color_fragment>',`#include <color_fragment>
   float fridayGlow=fridayLight();
   diffuseColor.rgb=mix(diffuseColor.rgb,fridayTint,fridayGlow*.90);
   diffuseColor.rgb=mix(diffuseColor.rgb,vec3(.48,.60,.49),fridaySelected*.12);
  `).replace('#include <emissivemap_fragment>','#include <emissivemap_fragment>\ntotalEmissiveRadiance+=fridayTint*fridayGlow*.28;');
 };
 material.customProgramCacheKey=()=> 'friday-room-light-v1';
 return {material,uniforms,distance};
}
export function mountHouse(container,{rooms,devices,placements=[],selectedRoom,onRoom,onDevice,onPlace,onCancelPlace,onQuick,onRoomDevices,thumbnail}){
 let alive=true,angle=INITIAL_ANGLE,elevation=.9,zoom=1,down=null,moved=false,animation=0,lightAnimation=0,placing=null,focusedRoom=selectedRoom,writeBusy=false;
 const scene=new T.Scene(),camera=new T.OrthographicCamera(-10,10,10,-10,.1,100),renderer=new T.WebGLRenderer({antialias:true,alpha:true,powerPreference:'low-power'});
 renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setClearColor(0,0);renderer.shadowMap.enabled=true;renderer.shadowMap.type=T.PCFSoftShadowMap;renderer.outputColorSpace=T.SRGBColorSpace;renderer.toneMapping=T.ACESFilmicToneMapping;renderer.toneMappingExposure=1.05;
 container.append(renderer.domElement);renderer.domElement.tabIndex=0;renderer.domElement.setAttribute('aria-label','家庭空间。方向键旋转，加减键缩放。定位设备时，方向键移动位置，回车保存，Esc 取消。');
 scene.add(new T.HemisphereLight(0xffffff,0x9f9b91,2.2));const sun=new T.DirectionalLight(0xffffff,2.8);sun.position.set(-5,18,6);sun.castShadow=true;sun.shadow.mapSize.set(2048,2048);Object.assign(sun.shadow.camera,{left:-18,right:18,top:18,bottom:-18});sun.shadow.bias=-.001;scene.add(sun);
 const group=new T.Group();scene.add(group);const floors=[],interactive=[],labels=[],markers=[],walls=[],roomInfo=new Map();
 function box(parent,w,height,d,x,y,z,color){const mesh=new T.Mesh(new T.BoxGeometry(w,height,d),new T.MeshStandardMaterial({color,roughness:.85}));mesh.position.set(x,y,z);mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);return mesh;}
 function label(text,p,y,kind,action){
  const isRoom=kind==='room-label',n=document.createElement(isRoom?'div':'button');n.className='model-label '+kind;
  let wire=null;
  if(isRoom){
   n.classList.add('model-room-anchor');
   const title=document.createElement('button');title.type='button';title.className='model-room-name';title.textContent=text;title.onclick=action;
   const actions=document.createElement('div');actions.className='model-room-actions';n.append(title,actions);n.dataset.room=text;
   wire=document.createElementNS('http://www.w3.org/2000/svg','line');links.append(wire);
  }else{n.type='button';n.textContent=text;n.onclick=action;}
  container.append(n);labels.push({node:n,room:isRoom?text:null,wire,point:new T.Vector3(p.x,y,p.z)});return n;
 }
 const links=document.createElementNS('http://www.w3.org/2000/svg','svg');links.classList.add('model-room-links');links.setAttribute('aria-hidden','true');container.append(links);
 function updateRoomControls(next){
  const active=document.activeElement,focusKey=active?.dataset.quickKey;
  for(const info of roomInfo.values()){
   const actions=info.label.querySelector('.model-room-actions');actions.replaceChildren();
   for(const entry of roomQuickControls(next,info.room.name)){
    const b=document.createElement('button');b.type='button';b.className='model-quick'+(entry.on?' on':'');
    const name={light:'灯光',cover:'窗帘',climate:'空调',camera:'摄像头',lock:'门锁'}[entry.kind]||entry.name;
    const text=entry.control?name+'·'+(!entry.available?'离线':entry.on?'开':'关'):name;
    const device=next.find(d=>d.id===entry.device),surface=thumbnail?.(device),icon=surface||document.createElement('span');
    if(!surface){icon.className='icon icon-'+({light:'bulb',cover:'adjustments-horizontal',climate:'ceiling-vent',camera:'video',lock:'door'}[entry.kind]);icon.setAttribute('aria-hidden','true');}
    const caption=document.createElement('span');caption.textContent=text;b.append(icon,caption);
    b.dataset.quickKey=entry.control||entry.device;b.title=entry.name;b.setAttribute('aria-label',entry.control?(entry.on?'关闭':'开启')+entry.name:'控制'+entry.name);
    if(entry.control){b.setAttribute('role','switch');b.setAttribute('aria-checked',String(entry.on));b.dataset.quickControl=entry.control;b.dataset.offline=String(!entry.available);b.disabled=writeBusy||!entry.available;}
    b.onclick=()=>onQuick?.(entry);actions.append(b);
   }
   const more=document.createElement('button');more.type='button';more.className='model-room-more';more.dataset.quickKey='room:'+info.room.id;more.textContent='设备 ›';more.setAttribute('aria-label',info.room.name+'全部设备');more.onclick=()=>onRoomDevices?.(info.room.name);actions.append(more);
  }
  if(focusKey&&container.contains(active)===false)for(const b of container.querySelectorAll('[data-quick-key]'))if(b.dataset.quickKey===focusKey){b.focus({preventScroll:true});break;}
 }
 const all=[];
 for(const room of rooms){
  const pieces=[{polygon:room.polygon,holes:room.holes||[]},...(room.parts||[])],roomFloors=[],points=[];
  for(const piece of pieces){
   const pts=piece.polygon.map(world);all.push(...pts);points.push(...pts);const rings=[piece.polygon,...(piece.holes||[])].map(r=>r.map(p=>{const w=world(p);return new T.Vector2(w.x,-w.z);})),shape=new T.Shape(rings[0]);
   for(const hole of rings.slice(1))shape.holes.push(new T.Path(hole));const visual=floorMaterial(rings);
   const floor=new T.Mesh(new T.ShapeGeometry(shape),visual.material);floor.rotation.x=-Math.PI/2;floor.receiveShadow=true;floor.userData.room=room;group.add(floor);floors.push(floor);roomFloors.push({...visual,mesh:floor});interactive.push(floor);
   // Full-height display walls fade on the camera-facing side.
   for(const ring of [piece.polygon,...(piece.holes||[])]){
    const edge=ring.map(world),signedArea=edge.reduce((sum,a,i)=>{const b=edge[(i+1)%edge.length];return sum+a.x*b.z-b.x*a.z;},0);
    for(let i=0;i<edge.length;i++){
     const a=edge[i],b=edge[(i+1)%edge.length],length=a.distanceTo(b);
     const wall=box(group,length+WALL_THICKNESS/2,WALL_HEIGHT,WALL_THICKNESS,(a.x+b.x)/2,WALL_HEIGHT/2,(a.z+b.z)/2,0xf1eee5);
     wall.rotation.y=-Math.atan2(b.z-a.z,b.x-a.x);wall.material.transparent=true;
     const outward=new T.Vector3(b.z-a.z,0,a.x-b.x).normalize().multiplyScalar(signedArea>0?1:-1);
     walls.push({mesh:wall,outward,room:room.name});
    }
   }
  }
  const c=world(center(room.polygon)),bounds=new T.Box3().setFromPoints(points),size=bounds.getSize(new T.Vector3()),node=label(room.name,c,.08,'room-label',()=>onRoom(room.name));
  roomInfo.set(room.name,{room,center:c,size,floors:roomFloors,label:node});
 }
 function fixture(d,p){
  const node=new T.Group(),kind=d.presentation?.fixture;node.position.set(p.x,0,p.z);group.add(node);
  const deviceModel=createDeviceModel(d);node.add(deviceModel.group);
  deviceModel.group.scale.setScalar(.65);
  if(d.presentation?.mount==='ceiling')deviceModel.group.position.y=WALL_HEIGHT-.3;
  node.traverse(o=>{if(o.isMesh)o.userData.device=d;});interactive.push(node);
  const caption=label('',p,kind==='ceiling_vent'?WALL_HEIGHT:.8,'device-label',()=>onDevice(node.userData.device));node.userData.device=d;
  const icon=document.createElement('span');icon.className='icon icon-'+({ceiling_vent:'ceiling-vent',led_strip:'light-strip',downlight:'downlight',spotlight:'spotlight',pendant:'pendant',power_supply:'plug'}[kind]||{light:'bulb',cover:'adjustments-horizontal',camera:'video',climate:'ceiling-vent'}[d.kind]||'devices');icon.setAttribute('aria-hidden','true');
  const name=document.createElement('span');name.textContent=d.name;caption.append(icon,name);caption.title=d.name;caption.setAttribute('aria-label','控制'+d.name);
  return {id:d.id,node,caption,deviceModel};
 }
 for(const placement of placements){const d=placedDevice(devices,placement.id);if(d)markers.push(fixture(d,world(placement.position)));}
 const bounds=new T.Box3().setFromPoints(all),fullTarget=bounds.getCenter(new T.Vector3()),extent=bounds.getSize(new T.Vector3());let target=fullTarget.clone(),viewSize=Math.max(extent.z*1.2,extent.x*.95,12),fullSize=viewSize;
 const ground=new T.Mesh(new T.PlaneGeometry(150,150),new T.ShadowMaterial({opacity:.07}));ground.rotation.x=-Math.PI/2;ground.position.y=-.01;ground.receiveShadow=true;scene.add(ground);
 function paint(){if(!alive)return;const w=container.clientWidth,h=container.clientHeight;if(!w||!h)return;const aspect=w/h,half=viewSize/zoom/2;camera.left=-half*aspect;camera.right=half*aspect;camera.top=half;camera.bottom=-half;camera.position.set(target.x+Math.sin(angle)*24,elevation*24,target.z+Math.cos(angle)*24);camera.lookAt(target);camera.updateProjectionMatrix();
  const facing=new T.Vector3(Math.sin(angle),0,Math.cos(angle));
  for(const wall of walls){const near=T.MathUtils.clamp((wall.outward.dot(facing)+.1)/.7,0,1);wall.mesh.material.opacity=1-.87*near;wall.mesh.material.depthWrite=near<.5;wall.mesh.castShadow=near<.5;}
  renderer.setSize(w,h,false);renderer.render(scene,camera);const projected=[];
  labels.forEach(({node,point,room,wire})=>{
   const p=point.clone().project(camera),x=(p.x*.5+.5)*w,y=(-p.y*.5+.5)*h;
   node.hidden=p.z>1||p.x<-1.2||p.x>1.2||p.y<-1.2||p.y>1.2||Boolean(room&&roomInfo.has(focusedRoom)&&focusedRoom!==room);
   if(wire)wire.style.display=node.hidden?'none':'';
   if(!node.hidden&&room)projected.push({node,wire,anchorX:x,anchorY:y,w:node.offsetWidth||132,h:node.offsetHeight||64});
   else{node.style.left=x+'px';node.style.top=y+'px';}
  });
  for(const p of arrangeRoomControls(projected,w,h,w<600?112:70)){p.node.style.left=p.x+'px';p.node.style.top=p.y+'px';p.wire.setAttribute('x1',p.anchorX);p.wire.setAttribute('y1',p.anchorY);p.wire.setAttribute('x2',p.x);p.wire.setAttribute('y2',p.y);}

 }
 function lighting(next,animated=true){
  cancelAnimationFrame(lightAnimation);updateRoomControls(next);const changes=[];
  for(const info of roomInfo.values()){
   const state=roomLighting(next,info.room.name),energy=new T.Vector4(state.main,state.strip,state.downlight,state.spotlight),tint=new T.Color(0xffc56e).lerp(new T.Color(0xe7efff),T.MathUtils.clamp((state.kelvin-2700)/4300,0,1));
   info.label.classList.toggle('lit',state.lights.length>0);
   info.label.querySelector('.model-room-name').setAttribute('aria-label',info.room.name+(state.lights.length?'，灯光已开启':'，灯光已关闭'));
   for(const f of info.floors){
    changes.push({u:f.uniforms,from:f.uniforms.fridayEnergy.value.clone(),energy,accent:state.accent,fromAccent:f.uniforms.fridayAccent.value,tint,fromTint:f.uniforms.fridayTint.value.clone()});
    f.uniforms.fridayPoints.value.forEach(p=>p.w=0);let i=0;
    for(const light of state.lights){const point=placements.find(p=>placedDevice(next,p.id)?.id===light.device);if(!point||i>=12)continue;const p=world(point.position),radius={main:2.6,strip:.7,downlight:.6,spotlight:.4,accent:.9}[light.role];f.uniforms.fridayPoints.value[i++].set(p.x,-p.z,radius,light.level);}
   }
  }
  for(const item of markers){const d=next.find(d=>d.id===item.id);if(!d)continue;item.deviceModel.update(d);item.node.userData.device=d;item.node.traverse(o=>{if(o.isMesh)o.userData.device=d;});const on=d.controls?.some(c=>c.available&&c.value==='on');item.caption.classList.toggle('on',on);const power=directPower(d);if(power){item.caption.setAttribute('role','switch');item.caption.setAttribute('aria-checked',String(power.value==='on'));item.caption.setAttribute('aria-label',(power.value==='on'?'关闭':'开启')+d.name);item.caption.dataset.quickControl=power.id;item.caption.dataset.offline=String(!power.available);item.caption.disabled=writeBusy||!power.available;}}
  const began=performance.now(),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  function step(now){const t=animated&&!reduced?Math.min(1,(now-began)/420):1,ease=t*t*(3-2*t);for(const c of changes){c.u.fridayEnergy.value.copy(c.from).lerp(c.energy,ease);c.u.fridayAccent.value=c.fromAccent+(c.accent-c.fromAccent)*ease;c.u.fridayTint.value.copy(c.fromTint).lerp(c.tint,ease);}paint();if(t<1)lightAnimation=requestAnimationFrame(step);}
  lightAnimation=requestAnimationFrame(step);
 }
 function focus(name,animated=true){focusedRoom=name;const info=roomInfo.get(name);container.classList.toggle('model-focused-room',Boolean(info));for(const x of roomInfo.values())for(const f of x.floors)f.uniforms.fridaySelected.value=x===info?1:0;const dest=info?info.center:fullTarget,size=info?Math.max(info.size.x,info.size.z)*1.55:fullSize;cancelAnimationFrame(animation);const begin=performance.now(),from=target.clone(),initial=viewSize;zoom=1;const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  function step(now){const t=animated&&!reduced?Math.min(1,(now-begin)/650):1,ease=1-Math.pow(1-t,3);target.copy(from).lerp(dest,ease);viewSize=initial+(size-initial)*ease;paint();if(t<1)animation=requestAnimationFrame(step);}animation=requestAnimationFrame(step);
 }
 const ray=new T.Raycaster(),pointer=new T.Vector2(),placementPoint=new T.Vector3();
 const cursor=new T.Mesh(new T.RingGeometry(.13,.23,32),new T.MeshBasicMaterial({color:0x365c47,side:T.DoubleSide,depthTest:false}));cursor.rotation.x=-Math.PI/2;cursor.position.y=.04;cursor.renderOrder=10;cursor.visible=false;scene.add(cursor);
 function placementHit(){ray.set(new T.Vector3(placementPoint.x,30,placementPoint.z),new T.Vector3(0,-1,0));return ray.intersectObjects(floors,false)[0];}
 function movePlacement(){cursor.position.set(placementPoint.x,.04,placementPoint.z);cursor.material.color.set(placementHit()?0x365c47:0xa84630);paint();}
 renderer.domElement.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&placing){e.preventDefault();onCancelPlace?.();return;}
  if(e.key==='Enter'&&placing){e.preventDefault();const hit=placementHit();if(hit)onPlace?.(placing,hit.object.userData.room,sourcePoint([hit.point.x,hit.point.z]));return;}
  if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){
   e.preventDefault();if(placing){const step=viewSize/zoom*.018*(e.shiftKey?.25:1),x=e.key==='ArrowLeft'?-1:e.key==='ArrowRight'?1:0,y=e.key==='ArrowUp'?-1:e.key==='ArrowDown'?1:0;placementPoint.x+=step*(x*Math.cos(angle)+y*Math.sin(angle));placementPoint.z+=step*(-x*Math.sin(angle)+y*Math.cos(angle));movePlacement();}
   else{angle+=e.key==='ArrowLeft'?.12:e.key==='ArrowRight'?-.12:0;elevation=T.MathUtils.clamp(elevation+(e.key==='ArrowUp'?.08:e.key==='ArrowDown'?-.08:0),.35,1.6);paint();}return;
  }
  if(['+','=','-','_'].includes(e.key)){e.preventDefault();zoom=T.MathUtils.clamp(zoom+(e.key==='-'||e.key==='_'?-.1:.1),.7,2);paint();}
 });
 renderer.domElement.addEventListener('pointerdown',e=>{down=[e.clientX,e.clientY,angle,elevation];moved=false;renderer.domElement.setPointerCapture(e.pointerId);});renderer.domElement.addEventListener('pointermove',e=>{if(!down)return;const dx=e.clientX-down[0],dy=e.clientY-down[1];if(Math.hypot(dx,dy)>5)moved=true;angle=down[2]-dx*.006;elevation=Math.max(.35,Math.min(1.6,down[3]+dy*.004));paint();});renderer.domElement.addEventListener('pointercancel',()=>{down=null;});renderer.domElement.addEventListener('pointerup',e=>{if(!down)return;down=null;if(moved)return;const r=renderer.domElement.getBoundingClientRect();pointer.set((e.clientX-r.left)/r.width*2-1,-(e.clientY-r.top)/r.height*2+1);ray.setFromCamera(pointer,camera);const hit=ray.intersectObjects(placing?floors:interactive,true)[0];if(!hit)return;if(placing){onPlace?.(placing,hit.object.userData.room,sourcePoint([hit.point.x,hit.point.z]));return;}if(hit.object.userData.device)onDevice(hit.object.userData.device);else if(hit.object.userData.room)onRoom(hit.object.userData.room.name);});renderer.domElement.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.7,Math.min(2,zoom-e.deltaY*.001));paint();},{passive:false});const observer=new ResizeObserver(paint);observer.observe(container);focus(selectedRoom,false);lighting(devices,false);
 return{focus,updateDevices:lighting,setBusy(value){writeBusy=value;for(const b of container.querySelectorAll('[data-quick-control]'))b.disabled=value||b.dataset.offline==='true';},reset(){angle=INITIAL_ANGLE;elevation=.9;focus(null);},place(device){placing=device;cursor.visible=Boolean(device);renderer.domElement.style.cursor=device?'crosshair':'grab';if(device){placementPoint.copy(roomInfo.get(device.room)?.center||target);movePlacement();renderer.domElement.focus({preventScroll:true});}else paint();},dispose(){alive=false;cancelAnimationFrame(animation);cancelAnimationFrame(lightAnimation);observer.disconnect();for(const info of roomInfo.values())for(const f of info.floors)f.distance.dispose();scene.traverse(o=>{o.geometry?.dispose();if(Array.isArray(o.material))o.material.forEach(m=>m.dispose());else o.material?.dispose();});renderer.dispose();renderer.forceContextLoss();container.replaceChildren();}};
}
