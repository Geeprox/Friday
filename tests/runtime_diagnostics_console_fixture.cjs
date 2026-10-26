'use strict';
// Executes the production diagnostics component with a finite DOM; no browser/network.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
class Element {
  constructor(tag,text=''){this.tag=tag;this.children=[];this.text=String(text??'');this.attrs={};this.isConnected=true;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;this.text='';}
  setAttribute(key,value){this.attrs[key]=value;}
  get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}
}
const walk=e=>[e,...e.children.flatMap(walk)];
const source=fs.readFileSync(path.join(process.argv[2],'homeplane/static/app.js'),'utf8');
const copySource=fs.readFileSync(path.join(process.argv[2],'homeplane/static/copy-layout.js'),'utf8');
const component=source.slice(source.indexOf('function renderDiagnostics('),source.indexOf('function scenarioSelector('));
async function scenario(report,expectText){
 const calls=[],root=new Element('main');
 const context=vm.createContext({page:'System',root,Array,Error,queueMicrotask,
  document:{documentElement:root,querySelectorAll:()=>[]},MutationObserver:class {observe(){}},
  el:(tag,text,attrs)=>Object.assign(new Element(tag,text),{attrs:attrs||{}}),
  add:(target,...nodes)=>{target.append(...nodes);return target;},
  button:(text,click,attrs)=>Object.assign(new Element('button',text),{click,attrs:attrs||{}}),
  detail:(text,value)=>new Element('details',text+JSON.stringify(value)),
  api:async url=>{calls.push(url);return url==='/api/diagnostics'?{redacted:true,server_retention_seconds:0,max_bytes:65536,automatic_upload:false}:report;}
 });
 vm.runInContext(copySource,context);
 vm.runInContext('const copyName = globalThis.FridayCopyLayout.name;'+component+';renderDiagnostics(root);',context);
 assert.equal(walk(root).find(e=>e.attrs.id==='diagnostics-export-download').textContent,'导出诊断');
 await new Promise(resolve=>setImmediate(resolve));
 await walk(root).find(e=>e.attrs.id==='runtime-diagnostics-read').click();
 assert(root.textContent.includes(expectText));
 assert.deepEqual(calls,['/api/diagnostics','/api/diagnostics/runtime']);
 assert(!root.textContent.includes('<script>'));
}
(async()=>{
 await scenario({schema:1,redacted:true,automatic_upload:false,disk_available:true,events:[{stage:'flow.turn',code:'none',correlation:'a'.repeat(32)}]},'flow.turn');
 await scenario({schema:1,redacted:true,automatic_upload:false,disk_available:false,events:[]},'日志文件暂不可写');
 await scenario({schema:1,redacted:false,automatic_upload:false,events:[]},'运行诊断暂不可用');
 await scenario({schema:1,redacted:true,automatic_upload:false,events:Array(65).fill({})},'运行诊断暂不可用');
 console.log(JSON.stringify({passed:4,failures:0,scope:'production renderDiagnostics component; synthetic finite DOM; no GUI/network'}));
})().catch(error=>{console.error(error);process.exit(1);});
