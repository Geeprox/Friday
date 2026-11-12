/* User-approved Godot XR Tools hand model/poses. Notices in motion-art/godot-xr.
 * Visible tutorials share one WebGL context and one clock. Each tile keeps a
 * 2D copy; hidden, paused and reduced-motion tiles need no animation loop.
 */
const source=new URL('./motion-art/godot-xr/',import.meta.url),tiles=new Set();
let resources=null,renderer=null,atlas=null,raf=null,last=0,failed=false;
const period=4200,cellWidth=216,cellHeight=240,ratio=1.5;
const reduced=globalThis.matchMedia?.('(prefers-reduced-motion: reduce)');
async function load(){
 if(resources)return resources;
 resources=Promise.all([
  import('./vendor/three.module.js'),
  import('./motion-art/godot-xr/hand-renderer.js'),
  ...['hand.gltf','poses.json'].map(async name=>{const r=await fetch(new URL(name,source),{signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error('hand_source_unavailable');return r.json();})
 ]).then(([THREE,adapter,model,poses])=>({THREE,adapter,model,poses})).catch(error=>{resources=null;throw error;});
 return resources;
}
function canAnimate(item){return item.visible&&!item.paused&&!reduced?.matches&&item.node.isConnected&&!document.hidden;}
function schedule(){if(!raf&&!document.hidden&&[...tiles].some(canAnimate)&&!failed)raf=requestAnimationFrame(tick);}
function ensureRenderer(data){
 if(renderer)return;
 atlas=document.createElement('canvas');
 renderer=data.adapter.createHandRenderer(data.THREE,atlas,data.model,data.poses,{pixelRatio:ratio});
 atlas.addEventListener('webglcontextlost',fail);
}
function paint(at,initial=false){
 const active=[...tiles].filter(item=>(initial||item.visible)&&item.node.isConnected&&(canAnimate(item)||item.dirty));
 if(!active.length||!renderer||failed||reduced?.matches)return;
 const cols=Math.min(4,active.length),rows=Math.ceil(active.length/cols),width=cellWidth*cols,height=cellHeight*rows;
 const instructions=active.map((item,i)=>{const phase=reduced?.matches||!item.visible? .57:item.paused?item.phase:(at%period)/period;item.phase=phase;return{key:item.key,phase,rect:{x:(i%cols)*cellWidth,y:Math.floor(i/cols)*cellHeight,width:cellWidth,height:cellHeight}};});
 try{renderer.draw(instructions,{width,height,ratio});}catch{fail();return;}
 active.forEach((item,i)=>{const rect=instructions[i].rect;item.context.clearRect(0,0,item.node.width,item.node.height);item.context.drawImage(atlas,rect.x*ratio,(height-rect.y-rect.height)*ratio,rect.width*ratio,rect.height*ratio,0,0,item.node.width,item.node.height);item.dirty=false;item.node.dataset.motion='ready';});
}
function tick(at){raf=null;if(at-last>=1000/24){last=at;paint(at);}if([...tiles].some(canAnimate))schedule();}
function fallback(item){if(!item.fallback.complete||!item.fallback.naturalWidth)return;item.context.clearRect(0,0,item.node.width,item.node.height);item.context.drawImage(item.fallback,0,0,item.node.width,item.node.height);item.node.dataset.motion='static';}
function fail(){failed=true;if(raf)cancelAnimationFrame(raf);raf=null;for(const item of tiles)fallback(item);}
function prepare(item){void load().then(data=>{if(!tiles.has(item)||reduced?.matches)return;ensureRenderer(data);item.dirty=true;paint(performance.now(),true);schedule();}).catch(fail);}
function targetFrame(){for(const item of tiles){item.dirty=true;if(reduced?.matches){item.phase=.57;fallback(item);}else if(!renderer&&!failed)prepare(item);}paint(performance.now());schedule();}
export function createSourceHandDiagram(key,{label=true,name='手势'}={}){
 const node=document.createElement('canvas');node.width=cellWidth*ratio;node.height=cellHeight*ratio;node.className='motion-diagram motion-hand-source';node.dataset.motion='loading';if(label){node.setAttribute('role','img');node.setAttribute('aria-label',name+'：张掌、成形、保持、复位');}else node.setAttribute('aria-hidden','true');
 const item={node,key,context:node.getContext('2d'),fallback:new Image(),visible:false,paused:false,phase:.57,dirty:true};tiles.add(item);
 item.fallback.onload=()=>{if(tiles.has(item)&&(node.dataset.motion!=='ready'||reduced?.matches||failed))fallback(item);};item.fallback.src=new URL('targets/'+key.replaceAll('.','-')+'.png',source).href;
 const observer=globalThis.IntersectionObserver?new IntersectionObserver(entries=>{item.visible=entries[0].isIntersecting;item.dirty=true;paint(performance.now());schedule();},{threshold:.15}):null;
 observer?.observe(node);if(!observer)item.visible=true;
 if(!reduced?.matches)prepare(item);
 node.setPaused=value=>{item.paused=Boolean(value);if(item.paused)item.phase=.57;item.dirty=true;paint(performance.now());schedule();};
 node.dispose=()=>{tiles.delete(item);observer?.disconnect();item.fallback.onload=null;if(!tiles.size){if(raf)cancelAnimationFrame(raf);raf=null;renderer?.dispose();renderer=null;atlas=null;failed=false;}};
 return node;
}
reduced?.addEventListener?.('change',targetFrame);
document.addEventListener('visibilitychange',()=>{if(document.hidden){if(raf)cancelAnimationFrame(raf);raf=null;}else targetFrame();});
