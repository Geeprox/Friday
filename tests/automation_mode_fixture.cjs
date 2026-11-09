const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.textContent='';}
 append(...nodes){this.children.push(...nodes);}
 setAttribute(k,v){this.attrs[k]=String(v);}
 remove(){this.removed=true;}
}
const walk=n=>[n,...n.children.flatMap(walk)];
(async()=>{
 const root=new Element('main'),calls=[],notices=[];let poll;
 let report={revision:7,enabled:false,mode_source:{state:'unconfigured',context:'a'.repeat(64),mode:null},runtime:{available:true,automatic_enabled:false,mode_window_current:false,execution_state:'paused',modes:{}}};
 const api=async(path,body)=>{if(body){calls.push({path,body});if(path.endsWith('/mode')){assert.equal(body.expected_context,report.mode_source.context);report.mode_source={state:'current',context:'b'.repeat(64),mode:body.mode,persistent:true};report.runtime={...report.runtime,mode_window_current:true,modes:{'home.sleep':{value:body.mode==='sleep'},'home.manual':{value:body.mode==='manual'}},execution_state:'paused'};}else{assert.equal(body.expected_revision,report.revision);report={...report,revision:8,enabled:body.automatic_running};}return{};}return structuredClone(report);};
 const context=vm.createContext({document:{createElement:t=>new Element(t),hidden:false},setInterval:fn=>(poll=fn,1),clearInterval(){},api,root,toast:(...args)=>notices.push(args)});
 vm.runInContext(fs.readFileSync('homeplane/static/automation-mode.js','utf8').replace('export function','function'),context);
 const owner=vm.runInContext('mountAutomationMode(root,{api,toast})',context),settle=()=>new Promise(setImmediate);
 await settle();assert.equal(calls.length,0,'Browsing does not establish a household mode or resume automation');
 const button=text=>walk(root).find(n=>n.tagName==='button'&&n.textContent===text);
 await button('休息').onclick();await settle();assert.equal(calls[0].path,'/api/home/mode');assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{mode:'sleep',expected_context:'a'.repeat(64)});assert.equal(report.enabled,false,'Choosing a mode preserves an explicit automatic pause');
 assert.equal(button('休息').attrs['aria-pressed'],'true');
 button('自动运行已暂停').onclick();await settle();assert.equal(calls[1].path,'/api/home/preferences');assert.equal(calls[1].body.expected_revision,7);assert.equal(calls[1].body.automatic_running,true);
 report.runtime.mode_window_current=false;await poll();await settle();assert.equal(button('休息').attrs['aria-pressed'],'true','A saved choice remains selected while short execution leases recover');
 report.mode_source.state='changed';await poll();await settle();assert.ok(walk(root).filter(n=>n.attrs['aria-pressed']).every(n=>n.attrs['aria-pressed']==='false'),'Changed source cannot present the old choice as effective');
 const count=calls.length;owner.dispose();await poll();await settle();assert.equal(calls.length,count);assert.equal(root.children[0].removed,true);
 console.log('Explicit household mode, preserved pause, revision-checked automatic preference and expiry: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
