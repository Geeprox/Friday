'use strict';
// Actual mountFiles + copy-layout consuming captured real generated ASGI responses.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const sourceRoot=process.argv[2],capture=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
assert.equal(capture.basis,'actual_same_origin_ASGI_generated_cache');assert.equal(capture.source_IO_calls.length,0);
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.events={};this._text='';const values=new Set();this.classList={add:v=>values.add(v),remove:v=>values.delete(v),contains:v=>values.has(v)};}
  set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
  set innerHTML(_){throw Error('HTML is forbidden');}setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k];}
  append(...children){this.children.push(...children);}replaceChildren(...children){this._text='';this.children=[...children];}
  addEventListener(k,v){this.events[k]=v;}focus(){this.ownerDocument.activeElement=this;}setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}
}
const moduleSource=fs.readFileSync(path.join(sourceRoot,'homeplane/static/file-steward.js'),'utf8').replace(/^import '[^']*copy-layout.js';\n/m,'').replace('export function mountFiles','function mountFiles');
const walk=node=>[node,...node.children.flatMap(walk)],button=(root,label)=>walk(root).find(n=>n.tag==='button'&&n.textContent===label);
const response=body=>({ok:true,json:async()=>JSON.parse(JSON.stringify(body))});
const tick=()=>new Promise(setImmediate);
function setup(fetch,state={tab:'Search',query:'保险计划',selected:new Set(),destinations:new Map()}) {
  const root=new Element('section'),document={activeElement:null,createElement:tag=>{const n=new Element(tag);n.ownerDocument=document;return n;}};
  const copyDOM=require('./copy_layout_dom.cjs').install(Element,document,root);
  const context=vm.createContext({...copyDOM,document,fetch,AbortController,DOMException,Set,Map,console,scrollY:0,scrollTo:()=>{}});
  vm.runInContext(fs.readFileSync(path.join(sourceRoot,'homeplane/static/copy-layout.js'),'utf8'),context);
  vm.runInContext(moduleSource,context);context.root=root;context.options={state,readOnly:true};
  return {root,state,context,view:vm.runInContext('mountFiles(root,options)',context)};
}
function replies(calls) {return async(url,options)=>{
  if(url.endsWith('/context'))return response(capture.context);
  if(url.endsWith('/relocations'))return response([]);
  assert(url.endsWith('/query'));const body=JSON.parse(options.body);calls.push({body,signal:options.signal});
  if(body.search_mode==='catalog_page')return response(capture.files);
  assert.equal(body.search_mode,'indexed_cache');
  return response(body.query==='保险计划'?capture.needle:body.cursor?capture.more:capture.public);
};}
async function main(){
  const calls=[],current=setup(replies(calls));await current.view.ready;await tick();
  assert.equal(calls[0].body.search_mode,'indexed_cache');assert.equal(calls[0].body.query,'保险计划');
  assert(current.root.textContent.includes('第 10 页'));assert(current.root.textContent.includes('保险计划'));
  assert(current.root.textContent.includes('不含未提取、过期或无法访问的内容'));assert(!current.root.textContent.includes('当前目录页没有匹配'));
  assert(walk(current.root).filter(n=>n.attrs['data-files-focus']===`row:${capture.late_item_id}`).length===1);
  const input=walk(current.root).find(n=>n.attrs.type==='search');input.focus();input.value='公共课程';input.setSelectionRange(2,3);input.events.input();await tick();
  let fresh=walk(current.root).find(n=>n.attrs.type==='search');assert.equal(fresh.value,'公共课程');assert.equal(current.context.document.activeElement,fresh);assert.equal(fresh.selectionStart,2);
  assert(!current.root.textContent.includes('第 10 页'));walk(current.root).find(n=>n.tag==='form').events.submit({preventDefault(){}});await tick();await tick();
  assert.equal(walk(current.root).filter(n=>(n.attrs['data-files-focus']||'').startsWith('row:')).length,50);
  await button(current.root,'更多结果').events.click();await tick();
  assert.equal(calls.at(-1).body.cursor,capture.public.next_cursor);assert.equal(walk(current.root).filter(n=>(n.attrs['data-files-focus']||'').startsWith('row:')).length,10);
  button(current.root,'全部文件').events.click();await tick();await tick();
  assert.equal(calls.at(-1).body.search_mode,'catalog_page');assert.equal(calls.at(-1).body.query,'');assert.equal(calls.at(-1).body.cursor,null);
  assert(current.root.textContent.includes('当前目录页'));current.view.destroy();

  let resolveOld;const lateCalls=[],late=setup(async(url,options)=>{
    if(url.endsWith('/context'))return response(capture.context);if(url.endsWith('/relocations'))return response([]);
    const body=JSON.parse(options.body);lateCalls.push({body,signal:options.signal});
    if(body.query==='保险计划')return {ok:true,json:()=>new Promise(resolve=>{resolveOld=resolve;})};
    return response(capture.public);
  });await tick();const oldSignal=lateCalls[0].signal;
  const oldInput=walk(late.root).find(n=>n.attrs.type==='search');oldInput.focus();oldInput.value='公共课程';oldInput.events.input();
  assert(oldSignal.aborted);resolveOld(capture.needle);await late.view.ready;await tick();assert(!late.root.textContent.includes('第 10 页'));
  walk(late.root).find(n=>n.tag==='form').events.submit({preventDefault(){}});await tick();await tick();assert(late.root.textContent.includes('50 项匹配'));
  const pending=late.view.refresh();await tick();
  late.view.destroy();await pending;assert.equal(late.root.children.length,0);

  let deliverDestroyed,lastSignal;const destroyed=setup(async(url,options)=>{
    if(url.endsWith('/context'))return response(capture.context);if(url.endsWith('/relocations'))return response([]);
    lastSignal=options.signal;return {ok:true,json:()=>new Promise(resolve=>{deliverDestroyed=resolve;})};
  });await tick();destroyed.view.destroy();assert(lastSignal.aborted);deliverDestroyed(capture.needle);await destroyed.view.ready;assert.equal(destroyed.root.children.length,0);

  let deliverSearch;const tabCalls=[],tabs=setup(async(url,options)=>{
    if(url.endsWith('/context'))return response(capture.context);if(url.endsWith('/relocations'))return response([]);
    const body=JSON.parse(options.body);tabCalls.push({body,signal:options.signal});
    return body.search_mode==='indexed_cache'?{ok:true,json:()=>new Promise(resolve=>{deliverSearch=resolve;})}:response(capture.files);
  },{tab:'Files',query:'保险计划',selected:new Set(),destinations:new Map()});await tabs.view.ready;
  button(tabs.root,'搜索').events.click();await tick();const tabSignal=tabCalls.at(-1).signal;
  button(tabs.root,'收件箱').events.click();await tick();assert(tabSignal.aborted);deliverSearch(capture.needle);await tick();
  assert.equal(tabCalls.at(-1).body.search_mode,'catalog_page');assert.equal(tabCalls.at(-1).body.query,'');assert(!tabs.root.textContent.includes('第 10 页'));tabs.view.destroy();

  let fail=false;const denied=setup(async(url,options)=>{
    if(url.endsWith('/context'))return response(capture.context);if(url.endsWith('/relocations'))return response([]);
    return fail?{ok:false,status:409}:response(capture.public);
  },{tab:'Search',query:'公共课程',selected:new Set(),destinations:new Map()});await denied.view.ready;
  fail=true;await button(denied.root,'更多结果').events.click();await tick();
  assert(denied.root.textContent.includes('记录已变化'));assert.equal(walk(denied.root).filter(n=>(n.attrs['data-files-focus']||'').startsWith('row:')).length,0);
  assert(!button(denied.root,'更多结果'));denied.view.destroy();

  const wrong=setup(async url=>response(url.endsWith('/context')?capture.context:url.endsWith('/query')?capture.files:[]));await wrong.view.ready;
  assert(wrong.root.textContent.includes('本机索引暂不可用'));assert.equal(walk(wrong.root).filter(n=>(n.attrs['data-files-focus']||'').startsWith('row:')).length,0);wrong.view.destroy();
  const offline=setup(async url=>response(url.endsWith('/context')?capture.context:url.endsWith('/query')?{...capture.needle,items:[],next_cursor:null,coverage:{...capture.needle.coverage,source_online:false}}:[]));
  await offline.view.ready;await tick();assert(offline.root.textContent.includes('来源离线'));assert(offline.root.textContent.includes('已存内容不代表原件可用'));assert(offline.root.textContent.includes('当前可查内容无匹配，不代表来源没有文件'));offline.view.destroy();
  console.log(JSON.stringify({actual_ASGI_payload_consumed:true,beyond_first_catalog_page:true,page10_reference_visible:true,explicit_cache_only_scope:true,unique_more_page:true,
    captured_query_and_tab_mode:true,superseded_read_aborted_and_fenced:true,destroyed_search_reply_fenced:true,pending_tab_switch_fenced:true,focus_selection_retained:true,stale_permission_response_cleared:true,wrong_scope_rejected:true,
    offline_not_full_library_absence:true,readonly_mutation_calls:0,provider_or_source_IO:0}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
