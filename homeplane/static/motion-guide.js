import {createSourceHandDiagram} from './hand-motions.js';
import {createSourceBodyDiagram} from './body-motions.js';
/* Instructional diagrams are never camera evidence. Hand geometry is sourced. */
const moves = [
 ['hand.ok','OK','拇指与食指轻触成圈，其余手指伸开。','hand','保持片刻'],
 ['hand.yeah','剪刀手','伸出食指与中指，其余手指收起。','hand','保持片刻'],
 ['hand.open_palm','张开手掌','五指展开，掌心朝向镜头。','hand','保持片刻'],
 ['hand.point','指向','伸出食指，其余手指收起。','hand','保持片刻'],
 ['hand.fist','握拳','四指收拢，拇指轻放在拳头外侧。','hand','保持片刻'],
 ['hand.thumb_up','点赞','收起四指，伸直拇指并朝上。','hand','保持片刻'],
 ['hand.thumb_down','拇指向下','收起四指，伸直拇指并朝下。','hand','保持片刻'],
 ['hand.shaka','电话手势','伸开拇指与小指，其余三指收起。','hand','保持片刻'],
 ['hand.horns','摇滚手势','伸出食指与小指，拇指、中指和无名指收起。','hand','保持片刻'],
 ['body.arms_open','双臂打开','双手先在胸前合拢，再水平向两侧打开。','body','先合再开'],
 ['body.arms_close','双臂合拢','双臂先向两侧展开，再水平向胸前合拢。','body','先开再合'],
 ['body.hands_up','双手举高','双手举过肩部，保持清晰入镜。','body','保持片刻'],
 ['body.left_hand_up','举起左手','举起左手，右手自然放下。','body','保持片刻'],
 ['body.right_hand_up','举起右手','举起右手，左手自然放下。','body','保持片刻'],
 ['body.squat','下蹲','让双膝与脚踝入镜，屈膝下蹲并保持。','body','保持片刻'],
 ['body.lean_left','向左倾身','保持双脚站稳，上身向左侧倾。','body','保持片刻'],
 ['body.lean_right','向右倾身','保持双脚站稳，上身向右侧倾。','body','保持片刻'],
 ['pose.horizontal','双臂平举','双臂向两侧平举，保持在肩部附近。','game','保持片刻'],
 ['pose.overhead','双手上举','双手举高，保持在肩部以上。','game','保持片刻'],
 ['body.lateral','横向移动','先自然站立，再向左或向右迈步。','game','左右移动'],
 ['body.arm_effort','交替摆臂','双臂前后交替摆动，像滑雪撑杖。','game','连续动作'],
 ['body.swing','挥拍','抬起一只手臂，向侧前方挥动。','game','完成一次'],
 ['exercise.squat','深蹲起立','先站直半秒，屈膝下蹲，再站直。','game','往返一次'],
 ['exercise.jack','开合动作','双脚打开、双手举高，再收脚放手；也可做开合步。','game','往返一次'],
 ['exercise.high_knee','交替抬膝','先自然站立，抬起一侧膝盖，再放回。','game','往返一次'],
 ['exercise.side_lunge','侧弓步','向一侧迈开并屈膝，再回到中间。','game','往返一次'],
 ['exercise.side_step','左右侧迈','自然站定后，向一侧迈步并回到中间，左右交替。','game','往返一次'],
 ['exercise.side_tap','侧点步','一脚站稳，另一脚向侧面轻点地，再收回来。','game','收脚计一次'],
 ['exercise.side_bend','站姿侧倾','双脚站稳，上身向一侧轻倾，再回到正中，左右交替。','game','往返一次'],
 ['exercise.reach_up','交替上伸','一只手向上伸展，再放回身侧，左右交替。','game','放回计一次'],
 ['exercise.arm_open','胸前开合','双手在胸前靠拢，平举打开，再合回胸前。','game','往返一次'],
 ['exercise.balance','单腿站立','轻抬一脚，站稳 1 秒后落地，左右换脚。','game','站稳再落脚'],
 ['exercise.cross_knee','交叉触膝','抬膝，用对侧手靠近膝盖，再放下换侧。','game','放回计一次'],
 ['exercise.side_raise','交替侧抬腿','一只脚向侧面轻抬，停一下再落地，左右交替。','game','落脚计一次'],
 ['exercise.overhead_clap','头顶合掌','举起并打开双臂，在头顶合掌，再打开双臂。','game','再次打开计一次'],
];
export const MOTION_GUIDES=Object.fromEntries(moves.map(([id,name,instruction,group,timing])=>[id,{id,name,instruction,group,timing}]));
export function createMotionDiagram(key,{label=true}={}){
 const guide=MOTION_GUIDES[key]||MOTION_GUIDES['body.hands_up'];
 return guide.group==='hand'?createSourceHandDiagram(guide.id,{label,name:guide.name}):createSourceBodyDiagram(guide.id,{label,name:guide.name});
}
export function motionTile(key,onTry){const guide=MOTION_GUIDES[key];if(!guide)return null;const tile=document.createElement('article');tile.className='motion-tile';const diagram=createMotionDiagram(key,{label:false}),name=document.createElement('h3'),timing=document.createElement('span');name.textContent=guide.name;timing.textContent=guide.timing;timing.className='motion-timing';tile.append(diagram,name,timing);if(onTry){const button=document.createElement('button');button.type='button';button.textContent='试一试';button.setAttribute('aria-label','体验'+guide.name);button.onclick=()=>onTry(key);tile.append(button);}tile.dispose=()=>diagram.dispose();return tile;}
