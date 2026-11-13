// Execute the full shipped game entry, including its first synchronous draw.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2]||'homeplane/embodied/static/games.js','utf8');
let animationRequests=0,sockets=0;const nodes=new Map(),documentEvents={},windowEvents={};
const context=new Proxy({createLinearGradient:()=>({addColorStop(){}}),createPattern:()=>null},{get:(o,k)=>k in o?o[k]:()=>{}});
class Element{
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.width=1600;this.height=900;this.dataset={};this.children=[];this.attrs={};this._text='';this.innerHTML='';const classes=new Set();this.classList={add:(...names)=>names.forEach(name=>classes.add(name)),remove:(...names)=>names.forEach(name=>classes.delete(name)),contains:name=>classes.has(name),toggle:(name,force)=>{const on=force??!classes.has(name);if(on)classes.add(name);else classes.delete(name);return on;}};}
 get style(){return this.__style??={setProperty(){}};}
 get parentElement(){return this.parentNode??null;}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
 append(...children){for(const child of children){child.parentNode=this;this.children.push(child);}}
 replaceChildren(...children){this.children=[];this.append(...children);}
 getContext(){return context;}
 getBoundingClientRect(){return{width:1600,height:900};}
 querySelector(){return null;}
 querySelectorAll(){return[];}
 addEventListener(){}
 setAttribute(key,value){this.attrs[key]=String(value);}
 getAttribute(key){return this.attrs[key]??null;}
}
function node(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);}
const sandbox={navigator:{userAgent:'Friday desktop test'},matchMedia:()=>({matches:false,addEventListener(){}}),document:{getElementById:node,createElement:tag=>new Element(tag),querySelectorAll:()=>[],querySelector:()=>null,addEventListener(k,f){documentEvents[k]=f;},dispatchEvent(){},body:node('body'),documentElement:{}},CustomEvent:class{},AbortController,
 Image:class{constructor(){this.complete=false;this.naturalWidth=0;this.src='';}},sessionStorage:{getItem:()=>null,setItem(){}},history:{replaceState(){}},location:{search:'',href:'http://127.0.0.1:8860/games',hostname:'127.0.0.1',host:'127.0.0.1:8860',pathname:'/games',protocol:'http:'},URL,URLSearchParams,performance:{now:()=>200},devicePixelRatio:1,Date,console,WebSocket:class{constructor(){sockets++;}close(){}},requestAnimationFrame(){animationRequests++;},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){},window:{addEventListener(k,f){windowEvents[k]=f;}}};
vm.createContext(sandbox);
const html=fs.readFileSync('homeplane/embodied/static/games.html','utf8');
const progressTrack=new Element();progressTrack.setAttribute('role','progressbar');progressTrack.append(node('progress'));
node('gameTutorial').hidden=true;node('playerView').hidden=true;
for(const match of html.matchAll(/<script defer src="\/assets\/([a-z-]+\.js)"><\/script>/g)){
 const path='homeplane/embodied/static/'+match[1];sandbox.document.currentScript={src:'http://127.0.0.1:8860/assets/'+match[1]};
 vm.runInContext(match[1]==='games.js'?source:fs.readFileSync(path,'utf8'),sandbox,{timeout:1000});
}
assert.equal(sockets,1);assert.equal(animationRequests,1,'The first draw must schedule the continuing animation loop');
vm.runInContext('updateRecording({armed:false,status:"off"});draw();',sandbox,{timeout:1000});
assert.equal(animationRequests,2);assert.equal(node('recordToggle').textContent,'录制本局');assert.equal(node('recordToggle').disabled,true);
if(require.main===module){
 const drawAt=at=>vm.runInContext(`draw(${at});lastScenePaint`,sandbox,{timeout:1000});
 vm.runInContext('active=true;lastScenePaint=1000;',sandbox);
 assert.equal(drawAt(1016.7),1000,'single 60 Hz refresh does not exceed the 30 fps budget');
 assert.equal(drawAt(1033.3),1033.3,'rounded timestamps should not skip a 30 fps paint');
 assert.equal(drawAt(1050),1033.3);
 assert.equal(drawAt(1066.6),1066.6);
 console.log('Game bootstrap, animation cadence and disabled recording: OK');
}
module.exports={sandbox,node,documentEvents,windowEvents};
