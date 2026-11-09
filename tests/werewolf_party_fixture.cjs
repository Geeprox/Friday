// Execute the shipped phone client with synthetic DOM/HTTP, never a real browser or LAN.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this._text='';this.classList={toggle(){},remove(){},add(){}};}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
 setAttribute(k,v){this.attrs[k]=String(v);}
 removeAttribute(k){delete this.attrs[k];}
 addEventListener(){}
 querySelector(selector){return this.children.flatMap(all).find(n=>selector==='[data-timer]'&&n.dataset.timer);}
 querySelectorAll(selector){return all(this).filter(n=>selector.split(',').includes(n.tagName));}
}
const all=n=>[n,...n.children.flatMap(all)],settle=()=>new Promise(setImmediate);
(async()=>{
 const nodes=Object.fromEntries(['party','notice','roomLabel'].map(id=>[id,new Element('div')]));
 const events={},requests=[],token='a'.repeat(64);let clock=0,replies=[429,200],tick;
 const publicGame={revision:1,phase:'wolves',phase_name:'狼人行动',round:1,paused:true,seconds_left:30,players:[{seat:1,name:'合成玩家',alive:true}],roles:[]};
 const player={seat:1,name:'合成玩家',role:'wolf',role_name:'狼人',alive:true,actions:[],public:publicGame};
 const storage=new Map([['Friday.werewolf.player',token]]);
 const context=vm.createContext({console,URLSearchParams,AbortController,AbortSignal,performance:{now:()=>clock},
  location:{pathname:'/phone',hash:''},history:{replaceState(){}},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  document:{body:new Element('body'),hidden:false,getElementById:id=>nodes[id],createElement:tag=>new Element(tag),addEventListener:(name,fn)=>events[name]=fn},
  window:{addEventListener:(name,fn)=>events[name]=fn},setInterval:fn=>{tick=fn;},
  fetch:async(path,options)=>{requests.push({path,options});const status=replies.shift()||200;return{ok:status===200,status,headers:{get:()=> '5'},json:async()=>status===200?player:{detail:'party_rate_limited'}};}
 });
 vm.runInContext(fs.readFileSync('homeplane/static/werewolf-party.js','utf8'),context);
 await settle();assert.equal(requests.length,1);assert.equal(nodes.notice.textContent,'正在同步对局…');
 await tick();await settle();assert.equal(requests.length,1,'Rate-limited polling honors Retry-After');
 clock=4999;await tick();await settle();assert.equal(requests.length,1);
 clock=5001;await tick();await settle();assert.equal(requests.length,2);assert.equal(nodes.notice.textContent,'','Recovered polling removes its stale notice');
 assert.equal(requests[1].options.headers.Authorization,'Bearer '+token);
 const card=()=>all(nodes.party).find(n=>n.className==='role-card');
 assert.match(card().textContent,/身份已收起/);assert(!card().textContent.includes('狼人'));
 vm.runInContext('revealRole()',context);assert.equal(card().textContent,'狼人');
 events.blur();assert.match(card().textContent,/身份已收起/);assert(!card().textContent.includes('狼人'));
 nodes.notice.textContent='已确认，Friday 收到了你的选择。';await tick();await settle();
 assert.equal(nodes.notice.textContent,'已确认，Friday 收到了你的选择。','Background refresh preserves an explicit submission receipt');
 assert.equal(typeof tick,'function');console.log('Phone polling recovery, bounded retry, explicit receipt and private-role hiding: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
