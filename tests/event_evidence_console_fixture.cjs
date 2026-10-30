// This fixture consumes actual EventEvidence API projections emitted by Python.
const fs=require('node:fs');
const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const nodes=[];
class Element{
  constructor(tag){this.tag=tag;this.attrs={};this.children=[];this.listeners={};this.dataset={};this._text='';this._value='';this.disabled=false;this.hidden=false;nodes.push(this);}
  set textContent(value){this._text=String(value??'');this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  set value(value){this._value=String(value);}
  get value(){return this._value;}
  setAttribute(key,value){this.attrs[key]=String(value);if(key==='value')this.value=value;if(key==='hidden')this.hidden=true;}
  getAttribute(key){return this.attrs[key]??null;}
  append(...children){for(const child of children){child.parent=this;this.children.push(child);if(this.tag==='select'&&this.children.length===1)this._value=child.value;}}
  replaceChildren(...children){this._text='';this.children=[];this.append(...children);}
  addEventListener(key,callback){(this.listeners[key]??=[]).push(callback);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  async dispatch(key){for(const callback of this.listeners[key]||[])await callback({preventDefault(){}});}
}
global.document={createElement:tag=>new Element(tag),head:new Element('head'),getElementById:id=>nodes.find(node=>node.getAttribute('id')===id)||null};
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
(async()=>{
  const {mountEventEvidence}=await import(pathToFileURL(path.resolve('homeplane/static/event-evidence.js')));
  const root=new Element('div'),calls=[],playbacks=[];
  let lateDetail=null,holdDetail=false,lateQuery=null,holdQuery=false;
  const clone=value=>JSON.parse(JSON.stringify(value));
  const api=async(url,body)=>{
    calls.push({url,body});
    if(url==='/api/home/evidence/context')return clone(fixture.context);
    if(url==='/api/home/evidence/query'){
      assert.equal(body.expected_context,fixture.context.fingerprint);
      assert(fixture.projections[body.room]);
      if(holdQuery)return new Promise(resolve=>lateQuery=resolve);
      return clone(fixture.projections[body.room]);
    }
    if(url==='/api/home/evidence/detail'){
      assert.equal(body.projection_id,fixture.projection.projection_id);
      assert.equal(body.event_id,fixture.detail.event.id);
      if(holdDetail)return new Promise(resolve=>lateDetail=resolve);
      return clone(fixture.detail);
    }
    if(url.startsWith('/api/home/evidence/targets?'))return {entities:[],partial:false};
    throw new Error('unexpected fixture call');
  };
  const view=mountEventEvidence(root,{api,isAlive:()=>true,onOpenRecording:request=>playbacks.push(request)});
  await settle();
  assert(root.textContent.includes('simulated · 合成示例'));
  const rooms=nodes.find(node=>node.getAttribute('aria-label')==='事件房间');
  rooms.value=fixture.projection.room;await rooms.dispatch('change');await settle();
  const events=nodes.filter(node=>node.getAttribute('data-event-id')===fixture.detail.event.id);
  assert.equal(events.length,1);
  await events[0].dispatch('click');await settle();
  assert(root.textContent.includes(fixture.detail.explanation.caption));
  assert(root.textContent.includes('物理效果未独立确认'));
  assert(!root.textContent.includes('SIMULATED-private'));
  assert(calls.every(call=>!call.url.includes('/recordings/media/')&&!call.url.includes('/archive/media/')));
  assert(playbacks.every(value=>value===null),'reading events must never start playback');
  const play=nodes.find(node=>node.tag==='button'&&node.textContent.startsWith('查看 SIMULATED camera 隐私录像'));
  assert(play);await play.dispatch('click');
  const lookup=playbacks.at(-1);
  assert.deepEqual(lookup.request,fixture.detail.recordings[0].request);
  assert.equal(lookup.causal_relation,'unknown');
  assert(!('url' in lookup)&&!('path' in lookup));
  // Filtering invalidates both old rows and an in-flight query immediately.
  const hours=nodes.find(node=>node.getAttribute('aria-label')==='事件时间范围');
  holdQuery=true;hours.value='1';await hours.dispatch('change');await settle();
  const search=nodes.find(node=>node.getAttribute('aria-label')==='筛选事件设备');
  search.value='no match';await search.dispatch('input');await settle();
  const detailCalls=calls.filter(call=>call.url==='/api/home/evidence/detail').length;
  await events[0].dispatch('click');await settle();
  assert.equal(calls.filter(call=>call.url==='/api/home/evidence/detail').length,detailCalls);
  lateQuery(clone(fixture.projection));await settle();
  assert.equal(nodes.find(node=>node.getAttribute('class')==='evidence-list').children.length,0);
  await new Promise(resolve=>setTimeout(resolve,300));await settle();
  assert(root.textContent.includes('没有匹配的设备'));
  holdQuery=false;search.value='';await search.dispatch('input');
  await new Promise(resolve=>setTimeout(resolve,300));await settle();
  // A selection change revokes existing playback before a late detail can render.
  holdDetail=true;await events[0].dispatch('click');await settle();
  hours.value='6';await hours.dispatch('change');await settle();
  const before=root.textContent;
  lateDetail(clone(fixture.detail));await settle();
  assert.equal(root.textContent,before);
  assert.equal(playbacks.at(-1),null);
  view.dispose();await settle();assert.equal(root.children.length,0);
  assert.equal(playbacks.at(-1),null);
  process.stdout.write('real API projections: timeline, evidence, typed recording request, stale cancellation and cleanup passed\n');
})().catch(error=>{process.stderr.write(String(error.stack||error));process.exitCode=1;});
