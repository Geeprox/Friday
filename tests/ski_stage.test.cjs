const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('homeplane/embodied/static/games.js','utf8');
function presentation(g){const scope=vm.createContext({});vm.runInContext(source.slice(source.indexOf('function skiPresentation('),source.indexOf('function updateTennisHUD(')),scope);return scope.skiPresentation(g);}
const game=(extra={})=>({status:'playing',ski:{gate_count:18,gates_passed:3,speed:2,guide:{cue:'left'},...extra}});
test('live ski guidance describes actual steering and crossing targets',()=>{
 for(const[cue,text]of[['left','向左'],['right','向右'],['center','门中央'],['finish','终点']])assert(presentation(game({guide:{cue}})).instruction.includes(text));
 assert(presentation(game({speed:0})).instruction.includes('摆动双臂'));
 assert.equal(presentation(game()).detail,'穿门 3 / 18');
 assert.equal(presentation({...game(),status:'complete'}).instruction,'');
});
test('success, chain, miss and collision feedback reflects only fresh real events',()=>{
 assert.equal(presentation(game({event:{kind:'gate',combo:1,points:250,age:0}})).event,'穿门成功 · +250');
 assert.equal(presentation(game({event:{kind:'gate',combo:4,points:400,age:.6}})).event,'连续穿门 4 道 · +400');
 assert(presentation(game({event:{kind:'miss',age:0}})).event.includes('漏过'));
 assert(presentation(game({event:{kind:'collision',age:0}})).event.includes('碰到树木'));
 for(const age of[-1,1.6,100])assert.equal(presentation(game({event:{kind:'gate',combo:1,points:250,age}})).event,'');
 assert.equal(presentation({...game({event:{kind:'gate',combo:1,points:250,age:0}}),status:'paused'}).event,'');
});
test('a still-running older backend is not shown gates it cannot supply',()=>{
 const view=presentation({status:'playing',ski:{speed:1,passed:4}});
 assert(view.instruction.includes('障碍'));assert(!view.instruction.includes('木门'));assert(view.detail.includes('避开 4'));
});
test('the completed overlay uses the real section results and keeps the original replay control',()=>{
 const {makeClient,part,game:stageGame}=require('./game_stage_harness.cjs');
 const {c}=makeClient({selected:'ski',artError:false});
 vm.runInContext(part('function completedGame(', 'const legacyQuestStages='),c);
 const finished=stageGame({mode:'ski',status:'complete',ski:{points:4100,stars:2,course_count:3,
  gate_count:18,gates_passed:12,section_results:[
   {name:'林间初雪',gates_passed:6,gate_count:6},
   {name:'松林回弯',gates_passed:4,gate_count:6},
   {name:'山顶归途',gates_passed:2,gate_count:6},
  ]}});
 const markup=c.stageOverlayMarkup(finished,true,true);
 assert.match(markup,/4100 分.*2 星/);
 assert.match(markup,/林间初雪<\/dt><dd>6 \/ 6.*松林回弯<\/dt><dd>4 \/ 6.*山顶归途<\/dt><dd>2 \/ 6/);
 assert.match(markup,/data-click="startGame\(\)">再滑一程/);
 assert.doesNotMatch(c.stageOverlayMarkup({...finished,status:'playing'},true,true),/ski-splits/);
 delete finished.ski.section_results;
 assert.match(c.stageOverlayMarkup(finished,true,true),/穿门 12 \/ 18/);
});
test('the result picker keeps the round until a trail is explicitly selected and read back',async()=>{
 const {makeClient,part,game:stageGame}=require('./game_stage_harness.cjs');
 const options=[{id:'first-snow',name:'初雪坡',distance:54,gate_count:5},{id:'pine-slalom',name:'松林回转',distance:92,gate_count:8}];
 const finished=stageGame({mode:'ski',selected_mode:'ski',status:'complete',ski_trail:'first-snow',ski_trails:options,
  ski:{points:2000,stars:3,course_count:1,time_seconds:25.34,section_results:[{name:'初雪坡',gates_passed:5,gate_count:5}]}});
 const next={...finished,mode:'off',status:'off',control_revision:2,round_id:2,ski_trail:'pine-slalom'};
 const writes=[];let mismatch=true;
 const h=makeClient({selected:'ski',meta:{game:finished},artError:false,fetch:async(_path,request)=>{
  writes.push(JSON.parse(request.body));return{ok:true,json:async()=>({...next,ski_trail:mismatch?'first-snow':next.ski_trail})};
 }});
 const {c}=h;vm.runInContext(part('function completedGame(', 'const legacyQuestStages='),c);
 let markup=c.stageOverlayMarkup(finished,true,true);
 assert.match(markup,/初雪坡 · 成绩.*25\.3 秒.*穿门<\/dt><dd>5 \/ 5/);
 assert.match(markup,/再滑一程.*换雪道/);
 assert.equal(await c.chooseSkiTrail('pine-slalom'),false);
 c.toggleSkiChooser();markup=c.stageOverlayMarkup(finished,true,true);
 assert.match(markup,/更换雪道.*aria-pressed="true"><strong>初雪坡.*返回成绩/);
 assert.equal(writes.length,0);
 c.toggleSkiChooser();assert.match(c.stageOverlayMarkup(finished,true,true),/初雪坡 · 成绩/);
 c.toggleSkiChooser();assert.equal(await c.chooseSkiTrail('pine-slalom'),false);
 assert.equal(c.meta.game.round_id,finished.round_id);
 mismatch=false;
 assert.equal(await c.chooseSkiTrail('pine-slalom'),true);
 assert.deepEqual(writes.at(-1),{mode:'ski',action:'select',ski_trail:'pine-slalom',
  expected_context:finished.control_context,expected_revision:finished.control_revision});
 assert.equal(c.meta.game.ski_trail,'pine-slalom');
 assert.equal(c.meta.game.mode,'off');
 assert.match(c.stageOverlayMarkup(c.meta.game,true,false),/aria-pressed="true"><strong>松林回转/);
 h.setNow(1600);assert.equal(await c.chooseSkiTrail('first-snow'),false);assert.equal(writes.length,2);
});
