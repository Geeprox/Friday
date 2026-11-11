/* MakeHuman CC0 continuous surface; Desk Break MIT skeletal motion.
 * Notices and exact adaptations live in motion-art/body/provenance.json.
 * A single renderer draws the visible tutorials; individual tiles are 2D copies.
 */
const source = new URL('./motion-art/body/', import.meta.url);
const tiles = new Set(), width = 240, height = 270, ratio = 1.5, period = 5200;
const reduced = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)');
let resources, renderer, atlas, raf, last = 0, failed = false;
const smooth = value => { const t = Math.max(0, Math.min(1, value)); return t*t*(3-2*t); };
// Hold the target long enough to read it, then return through the same source.
function sourceTime(phase, start, target) {
  if (phase < .10 || phase > .94) return start;
  if (phase < .43) return start + (target-start)*smooth((phase-.10)/.33);
  if (phase < .67) return target;
  return target + (start-target)*smooth((phase-.67)/.27);
}
function alternatingSourceTime(phase, neutral, left, right) {
  if (phase < .07 || phase >= .93) return neutral;
  if (phase < .23) return neutral+(left-neutral)*smooth((phase-.07)/.16);
  if (phase < .31) return left;
  if (phase < .45) return left+(neutral-left)*smooth((phase-.31)/.14);
  if (phase < .55) return neutral;
  if (phase < .71) return neutral+(right-neutral)*smooth((phase-.55)/.16);
  if (phase < .79) return right;
  return right+(neutral-right)*smooth((phase-.79)/.14);
}
const mappings = {
  'pose.horizontal': {key:'horizontal'},
  'pose.overhead': {key:'overhead'},
  'body.hands_up': {key:'overhead'},
  'body.squat': {key:'squat'},
  'exercise.squat': {key:'squat'},
  'exercise.high_knee': {key:'high_knee'},
  'body.arms_open': {key:'open-close-swings', times:[.6,1.2333333492]},
  'body.arms_close': {key:'open-close-swings', times:[0,.6]},
  'body.left_hand_up': {key:'overhead-reach', times:[.9,0]},
  'body.right_hand_up': {key:'overhead-reach', times:[.9,1.8]},
  // These signals use unmirrored image coordinates in interaction.body_gestures.
  'body.lean_left': {key:'hands-on-hips-side-bend', times:[0,1.1]},
  'body.lean_right': {key:'hands-on-hips-side-bend', times:[2.2,3.3]},
  'body.lateral': {key:'lateral_step'},
  'body.arm_effort': {key:'ski_alternate'},
  'body.swing': {key:'tennis_swing'},
  'exercise.jack': {key:'step_jack'},
  'exercise.side_lunge': {key:'side_lunge'},
  'exercise.side_step': {key:'side_step'},
  'exercise.side_bend': {key:'side_bend'},
  'exercise.reach_up': {key:'overhead-reach', alternatingTimes:[.9,0,1.8], fallback:'body.left_hand_up'},
  'exercise.arm_open': {key:'open-close-swings', times:[.6,1.2333333492], fallback:'body.arms_open'},
  'exercise.balance': {key:'balance', mirrorCycle:true, yaw:0},
  'exercise.cross_knee': {key:'cross_knee', mirrorCycle:true, yaw:0},
  'exercise.side_raise': {key:'side_raise', mirrorCycle:true, yaw:0},
  'exercise.overhead_clap': {key:'dance-overhead-clap', times:[0,.50], fromStart:true, yaw:0},
};
export const hasSourceBodyDiagram = key => Boolean(mappings[key]);
async function load() {
  if (!resources) resources = Promise.all([
    import('./vendor/three.module.js'), import('./motion-art/body/body-renderer.js'),
    fetch(new URL('body.gltf', source), {signal:AbortSignal.timeout(10000)}).then(r => {
      if (!r.ok) throw Error('body_source_unavailable');
      return r.json();
    }),
  ]).then(([THREE, adapter, model]) => ({THREE,adapter,model})).catch(error => {
    resources = null; throw error;
  });
  return resources;
}
const canAnimate = item => item.visible && !item.paused && !reduced?.matches && item.node.isConnected && !document.hidden;
function schedule() {
  if (!raf && !document.hidden && !failed && [...tiles].some(canAnimate)) raf=requestAnimationFrame(tick);
}
function paint(at) {
  if (!renderer || failed || reduced?.matches) return;
  const active=[...tiles].filter(item => item.visible && item.node.isConnected && (item.dirty || canAnimate(item)));
  if (!active.length) return;
  const cols=Math.min(4,active.length), rows=Math.ceil(active.length/cols), w=width*cols,h=height*rows;
  const items=active.map((item,i) => {
    const map=mappings[item.key];
    const localClock=map.mirrorCycle||map.fromStart;
    if(localClock){
      const step=item.lastPaint===null?0:at-item.lastPaint;
      const playing=canAnimate(item);
      if(playing&&step>0&&step<=250)item.motionTime+=step;
      item.lastPaint=playing?at:null;
    }
    const elapsed=localClock?item.motionTime:at;
    const phase=item.paused || reduced?.matches ? .57 : (elapsed%period)/period;
    const instruction={key:map.key,phase,yaw:map.yaw,mirror:map.mirrorCycle&&Math.floor(elapsed/period)%2===1,
      transparentBackground:item.transparentBackground,rect:{x:(i%cols)*width,y:Math.floor(i/cols)*height,width,height}};
    if (map.times) instruction.sourceTime=sourceTime(phase,...map.times);
    if (map.alternatingTimes) instruction.sourceTime=alternatingSourceTime(phase,...map.alternatingTimes);
    return instruction;
  });
  try {renderer.draw(items,{width:w,height:h,ratio});}catch{fail();return;}
  active.forEach((item,i) => {
    const rect=items[i].rect;
    item.context.clearRect(0,0,item.node.width,item.node.height);
    item.context.drawImage(atlas,rect.x*ratio,(h-rect.y-height)*ratio,width*ratio,height*ratio,0,0,item.node.width,item.node.height);
    item.dirty=false;item.node.dataset.motion='ready';
  });
}
function tick(at) {
  raf=null;
  if(reduced?.matches){targetFrame();return;}
  if(at-last>=50){last=at;paint(at);}
  schedule();
}
function fallback(item){
  if(!item.fallback.complete||!item.fallback.naturalWidth)return;
  item.context.clearRect(0,0,item.node.width,item.node.height);
  item.context.drawImage(item.fallback,0,0,item.node.width,item.node.height);
  item.node.dataset.motion='static';
}
function fail(){failed=true;if(raf)cancelAnimationFrame(raf);raf=null;for(const item of tiles)fallback(item);}
function prepare(item){
  void load().then(data=> {
    if(!tiles.has(item)||reduced?.matches)return;
    if(!renderer){atlas=document.createElement('canvas');renderer=data.adapter.createBodyRenderer(data.THREE,atlas,data.model,{pixelRatio:ratio,outlineWidth:.010});atlas.addEventListener('webglcontextlost',fail);}
    item.dirty=true;paint(performance.now());schedule();
  }).catch(fail);
}
function targetFrame() { for(const item of tiles){item.lastPaint=null;item.dirty=true;if(reduced?.matches)fallback(item);else if(!renderer&&!failed)prepare(item);}paint(performance.now());schedule(); }
export function createSourceBodyDiagram(key,{label=true,name='体感动作',transparentBackground=false}={}) {
  const node=document.createElement('canvas');node.width=width*ratio;node.height=height*ratio;
  node.className='motion-diagram motion-body-source';node.dataset.motion='loading';
  if(label){node.setAttribute('role','img');node.setAttribute('aria-label',name+'：准备、动作、保持、复位');}
  else node.setAttribute('aria-hidden','true');
  const item={node,key,context:node.getContext('2d'),fallback:new Image(),visible:false,paused:false,dirty:true,
    motionTime:0,lastPaint:null,transparentBackground};tiles.add(item);
  item.fallback.onload=()=>{if(tiles.has(item)&&(node.dataset.motion!=='ready'||reduced?.matches||failed))fallback(item);};
  item.fallback.src=new URL('targets/'+(mappings[key]?.fallback||key).replaceAll('.','-')+'.png',source).href;
  const observer=globalThis.IntersectionObserver ? new IntersectionObserver(entries=> {
    item.visible=entries[0].isIntersecting;item.dirty=true;paint(performance.now());schedule();
  },{threshold:.15}) : null;
  observer?.observe(node);if(!observer)item.visible=true;
  if(!reduced?.matches)prepare(item);
  node.setPaused=value=> {const paused=Boolean(value);if(item.paused===paused)return;item.paused=paused;item.lastPaint=null;item.dirty=true;paint(performance.now());schedule();};
  node.dispose=()=> {tiles.delete(item);observer?.disconnect();item.fallback.onload=null;if(!tiles.size){if(raf)cancelAnimationFrame(raf);raf=null;renderer?.dispose();renderer=null;atlas=null;failed=false;}};
  return node;
}
reduced?.addEventListener?.('change',targetFrame);
document.addEventListener('visibilitychange',()=>{if(document.hidden){if(raf)cancelAnimationFrame(raf);raf=null;}else targetFrame();});
