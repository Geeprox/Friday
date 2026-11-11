const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync('homeplane/static/body-motions.js','utf8');
const scope=vm.createContext({Math});
vm.runInContext(source.slice(source.indexOf('const smooth'),source.indexOf('export const hasSourceBodyDiagram')),scope);
const mapping=key=>vm.runInContext('mappings['+JSON.stringify(key)+']',scope);

test('alternate reach samples both original source poses through neutral,hold and return',()=>{
 const map=mapping('exercise.reach_up'),sample=p=>scope.alternatingSourceTime(p,...map.alternatingTimes);
 for(const[p,time]of [[0,.9],[.27,0],[.5,.9],[.75,1.8],[.99,.9]])assert.equal(sample(p),time);
 for(const edge of[.07,.23,.31,.45,.55,.71,.79,.93])assert(Math.abs(sample(edge-.000001)-sample(edge+.000001))<.0001);
});

test('chest open samples the real closed-to-open clip and returns to chest',()=>{
 const map=mapping('exercise.arm_open'),sample=p=>scope.sourceTime(p,...map.times);
 assert.equal(sample(0),.6);assert.equal(sample(.57),1.2333333492);assert.equal(sample(.99),.6);
});

test('new tutorials reuse licensed rig clips and exact existing static targets',()=>{
 const root='homeplane/static/motion-art/body',model=JSON.parse(fs.readFileSync(path.join(root,'body.gltf'))),targets=JSON.parse(fs.readFileSync(path.join(root,'targets.json'))).targets;
 for(const key of['exercise.reach_up','exercise.arm_open']){
  const map=mapping(key);assert(model.animations.some(a=>a.name===map.key));
  assert(targets[map.fallback]);assert(fs.statSync(path.join(root,targets[map.fallback].file)).isFile());
 }
});

test('camp tutorials use the same authored source poses in animation and reduced motion',()=>{
 const root='homeplane/static/motion-art/body';
 const model=JSON.parse(fs.readFileSync(path.join(root,'body.gltf')));
 const {targets}=JSON.parse(fs.readFileSync(path.join(root,'targets.json')));
 const clap=mapping('exercise.overhead_clap'),side=mapping('exercise.side_raise');
 const sample=p=>scope.sourceTime(p,...clap.times);
 assert.equal(sample(0),0);
 assert(sample(.27)>0 && sample(.27)<sample(.57));
 assert.equal(sample(.57),.5);
 assert.equal(sample(.99),0);
 assert.equal(clap.fromStart,true);
 assert.equal(side.mirrorCycle,true);
 for(const key of ['exercise.overhead_clap','exercise.side_raise']){
  const target=targets[key];
  assert.equal(mapping(key).key,target.key);
  assert(model.animations.some(clip=>clip.name===target.clip));
  assert(fs.statSync(path.join(root,target.file)).isFile());
 }
});
