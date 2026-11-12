/* Actual phone source and ASGI receipts; dropped reply + page reload, no browser/LAN. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const {readClient}=require('./werewolf_client_fixture.cjs');
const packet=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),source=readClient('party');
const all=n=>[n,...n.children.flatMap(all)],settle=()=>new Promise(setImmediate);
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.events={};this.disabled=false;this._text='';this.classList={toggle(){},add(){},remove(){}};}
 set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
 append(...n){this.children.push(...n);}replaceChildren(...n){this._text='';this.children=[];this.append(...n);}
 setAttribute(k,v){this.attrs[k]=String(v);}removeAttribute(k){delete this.attrs[k];}addEventListener(k,v){this.events[k]=v;}
 querySelectorAll(s){return all(this).filter(n=>s.split(',').includes(n.tagName));}
 querySelector(s){return all(this).find(n=>s==='[data-timer]'&&n.dataset.timer);}
}
function phone(storage,{drop=false,seed=1,invitation=packet.command.code}={}){
 const nodes=Object.fromEntries(['party','notice','roomLabel'].map(id=>[id,new Element('div')])),requests=[],events={};
 const reply=data=>({ok:true,status:200,headers:{get:()=>null},json:async()=>structuredClone(data)});
 const context=vm.createContext({console,URLSearchParams,AbortController,AbortSignal,performance:{now:()=>0},navigator:{},crypto:{getRandomValues:a=>a.fill(seed)},
  location:{pathname:'/phone',hash:'#code='+invitation},history:{replaceState(){}},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  document:{body:new Element('body'),hidden:false,getElementById:id=>nodes[id],createElement:t=>new Element(t),addEventListener(){}},
  window:{addEventListener:(k,v)=>events[k]=v},setInterval(){},
  fetch:async(path,options)=>{const body=options.body?JSON.parse(options.body):null;requests.push({path,body});
   if(path==='/api/lobby'){if(body.code!==packet.command.code)throw Error('SYNTHETIC new room offline');return reply(packet.lobby);}
   if(path==='/api/join'){assert.deepEqual(body,packet.command);if(drop)throw Error('SYNTHETIC lost committed reply');return reply(packet.retried);}
   throw Error('unowned request '+path);
  }});
 vm.runInContext(source,context);
 return{nodes,requests,events,button:text=>all(nodes.party).find(n=>n.tagName==='button'&&n.textContent===text)};
}
(async()=>{
 const storage=new Map(),first=phone(storage,{drop:true});await settle();
 first.button('1 空位').onclick();const name=all(first.nodes.party).find(n=>n.tagName==='input'&&n.autocomplete==='nickname');name.value=packet.command.name;
 all(first.nodes.party).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});await settle();
 assert(first.button('重试入座'));assert.equal(storage.has('Friday.werewolf.player'),false);
 assert.deepEqual(JSON.parse(storage.get('Friday.werewolf.join')),packet.command);
 first.events.pagehide();
 const resumed=phone(storage,{seed:2});await settle();
 assert.equal(resumed.requests.filter(r=>r.path==='/api/join').length,1);
 assert.equal(storage.get('Friday.werewolf.player'),packet.retried.token);assert.equal(storage.has('Friday.werewolf.join'),false);
 assert(resumed.nodes.party.textContent.includes('你的座位已留好'));assert(!resumed.button('重试入座'));
 assert(!resumed.nodes.party.textContent.includes(packet.retried.token));resumed.events.pagehide();
 const different=packet.command.code==='654321'?'654322':'654321';
 const oldAttempt=new Map([['Friday.werewolf.join',JSON.stringify(packet.command)]]),redirected=phone(oldAttempt,{invitation:different});await settle();
 assert.equal(redirected.requests.filter(r=>r.path==='/api/join').length,0,'a new invitation cannot replay the old join');
 assert.equal(redirected.requests[0].body.code,different);assert.equal(oldAttempt.has('Friday.werewolf.join'),false);redirected.events.pagehide();
 const malformed=new Map([['Friday.werewolf.join',JSON.stringify({...packet.command,code:Number(packet.command.code)})]]),invalid=phone(malformed);await settle();
 assert.equal(invalid.requests.filter(r=>r.path==='/api/join').length,0);assert.equal(malformed.has('Friday.werewolf.join'),false);invalid.events.pagehide();
 console.log(JSON.stringify({actual_phone_source:true,asgi_receipts:true,dropped_reply_retains_intent:true,reload_reuses_intent:true,same_credential:true,no_second_seat:true,no_secret_in_ui:true}));
})().catch(e=>{console.error(e);process.exitCode=1;});
