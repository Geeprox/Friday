/* 真实设置与语音组件配合合成 DOM，不打开浏览器麦克风或发送网络请求。 */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {Element:Base}=require('./games_library_harness.cjs');
const walk=node=>[node,...node.children.flatMap(walk)],settle=()=>new Promise(setImmediate);
class Element extends Base{
 constructor(tag){super(tag);this.listeners=new Map();this.readOnly=false;this.events=[];}
 setAttribute(key,value){super.setAttribute(key,value);if(['id','type','class'].includes(key))this[key==='class'?'className':key]=String(value);}
 get isConnected(){return this===this.ownerDocument.body||this===this.ownerDocument.head||!!this.parent?.isConnected;}
 addEventListener(type,handler){const entries=this.listeners.get(type)||[];entries.push(handler);this.listeners.set(type,entries);}
 removeEventListener(type,handler){this.listeners.set(type,(this.listeners.get(type)||[]).filter(item=>item!==handler));}
 async fire(type,fields={}){for(const handler of this.listeners.get(type)||[])await handler({type,preventDefault(){},stopPropagation(){},...fields});}
 dispatchEvent(event){this.events.push(event.type);return true;}
 closest(tag){return this.parent?.tagName===tag?this.parent:this.parent?.closest(tag)||null;}
 querySelectorAll(selector){return walk(this).slice(1).filter(node=>selector.startsWith('[')?Object.hasOwn(node.attrs,selector.slice(1,-1)):selector.startsWith('.')?node.className.split(' ').includes(selector.slice(1)):node.tagName===selector);}
 remove(){super.remove();this.parent=null;}
}
function rig(file,exported,extra={}){
 const document={listeners:new Map(),hidden:false,createElement(tag){const node=new Element(tag);node.ownerDocument=this;return node;},addEventListener(type,fn){this.listeners.set(type,fn);},removeEventListener(type){this.listeners.delete(type);}};
 document.body=document.createElement('body');document.head=document.createElement('head');document.getElementById=id=>[...walk(document.body),...walk(document.head)].find(node=>node.id===id)||null;
 const timers=new Map();let timer=0;
 const context=vm.createContext({document,console,Date,URL,Event,ArrayBuffer,location:{href:'http://127.0.0.1:8740/'},setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),...extra});
 vm.runInContext(fs.readFileSync(file,'utf8').replace(/^export /gm,''),context);
 return {document,timers,context,mount:vm.runInContext(exported,context),create:tag=>document.createElement(tag)};
}
function report(){return {config:{provider:'arl',cloud_text:false,allow_arl_fallback:true,cloud_images:false,cloud_voice:false,background_with_mv:false,cached_background_limit:128,responses_model:'gpt-6-luna',decisions_model:'gpt-6-luna',image_model:'gpt-image-2.5-flare',realtime_model:'gpt-realtime-2.1',daily_requests:100,daily_text_tokens:100000,daily_images:5,daily_voice_seconds:300,timezone:'Asia/Shanghai',max_output_tokens:2048},api_key_configured:false,usage:{used:{requests:2,text_tokens:40,images:1,voice_seconds:0},limits:{requests:100,text_tokens:100000,images:5,voice_seconds:300},resets_at:'2026-11-11T16:00:00Z',uncertain_requests:0}};}
test('设置保存选择和每日上限，已有 Key 不回显或留在输入框',async()=>{
 const r=rig('homeplane/static/intelligence-settings.js','mountIntelligenceSettings'),calls=[];let value=report();
 const api=async(path,options={})=>{calls.push({path,...options});if(options.method==='PUT')value.config={...value.config,...JSON.parse(options.body)};if(path.endsWith('/credential'))value.api_key_configured=true;return structuredClone(value);};
 await r.mount(r.document.body,{api});const byId=id=>r.document.getElementById('intelligence-'+id),button=text=>walk(r.document.body).find(node=>node.tagName==='button'&&node.textContent===text);
 byId('provider').value='openai';byId('cloud_text').checked=true;byId('daily_requests').value='0';await button('保存智能配置').fire('click');
 assert.equal(value.config.provider,'openai');assert.equal(value.config.daily_requests,0);assert.equal(value.config.cloud_text,true);
 const secret=byId('api-key');assert.equal(secret.type,'password');secret.value='SIMULATED-private-key';await button('保存已有 Key').fire('click');
 assert.equal(secret.value,'');assert(!r.document.body.textContent.includes('SIMULATED-private-key'));assert.equal(calls.filter(call=>call.method==='PUT').length,1);
 assert(calls.every(call=>call.path.startsWith('/api/settings/intelligence')));
});
function voiceRig(){
 const sockets=[];let grant;
 class Socket{static OPEN=1;constructor(url){this.url=String(url);this.readyState=1;this.sent=[];sockets.push(this);}send(value){this.sent.push(value);}close(){this.readyState=3;queueMicrotask(()=>this.onclose?.());}receive(value){this.onmessage?.({data:JSON.stringify(value)});}}
 const r=rig('homeplane/static/intelligence-voice.js','mountVoiceInput',{navigator:{mediaDevices:{getUserMedia:()=>new Promise(resolve=>{grant=resolve;})}},WebSocket:Socket,AudioWorkletNode:class{}});
 const form=r.create('form'),controls=r.create('div'),input=r.create('textarea');form.append(input,controls);r.document.body.append(form);input.value='原有草稿';
 const owner=r.mount(controls,input,{api:async()=>({config:{cloud_voice:true}})});
 return {...r,owner,input,controls,sockets,button:controls.children[0],grant:stream=>grant(stream)};
}
test('配额不足保留草稿与文字发送能力',async()=>{
 const r=voiceRig();await settle();await r.button.fire('click');r.sockets[0].receive({event:'unavailable',code:'daily_voice_seconds_reached'});
 await settle();assert.equal(r.input.value,'原有草稿');assert.equal(r.input.readOnly,false);assert.equal(r.sockets[0].readyState,3);assert.equal(r.timers.size,0);r.owner.dispose();
});
test('离开页面后迟到的麦克风授权立即停止，不上传音频',async()=>{
 const r=voiceRig();await settle();await r.button.fire('click');r.sockets[0].receive({event:'ready'});r.owner.dispose();let stopped=0;
 r.grant({getTracks:()=>[{stop(){stopped++;}}]});await settle();assert.equal(stopped,1);assert.equal(r.sockets[0].sent.length,0);assert.equal(r.timers.size,0);
});
test('语音插入光标位置且不自动发送，旧回复不覆盖变化后的草稿',async()=>{
 const r=voiceRig();await settle();r.input.selectionStart=2;r.input.selectionEnd=2;await r.button.fire('click');r.sockets[0].receive({event:'transcript',text:'语音'});
 assert.equal(r.input.value,'原有语音草稿');assert.deepEqual(r.input.events,['input']);
 await r.button.fire('click');r.input.value='新的草稿';r.sockets[1].receive({event:'transcript',text:'迟到文字'});assert.equal(r.input.value,'新的草稿');r.owner.dispose();
});
test('Worklet 生成 24 kHz PCM，结束时保留最后一包',()=>{
 const packets=[];let Processor;class Audio{constructor(){this.port={postMessage:value=>packets.push(value)};}}
 vm.runInNewContext(fs.readFileSync('homeplane/static/intelligence-voice-worklet.js','utf8'),{AudioWorkletProcessor:Audio,sampleRate:48000,Int16Array,Math,registerProcessor:(name,type)=>{assert.equal(name,'friday-voice-input');Processor=type;}});
 const worker=new Processor();worker.process([[new Float32Array(4800).fill(.25)]]);worker.port.onmessage({data:'flush'});
 const pcm=packets.filter(value=>value instanceof ArrayBuffer).flatMap(value=>Array.from(new Int16Array(value)));
 assert.equal(pcm.length,2400);assert(pcm.every(value=>value===8192));assert.equal(packets.at(-1).event,'flushed');
});
