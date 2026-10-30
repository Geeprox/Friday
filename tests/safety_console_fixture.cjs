const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.textContent='';this.disabled=false;}
 setAttribute(k,v){this.attrs[k]=String(v);}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}
 click(){this.onclick?.();}
}
(async()=>{
 const root=new Element('main'),head=new Element('head'),all=n=>[n,...n.children.flatMap(all)];
 const find=(tag,text)=>all(root).find(n=>n.tagName===tag&&n.textContent===text),calls=[];
 let policy={enabled:false,announce:false,visual_review:false,revision:0},events=[],interval;
 const fakeEvent={id:'a'.repeat(32),at:1800000000,simulated:true,status:'test',message:'测试播报',review:'unreviewed',announcement:{status:'paused'},frames:[],camera:null};
 const fetch=async(path,options)=>{const body=options.body&&JSON.parse(options.body);calls.push({path,body});
   let value;
   if(path.endsWith('/policy')){policy={...body,revision:policy.revision+1};value=policy;}
   else if(path.endsWith('/review')){events[0]={...events[0],review:body.review};value=events[0];}
   else if(path.endsWith('/test')){events=[fakeEvent];value=fakeEvent;}
   else if(path.endsWith('/events'))value={events};
   else value={policy,worker:{cameras:[]},visual_review:{available:true,state:'idle'},capabilities:[{name:'疑似跌倒',status:'experimental',basis:'骨骼时序'},{name:'粗暴照护',status:'unavailable',basis:'尚未接入'}]};
   return {ok:true,json:async()=>value};
 };
 const document={head,createElement:t=>new Element(t),getElementById:id=>all(head).find(n=>n.attrs.id===id)};
 const context=vm.createContext({document,fetch,Date,JSON,console,root,setInterval:f=>(interval=f,1),clearInterval(){},setTimeout(){},Blob,URL});
 vm.runInContext(fs.readFileSync('homeplane/static/safety.js','utf8').replace('export function mountSafety','function mountSafety'),context);
 const owner=vm.runInContext('mountSafety(root)',context),settle=()=>new Promise(setImmediate);await settle();
 assert.equal(calls.length,2);assert(calls.every(c=>!c.body),'viewing safety must be silent and read-only');
 assert(find('span','尚未接入'));assert(find('p','监测已暂停'));
 await find('button','开启监测').onclick();assert.equal(policy.enabled,true);assert.equal(policy.announce,false);
 assert.equal(find('button','本机画面复核').disabled,false);
 await find('button','本机画面复核').onclick();assert.equal(policy.visual_review,true);
 assert.equal(find('button','本机画面复核').disabled,false,'successful refresh must restore the model switch');
 assert(calls.every(c=>!c.path.endsWith('/test')),'enabling visual review must never test or request speech');
 await find('button','开启语音提醒').onclick();assert.equal(policy.announce,true);
 await find('button','试听跌倒提醒').onclick();find('button','').onclick();
 assert(find('p','安静模式，未播报'));assert(find('p','播报测试不采集摄像头画面。'));
 await find('button','标记误报').onclick();assert.equal(events[0].review,'false_alarm');
 events=[{...fakeEvent,person_name:'<img src=x onerror=alert(1)>',simulated:false,message:'<unsafe>',frames:[{ordinal:0,at:1800000000}]}];interval();await settle();
 find('button','').onclick();assert(find('p','<unsafe>'));assert.equal(all(root).filter(n=>n.tagName==='img').length,1);
 assert(all(root).find(n=>n.tagName==='img').attrs.src.endsWith('/frames/0'));
 owner.dispose();assert.equal(root.children.length,0);interval();await settle();assert.equal(root.children.length,0);
 console.log('Safety UI: silent reads, policy revisions, explicit quiet test, review, safe names, keyframes, disposal: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
