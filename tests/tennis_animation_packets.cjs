/* Synthetic joints pass through the actual BodyMotion and MotionGames producer. */
const{execFileSync}=require('node:child_process');
module.exports=function(receiver=0){return JSON.parse(execFileSync('.venv/bin/python',['-B','-c',`
import copy,json
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_motion_games import human, LOCK
game=MotionGames();game.reset('tennis',difficulty='challenge')
for i in range(30):game.update([human(1,x=.3),human(2,x=.7)],LOCK,i*.05)
assert game.public['status']=='playing'
game.round_pause=0;now=game.previous
start=.09+game.ball['speed']*.02
game.ball.update(x=.5,target_x=.5,z=start if ${receiver}==0 else 1-start,toward=${receiver})
before=copy.deepcopy(game.publish(game.public['players'],'playing',''))
people=[human(1,x=.3),human(2,x=.7)]
player=next(p for p in people if p['id']==game.slots[${receiver}])
player['joints']['rightWrist']['x']=.9
lead=copy.deepcopy(game.update(people,LOCK,now+.02))
hit=copy.deepcopy(game.update(people,LOCK,now+.14))
assert lead['players'][${receiver}]['swings']>before['players'][${receiver}]['swings']
assert lead['tennis']['rally']==0 and hit['tennis']['rally']==1
print(json.dumps(dict(before=before,lead=lead,hit=hit)))
`],{encoding:'utf8'}));};
