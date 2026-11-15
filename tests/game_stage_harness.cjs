/* Real production functions with inert DOM, WebSocket and network ports. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('homeplane/embodied/static/games.js','utf8');
function part(begin,end){const a=source.indexOf(begin),b=source.indexOf(end,a);assert(a>=0&&b>a);return source.slice(a,b);}
const game=(extra={})=>({control_context:'a'.repeat(32),control_revision:1,round_id:1,
 mode:'quest',selected_mode:'quest',status:'playing',...extra});
function packet(g,jpeg=0){const bytes=new TextEncoder().encode(JSON.stringify({game:g,simulated:true,status:'live'})),data=new ArrayBuffer(bytes.length+8+jpeg),view=new DataView(data);view.setUint32(0,bytes.length);view.setUint32(4,jpeg);new Uint8Array(data,8).set(bytes);return data;}
function makeClient(extra={}){
 let at=0,nextTimer=0;const timers=new Map(),sockets=[],calls=[],nodes=new Map(),images=[],readers=[];
 function node(id){if(!nodes.has(id))nodes.set(id,{id,textContent:'',innerHTML:'',disabled:false,dataset:{},attrs:id==='start'?{'data-click':'startGame()'}:{},children:[],replaceChildren(...children){this.children=children;},append(...children){this.children.push(...children);},setAttribute(k,v){this.attrs[k]=v;this[k]=v;},getAttribute(k){return this.attrs[k]??null;}});return nodes.get(id);}
 let c;
 c=vm.createContext({meta:{game:game()},selected:'quest',active:true,received:0,lastAcceptedAt:0,
  streamOpen:true,streamGeneration:1,gameSocket:null,reconnectTimer:null,stageClosed:false,commandPending:null,
  lastConnectionFresh:true,socketAttemptAt:0,debugReceived:null,decodeOwner:0,debugFrame:null,debugMeta:null,decoding:false,
  firstPacket:false,lastBackendMode:'quest',lastBackendRound:'quest',stageFrame:3,key:'',television:false,
  AbortController,ArrayBuffer,DataView,Uint8Array,TextDecoder,Blob,performance:{now:()=>at},
  location:{host:'127.0.0.1',protocol:'http:'},document:{hidden:false,createElement:tag=>({tagName:tag.toUpperCase()}),querySelectorAll:()=>[node('mode'),node('start')],querySelector:()=>node('actions')},
  $:node,url:p=>p,fetch:async()=>{throw Error('unexpected network');},
  selectMode:mode=>{c.selected=mode;calls.push(['selected',mode]);},updateHUD:()=>calls.push(['updated']),
  skiScenery:{receive(){}},scenery:{receive(){},ready:()=>true},winterReady:()=>true,beamStage:{forget(){},receive(){},ready:()=>true},presentationClient:{tick(){},ready:()=>true,release(){},close(){}},gameSound:{quiet(){},close(){},tick(){}},
  setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
  cancelAnimationFrame:id=>calls.push(['cancelled-frame',id]),
  WebSocket:function(url){this.url=url;this.close=()=>{this.closed=true;};sockets.push(this);},
  Image:function(){images.push(this);},FileReader:function(){this.result='data:synthetic-only';this.readAsDataURL=()=>{};readers.push(this);},
  ...extra});
 vm.runInContext(part('const tennisModes=', '\nif(embedded||television)'),c);
 vm.runInContext(part('let renderedOverlay=null;', '\nfunction updateSourceMode('),c);
 vm.runInContext(part('const legacyQuestStages=', '\nfunction updateQuestHUD('),c);
 vm.runInContext(part('async function commandGame(', '\nfunction selectMode('),c);
 vm.runInContext(part('async function startGame(', '\nfunction tennisPresentation('),c);
 vm.runInContext(part('async function pauseGame()', '\nfunction togglePlayerView('),c);
 vm.runInContext(part('function streamLost(', '\nfunction updateHUD('),c);
 vm.runInContext(part('function closeGameView()', "\nwindow.addEventListener('pagehide'"),c);
 return {c,node,calls,sockets,images,readers,timers,setNow:value=>{at=value;},fire:ms=>{const [id,t]=[...timers].find(([,t])=>t.ms===ms)||[];assert(t,'missing timer '+ms);timers.delete(id);t.fn();}};
}
module.exports={source,part,game,packet,makeClient};
