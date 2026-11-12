/* Production library with inert DOM and explicitly controlled read responses. */
const fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto');
const walk=n=>[n,...n.children.flatMap(walk)];
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.className='';this.hidden=false;this.disabled=false;this.value='';this.ownText='';}
 set textContent(value){this.ownText=String(value);this.children=[];}
 get textContent(){return this.ownText+this.children.map(n=>n.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);}
 getAttribute(k){return this.attrs[k]??null;}
 append(...children){this.children.push(...children);for(const n of children)n.parent=this;}
 prepend(...children){this.children.unshift(...children);for(const n of children)n.parent=this;}
 replaceChildren(...children){this.ownText='';this.children=[];this.append(...children);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
 contains(node){return walk(this).includes(node);}
 querySelectorAll(selector){return walk(this).slice(1).filter(n=>selector.startsWith('.')?n.className.split(' ').includes(selector.slice(1)):n.tagName===selector);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 after(node){if(this.parent){const at=this.parent.children.indexOf(this);this.parent.children.splice(at+1,0,node);node.parent=this.parent;}}
 scrollIntoView(){}
 focus(){Element.active=this;}
}
const settle=()=>new Promise(setImmediate),clone=value=>JSON.parse(JSON.stringify(value));
function library({mode='quest',owned=true,firstError=false}={}){
 Element.active=null;
 let tick,at=100000;const requests=[],deferred=[];
 const root=new Element('main'),service={configured:true,owned,capture_enabled:true,camera:'cam_1111111111',game:{mode,selected_mode:mode,game_input_enabled:mode!=='off',control_context:'a'.repeat(32),control_revision:1,round_id:1}};
 let intentAvailable=true;
 Object.defineProperty(service,'game_intent',{enumerable:true,get:()=>intentAvailable?crypto.createHash('sha256').update(JSON.stringify(service.game)).digest('hex'):null});
 const apps=[['quest','山径冒险','game'],['hiit','运动训练','game'],['ski','林间滑雪','game'],['werewolf','狼人杀','party']].map(([id,name,kind])=>({id,name,kind,mode:id,description:name,players:[1,1]}));
 const response=(data,status=200)=>({ok:status===200,status,json:async()=>data});
 const context=vm.createContext({root,URL,Date:{now:()=>at},console,AbortController,AbortSignal,FridayPartyAudio:{},
  document:{get activeElement(){return Element.active;},createElement:t=>new Element(t),querySelector:()=>null,head:new Element('head'),body:new Element('body')},
  setInterval:fn=>{tick=fn;return 1;},clearInterval(){},
  motionTile:()=>{const node=new Element('article');node.dispose=()=>{};return node;},
  mountPractice:()=>({dispose(){}}),mountWerewolf:()=>({dispose(){}}),mountKtv:(target,options)=>{const node=new Element('ktv-player');node.options=options;target.append(node);return{dispose(){node.remove();}};},
  fetch:async(path,options={})=>{
   const request={path,options};requests.push(request);
   if(options.method==='POST'){
    const data=JSON.parse(options.body);
    if(data.expected_intent!==service.game_intent)return response({detail:'game_state_changed'},409);
    service.game.control_revision++;service.game.round_id++;
    if(path==='/api/embodied/start'){service.owned=true;service.game.mode='off';service.game.selected_mode=data.app;service.game.game_input_enabled=true;}
    else if(path==='/api/embodied/game/stop'){service.game.mode='off';service.game.game_input_enabled=false;}
    else throw Error('unexpected write '+path);
    return response(clone(path==='/api/embodied/game/stop'?service.game:service));
   }
   if(path==='/api/games'){
    if(firstError){firstError=false;throw Error('synthetic initial failure');}
    const data=clone({service,apps}),pending=deferred.shift();
    if(pending){pending.request=request;return pending.promise.then(()=>response(data));}
    return response(data);
   }
   if(path==='/api/embodied/cameras')return response({cameras:[{id:service.camera,room:'合成房间',name:'合成摄像头'}]});
   if(/^\/api\/games\/\w+\/open$/.test(path))return response({url:'http://127.0.0.1:8860/games'});
   throw Error('unexpected read '+path);
  }});
 vm.runInContext(fs.readFileSync('homeplane/static/games-library.js','utf8').replace(/^import.*$/gm,'').replace('export function mountGames','function mountGames'),context);
 const owner=vm.runInContext('mountGames(root)',context);
 const button=text=>walk(root).find(n=>n.tagName==='button'&&n.textContent===text);
 return{root,service,requests,owner,button,settle,focused:()=>Element.active,all:()=>walk(root),tick:()=>tick(),advance:ms=>{at+=ms;},intentUnavailable(){intentAvailable=false;},
  defer(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;}),item={promise,resolve,reject};deferred.push(item);return item;}};
}
module.exports={library,Element};
