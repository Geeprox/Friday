const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.textContent='';this.style={};this.value='';this.disabled=false;this.hidden=false;}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='value')this.value=String(v);}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parent=this;}if(this.tagName==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
 get isConnected(){return true;}
 getBoundingClientRect(){return {left:0,top:0,width:640,height:360};}
}
(async()=>{
 const cameras=['cam_1111111111','cam_2222222222','cam_3333333333'].map((id,i)=>({id,kind:'camera',room:['客厅','健身房','主卧'][i],name:'监控'}));
 let config={revision:0,enabled:true,bindings:[]},policy={revision:0,enabled:true,recognition:true,interaction:true},owned=true;
 const writes=[],messages=[],root=new Element('main'),all=n=>[n,...n.children.flatMap(all)],find=(tag,text)=>all(root).find(n=>n.tagName===tag&&n.textContent===text),byLabel=label=>all(root).find(n=>n.attrs['aria-label']===label);
 const targets=[{id:'lamp',kind:'light',room:'客厅',name:'主灯',controllable:true,actions:['on','off']},{id:'curtain',kind:'cover',room:'客厅',name:'梦幻帘',controllable:true,actions:['open','close','position:50']}];
 const status=()=>({owned,voice_input:true,quiet:true,camera_sessions:cameras.map(c=>({camera:c.id,state:owned?'live':'stopped'})),interactions:{cameras:[],events:[]}});
 const fetch=async(path,options={})=>({ok:true,json:async()=>{
  if(options.method==='POST'){
   const body=JSON.parse(options.body);writes.push({path,body});
   if(path==='/api/embodied/infrastructure'){policy={...body,revision:policy.revision+1};return {policy};}
   if(path==='/api/home/interactions'){config={...body,revision:config.revision+1};return config;}
   throw Error('unexpected write '+path);
  }
  if(path==='/api/home/interactions')return{config,gestures:{'hand.ok':'OK','body.arms_open':'双臂打开'}};
  if(path==='/api/embodied/devices')return{devices:targets};
  if(path==='/api/embodied/status')return status();
  if(path==='/api/embodied/infrastructure')return {policy};
  throw Error('unexpected read '+path);
 }});
 const sandbox={document:{createElement:t=>new Element(t)},fetch,crypto:require('node:crypto').webcrypto,setInterval:()=>1,clearInterval(){},Date,console};
 const context=vm.createContext(sandbox);context.root=root;context.options={data:{devices:cameras},go(){},toast:(...m)=>messages.push(m)};
 vm.runInContext(fs.readFileSync('homeplane/static/interactions.js','utf8').replace('export function mountInteractions','function mountInteractions'),context);
 const owner=vm.runInContext('mountInteractions(root,options)',context),settle=()=>new Promise(setImmediate);await settle();
 assert.equal(all(root).filter(n=>n.attrs.class==='interaction-camera').length,3);
 assert.ok(!byLabel('互动摄像头'));assert.ok(!byLabel('查看与标定镜头'));
 await find('button','暂停互动').onclick();await settle();
 assert.equal(writes[0].path,'/api/embodied/infrastructure');assert.equal(writes[0].body.interaction,false);assert.equal(writes[0].body.recognition,true);assert.equal(writes[0].body.camera,undefined);
 await find('button','恢复互动').onclick();await settle();assert.equal(policy.interaction,true);
 const target=byLabel('控制设备');target.value='lamp';target.onchange();
 const form=all(root).find(n=>n.tagName==='form');form.onsubmit({preventDefault(){}});await settle();
 assert.equal(config.bindings[0].camera,null);assert.equal(config.bindings[0].target,'lamp');
 await find('button','指哪儿控哪儿').onclick();
 const camera=byLabel('查看与标定镜头');camera.value=cameras[2].id;camera.onchange();
 const image=all(root).find(n=>n.tagName==='img');image.naturalWidth=640;image.onclick({clientX:320,clientY:180});
 const pointingTarget=byLabel('控制设备');pointingTarget.value='curtain';pointingTarget.onchange();
 all(root).find(n=>n.tagName==='form').onsubmit({preventDefault(){}});await settle();
 assert.equal(config.bindings[1].camera,cameras[2].id);assert.equal(config.bindings[1].gesture,'hand.point');assert.deepEqual(config.bindings[1].anchor,[.5,.5]);
 assert.equal(writes.filter(w=>['/api/embodied/start','/api/embodied/stop'].includes(w.path)).length,0,'Interaction policy and preview choice must never restart recognition');
 owner.dispose();console.log('Whole-house status, camera-independent bindings and preview-only camera selection: OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
