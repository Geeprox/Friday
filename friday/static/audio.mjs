export function resample(input, rate, target = 16000) {
  if (rate === target) return new Float32Array(input);
  const length = Math.floor(input.length * target / rate), out = new Float32Array(length);
  for (let i = 0; i < length; i++) {
    const from = i * rate / target, to = (i + 1) * rate / target;
    let sum = 0, weight = 0;
    for (let j = Math.floor(from); j < Math.ceil(to) && j < input.length; j++) {
      const w = Math.min(to, j + 1) - Math.max(from, j);
      sum += input[j] * w; weight += w;
    }
    out[i] = weight ? sum / weight : 0;
  }
  return out;
}

export function decodeG711(data, codec) {
  return Float32Array.from(data, byte => {
    if (codec === 'g711u') {
      const u = (~byte) & 255;
      let value = (((u & 15) << 3) + 132) << ((u & 112) >> 4);
      return ((u & 128) ? 132 - value : value - 132) / 32768;
    }
    const a = byte ^ 0x55, segment = (a & 112) >> 4;
    let value = (a & 15) << 4;
    value += segment === 0 ? 8 : 264;
    if (segment > 1) value <<= segment - 1;
    return ((a & 128) ? value : -value) / 32768;
  });
}

export function wavBytes(chunks) {
  const count = chunks.reduce((n, a) => n + a.length, 0);
  const buffer = new ArrayBuffer(44 + count * 2), v = new DataView(buffer);
  const text = (offset, s) => [...s].forEach((c, i) => v.setUint8(offset + i, c.charCodeAt(0)));
  text(0, 'RIFF'); v.setUint32(4, 36 + count * 2, true); text(8, 'WAVE'); text(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); text(36, 'data'); v.setUint32(40, count * 2, true);
  let offset = 44;
  for (const chunk of chunks) for (const value of chunk) {
    v.setInt16(offset, Math.round(Math.max(-1, Math.min(1, value)) * 32767), true); offset += 2;
  }
  return new Uint8Array(buffer);
}

export class SpeechGate {
  constructor(onSpeech, onLevel) { this.onSpeech = onSpeech; this.onLevel = onLevel; this.reset(); }
  reset() { this.chunks = []; this.pre = []; this.samples = 0; this.silent = 0; this.voiced = 0; this.saturated = false; }
  feed(input, rate) {
    const samples = resample(input, rate);
    if (!samples.length) return;
    const rms = Math.sqrt(samples.reduce((sum, v) => sum + v * v, 0) / samples.length);
    this.onLevel(Math.min(1, rms * 12));
    const voice = rms > 0.015;
    if (this.saturated) {
      this.silent = voice ? 0 : this.silent + samples.length;
      if (this.silent > 10400) this.reset();
      return;
    }
    if (!this.chunks.length) {
      this.pre.push(samples);
      while (this.pre.reduce((n, a) => n + a.length, 0) > 4800) this.pre.shift();
      if (!voice) return;
      this.chunks = [...this.pre]; this.pre = [];
      this.samples = this.chunks.reduce((n, a) => n + a.length, 0);
      this.voiced = samples.length;
    } else { this.chunks.push(samples); this.samples += samples.length; if (voice) this.voiced += samples.length; }
    this.silent = voice ? 0 : this.silent + samples.length;
    if (this.samples >= 96000) { this.reset(); this.saturated = true; return; } // discard overlong speech, never execute its tail
    if (this.silent >= 10400) {
      const chunks = this.chunks, accept = this.voiced >= 4800;
      this.reset(); if (accept) this.onSpeech(wavBytes(chunks));
    }
  }
}
