import test from 'node:test';
import assert from 'node:assert/strict';
import {SpeechGate, wavBytes, decodeG711, resample} from '../friday/static/audio.mjs';

test('PCM WAV is mono 16 kHz with correct duration',()=>{
  const bytes=wavBytes([new Float32Array(16000)]),v=new DataView(bytes.buffer);
  assert.equal(v.getUint32(24,true),16000);assert.equal(v.getUint32(40,true),32000);assert.equal(bytes.length,32044);
});
test('silence never submits; speech submits once after end',()=>{
  const outputs=[],gate=new SpeechGate(b=>outputs.push(b),()=>{});
  for(let i=0;i<100;i++)gate.feed(new Float32Array(320),16000);
  assert.equal(outputs.length,0);
  for(let i=0;i<25;i++)gate.feed(new Float32Array(320).fill(.1),16000);
  for(let i=0;i<40;i++)gate.feed(new Float32Array(320),16000);
  assert.equal(outputs.length,1);
});
test('overlong speech is discarded, including its tail',()=>{
  const outputs=[],gate=new SpeechGate(b=>outputs.push(b),()=>{});
  for(let i=0;i<400;i++)gate.feed(new Float32Array(320).fill(.1),16000);
  for(let i=0;i<40;i++)gate.feed(new Float32Array(320),16000);
  assert.equal(outputs.length,0);
});
test('G711 silence and rate conversion',()=>{
  assert.equal(decodeG711(new Uint8Array([255]),'g711u')[0],0);
  assert.equal(decodeG711(new Uint8Array([213]),'g711a')[0],8/32768);
  assert.equal(resample(new Float32Array(480),48000).length,160);
});
