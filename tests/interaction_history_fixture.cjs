const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fixture=JSON.parse(fs.readFileSync('fixtures/homeplane/interaction-history.json','utf8'));
let focused=null;
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.value='';this.ownText='';this.scrollLeft=0;}
 set textContent(value){this.ownText=value;this.children=[];}
 get textContent(){return this.ownText+this.children.map(n=>n.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=String(v);}
 append(...nodes){this.children.push(...nodes);if(this.tagName==='select'&&!this.value)this.value=this.children[0]?.value||'';}
 replaceChildren(...nodes){this.ownText='';this.children=[];this.append(...nodes);}
 querySelector(selector){const match=selector.match(/^\[([^\]]+)\]$/);return all(this).find(n=>match?Object.hasOwn(n.attrs,match[1]):n.tagName===selector)||null;}
 get isConnected(){return true;}
 focus(){focused=this;}
 remove(){}
}
const all=n=>[n,...n.children.flatMap(all)],settle=()=>new Promise(setImmediate);
(async()=>{
 const root=new Element('main'),writes=[];
 let events=[],poll;
 const fetch=async(path,options={})=>{
  if(options.method){writes.push(path);throw Error('预览不允许写入');}
  const routes={
   '/api/home/interactions':{config:fixture.config,gestures:fixture.gestures},
   '/api/embodied/devices':{devices:fixture.targets},
   '/api/embodied/status':{interactions:{events},camera_sessions:[{camera:fixture.devices[0].id,state:'live'}]},
   '/api/embodied/infrastructure':{policy:{enabled:true,recognition:true,interaction:true}},
   '/api/embodied/cameras/cam_1111111111/power':{enabled:false}
  };
  assert.ok(Object.hasOwn(routes,path),path);return {ok:true,json:async()=>routes[path]};
 };
 const context=vm.createContext({document:{createElement:tag=>new Element(tag),get activeElement(){return all(root).includes(focused)?focused:null;}},fetch,Date,
  sessionStorage:{getItem:()=>null,setItem(){}},setInterval:fn=>{poll=fn;return 1;},clearInterval(){},
  root,options:{data:{devices:fixture.devices},go(){},toast(){}}});
 vm.runInContext(fs.readFileSync('homeplane/static/interactions.js','utf8').replace('export function mountInteractions','function mountInteractions'),context);
 let mounted=vm.runInContext('mountInteractions(root,options)',context);await settle();
 all(root).find(n=>n.tagName==='button'&&n.textContent==='手势控制').onclick();
 const history=()=>root.querySelector('[data-interaction-history]'),table=()=>all(history()).find(n=>n.tagName==='table'),rows=()=>table().children.find(n=>n.tagName==='tbody').children;
 assert.match(history().textContent,/做出已设置的手势/);assert.equal(table(),undefined);
 events=structuredClone(fixture.events);poll();await settle();
 assert.deepEqual(all(table()).filter(n=>n.tagName==='th').map(n=>[n.textContent,n.attrs.scope]),[['触发时间','col'],['触发动作','col'],['对应规则','col'],['响应结果','col']]);
 assert.equal(rows().length,3);
 assert.equal(rows()[0].children[1].children[0].textContent,'张开手掌');
 assert.equal(rows()[1].children[3].textContent,'指令已发送');
 assert.equal(rows()[2].children[3].textContent,'状态已更新');
 assert.equal(rows()[2].children[0].children[0].attrs.datetime,new Date(events[0].triggered_at*1000).toISOString());
 assert.equal(rows()[0].children[3].textContent,'操作未完成');
 const scroll=all(history()).find(n=>n.attrs.role==='region');assert.equal(scroll.attrs.tabindex,'0');
 scroll.focus();scroll.scrollLeft=130;
 const assertScroll=(position=130)=>{
  assert.ok(all(history()).find(n=>n.attrs.role==='region')===scroll,'新回执必须保留滚动容器');
  assert.ok(context.document.activeElement===scroll,'更新后键盘焦点应留在滚动容器');
  assert.equal(scroll.scrollLeft,position,'更新后保留横向滚动位置');
 };
 const unchanged=table();poll();await settle();assert.equal(table(),unchanged,'无变化的轮询保留表格');assertScroll();
 events=[...fixture.events,{...fixture.events[2],triggered_at:fixture.events[2].triggered_at+1,acknowledged:true}];poll();await settle();
 assert.equal(rows().length,4);assert.equal(rows()[0].children[3].textContent,'指令已发送');assertScroll();
 events=[];poll();await settle();
 assert.equal(table(),undefined);assert.match(history().textContent,/做出已设置的手势/);
 assert.ok(all(history()).find(n=>n.attrs.role==='region')===scroll,'清空记录时保留已聚焦的滚动容器');
 assert.ok(context.document.activeElement===scroll,'空状态不应移除当前键盘焦点');
 scroll.scrollLeft=0;
 // 配置编辑或删除后仍使用事件记录的手势、动作与范围。
 fixture.config.bindings[2].gesture='hand.ok';fixture.config.bindings[2].action='off';fixture.config.bindings=[];
 events=[{...fixture.events[2],reached_target:true},...fixture.events.slice(0,2)];poll();await settle();
 assertScroll(0);
 assert.match(rows()[0].children[2].textContent,/张开手掌 → 示例主灯 · 开启/);
 assert.match(rows()[0].children[2].textContent,/示例客厅/);
 events=Array.from({length:8},(_,i)=>({...fixture.events[0],triggered_at:1794024000+i})).reverse();poll();await settle();
 assert.equal(rows().length,5);assert.equal(rows()[0].children[0].children[0].attrs.datetime,new Date(1794024007*1000).toISOString());
 assert.equal(rows()[4].children[0].children[0].attrs.datetime,new Date(1794024003*1000).toISOString());
 events=[{...fixture.events[0],gesture:'hand.point',scope:'home'}];poll();await settle();
 assert.match(rows()[0].textContent,/指向设备并比 OK/);assert.ok(!rows()[0].children[2].textContent.includes('全屋'));
 events=[{camera:'missing-camera',target:'missing-target',action:'off',at:1794024000}];poll();await settle();
 assert.match(rows()[0].textContent,/未记录手势/);assert.match(rows()[0].textContent,/未记录规则/);assert.match(rows()[0].textContent,/原设备不可用 · 关闭/);assert.equal(rows()[0].children[0].textContent,'未记录');
 for(const value of [null,'invalid',Number.NaN,Number.POSITIVE_INFINITY,-1,1e100]){
  events=[{...fixture.events[0],triggered_at:value}];poll();await settle();assert.equal(rows()[0].children[0].textContent,'未记录');
 }
 fixture.targets[0].name='<img src=x onerror=alert(1)>';events=[fixture.events[0]];
 mounted.dispose();root.replaceChildren();mounted=vm.runInContext('mountInteractions(root,options)',context);await settle();
 all(root).find(n=>n.tagName==='button'&&n.textContent==='手势控制').onclick();
 assert.match(rows()[0].textContent,/<img src=x onerror=alert\(1\)>/);assert.equal(all(table()).filter(n=>n.tagName==='img').length,0);
 events=[];poll();await settle();assert.equal(table(),undefined);assert.match(history().textContent,/做出已设置的手势/);
 assert.deepEqual(writes,[]);mounted.dispose();console.log('手势历史表格的排序、快照、兼容与安全文本检查通过');
})().catch(e=>{console.error(e);process.exitCode=1;});
