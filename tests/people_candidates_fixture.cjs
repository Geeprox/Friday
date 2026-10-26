// Actual view lifecycle with an in-memory DOM and synthetic responses only.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.style={};this.classList={add(){}};this.attributes={};this.value='';}
 setAttribute(k,v){this.attributes[k]=String(v);if(k==='value')this.value=v;}
 append(...items){for(const child of items){child.parent=this;this.children.push(child);}}
 replaceChildren(...items){this.children=[];this.append(...items);}
 addEventListener(){}
 remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
 querySelector(tag){return all(this).find(n=>n!==this&&n.tagName===tag);}
}
const all=n=>[n,...n.children.flatMap(all)];
async function main(){
 let poll;const requests=[];let response={revision:0,cameras:[],profiles:[],observations:[],candidates:[{id:'candidate_synthetic',portrait_revision:100}],recognition:{state:'live',tracks:[]}};
 const root=new Element('div');const context=vm.createContext({document:{createElement:t=>new Element(t)},Date,Map,Set,console,
  setInterval:f=>{poll=f;return 1;},clearInterval(){},fetch:async(path,options)=>{requests.push({path,options});return {ok:options?.method!=='POST',json:async()=>options?.method==='POST'?{detail:{code:'people_name_conflict'}}:response};}});
 vm.runInContext(fs.readFileSync('homeplane/people/static/live.js','utf8').replace('export function','function'),context);
 const ui=context.mountLivePeople(root);await new Promise(setImmediate);
 const candidate=all(root).find(n=>n.attributes.class==='live-candidate'),input=candidate.querySelector('input');input.value='Draft';
 const hidden=all(root).find(n=>n.attributes.class==='live-workspace');assert.equal(hidden.hidden,true);
 response={...response,revision:1,candidates:[{id:'candidate_synthetic',portrait_revision:200}]};await poll();
 assert.equal(candidate.querySelector('input'),input);assert.equal(input.value,'Draft');assert(candidate.querySelector('img').src.endsWith('v=200'));
 candidate.onsubmit({preventDefault(){}});await new Promise(setImmediate);
 assert(requests.some(r=>r.path.endsWith('/name-candidate')&&JSON.parse(r.options.body).expected_revision===1));
 const notice=all(root).find(n=>n.attributes.role==='status');assert(notice.textContent.includes('名字已存在'));assert.equal(notice.parent,root);assert.equal(input.value,'Draft');
 assert(!requests.some(r=>/embodied\/(start|stop)/.test(r.path)));
 ui.destroy();assert.equal(root.children.length,0);console.log('candidate drafts, updated portraits, visible failure, default hidden preview and background ownership passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
