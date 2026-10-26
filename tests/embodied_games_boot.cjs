// Execute the full shipped game entry, including its first synchronous draw.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2]||'homeplane/embodied/static/games.js','utf8');
let animationRequests=0,sockets=0;const nodes=new Map();
const context=new Proxy({createLinearGradient:()=>({addColorStop(){}})},{get:(o,k)=>k in o?o[k]:()=>{}});
function node(id){if(!nodes.has(id))nodes.set(id,{width:1600,height:900,style:{},dataset:{},textContent:'',innerHTML:'',getContext:()=>context,getBoundingClientRect:()=>({width:1600,height:900}),querySelector:()=>({}),querySelectorAll:()=>[],addEventListener(){},setAttribute(){},classList:{add(){},remove(){}}});return nodes.get(id);}
const sandbox={navigator:{userAgent:'Friday desktop test'},document:{getElementById:node,querySelectorAll:()=>[],addEventListener(){},documentElement:{}},sessionStorage:{getItem:()=>null,setItem(){}},history:{replaceState(){}},location:{search:'',hostname:'127.0.0.1',host:'127.0.0.1:8860',pathname:'/games',protocol:'http:'},URLSearchParams,performance:{now:()=>200},devicePixelRatio:1,Date,console,WebSocket:class{constructor(){sockets++;}},requestAnimationFrame(){animationRequests++;},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){},window:{}};
vm.createContext(sandbox);vm.runInContext(source,sandbox,{timeout:1000});
assert.equal(sockets,1);assert.equal(animationRequests,1,'The first draw must schedule the continuing animation loop');
vm.runInContext('updateRecording({armed:false,status:"off"});draw();',sandbox,{timeout:1000});
assert.equal(animationRequests,2);assert.equal(node('recordToggle').textContent,'自动录制：关');
console.log('Game bootstrap, animation continuation and disabled recording: OK');
