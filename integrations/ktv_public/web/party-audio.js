"use strict";
(function (scope) {
  const presets = Object.freeze([
    ["dry", "关闭混响"], ["pop", "流行音效"], ["ktv", "KTV 音效"], ["power", "唱将音效"], ["opera", "歌剧音效"],
    ["studio", "清晰小房间"], ["echo", "短回声"],
  ]);
  const actions = new Set(["sfx_applause", "sfx_cheer", "sfx_stop"]);
  const labels = {sfx_applause: "鼓掌", sfx_cheer: "喝彩", sfx_stop: "氛围音效"};

  function capabilities(playback) {
    const value = playback.audio_capabilities;
    const valid = value && Array.isArray(value.vocal_presets) && value.vocal_presets.includes("dry")
      && value.vocal_presets.every(p => presets.some(([key]) => key === p)) && typeof value.sfx === "boolean";
    return {ready: !!valid, vocal: valid ? value.vocal_presets : [], sfx: !!valid && value.sfx};
  }

  function vocalView(controls, playback) {
    const caps = capabilities(playback);
    const selected = presets.some(([key]) => key === controls.vocal_preset) ? controls.vocal_preset : "dry";
    const label = presets.find(([key]) => key === selected)[1];
    let text = !playback.online ? "等待电视连接" : !caps.ready ? "电视暂未提供音效设置" : "已选择 · " + label;
    if (playback.online && caps.ready && playback.vocal_preset === selected) {
      if (playback.vocal_preset_state === "applied") text = "已生效 · " + label;
      else if (playback.vocal_preset_state === "pending") text = "已发送，等待电视确认";
      else if (playback.vocal_preset_state === "failed") text = "音效暂未应用，请重试";
      else if (playback.vocal_preset_state === "unavailable") text = "已选择 · " + label + "，等待电视确认";
    }
    if (playback.online && caps.ready && caps.vocal.length === 1) text = playback.vocal_preset_state === "failed"
      ? "电视混响启动失败，暂不可用"
      : playback.vocal_preset_state === "unavailable" ? "音频通道尚未就绪，请查看电视提示" : "当前通道仅支持原声";
    if (playback.online && caps.ready && caps.vocal.length === 1 && playback.audio_capabilities?.voice_protection?.state === "preparing") text = "语音保护准备完成后可切换混响";
    return {selected, text, supported: playback.online && caps.ready ? caps.vocal : []};
  }

  function sfxMessage(state) {
    const label = labels[state.action] || "氛围音效";
    return ({idle: "为这一首加点气氛", sending: "正在发送…", sent: "已发送，等待电视确认",
      played: label + "已播放", stopped: "氛围音效已停止", ended: "氛围音效已结束",
      expired: "这次操作已过期，请重新点击", failed: "电视未能播放，请重试",
      unconfirmed: "尚未收到电视确认", superseded: "已被更新的氛围操作替换"})[state.phase] || "暂未发送成功，请重试";
  }

  function commandObserved(action, song, before, playback, seq) {
    if (!playback.online || playback.last_command < seq) return false;
    if (action === "pause") return playback.current === song && ["paused", "idle"].includes(playback.state);
    if (action === "resume") return playback.current === song && playback.state === "playing";
    if (action === "next") return playback.current !== before.current
      || !!before.current && playback.played?.includes(before.current);
    if (action !== "play" || playback.current !== song) return false;
    if (playback.state === "preparing") return true; // Loading is visible as loading, never "played".
    if (playback.state !== "playing") return false;
    if (before.current !== song || before.state !== "playing") return true;
    // Replaying the same playing song needs a visible timeline reset as well
    // as the receipt. A pending local job can already have last_command ACKed.
    const expected = before.position_ms + Math.max(0, playback.position_at - before.position_at);
    return Number.isFinite(expected) && Number.isFinite(playback.position_ms)
      && playback.position_ms <= 2500 && playback.position_ms + 300 < expected;
  }

  class SfxCommands {
    constructor({send, onState = () => {}, now = () => performance.now(), requestId = () => crypto.randomUUID()}) {
      this.send = send; this.onState = onState; this.now = now; this.requestId = requestId;
      this.writes = Promise.resolve(); this.serial = 0;
      this.state = {phase: "idle", serial: 0, seq: 0, action: ""};
    }
    change(next) { this.state = {...this.state, ...next}; this.onState(this.state); }
    trigger(action) {
      if (!actions.has(action)) return Promise.reject(new Error("invalid_sfx"));
      const serial = ++this.serial, payload = {action, song: "", request_id: this.requestId()};
      this.change({phase: "sending", serial, seq: 0, action, at: this.now()});
      // Only serialize HTTP writes. A stop never waits for playback acknowledgement.
      const write = this.writes.catch(() => {}).then(() => this.send(payload));
      this.writes = write;
      return write.then(result => {
        if (!Number.isSafeInteger(result.seq) || result.seq < 1) throw new Error("invalid_sfx_receipt");
        if (serial === this.serial) this.change({phase: "sent", seq: result.seq, at: this.now()});
        return result;
      }).catch(error => {
        if (serial === this.serial) this.change({phase: "error"});
        throw error;
      });
    }
    observe(playback) {
      if (!this.state.seq || !["sent", "unconfirmed"].includes(this.state.phase)) return;
      if (playback.online && playback.sfx_seq === this.state.seq && playback.sfx_action === this.state.action) {
        const value = playback.sfx_state;
        if (value === "playing" && this.state.action !== "sfx_stop") this.change({phase: "played"});
        else if (value === "stopped") this.change({phase: this.state.action === "sfx_stop" ? "stopped" : "ended"});
        else if (["expired", "failed"].includes(value)) this.change({phase: value});
      } else if (playback.online && playback.sfx_seq > this.state.seq) this.change({phase: "superseded"});
      if (this.state.phase === "sent" && this.now() - this.state.at > 7000) this.change({phase: "unconfirmed"});
    }
  }

  const api = {presets, capabilities, vocalView, sfxMessage, commandObserved, SfxCommands};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else scope.FridayPartyAudio = Object.freeze(api);
})(globalThis);
