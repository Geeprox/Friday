// Actual view lifecycle with an in-memory DOM and synthetic responses only.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.style={};this.classList={add(){}};this.attributes={};this.value='';}
 setAttribute(k,v){this.attributes[k]=String(v);if(k==='value')this.value=v;}
 append(...items){for(const child of items){child.parent=this;this.children.push(child);}}
 replaceChildren(...items){this.children=[];this.append(...items);}
 addEventListener(key,handler){(this.events||={})[key]=handler;}
 showModal(){this.open=true;}
 close(){this.open=false;}
 focus(){this.ownerDocument.activeElement=this;}
 remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
 querySelector(tag){return all(this).find(n=>n!==this&&n.tagName===tag);}
}
const all=n=>[n,...n.children.flatMap(all)];
async function main(){
 let poll;const requests=[];let response={revision:0,cameras:[],profiles:[],observations:[],candidates:[{id:'candidate_synthetic',portrait_revision:100}],recognition:{state:'live',tracks:[]}};
 const root=new Element('div'),document={activeElement:null,createElement:t=>{const n=new Element(t);n.ownerDocument=document;return n;}};const context=vm.createContext({document,Date,Map,Set,console,
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
 response={...response,profiles:[{person_id:'person_synthetic',name:'Synthetic',enrolled:true}],candidates:[]};await poll();
 let forget=all(root).find(n=>n.tagName==='button'&&n.textContent==='删除人脸登记');forget.focus();await poll();
 forget=all(root).find(n=>n.tagName==='button'&&n.textContent==='删除人脸登记');assert.equal(document.activeElement,forget);
 const posts=()=>requests.filter(r=>r.options?.method==='POST').length,before=posts();forget.onclick();
 let dialog=all(root).find(n=>n.tagName==='dialog');assert(dialog.open);assert.equal(posts(),before);
 all(dialog).find(n=>n.textContent==='取消').onclick();assert(!all(root).some(n=>n.tagName==='dialog'));assert.equal(posts(),before);assert.equal(document.activeElement,forget);
 forget.onclick();dialog=all(root).find(n=>n.tagName==='dialog');dialog.events.cancel({preventDefault(){}});assert.equal(posts(),before);
 forget.onclick();dialog=all(root).find(n=>n.tagName==='dialog');all(dialog).find(n=>n.tagName==='button'&&n.textContent==='删除人脸登记').onclick();await new Promise(setImmediate);
 assert.equal(posts(),before+1);assert(requests.at(-2).path.endsWith('/forget'));
 ui.destroy();assert.equal(root.children.length,0);console.log('candidate drafts, updated portraits, visible failure, default hidden preview and background ownership passed');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
