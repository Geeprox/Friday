// Friday's own procedural device models. No manufacturer assets or house data.
import * as T from './vendor/three.module.js';

const neutral=0xf1f0e9,edge=0xc1c8c1,dark=0x293b39;
export function createDeviceModel(device){
 const group=new T.Group(),lights=[],drapes=[];let status=null;
 const fixture=device.presentation?.fixture||({cover:'curtain',camera:'camera',lock:'door_lock',climate:'ceiling_vent',switch:'switch_panel',media_player:'speaker',sensor:'sensor',binary_sensor:'sensor'}[device.kind]||'generic');
 const material=color=>new T.MeshStandardMaterial({color,roughness:.55,metalness:.06});
 const box=(x,y,z,w,h,d,color=neutral)=>{const mesh=new T.Mesh(new T.BoxGeometry(w,h,d),material(color));mesh.position.set(x,y,z);group.add(mesh);return mesh;};
 const cylinder=(x,y,z,r,h,color=neutral)=>{const mesh=new T.Mesh(new T.CylinderGeometry(r,r,h,32),material(color));mesh.position.set(x,y,z);group.add(mesh);return mesh;};
 const orb=(x,y,z,r,color=neutral)=>{const mesh=new T.Mesh(new T.SphereGeometry(r,28,20),material(color));mesh.position.set(x,y,z);group.add(mesh);return mesh;};
 if(fixture==='ceiling_vent'){
  box(0,.13,0,1.65,.22,.53);box(0,.245,0,1.74,.035,.61,edge);
  box(0,.018,0,1.45,.025,.36,dark);
  for(let i=-4;i<=4;i++)box(0,-.01,i*.039,1.42,.04,.014,0xa6b0aa);
  status=box(.67,.025,.23,.07,.035,.025,0x7a9783);
 }else if(fixture==='wall_unit'){
  box(0,.45,0,1.5,.62,.36);box(0,.18,.19,1.3,.06,.025,dark);
  for(let i=-3;i<=3;i++)box(i*.16,.19,.21,.1,.015,.02,edge);
 }else if(['main_light','pendant','downlight','spotlight','accent_light'].includes(fixture)){
  const radius=fixture==='main_light'?.62:fixture==='pendant'?.5:.3;
  const shade=cylinder(0,.3,0,radius,.15,edge);
  const glow=cylinder(0,.21,0,radius*.91,.025,0xffedc4);lights.push(glow);
  if(fixture==='pendant'){cylinder(0,.85,0,.018,.95,dark);cylinder(0,1.32,0,.18,.04);}
  if(fixture==='spotlight'){shade.rotation.z=.4;glow.rotation.z=.4;cylinder(0,.66,0,.08,.46,dark);}
  if(fixture==='accent_light'){cylinder(0,.06,0,.35,.07);cylinder(0,.46,0,.022,.8,dark);shade.position.y=.92;glow.position.y=.84;}
 }else if(fixture==='led_strip'){
  box(0,.06,0,1.65,.1,.18,edge);lights.push(box(0,.12,0,1.58,.025,.12,0xffedc4));
 }else if(fixture==='curtain'){
  box(0,1.57,0,1.75,.075,.12,edge);
  for(const side of [-1,1]){const panel=new T.Group();group.add(panel);drapes.push({panel,side});
   for(let i=0;i<12;i++){const fold=box(0,0,0,.061,1.48,.08,i%2?0xd5d9d1:0xeceee6);group.remove(fold);panel.add(fold);fold.position.set((i-5.5)*.061,.79,i%2*.04);}
  }
 }else if(fixture==='camera'){
  cylinder(0,.06,0,.33,.12,edge);cylinder(0,.25,0,.12,.35);
  orb(0,.68,0,.39);const lens=cylinder(0,.7,.33,.2,.06,dark);lens.rotation.x=Math.PI/2;
  const glass=cylinder(0,.7,.37,.11,.065,0x101f2a);glass.rotation.x=Math.PI/2;
  status=orb(.19,.53,.29,.025,0x7a9783);
 }else if(fixture==='door_lock'){
  box(0,.9,0,.38,1.7,.17,dark);orb(0,1.5,.11,.075,0x101f2a);
  for(let y=0;y<4;y++)for(let x=-1;x<=1;x++)box(x*.08,1.28-y*.09,.095,.022,.025,.01,edge);
  cylinder(0,.55,.12,.075,.035,edge).rotation.x=Math.PI/2;
  box(.18,.55,.18,.42,.07,.08,edge);status=box(0,1.69,.1,.07,.025,.015,0x7a9783);
 }else if(fixture==='switch_panel'||fixture==='power_supply'){
  box(0,.46,0,.88,.88,.16);const count=Math.min(3,Math.max(1,device.controls?.length||1));
  for(let i=0;i<count;i++){box((i-(count-1)/2)*.75/count,.46,.1,.68/count,.68,.08);status=box((i-(count-1)/2)*.75/count,.25,.15,.1,.025,.01,edge);}
 }else if(fixture==='speaker'){
  box(0,.65,0,.72,1.3,.6,dark);for(const[y,r]of [[.4,.22],[.98,.11]]){const cone=cylinder(0,y,.32,r,.045,0x718079);cone.rotation.x=Math.PI/2;}
 }else{
  box(0,.33,0,.72,.66,.27);status=box(0,.42,.145,.43,.23,.015,dark);
 }
 group.traverse(node=>{if(node.isMesh){node.castShadow=true;node.receiveShadow=true;}});
 function update(next){const on=next.controls?.some(c=>c.available&&!['off','closed','unavailable','unknown',null].includes(c.value));
  for(const light of lights){light.material.emissive.set(on?0xffcc7a:0x000000);light.material.emissiveIntensity=on?.7:0;light.material.color.set(on?0xffe2a4:0xe4e5df);}
  if(status)status.material.color.set(next.available===false?0x929991:on?0x527c65:edge);
  const control=next.controls?.find(c=>c.kind==='cover'),position=control?.available?control.attributes?.current_position:null;
  const open=typeof position==='number'?Math.max(0,Math.min(1,position/100)):0;
  for(const {panel,side}of drapes){panel.scale.x=1-open*.8;panel.position.x=side*(.38+open*.31);}
 }
 update(device);return{group,update};
}

