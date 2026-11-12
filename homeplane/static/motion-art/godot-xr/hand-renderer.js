/* Original source: Godot XR Tools hands (CC0 hand mesh/poses; notices adjacent).
 * Rendering adapter: no original hand contours or pose endpoints are authored.
 * Inject the app's existing Three r180 module. One canvas / renderer; no RAF.
 * Caller supplies visibility/reduced-motion policy and a <=24fps shared clock.
 */
export function createHandRenderer(THREE,canvas,model,poses,{pixelRatio=1.5,outlineWidth=.0016,yaw=35,side=1}={}){
 const buffers=model.buffers.map(b=>Uint8Array.from(atob(b.uri.split(',')[1]),c=>c.charCodeAt(0)).buffer),types={5121:Uint8Array,5123:Uint16Array,5125:Uint32Array,5126:Float32Array},sizes={SCALAR:1,VEC2:2,VEC3:3,VEC4:4,MAT4:16};
 const read=i=>{const a=model.accessors[i],v=model.bufferViews[a.bufferView],T=types[a.componentType],o=(v.byteOffset||0)+(a.byteOffset||0);if(v.byteStride)throw Error('Unexpected interleaved hand data');return new T(buffers[v.buffer].slice(o,o+a.count*sizes[a.type]*T.BYTES_PER_ELEMENT));};
 const skin=model.skins[0],joints=new Set(skin.joints),primitive=model.meshes[0].primitives[0],geometry=new THREE.BufferGeometry();
 for(const[a,b,n]of [['POSITION','position',3],['NORMAL','normal',3],['JOINTS_0','skinIndex',4],['WEIGHTS_0','skinWeight',4]])geometry.setAttribute(b,new THREE.BufferAttribute(read(primitive.attributes[a]),n));
 geometry.setIndex(new THREE.BufferAttribute(read(primitive.indices),1));
 const inv=read(skin.inverseBindMatrices),inverses=skin.joints.map((_,i)=>new THREE.Matrix4().fromArray(inv,i*16)),white=new THREE.MeshBasicMaterial({color:0xffffff}),ink=new THREE.MeshBasicMaterial({color:0x292a29,side:THREE.BackSide});
 ink.onBeforeCompile=s=>{s.vertexShader=s.vertexShader.replace('#include <begin_vertex>',`#include <begin_vertex>\ntransformed += normal * ${outlineWidth.toFixed(6)};`);};ink.customProgramCacheKey=()=>`hand-source-outline-${outlineWidth}`;
 const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true,preserveDrawingBuffer:true});renderer.setPixelRatio(pixelRatio);renderer.setClearColor(0,0);renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.setScissorTest(true);
 const aliases={'hand.ok':'OK','hand.yeah':'Peace','hand.point':'Sign_1','hand.open_palm':'Sign_5','hand.fist':'Grip','hand.thumb_up':'Thumb','hand.thumb_down':'Thumb','hand.shaka':'Surfer','hand.horns':'Horns'},pool=new Map();let oldW=0,oldH=0,oldRatio=pixelRatio;
 const smooth=v=>{v=THREE.MathUtils.clamp(v,0,1);return v*v*(3-2*v);};
 const amount=t=>t<.12?0:t<.42?smooth((t-.12)/.30):t<.72?1:t<.96?1-smooth((t-.72)/.24):0;
 function rig(key){
  if(pool.has(key))return pool.get(key);const scene=new THREE.Scene(),nodes=model.nodes.map((n,i)=>{const o=joints.has(i)?new THREE.Bone():n.mesh!==undefined?new THREE.SkinnedMesh(geometry,white):new THREE.Object3D();o.name=n.name;if(n.translation)o.position.fromArray(n.translation);if(n.rotation)o.quaternion.fromArray(n.rotation);if(n.scale)o.scale.fromArray(n.scale);return o;});
  model.nodes.forEach((n,i)=>(n.children||[]).forEach(c=>nodes[i].add(nodes[c])));model.scenes[model.scene||0].nodes.forEach(i=>scene.add(nodes[i]));scene.updateMatrixWorld(true);
  const skeleton=new THREE.Skeleton(skin.joints.map(i=>nodes[i]),inverses.map(m=>m.clone())),mesh=nodes.find(n=>n.isSkinnedMesh);mesh.bind(skeleton,new THREE.Matrix4());mesh.frustumCulled=false;
  const shell=new THREE.SkinnedMesh(geometry,ink);shell.bind(skeleton,new THREE.Matrix4());shell.frustumCulled=false;mesh.parent.add(shell);shell.renderOrder=-1;
  const camera=new THREE.OrthographicCamera(-.13,.13,.13,-.13,.01,5);camera.up.set(0,0,-1);const h={scene,camera,skeleton,bones:Object.fromEntries(skin.joints.map(i=>[nodes[i].name,nodes[i]]))};pool.set(key,h);return h;
 }
 return {
  draw(items,{width,height,ratio=oldRatio}={}){
   const began=performance.now();if(width!==oldW||height!==oldH||ratio!==oldRatio){renderer.setPixelRatio(ratio);renderer.setSize(width,height,false);oldW=width;oldH=height;oldRatio=ratio;}
   renderer.setViewport(0,0,width,height);renderer.setScissor(0,0,width,height);renderer.clear();
   for(const item of items){const key=aliases[item.key]||item.key;if(!poses[key])throw Error('Unknown source hand pose '+key);const h=rig(key),from=key==='Sign_5'?poses.Default_pose.bones:poses.Sign_5.bones,to=poses[key].bones,p=amount(item.phase);
    for(const[name,q]of Object.entries(to))h.bones[name].quaternion.fromArray(from[name]).slerp(new THREE.Quaternion().fromArray(q),p);h.scene.updateMatrixWorld(true);h.skeleton.update();
    const{x,y,width:w,height:ht}=item.rect;renderer.setViewport(x,y,w,ht);renderer.setScissor(x,y,w,ht);h.camera.left=-.13*w/ht;h.camera.right=.13*w/ht;h.camera.updateProjectionMatrix();const a=yaw*Math.PI/180,roll=(item.roll??(item.key==='hand.thumb_up'?-90:item.key==='hand.thumb_down'?90:0))*Math.PI/180;h.camera.position.set(side*.7*Math.cos(a),.7*Math.sin(a),-.075);h.camera.up.set(Math.sin(roll)*Math.sin(a),-Math.sin(roll)*side*Math.cos(a),-Math.cos(roll));h.camera.lookAt(0,0,-.075);renderer.render(h.scene,h.camera);
   }
   return {cpuSubmissionMs:performance.now()-began,items:items.length,webglContexts:1};
  },
  setView(angle,face=1){yaw=angle;side=face;},
  dispose(){for(const h of pool.values())h.skeleton.dispose();pool.clear();geometry.dispose();white.dispose();ink.dispose();renderer.dispose();},
  metadata:{source:'Godot XR Tools CC0 hand mesh + original pose endpoints',vertices:model.accessors[primitive.attributes.POSITION].count,joints:skin.joints.length,three:THREE.REVISION,oneContext:true,ownsRAF:false,timing:{openEnd:.12,formEnd:.42,holdEnd:.72,resetEnd:.96}}
 };
}
