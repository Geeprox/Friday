import {createSourceBodyDiagram} from './body-motions.js';
const regular=document.getElementById('gameTutorial'),ski=document.getElementById('skiWarmupDemo');
let root=null,diagram=null,key=null;
const names={'pose.horizontal':'双臂平举','pose.overhead':'双手上举','exercise.squat':'下蹲后站起','exercise.jack':'开合步','exercise.high_knee':'交替抬膝','exercise.side_lunge':'侧弓步','exercise.side_step':'左右侧迈','exercise.side_bend':'站姿侧倾','exercise.reach_up':'交替上伸','exercise.arm_open':'胸前开合','exercise.balance':'单腿站立','exercise.cross_knee':'交叉触膝','exercise.side_raise':'交替侧抬腿','exercise.overhead_clap':'头顶合掌'};
names['ski.side_step']='向侧面迈步';
names['exercise.side_tap']='侧点步';
names['body.arm_effort']='交替摆臂';
function update(event){
 const state=event.detail;
 const host=state.mode==='ski'?ski:regular;
 if(host!==root){
  if(root){root.hidden=true;root.replaceChildren();delete root.dataset.teachingKey;}
  diagram?.dispose();root=host;diagram=null;key=null;
 }
 if(!root)return;
 root.hidden=!state.key||!state.visible;root.dataset.mode=state.mode;
 if(key!==state.key){
  const previous=diagram;key=state.key;
  diagram=key?createSourceBodyDiagram(key,{name:names[key]||'动作',transparentBackground:true}):null;
  previous?.dispose();
  root.replaceChildren(...(diagram?[diagram]:[]));
  if(diagram&&root===regular){const caption=document.createElement('p');caption.textContent='动作示范';root.append(caption);}
  if(key)root.dataset.teachingKey=key;else delete root.dataset.teachingKey;
 }
 diagram?.setStepSide?.(state.stepSide);
 diagram?.setPaused(state.paused);
}
document.addEventListener('friday:game-teaching',update);
window.addEventListener('pagehide',()=>{document.removeEventListener('friday:game-teaching',update);diagram?.dispose();},{once:true});
