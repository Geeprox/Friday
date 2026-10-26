import {SpeechGate, decodeG711} from './audio.mjs';

function base64(bytes) {
  let s = ''; for (let i = 0; i < bytes.length; i += 8192) s += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(s);
}

export class Capture {
  constructor(canvas, video, callbacks) {
    this.canvas = canvas; this.video = video; this.cb = callbacks; this.active = false;
    this.sockets = []; this.decoders = []; this.frames = []; this.session = crypto.randomUUID();
    this.gate = new SpeechGate(wav => this.submit(wav), level => this.cb.level(level));
  }
  async start(source, token) {
    this.active = true; this.token = token; this.source = source;
    this.lastVideo = this.lastAudio = Date.now();
    if (source === 'browser') await this.browser(); else await this.miloco();
    if (!this.active) return;
    this.timer = setInterval(() => {
      if (!this.active) return;
      if (Date.now() - this.lastVideo > 8000 || Date.now() - this.lastAudio > 8000) {
        this.cb.error('音视频流中断，监听已停止。检查摄像头或重新连接。'); return;
      }
      if (source === 'browser' && this.video.readyState >= 2) this.draw(this.video);
      if (Date.now() - this.lastVideo < 1200) {
        this.frames.push({at: Date.now(), image: this.canvas.toDataURL('image/jpeg', .65).split(',')[1]});
        this.frames = this.frames.filter(f => Date.now() - f.at < 3200).slice(-8);
      }
    }, 400);
  }
  draw(frame) {
    if (!this.active) return;
    // Fixed normalized image geometry; preview and submitted frames have identical dimensions.
    this.canvas.getContext('2d').drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
    this.lastVideo = Date.now(); if (!this.busy) this.cb.state('画面已连接 · 等待语音');
  }
  audio(samples, rate) {
    if (!this.active) return;
    this.lastAudio = Date.now();
    if (!this.busy) this.gate.feed(samples, rate);
  }
  async browser() {
    this.stream = await navigator.mediaDevices.getUserMedia({video: {width: 960, height: 540},
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}});
    if (!this.active) { this.stream.getTracks().forEach(t => t.stop()); return; }
    this.stream.getTracks().forEach(t => t.addEventListener('ended', () => this.cb.error('摄像头或麦克风已断开。')));
    this.video.srcObject = this.stream; await this.video.play();
    if (!this.active) return;
    this.context = new AudioContext(); await this.context.resume();
    await this.context.audioWorklet.addModule('/static/pcm-worklet.js');
    if (!this.active) return;
    this.input = this.context.createMediaStreamSource(this.stream);
    this.worklet = new AudioWorkletNode(this.context, 'pcm-capture');
    this.worklet.port.onmessage = e => this.audio(e.data, this.context.sampleRate);
    this.mute = this.context.createGain(); this.mute.gain.value = 0;
    this.input.connect(this.worklet).connect(this.mute).connect(this.context.destination);
  }
  async socket(kind, onData) {
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/camera/${kind}`);
    this.sockets.push(socket); socket.binaryType = 'arraybuffer';
    socket.onmessage = e => {
      if (!this.active) return;
      try {
        if (typeof e.data === 'string') {
          const msg = JSON.parse(e.data);
          if (msg.type === 'error') throw new Error(msg.message);
          onData(msg);
        } else onData(e.data);
      } catch { this.cb.error(`摄像头 ${kind} 解码失败，请检查格式与连接。`); }
    };
    socket.onclose = () => { if (this.active) this.cb.error(`摄像头 ${kind} 连接已关闭。`); };
    await new Promise((resolve, reject) => {
      socket.onopen = () => { socket.send(this.token); resolve(); };
      socket.onerror = () => reject(new Error('无法连接 Miloco 摄像头。'));
    });
  }
  async miloco() {
    if (!window.VideoDecoder || !window.AudioDecoder) throw new Error('米家取流需要支持 WebCodecs 的 Chrome。');
    let configured = false, keySeen = false;
    const video = new VideoDecoder({output: frame => { this.draw(frame); frame.close(); },
      error: () => this.cb.error('视频解码失败。')});
    this.decoders.push(video);
    await this.socket('video', data => {
      if (!(data instanceof ArrayBuffer)) {
        if (data.type === 'init' && data.codec !== 'h264') throw new Error('Unsupported video codec');
        return;
      }
      if (data.byteLength <= 16) throw new Error('Invalid video packet');
      const header = new DataView(data), key = header.getUint8(0) === 1, bytes = new Uint8Array(data, 16);
      if (!keySeen && !key) return;
      if (!configured) {
        let codec = 'avc1.42E01E';
        for (let i = 0; i < bytes.length - 7; i++) {
          if (bytes[i] === 0 && bytes[i+1] === 0 && bytes[i+2] === 1 && (bytes[i+3] & 31) === 7) {
            codec = 'avc1.' + [...bytes.slice(i+4,i+7)].map(v => v.toString(16).padStart(2,'0')).join(''); break;
          }
        }
        video.configure({codec, optimizeForLatency: true}); configured = true;
      }
      keySeen = true;
      if (video.decodeQueueSize > 15) throw new Error('Video backlog');
      const stamp = header.getBigUint64(8);
      if (stamp > BigInt(Math.floor(Number.MAX_SAFE_INTEGER / 1000))) throw new Error('Invalid timestamp');
      video.decode(new EncodedVideoChunk({type:key ? 'key':'delta', timestamp:Number(stamp)*1000, data:bytes}));
    });
    if (!this.active) return;
    let codec = '', rate = 48000, audio, timestamp = 0;
    await this.socket('audio', data => {
      if (!(data instanceof ArrayBuffer)) {
        if (data.type !== 'init') return;
        codec = data.codec; rate = data.sampleRate;
        if (audio && audio.state !== 'closed') audio.close();
        if (codec === 'opus') {
          audio = new AudioDecoder({output: frame => {
            const samples = new Float32Array(frame.numberOfFrames);
            frame.copyTo(samples, {planeIndex:0, format:'f32-planar'});
            this.audio(samples, frame.sampleRate); frame.close();
          }, error: () => this.cb.error('Opus 音频解码失败。')});
          audio.configure({codec:'opus', sampleRate:rate, numberOfChannels:1}); this.decoders.push(audio);
        } else if (!['g711a','g711u'].includes(codec)) throw new Error('Unsupported audio codec');
        return;
      }
      if (codec === 'opus') {
        if (audio.decodeQueueSize > 30) throw new Error('Audio backlog');
        audio.decode(new EncodedAudioChunk({type:'key', timestamp, data})); timestamp += 20000;
      } else if (codec) this.audio(decodeG711(new Uint8Array(data), codec), rate);
    });
  }
  async submit(wav) {
    if (!this.active || this.busy) return;
    const fresh = this.frames.filter(f => Date.now() - f.at < 3200);
    if (fresh.length < 2 || Date.now() - fresh.at(-1).at > 1000) { this.cb.state('等待同步画面'); return; }
    const selected = fresh.filter((_, i) => i % 2 === 0).slice(-4);
    this.busy = true; this.abort = new AbortController(); this.cb.state('正在理解指令…');
    try {
      const r = await fetch('/api/perceive', {method:'POST', signal:this.abort.signal,
        headers:{'Content-Type':'application/json','X-Friday-Token':this.token},
        body:JSON.stringify({event_id:crypto.randomUUID(),session:this.session,captured_at:Date.now()/1000,
          audio:base64(wav),frames:selected.map(f=>f.image)})});
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || '感知请求失败');
      if (this.active) this.cb.result(body);
    } catch(e) { if (e.name !== 'AbortError' && this.active) this.cb.feedback(e.message); }
    finally { this.busy = false; this.gate.reset(); }
  }
  stop() {
    this.active = false; clearInterval(this.timer); this.abort?.abort();
    this.sockets.forEach(s => s.close()); this.sockets = [];
    this.decoders.forEach(d => { if (d.state !== 'closed') d.close(); }); this.decoders = [];
    this.stream?.getTracks().forEach(t => t.stop()); this.worklet?.disconnect(); this.input?.disconnect();
    this.context?.close(); this.video.srcObject = null; this.frames = []; this.gate.reset();
  }
}
