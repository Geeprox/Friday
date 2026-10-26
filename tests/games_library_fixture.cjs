const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this._text='';this.value='';}
 set textContent(v){this._text=v;this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 setAttribute(k,v){this.attrs[k]=String(v);}
 append(...cs){cs.forEach(n=>{this.children.push(n);n.parent=this;});}
 replaceChildren(...cs){this.children=[];this.append(...cs);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
}
(async()=>{
 const root=new Element('main'),all=n=>[n,...n.children.flatMap(all)],button=text=>all(root).find(n=>n.tagName==='button'&&n.textContent===text),writes=[],navigated=[];
 let tick;
 const service={configured:true,owned:true,state:'live',quiet:true,camera:'cam_1111111111'};
 const apps=[{id:'ski',name:'林间滑雪',description:'摆动双臂，横移躲避障碍',kind:'game',players:[1,1]},{id:'home',name:'全屋互动',description:'全屋动作',kind:'interaction',players:[1,1]}];
 const context=vm.createContext({document:{createElement:t=>new Element(t)},URL,Date,console,location:{},setInterval:fn=>{tick=fn;return 1;},clearInterval(){},fetch:async(path,options={})=>({ok:true,json:async()=>{
  if(options.method==='POST'){writes.push({path,body:JSON.parse(options.body)});if(['/api/embodied/start','/api/embodied/game/stop'].includes(path))return service;throw Error('unexpected write '+path);}
  if(path==='/api/games')return{service,apps};if(path==='/api/embodied/cameras')return{cameras:[{id:service.camera,name:'监控',room:'客厅'}]};
  if(path==='/api/games/ski/open')return{url:'http://127.0.0.1:8860/games?k=SYNTHETIC&embed=1'};throw Error('unexpected read '+path);
 }})});
 context.root=root;context.options={go:p=>navigated.push(p)};
 vm.runInContext(fs.readFileSync('homeplane/static/games-library.js','utf8').replace('export function mountGames','function mountGames'),context);
 const owner=vm.runInContext('mountGames(root,options)',context),settle=()=>new Promise(setImmediate);await settle();
 await button('开始游戏').onclick();await settle();
 const frame=all(root).find(n=>n.tagName==='iframe');assert.ok(frame);assert.match(frame.src,/127\.0\.0\.1:8860\/games/);assert.equal(frame.attrs.referrerpolicy,'no-referrer');
 assert.deepEqual(writes[0],{path:'/api/embodied/start',body:{camera:service.camera,app:'ski'}});
 tick();await settle();assert.ok(all(root).includes(frame),'Status polling must not recreate the live game frame');
 await button('结束游戏').onclick();await settle();assert.equal(writes[1].path,'/api/embodied/game/stop');assert.equal(service.owned,true);assert.ok(!writes.some(w=>w.path==='/api/embodied/stop'));
 all(root).find(n=>n.tagName==='button'&&n.children[0]?.textContent==='全屋互动').onclick();button('前往互动控制').onclick();assert.deepEqual(navigated,['Interaction']);
 owner.dispose();console.log('Embedded game, retained frame, game-only stop and current navigation: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
