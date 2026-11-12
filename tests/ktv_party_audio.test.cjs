const test = require('node:test');
const assert = require('node:assert/strict');
const {SfxCommands, vocalView, capabilities, commandObserved} = require('../integrations/ktv_public/web/party-audio.js');
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };

test('portable software effects expose all seven supported presets and wait for DSP receipt', () => {
  const keys=['dry','pop','ktv','power','opera','studio','echo'];
  const base={online:true,audio_capabilities:{vocal_presets:keys,sfx:true},vocal_preset_state:'pending'};
  assert.deepEqual(capabilities(base).vocal,keys);
  for (const [key,label] of [['studio','清晰小房间'],['echo','短回声']]) {
    const pending=vocalView({vocal_preset:key},{...base,vocal_preset:key});
    assert.equal(pending.text,'已发送，等待电视确认');
    assert.equal(vocalView({vocal_preset:key},{...base,vocal_preset:key,vocal_preset_state:'applied'}).text,'已生效 · '+label);
  }
  assert.equal(capabilities({...base,audio_capabilities:{vocal_presets:[...keys,'unknown'],sfx:true}}).ready,false);
});

test('unsupported TV has no preset or atmosphere claim; requested is not applied', () => {
  assert.deepEqual(capabilities({}), {ready:false,vocal:[],sfx:false});
  const playback={online:true,audio_capabilities:{vocal_presets:['dry','pop'],sfx:true},vocal_preset:'pop',vocal_preset_state:'requested'};
  assert.equal(vocalView({vocal_preset:'pop'},playback).text,'已选择 · 流行音效');
  assert.equal(vocalView({vocal_preset:'pop'},{...playback,vocal_preset_state:'applied'}).text,'已生效 · 流行音效');
  assert.deepEqual(vocalView({vocal_preset:'pop'},{...playback,online:false}).supported,[]);
  assert.equal(vocalView({vocal_preset:'dry'},{online:true,audio_capabilities:{vocal_presets:['dry'],sfx:false},vocal_preset:'dry',vocal_preset_state:'requested'}).text,'当前通道仅支持原声');
});

test('SFX HTTP receipt alone never claims playback; exact action and sequence ACK required', async () => {
  const commands=new SfxCommands({send:async()=>({seq:7}),requestId:()=> 'one'});
  await commands.trigger('sfx_applause');
  assert.equal(commands.state.phase,'sent');
  commands.observe({online:true,sfx_seq:7,sfx_action:'sfx_applause',sfx_state:'pending'});
  assert.equal(commands.state.phase,'sent');
  commands.observe({online:true,last_command:7,sfx_seq:6,sfx_action:'sfx_applause',sfx_state:'playing'});
  commands.observe({online:true,sfx_seq:7,sfx_action:'sfx_cheer',sfx_state:'playing'});
  assert.equal(commands.state.phase,'sent');
  commands.observe({online:true,sfx_seq:7,sfx_action:'sfx_applause',sfx_state:'playing'});
  assert.equal(commands.state.phase,'played');
});

test('stop serializes only HTTP, never the preceding playback ACK', async () => {
  const first=deferred(), sent=[];
  const commands=new SfxCommands({send:payload=>{sent.push(payload);return sent.length===1?first.promise:Promise.resolve({seq:2});},requestId:()=>String(sent.length)});
  const applause=commands.trigger('sfx_applause');
  await Promise.resolve();await Promise.resolve();
  const stop=commands.trigger('sfx_stop');
  first.resolve({seq:1});await applause;await stop;
  assert.deepEqual(sent.map(v=>[v.action,v.song]),[['sfx_applause',''],['sfx_stop','']]);
  assert.equal(commands.state.seq,2);assert.equal(commands.state.phase,'sent');
  commands.observe({online:true,sfx_seq:1,sfx_action:'sfx_applause',sfx_state:'playing'});
  assert.equal(commands.state.action,'sfx_stop');assert.equal(commands.state.phase,'sent');
  commands.observe({online:true,sfx_seq:2,sfx_action:'sfx_stop',sfx_state:'stopped'});
  assert.equal(commands.state.phase,'stopped');
});

test('reconnect never resends an unconfirmed sound', async () => {
  let time=0,calls=0;
  const commands=new SfxCommands({send:async()=>{calls++;return {seq:8};},now:()=>time,requestId:()=> 'one'});
  await commands.trigger('sfx_cheer');time=8000;
  commands.observe({online:false});assert.equal(commands.state.phase,'unconfirmed');
  commands.observe({online:true,sfx_seq:9,sfx_action:'sfx_stop',sfx_state:'stopped'});
  assert.equal(commands.state.phase,'superseded');assert.equal(calls,1);
  await assert.rejects(commands.trigger('https://example.test/sound.mp3'));
  assert.equal(calls,1);
});


test('play receipts require matching current playback and replay needs a timeline reset',()=>{
  const before={online:true,current:'song',state:'playing',position_ms:30000,position_at:100000,last_command:4,played:[]};
  const acknowledged={...before,last_command:5,position_ms:30200,position_at:100200};
  assert.equal(commandObserved('play','song',before,acknowledged,5),false);
  assert.equal(commandObserved('play','other',before,acknowledged,5),false);
  assert.equal(commandObserved('play','song',before,{...acknowledged,state:'preparing'},5),true);
  assert.equal(commandObserved('play','song',before,{...acknowledged,position_ms:100},5),true);
  assert.equal(commandObserved('resume','song',before,{...acknowledged,state:'paused'},5),false);
  assert.equal(commandObserved('pause','song',before,acknowledged,5),false);
});


test('failed wet startup is visible even when only dry capability remains',()=>{
 const playback={online:true,audio_capabilities:{vocal_presets:['dry'],sfx:true},vocal_preset:'dry',vocal_preset_state:'failed'};
 assert.equal(vocalView({vocal_preset:'dry'},playback).text,'电视混响启动失败，暂不可用');
 assert.equal(vocalView({vocal_preset:'dry'},{...playback,vocal_preset_state:'unavailable'}).text,'音频通道尚未就绪，请查看电视提示');
});
