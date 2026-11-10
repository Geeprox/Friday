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