// A single renderer serves bounded, cached thumbnails. Thumbnails are controls
// grouped by room, never asserted device installation coordinates.
export function deviceThumbnails(){
 let renderer=null;const cache=new Map();
 const copy=source=>{const canvas=document.createElement('canvas');canvas.width=source.width;canvas.height=source.height;canvas.getContext('2d').drawImage(source,0,0);canvas.setAttribute('aria-hidden','true');return canvas;};
 function render(device){
  const key=JSON.stringify([device.presentation?.fixture,device.kind,device.available,
   device.controls?.map(c=>[c.available,c.value,c.attributes?.current_position]),device.controls?.length]);
  if(cache.has(key))return copy(cache.get(key));
  renderer??=new T.WebGLRenderer({alpha:true,antialias:true,powerPreference:'low-power'});
  renderer.setSize(128,112);renderer.setPixelRatio(1.5);renderer.setClearColor(0,0);
  const scene=new T.Scene(),camera=new T.OrthographicCamera(-1,1,1,-1,.1,20);
  scene.add(new T.HemisphereLight(0xffffff,0x718478,2.7));const sun=new T.DirectionalLight(0xffffff,3);sun.position.set(-3,5,4);scene.add(sun);
  const model=createDeviceModel(device);scene.add(model.group);
  const bounds=new T.Box3().setFromObject(model.group),center=bounds.getCenter(new T.Vector3()),size=bounds.getSize(new T.Vector3());
  const half=Math.max(size.x,size.y,size.z,.7)*.72;camera.left=-half*128/112;camera.right=half*128/112;camera.top=half;camera.bottom=-half;
  camera.position.copy(center).add(new T.Vector3(2.4,2.2,3.8));camera.lookAt(center);camera.updateProjectionMatrix();renderer.render(scene,camera);
  const canvas=copy(renderer.domElement);
  scene.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});
  if(cache.size>=128)cache.delete(cache.keys().next().value);cache.set(key,canvas);return copy(canvas);
 }
 return{render,dispose(){renderer?.dispose();renderer?.forceContextLoss();cache.clear();}};
}
