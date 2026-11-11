import {createSourceBodyDiagram} from './body-motions.js';
const root=document.getElementById('gameTutorial');let diagram=null,key=null;
const names={'pose.horizontal':'双臂平举','pose.overhead':'双手上举','exercise.squat':'下蹲后站起','exercise.jack':'开合步','exercise.high_knee':'交替抬膝','exercise.side_lunge':'侧弓步','exercise.side_step':'左右侧迈','exercise.side_bend':'站姿侧倾','exercise.reach_up':'交替上伸','exercise.arm_open':'胸前开合','exercise.balance':'单腿站立','exercise.cross_knee':'交叉触膝'};
function update(event){
 const state=event.detail;
 if(!root)return;
 root.hidden=!state.key||!state.visible;root.dataset.mode=state.mode;
 if(state.key&&key!==state.key){diagram?.dispose();key=state.key;diagram=createSourceBodyDiagram(key,{name:names[key]||'动作',transparentBackground:true});const caption=document.createElement('p');caption.textContent='动作示范';root.replaceChildren(diagram,caption);root.dataset.teachingKey=key;}
 diagram?.setPaused(state.paused);
}
document.addEventListener('friday:game-teaching',update);
window.addEventListener('pagehide',()=>{document.removeEventListener('friday:game-teaching',update);diagram?.dispose();},{once:true});
