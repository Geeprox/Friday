const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('homeplane/embodied/static/index.js', 'utf8');
const fields = Object.fromEntries(['object','imageX','imageY','mapX','mapY','editor','editerror'].map(id => [id, {
  value: '', attrs: {}, textContent: '', focused: false,
  setAttribute(k,v){this.attrs[k]=v;}, removeAttribute(k){delete this.attrs[k];},
  focus(){this.focused=true;}, querySelector(){return Object.values(fields).find(n=>n.attrs['aria-invalid']==='true');}
}]));
fields.object.value = 'anchor:0';
const draft = {anchors:[{image:[.1,.2],map:[.3,.4]}],seats:[]};
let redraws=0, requests=0;
const context=vm.createContext({$:id=>fields[id],draft,editing:true,Number,
  redrawFrozen(){redraws++;},fetch(){requests++;throw new Error('unexpected transport');}});
vm.runInContext(source.slice(source.indexOf('function updateCoordinateFields()'),source.indexOf('async function edit()')),context);
vm.runInContext(source.slice(source.indexOf('async function save()'),source.indexOf('setInterval(()=>')),context);
vm.runInContext('updateCoordinateFields()',context);
assert.equal(fields.imageX.value,'0.100');
fields.imageX.value='0.55';fields.mapY.value='1.1';
assert.equal(vm.runInContext('coordinateInput()',context),false);
assert.deepEqual(draft.anchors[0].image,[.1,.2],'an invalid form must not partially move a point');
assert.equal(redraws,0);
(async()=>{
  await vm.runInContext('save()',context);
  assert.equal(requests,0,'invalid coordinates must never reach calibration storage');
  assert.equal(fields.mapY.focused,true);
  fields.mapY.value='0.8';
  assert.equal(vm.runInContext('coordinateInput()',context),true);
  assert.deepEqual(draft.anchors[0].image,[.55,.2]);
  assert.deepEqual(draft.anchors[0].map,[.3,.8]);
  assert.equal(redraws,1);
  assert.equal(fields.editerror.textContent,'');
  console.log('Calibration fields: keyboard coordinates, atomic validation, invalid-save fence: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
