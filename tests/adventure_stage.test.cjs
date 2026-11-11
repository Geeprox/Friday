const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),{execFileSync}=require('node:child_process');
const source=fs.readFileSync('homeplane/embodied/static/games.js','utf8');
const stages=JSON.parse(execFileSync(process.env.FRIDAY_TEST_PYTHON||'.venv/bin/python',['-B','-c','import json;from homeplane.embodied.quest import STAGES;print(json.dumps([s.public() for s in STAGES]))'],{encoding:'utf8'}));
function setup(){
 const nodes=new Map();
 const create=()=>({textContent:'',innerHTML:'',dataset:{},style:{},children:[],setAttribute(k,v){this[k]=v;},replaceChildren(...values){this.children=values;}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,create());return nodes.get(id);};
 const scope=vm.createContext({selected:'quest',$:node,document:{createElement:create}});
 vm.runInContext(source.slice(source.indexOf('function completedGame('),source.indexOf('function gamePhaseMessage(')),scope);
 return{scope,node};
}
const game=(level,extra={})=>({round_id:1,status:level===stages.length?'complete':'playing',quest:{level,stages,total_levels:stages.length,phase:level===stages.length?'complete':'work',hold:0,reps:0,...extra}});

test('every route station presents the actual rule instruction and source teaching key',()=>{
 const{scope}=setup();
 for(const[level,stage]of stages.entries()){
  const view=scope.questPresentation(game(level));
  assert.equal(view.title,stage.title);assert.equal(view.instruction,stage.instruction);
  assert.equal(view.teaching,stage.teaching);assert.equal(view.total,10);
  assert.equal(view.progress,level/10);
 }
 const view=scope.questPresentation(game(10));
 assert.equal(view.progress,1);assert.equal(view.teaching,null);assert.equal(view.instruction,'');
 assert.equal(view.announcement,'10 站山径已完成');
});

test('route progress never rewinds between a finished station and the next one',()=>{
 const{scope}=setup();let before=0;
 for(const[level,stage]of stages.entries())for(const ratio of[0,.5,.99]){
  const view=scope.questPresentation(game(level,{hold:stage.hold*ratio,reps:stage.reps*ratio}));
  assert(view.progress>=before);assert(view.progress<1);before=view.progress;
 }
 assert(scope.questPresentation(game(10)).progress>=before);
});

test('preparation previews the next action but cannot display earned reps',()=>{
 const{scope}=setup();const view=scope.questPresentation(game(3,{phase:'transition',transition_seconds:2.21}));
 assert.equal(view.title,'下一站 · 左右侧迈');assert.equal(view.live,'3 秒后开始');
 assert.equal(view.teaching,'exercise.side_step');assert.equal(view.progress,3/10);
});

test('HUD gives each station a non-color state and announces only station or phase changes',()=>{
 const{scope,node}=setup();scope.updateQuestHUD(game(3));
 const children=node('questTrail').children;assert.equal(children.length,10);
 assert.deepEqual(children.flatMap((n,i)=>n.dataset.chapterStart==='true'?[i]:[]),[3,6]);
 assert.equal(node('questTrail')['aria-label'],'山径路线，当前：溪畔步道');
 assert(children.slice(0,3).every(n=>n.dataset.state==='complete'&&n['aria-label'].includes('已完成')));
 assert.equal(children[3]['aria-current'],'step');assert.equal(children[4].dataset.state,'upcoming');
 const notice=node('questAnnouncement').textContent;
 scope.updateQuestHUD(game(3,{reps:1}));
 assert.equal(node('questTrail').children,children);assert.equal(node('questAnnouncement').textContent,notice);
 assert.equal(node('liveProgress').textContent,'已完成 1 / 4 次');
 scope.updateQuestHUD(game(4,{phase:'transition',transition_seconds:2}));
 assert.notEqual(node('questTrail').children,children);
 assert.equal(node('questAnnouncement').textContent,'下一站：交替抬膝。'+stages[4].instruction);
});

test('an older running three-station backend keeps its actual route and completion count',()=>{
 const{scope}=setup(),old={round_id:2,status:'playing',quest:{level:1,hold:1.5,reps:0,targets:['horizontal','overhead','squat']}};
 const view=scope.questPresentation(old);assert.equal(view.total,3);assert.equal(view.progress,.5);
 old.status='complete';old.quest.level=3;old.quest.reps=3;
 assert.equal(scope.questPresentation(old).announcement,'3 站山径已完成');
 assert(scope.completedGame(old)[1].includes('3 站山径'));
});

test('the same numbered station refreshes source chapter labels after context replacement',()=>{
 const{scope,node}=setup(),old=game(3);old.control_context='a'.repeat(32);
 scope.updateQuestHUD(old);const children=node('questTrail').children;
 const current=structuredClone(old);current.control_context='b'.repeat(32);
 current.quest.stages=current.quest.stages.map(s=>({...s,chapter:s.chapter==='溪畔步道'?'溪边小径':s.chapter}));
 scope.updateQuestHUD(current);
 assert.notEqual(node('questTrail').children,children);
 assert.equal(node('questTrail')['aria-label'],'山径路线，当前：溪边小径');
 assert(node('questTrail').children[3]['aria-label'].startsWith('溪边小径，'));
});
