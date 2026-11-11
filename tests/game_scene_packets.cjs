/* Actual public producer packets, including deliberate removal of paused input. */
const{execFileSync}=require('node:child_process');
module.exports=function originalPausePackets(mode){
 return JSON.parse(execFileSync(process.env.FRIDAY_TEST_PYTHON||'.venv/bin/python',['-B','-c',`
import json,math
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_motion_games import human
g=MotionGames();g.reset('${mode}')
for i in range(64):
 people=[]
 for track in range(1,3 if '${mode}'=='tennis' else 2):
  center=(.32 if track==1 else .68)if '${mode}'=='tennis'else .5
  p=human(track,x=center+((.08 if track==1 else -.08)if i>20 else 0))
  for side,offset in [('left',0),('right',math.pi)]:p['joints'][side+'Wrist']['y']=.44+math.sin(i*.4+offset)*.2
  people.append(p)
 g.update(people,{'status':'locked'},i*.05)
live=g.public
paused=g.set_paused(True)
assert live['players'] and not paused['players']
print(json.dumps([live,paused]))
`],{encoding:'utf8'}));
};
