const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this._text='';}
 set textContent(v){this._text=v;this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);}
 append(...cs){cs.forEach(n=>{this.children.push(n);n.parent=this;});}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
}
(async()=>{
 const root=new Element('main'),all=n=>[n,...n.children.flatMap(all)],button=()=>all(root).find(n=>n.tagName==='button');
 let policy={revision:5,enabled:true,recognition:true,interaction:false,auto_open_cameras:true},tick,finish,delay=false,fail=false,cleared=false;
 const writes=[],settle=()=>new Promise(setImmediate);
 const context=vm.createContext({document:{createElement:t=>new Element(t)},setInterval:fn=>{tick=fn;return 1;},clearInterval(){cleared=true;},fetch:async(path,options)=>{
  assert.equal(path,'/api/embodied/infrastructure');
  if(options.method==='POST'){
   const body=JSON.parse(options.body);writes.push(body);
   assert.equal(body.revision,policy.revision);assert.equal(body.auto_open_cameras,false);assert.equal(body.interaction,false);
   if(delay)await new Promise(r=>finish=r);
   if(fail){policy={...policy,revision:policy.revision+1};return{ok:false};}
   policy={...body,revision:body.revision+1};
  }
  return{ok:true,json:async()=>({policy:{...policy},state:policy.enabled?'supervising':'paused',failure:null})};
 }});
 vm.runInContext(fs.readFileSync('homeplane/static/camera-privacy.js','utf8').replace('export function','function'),context);
 context.root=root;const owner=vm.runInContext('mountCameraPrivacy(root)',context);await settle();
 assert.equal(writes.length,0,'Mount and polling must never enable capture');await tick();assert.equal(writes.length,0);
 assert.equal(button().textContent,'暂停摄像头感知');
 delay=true;const pending=button().onclick();await settle();assert(button().disabled);await button().onclick();assert.equal(writes.length,1,'No duplicate writes');finish();await pending;delay=false;
 assert.equal(policy.enabled,false);assert.equal(button().textContent,'恢复摄像头感知');assert.match(root.textContent,/重启 Friday 后仍保持暂停/);
 await button().onclick();assert.equal(policy.enabled,true);assert.equal(policy.auto_open_cameras,false);
 fail=true;await button().onclick();assert.match(root.textContent,/操作结果待确认/);fail=false;
 await button().onclick();assert.equal(policy.enabled,false,'Readback after conflict updates revision without automatic resumption');
 delay=true;const late=button().onclick();await settle();owner.dispose();finish();await late;
 assert(cleared&&root.children.length===0,'Late save must not recreate disposed UI');
 console.log('PASS persistent pause, explicit safe resume, revision conflict, duplicate save and disposed UI');
})().catch(e=>{console.error(e);process.exitCode=1;});
