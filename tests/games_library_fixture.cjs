const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){Object.defineProperty(this,'tagName',{value:tag,writable:true,configurable:true});this.children=[];this.attrs={};this._text='';this.value='';this.hidden=false;Object.defineProperty(this,'style',{value:{},writable:true,configurable:true});this.dataset={};}
 set textContent(v){this._text=v;this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k]??null;}
 append(...cs){cs.forEach(n=>{this.children.push(n);n.parent=this;});}prepend(...cs){this.children.unshift(...cs);cs.forEach(n=>n.parent=this);}
 replaceChildren(...cs){this.children=[];this.append(...cs);}remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
 querySelectorAll(selector){return this.children.flatMap(all).filter(n=>selector.startsWith('.')?n.className?.split(' ').includes(selector.slice(1)):n.tagName===selector);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 after(node){if(this.parent){const i=this.parent.children.indexOf(this);this.parent.children.splice(i+1,0,node);node.parent=this.parent;}}
 scrollIntoView(){}focus(){}
}
const all=n=>[n,...n.children.flatMap(all)];
(async()=>{
 const root=new Element('main'),button=text=>all(root).find(n=>n.tagName==='button'&&n.textContent===text),writes=[],navigated=[];let tick;
 const service={configured:true,owned:true,state:'live',quiet:true,camera:'cam_1111111111',game_intent:'a'.repeat(64),game:{mode:'off',selected_mode:'quest',game_input_enabled:false}};
 const apps=[['ski','林间滑雪','game',1,1],['tennis','双人网球','game',2,2],['quest','动作闯关','game',1,1],['hiit','运动闯关','game',1,1],['werewolf','狼人杀','party',6,12],['home','全屋互动','interaction',1,1]].map(([id,name,kind,min,max])=>({id,name,description:name,kind,players:[min,max],mode:id}));
 const context=vm.createContext({document:{createElement:t=>new Element(t),querySelector:()=>null,head:new Element('head'),body:new Element('body')},FridayPartyAudio:{},mountKtv:(target,options)=>{const marker=new Element('ktv-game');target.append(marker);marker.options=options;return{dispose(){marker.remove();}};},URL,Date,console,AbortController,AbortSignal,location:{},setInterval:fn=>{tick=fn;return 1;},clearInterval(){},mountWerewolf:target=>{target.append(new Element('party-host'));return{dispose(){}};},motionTile:()=>{const n=new Element('article');n.dispose=()=>{};return n;},mountPractice:()=>({dispose(){}}),fetch:async(path,options={})=>({ok:true,json:async()=>{
  if(options.method==='POST'){const body=JSON.parse(options.body);assert.equal(body.expected_intent,service.game_intent);writes.push({path,body});if(path==='/api/embodied/start'){service.game.mode='off';service.game.selected_mode=body.app;service.game.game_input_enabled=true;service.game_intent='b'.repeat(64);return service;}if(path==='/api/embodied/game/stop'){service.game.mode='off';service.game.game_input_enabled=false;service.game_intent='c'.repeat(64);return service.game;}throw Error('unexpected write '+path);}
  if(path==='/api/games')return{service,apps};if(path==='/api/embodied/cameras')return{cameras:[{id:service.camera,name:'监控',room:'客厅'}]};
  if(/^\/api\/games\/\w+\/open$/.test(path))return{url:'http://127.0.0.1:8860/games?k=SYNTHETIC&embed=1'};throw Error('unexpected read '+path);
 }})});
 context.root=root;context.options={go:p=>navigated.push(p)};
 vm.runInContext(fs.readFileSync('homeplane/static/games-library.js','utf8').replace(/^import.*$/gm,'').replace('export function mountGames','function mountGames'),context);
 const owner=vm.runInContext('mountGames(root,options)',context),settle=()=>new Promise(setImmediate);await settle();
 assert.equal(writes.length,0,'Browsing must not start any camera or game');assert.equal(all(root).filter(n=>n.className==='game-card').length,5,'KTV is in the library; two circuit modes share one store entry');assert.ok(!button('运动闯关'));
 button('体感闯关').onclick();await settle();button('运动训练').onclick();await settle();await button('开始游戏').onclick();await settle();
 const frame=all(root).find(n=>n.tagName==='iframe');assert.ok(frame);assert.match(frame.src,/127\.0\.0\.1:8860\/games/);assert.equal(frame.attrs.referrerpolicy,'no-referrer');
 assert.deepEqual(writes[0],{path:'/api/embodied/start',body:{camera:service.camera,app:'hiit',expected_intent:'a'.repeat(64)}});
 tick();await settle();assert.ok(all(root).includes(frame),'Polling must retain the running frame');
 await button('结束游戏').onclick();await settle();assert.equal(writes[1].path,'/api/embodied/game/stop');assert.ok(!writes.some(w=>w.path==='/api/embodied/stop'));
 button('全部游戏').onclick();await settle();button('狼人杀').onclick();assert.ok(all(root).some(n=>n.tagName==='party-host'));assert.equal(writes.length,2,'Party lobby must not start perception');button('全部游戏').onclick();await settle();button('家庭 KTV').onclick();await settle();const ktv=all(root).find(n=>n.tagName==='ktv-game');assert.ok(ktv,'KTV opens inside the games surface');assert.equal(ktv.options.endpoint,'/api/games/ktv');assert.equal(writes.length,2,'Opening KTV must not start a camera or session');ktv.options.onBack();await settle();button('全屋互动控制').onclick();assert.deepEqual(navigated,['Interaction']);
 owner.dispose();console.log('Store browse, grouped modes, explicit launch, retained frame and game-only stop: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
