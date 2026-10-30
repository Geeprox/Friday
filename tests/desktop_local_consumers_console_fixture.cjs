'use strict';
// Actual Console components in a finite DOM harness. No browser/network/service.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.events={};this._text='';const classes=new Set();this.classList={add:v=>classes.add(v),remove:v=>classes.delete(v),contains:v=>classes.has(v)};}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
  set innerHTML(_){throw Error('Provider HTML must never execute');}
  setAttribute(k,v){this.attrs[k]=String(v);}
  getAttribute(k){return this.attrs[k];}
  focus(){this.ownerDocument.activeElement=this;}
  setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this._text='';this.children=[...children];}
  addEventListener(k,v){this.events[k]=v;}
}
const walk=node=>[node,...node.children.flatMap(walk)];
const find=(root,label)=>walk(root).find(n=>n.tag==='button'&&n.textContent===label);
function setup(file, mount, fetch, options={}) {
  const root=new Element('section'), document={activeElement:null,createElement:tag=>{const element=new Element(tag);element.ownerDocument=document;return element;}};
  const copyDOM=require('./copy_layout_dom.cjs').install(Element,document,root);
  const localFetch=async(url,request)=>url==='/api/people/live'?{ok:true,json:async()=>({profiles:[],observations:[],cameras:[],recognition:{state:'stopped'}})}:fetch(url,request);
  const context=vm.createContext({...copyDOM,document,fetch:localFetch,AbortController,DOMException,Set,Map,console,setInterval:()=>0,clearInterval(){},scrollY:0,scrollTo:({top})=>{context.scrollY=top;}});
  const source=fs.readFileSync(path.join(process.argv[2],file),'utf8').replace(/^import '[^']*copy-layout.js';\n/m,'').replace(/^import \{mountLivePeople\} from '\.\/live.js';\n/m,'').replace(`export function ${mount}`,`function ${mount}`);
  vm.runInContext(fs.readFileSync(path.join(process.argv[2],'homeplane/static/copy-layout.js'),'utf8'),context);
  if(file==='homeplane/people/static/people.js') {
    const live=fs.readFileSync(path.join(process.argv[2],'homeplane/people/static/live.js'),'utf8').replace('export function mountLivePeople','function mountLivePeople');
    vm.runInContext('{'+live+';globalThis.mountLivePeople=mountLivePeople;}',context);
  }
  vm.runInContext(source,context);context.root=root;context.options=options;
  return {root,view:vm.runInContext(`${mount}(root,options)`,context)};
}
const peopleFile='homeplane/people/static/people.js', filesFile='homeplane/static/file-steward.js';
async function main(){
  const peopleCalls=[], empty={revision:0,people:[],clusters:[],samples:[],voice_links:[],last_run:null,undo_available:true};
  const people=setup(peopleFile,'mountPeople',async(url,request)=>{peopleCalls.push({url,request});return {ok:true,json:async()=>empty};},{readOnly:true});
  await people.view.ready;
  assert(people.root.textContent.includes('人物目录'));
  assert(people.root.textContent.includes('人物识别接入与核对修改待启用'));
  assert(!people.root.textContent.includes('合成样本'));
  assert(find(people.root,'撤销修改').disabled);
  assert(!find(people.root,'合并人物')); // Empty catalog exposes no merge action.
  await find(people.root,'撤销修改').events.click(); // Direct dispatch also fails closed.
  assert(peopleCalls.every(c=>!c.request?.method));
  people.view.destroy();assert.equal(people.root.children.length,0);

  const sample={sample_id:'a'.repeat(64),state:'candidate',active:true};
  const page={...empty,samples:[sample,{sample_id:'b'.repeat(64),state:'excluded',active:false}],clusters:[{cluster_id:'SYNTHETIC-CLUSTER',person_id:null,sample_ids:[sample.sample_id],representatives:[sample.sample_id],state:'candidate'}]};
  const metadataCalls=[];
  const metadata=setup(peopleFile,'mountPeople',async(url,request)=>{metadataCalls.push({url,request});return {ok:true,json:async()=>page};},{readOnly:true});
  await metadata.view.ready;
  assert(metadata.root.textContent.includes('人物元数据只读'));
  assert(metadata.root.textContent.includes('几何占位'));
  assert(walk(metadata.root).filter(n=>n.tag==='form').every(n=>n.hidden));
  assert(walk(metadata.root).filter(n=>n.attrs.type==='checkbox').every(n=>n.disabled));
  const excluded=walk(metadata.root).find(n=>n.tag==='button'&&n.textContent.startsWith('已排除'));
  excluded.events.click();assert(find(metadata.root,'恢复样本').disabled);
  assert(metadata.root.textContent.includes('已排除的登记记录'));
  await find(metadata.root,'恢复样本').events.click();
  assert(metadataCalls.every(c=>!c.request?.method));metadata.view.destroy();
  const synthetic=setup(peopleFile,'mountPeople',async()=>({ok:true,json:async()=>({...empty,last_run:{backend:'synthetic-contract-fake'}})}),{readOnly:true});
  await synthetic.view.ready;assert(synthetic.root.textContent.includes('合成样本'));synthetic.view.destroy();

  // Simulated contract facts, independent of the clustering implementation.
  for (const flags of [{simulated:true},{last_run:{backend:'scikit-learn',simulated:true}},{samples:page.samples.map(sample=>({...sample,simulated:true}))}]) {
    const simulated=setup(peopleFile,'mountPeople',async()=>({ok:true,json:async()=>({...page,...flags})}),{readOnly:true});
    await simulated.view.ready;
    assert(simulated.root.textContent.includes('合成观察目录'));
    assert(simulated.root.textContent.includes('1 个合成样本'));
    assert(!simulated.root.textContent.includes('摄像头观察'));
    simulated.view.destroy();
  }
  let failedRead=true;
  const recovery=setup(peopleFile,'mountPeople',async()=>failedRead?{ok:false}:{ok:true,json:async()=>empty},{readOnly:true});
  await recovery.view.ready;assert(recovery.root.textContent.includes('人物记录暂时无法读取'));
  failedRead=false;await recovery.view.refresh();
  assert(!recovery.root.textContent.includes('人物记录暂时无法读取'));
  assert(recovery.root.textContent.includes('人物目录'));
  recovery.view.destroy();

  const context={catalog:{fingerprint:'c'.repeat(64),source_online:false,simulated:false},session:{reason:'connection_required',label:'需连接 NAS'}};
  const policy={label:'保留原样',source:'default_preserve',matched_rule_ids:[]};
  const rows=[{item:{item_id:'a'.repeat(64),original_hash:'a'.repeat(64),status:'ready',media_kind:'other',size_bytes:12},title:'SYNTHETIC-METADATA-ROW',extraction_status:'disabled',policy,references:[]}];
  const setting={fingerprint:'d'.repeat(64),saved:false,settings:{directory_ref:'SYNTHETIC-DIRECTORY',watched:false,extraction_mode:'metadata_only',budget:{}},rule:{label:'SYNTHETIC-DIRECTORY'},effective_policy:{source:'default_preserve'}};
  const plan={status:'blocked',counts:{selected:1,protected:1,collision:0},entries:[{before_relative_path:'SYNTHETIC.txt',destination:'SYNTHETIC-next.txt',policy_label:'保留原样',blockers:[]}]};
  const fileCalls=[];let connections=0;
  const state={tab:'Files',query:'',selected:new Set(['a'.repeat(64)]),destinations:new Map()};
  const files=setup(filesFile,'mountFiles',async(url,request)=>{
    fileCalls.push({url,request});return {ok:true,json:async()=>url.endsWith('/context')?context:url.endsWith('/query')&&!url.endsWith('/settings/query')?{items:rows,next_cursor:null}:url.endsWith('/settings/query')?setting:[plan]};
  },{readOnly:true,state,onOpenConnections:()=>connections++});
  await files.view.ready;
  assert(files.root.textContent.includes('需连接 NAS'));
  assert(files.root.textContent.includes('当前只读'));
  assert(walk(files.root).filter(n=>n.attrs.type==='checkbox').every(n=>n.disabled));
  assert(!find(files.root,'规划提取'));
  assert(!find(files.root,'查看命名建议'));
  find(files.root,'核对连接').events.click();assert.equal(connections,1);
  await find(files.root,'目录设置').events.click();
  assert(files.root.textContent.includes('目录设置修改与内容提取待启用'));
  assert(!walk(files.root).some(n=>n.tag==='form'));
  assert(!find(files.root,'保存设置'));
  find(files.root,'返回详情').events.click();assert(files.root.textContent.includes('原件内容'));
  find(files.root,'审核').events.click();
  assert(!find(files.root,'确认预览'));assert(!find(files.root,'取消此批'));
  assert(fileCalls.some(c=>c.url.endsWith('/settings/query')&&c.request?.method==='POST'));
  assert(fileCalls.filter(c=>c.request?.method==='POST').every(c=>['/api/files/query','/api/files/settings/query'].includes(c.url)));
  files.view.destroy();assert.equal(files.root.children.length,0);

  // Preserve the known connection reason when the subsequent catalog read fails.
  const unavailable=setup(filesFile,'mountFiles',async url=>url.endsWith('/context')?{ok:true,json:async()=>context}:{ok:false,status:409},{readOnly:true,onOpenConnections:()=>connections++});
  await unavailable.view.ready;assert(unavailable.root.textContent.includes('需连接 NAS'));
  assert(find(unavailable.root,'核对连接'));unavailable.view.destroy();
  console.log(JSON.stringify({checks:10,passed:10,skipped:0,actual_components:true,synthetic_dom_only:true,people_labels_and_read_only_actions:true,files_typed_reads_only:true,nas_needs_connection_visible:true,metadata_is_not_recognition:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
