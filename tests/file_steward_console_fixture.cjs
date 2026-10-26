'use strict';
// Actual module DOM contracts; no browser, network, installation or household content.
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
const source=fs.readFileSync(path.join(process.argv[2],'homeplane/static/file-steward.js'),'utf8').replace(/^import '[^']*copy-layout.js';\n/m,'').replace('export function mountFiles','function mountFiles');
const ctx={catalog:{fingerprint:'a'.repeat(64),source_online:true,simulated:true}};
function setup(fetch,options={}){const root=new Element('section'),document={activeElement:null,createElement:tag=>{const element=new Element(tag);element.ownerDocument=document;return element;}};
  const copyDOM=require('./copy_layout_dom.cjs').install(Element,document,root);
  const context=vm.createContext({...copyDOM,document,fetch,AbortController,DOMException,Set,Map,console,scrollY:0,scrollTo:({top})=>{context.scrollY=top;}});
  vm.runInContext(fs.readFileSync(path.join(process.argv[2],'homeplane/static/copy-layout.js'),'utf8'),context);
  vm.runInContext(source,context);context.root=root;context.options=options;return {root,context,view:vm.runInContext('mountFiles(root,options)',context)};}
async function main(){
  const policy={label:'保留原样',source:'default_preserve',matched_rule_ids:[]};
  const item=id=>({item_id:id,original_hash:id,status:'ready',media_kind:'other',size_bytes:12});
  const rows=[{item:item('a'.repeat(64)),title:'DoneSentinel',extraction_status:'done',policy,references:[]},
    {item:item('b'.repeat(64)),title:'InboxPending',extraction_status:'disabled',policy,references:[]}];
  const calls=[];const first=setup(async url=>{calls.push(url);return {ok:true,json:async()=>url.endsWith('/context')?ctx:url.endsWith('/query')?{items:rows,next_cursor:null}:[]};});
  await first.view.ready;assert(first.root.classList.contains('files-root'));assert(first.root.textContent.includes('全部文件'));
  assert.equal(calls.length,3);assert(calls.every(url=>!/(run|scan|upload|execute)/.test(url)));
  const walk=node=>[node,...node.children.flatMap(walk)];
  walk(first.root).find(node=>node.tag==='button'&&node.textContent==='收件箱').events.click();
  assert(first.root.textContent.includes('InboxPending'));assert(!first.root.textContent.includes('DoneSentinel'));
  first.view.destroy();assert.equal(first.root.children.length,0);assert(!first.root.classList.contains('files-root'));
  let deliver;const late=setup(async()=>({ok:true,json:()=>new Promise(resolve=>{deliver=resolve;})}));
  await new Promise(setImmediate);late.view.destroy();deliver(ctx);await late.view.ready;
  assert.equal(late.root.children.length,0);assert(!late.root.classList.contains('files-root'));
  const retained={tab:'Search',query:'宇宙',selected:new Set(['a'.repeat(64)]),destinations:new Map(),scrollY:260};
  const stable=setup(async url=>({ok:true,json:async()=>url.endsWith('/context')?ctx:url.endsWith('/query')?{items:rows,next_cursor:null,search_scope:'indexed_cache',coverage:{source_read_enabled:false,complete_source_scan_claimed:false}}:[]}),{state:retained});
  await stable.view.ready;assert.equal(stable.context.scrollY,260);
  const input=walk(stable.root).find(node=>node.attrs.type==='search');input.focus();input.setSelectionRange(1,2);
  await stable.view.refresh();const newInput=walk(stable.root).find(node=>node.attrs.type==='search');
  assert.equal(stable.context.document.activeElement,newInput);assert.equal(newInput.selectionStart,1);assert.equal(newInput.selectionEnd,2);
  assert.equal(newInput.value,'宇宙');assert(retained.selected.has('a'.repeat(64)));stable.context.scrollY=410;stable.view.destroy();assert.equal(retained.scrollY,410);
  let connected=0,reviewBody=null,failed=true;const plan={status:'blocked',plan_id:'fixture-plan',plan_fingerprint:'p',inventory_fingerprint:'i',counts:{selected:1,protected:1,collision:0},entries:[{entry_id:'one',before_relative_path:'camera/IMG.jpg',destination:'new.jpg',policy_label:'保留原名',blockers:['policy_preserve_name']}]};
  const recovery=setup(async(url,request)=>{if(failed){failed=false;return {ok:false,status:503};}
    if(url.endsWith('/relocations/review')){reviewBody=JSON.parse(request.body);plan.status='rejected';return {ok:true,json:async()=>plan};}
    return {ok:true,json:async()=>url.endsWith('/context')?{...ctx,session:{reason:'authentication_required',label:'核对 NAS 授权'}}:url.endsWith('/query')?{items:[],next_cursor:null}:[plan]};
  },{state:{tab:'Review',query:'',selected:new Set(),destinations:new Map()},onOpenConnections:()=>connected++});
  await recovery.view.ready;assert(recovery.root.textContent.includes('重试'));
  await walk(recovery.root).find(node=>node.tag==='button'&&node.textContent==='重试').events.click();
  assert(recovery.root.textContent.includes('核对 NAS 授权'));walk(recovery.root).find(node=>node.textContent==='核对连接').events.click();assert.equal(connected,1);
  assert.equal(walk(recovery.root).find(node=>node.textContent==='确认预览').attrs.disabled,'');
  await walk(recovery.root).find(node=>node.textContent==='取消此批').events.click();
  assert.equal(reviewBody.decision,'reject');assert.deepEqual(reviewBody.selected_entry_ids,['one']);assert(recovery.root.textContent.includes('已放下'));
  let settingBody=null,settingFailure=true;
  const settingView={fingerprint:'c'.repeat(64),saved:false,activation:'held',settings:{directory_ref:'fixture-downloads',watched:false,extraction_mode:'metadata_only',budget:{source_bytes:1048576,units:8,seconds:5,text_chars:32000}},
    rule:{rule_id:'fixture-dir-policy',label:'下载收藏',rename_preview:false,move_preview:false,structure_preview:false,dedup_review:false},effective_policy:{source:'file_exception'}};
  const configured=setup(async(url,request)=>{
    if(url.endsWith('/settings/query'))return {ok:true,json:async()=>settingView};
    if(url.endsWith('/settings/preview')){
      settingBody=JSON.parse(request.body);
      if(settingFailure){settingFailure=false;return {ok:false,status:409};}
      Object.assign(settingView,{fingerprint:'d'.repeat(64),saved:true,rule:settingBody.rule,settings:{...settingView.settings,...settingBody}});
      return {ok:true,json:async()=>settingView};
    }
    return {ok:true,json:async()=>url.endsWith('/context')?ctx:url.endsWith('/query')?{items:rows,next_cursor:null}:[]};
  });
  await configured.view.ready;
  await walk(configured.root).find(n=>n.tag==='button'&&n.textContent==='目录设置').events.click();
  assert(configured.root.textContent.includes('尚未保存'));assert(configured.root.textContent.includes('此文件继续采用单独规则。'));
  const control=k=>walk(configured.root).find(n=>n.attrs['data-files-focus']===`settings:${k}`);
  const rename=control('rename_preview');rename.checked=true;rename.events.change();
  const watched=control('watched');watched.checked=true;watched.events.change();
  const label=control('label');label.value='科学收藏';label.events.input();label.focus();label.setSelectionRange(1,3);
  assert.equal(walk(configured.root).find(n=>n.tag==='details'&&n.textContent.includes('提取限额')).open,false);
  walk(configured.root).find(n=>n.tag==='form').events.submit({preventDefault(){}});await new Promise(setImmediate);
  assert(configured.root.textContent.includes('记录已变化'));assert.equal(control('label').value,'科学收藏');assert.equal(control('rename_preview').checked,true);
  assert.equal(configured.context.document.activeElement,control('label'));assert.equal(control('label').selectionStart,1);
  walk(configured.root).find(n=>n.tag==='form').events.submit({preventDefault(){}});await new Promise(setImmediate);
  assert(configured.root.textContent.includes('已保存 · 提取待启用'));
  assert(settingBody.watched&&settingBody.rule.rename_preview&&!settingBody.rule.move_preview&&!settingBody.rule.structure_preview&&!settingBody.rule.dedup_review);
  assert.equal(settingBody.expected_fingerprint,'c'.repeat(64));
  assert(!Object.keys(settingBody).some(k=>/password|credential|host|transport/.test(k)));configured.view.destroy();
  console.log(JSON.stringify({local_metadata_only:true,late_response_discarded:true,view_marker_removed:true,provider_html_execution:false,focus_and_selection_retained:true,auth_status_and_retry:true,exact_batch_rejection:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
