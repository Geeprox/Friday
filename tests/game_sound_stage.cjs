/* Full HTML-declared game entry; WebAudio/network/canvas are inert API doubles. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const trace=JSON.parse(fs.readFileSync(0,'utf8')),{sandbox,node,documentEvents,windowEvents}=require('./embodied_games_boot.cjs');
Object.assign(sandbox,{ArrayBuffer,DataView,Uint8Array,TextDecoder,Blob,cancelAnimationFrame(){}});
let now=200,created=0,closed=0;const notes=[];
sandbox.performance.now=()=>now;sandbox.fetch=async()=>({ok:true,json:async()=>({})});
sandbox.AudioContext=class{
 constructor(){created++;this.state='suspended';this.currentTime=0;this.destination={};}
 resume(){this.state='running';return Promise.resolve();}
 close(){closed++;this.state='closed';return Promise.resolve();}
 createOscillator(){const o={frequency:{setValueAtTime(value){o.hz=value;}},connect(){},disconnect(){},start(){notes.push(o.hz);},stop(){}};return o;}
 createGain(){return{gain:{setValueAtTime(){},linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){},disconnect(){}};}
};
vm.runInContext('winterProps.complete=winterScene.complete=true;winterProps.naturalWidth=winterScene.naturalWidth=100;scenery.ready=()=>true;',sandbox);
const ws=vm.runInContext('gameSocket',sandbox);ws.onopen();
function deliver(game){
 const bytes=new TextEncoder().encode(JSON.stringify({status:'live',game,simulated:true,people:[],recording:{armed:false,status:'off'}})),data=new ArrayBuffer(bytes.length+8),view=new DataView(data);view.setUint32(0,bytes.length);new Uint8Array(data,8).set(bytes);now+=100;ws.onmessage({data});assert.equal(vm.runInContext('meta?.game.control_context',sandbox),game.control_context,'The full original packet must reach the real stage receiver');
}
async function main(){
 assert.equal(created,0);deliver(trace.packets[0]);assert.equal(created,0);
 const toggle=node('soundToggle'),markup=fs.readFileSync('homeplane/embodied/static/games.html','utf8').match(/<button id="soundToggle"[^>]*data-click="([^"]+)"/);assert(markup);toggle.setAttribute('data-click',markup[1]);
 documentEvents.click({target:{closest:()=>toggle}});assert.equal(created,1);await new Promise(setImmediate);assert.equal(toggle.getAttribute('aria-pressed'),'true');notes.length=0;
 for(const game of trace.packets.slice(1))deliver(game);
 assert.equal(notes.length,trace.expected_notes);windowEvents.pagehide();assert.equal(closed,1);assert.equal(toggle.getAttribute('aria-pressed'),'false');
 process.stdout.write(JSON.stringify({original_packets:trace.packets.length,outcome_notes:notes.length,closed,actual_delegated_click:true}));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
