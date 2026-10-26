const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this._text='';}
 set textContent(v){this._text=v;this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);}
 append(...cs){cs.forEach(n=>{this.children.push(n);n.parent=this;});}
 replaceChildren(...cs){this.children=[];this.append(...cs);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
}
(async()=>{
 const root=new Element('main'),all=n=>[n,...n.children.flatMap(all)],button=text=>all(root).find(n=>n.tagName==='button'&&n.textContent===text);
 const service={owned:true,state:'live',tv_viewer:null};let tick,finish,delay=false,cleared=false;const reads=[];
 const context=vm.createContext({document:{createElement:t=>new Element(t)},URL,Date,setInterval:fn=>{tick=fn;return 1;},clearInterval(){cleared=true;},fetch:async(path,options)=>{
  assert.equal(options,undefined,'Viewing recognition must never start/stop the camera or game');reads.push(path);
  if(path==='/api/embodied/status')return{ok:true,json:async()=>service};
  if(path==='/api/games/werewolf/open')return{ok:true,json:async()=>{if(delay)await new Promise(r=>finish=r);return{url:'http://127.0.0.1:8860/?k=SYNTHETIC'};}};
  throw Error(path);
 }});
 vm.runInContext(fs.readFileSync('homeplane/static/recognition-debug.js','utf8').replaceAll('export function','function'),context);
 context.root=root;const owner=vm.runInContext('mountRecognitionDebug(root)',context),settle=()=>new Promise(setImmediate);await settle();
 assert.match(root.textContent,/电视 · 未连接/);
 await button('查看识别').onclick();const frame=all(root).find(n=>n.tagName==='iframe');assert(frame&&frame.src.includes('debug=1'));
 assert.equal(frame.attrs.referrerpolicy,'no-referrer');await tick();assert(all(root).includes(frame));
 button('关闭查看').onclick();await settle();assert(!all(root).includes(frame));
 const state=context.televisionState;assert.equal(state(null,100),'未连接');
 assert.equal(state({reported_at:101,frames:3,fps:4,server_frame_age_ms:100},100),'未连接');
 assert.equal(state({reported_at:99,frames:3,fps:4,server_frame_age_ms:3000},100),'等待画面');
 assert.equal(state({reported_at:99,frames:3,fps:4,server_frame_age_ms:100},100),'画面同步');
 for(const field of ['frames','fps','server_frame_age_ms'])for(const value of [null,'',false,true,'1',NaN,Infinity]){
  assert.equal(state({reported_at:99,frames:3,fps:4,server_frame_age_ms:100,[field]:value},100),'等待画面');
 }
 for(const value of [null,'',false,true,'99',NaN,Infinity])assert.equal(state({reported_at:value,frames:3,fps:4,server_frame_age_ms:100},100),'未连接');
 delay=true;const pending=button('查看识别').onclick();await settle();owner.dispose();finish();await pending;
 assert(cleared&&root.children.length===0,'Late open must not recreate a disposed viewer');
 console.log('PASS debug entry, read-only lifecycle, stale TV evidence, late-open cancellation');
})().catch(e=>{console.error(e);process.exitCode=1;});
