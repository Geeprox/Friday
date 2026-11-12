"use strict";
(function (scope) {
  const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  const ID = /^[1-9][0-9]{0,17}$/;
  const SOURCES = new Set(["song", "cloud", "mv"]);
  const STATES = new Set(["queued", "sending", "retrying", "failed"]);
  const RECEIPT_STATES = new Set(["queued", "acquiring", "separating", "transcoding", "ready", "failed", "cancelled"]);
  const STOP_CODES = new Set(["room_closed", "room_unavailable", "guest_unavailable"]);
  const BUSINESS_CODES = new Set(["queue_full", "cloud_song_unavailable", "idempotency_conflict",
    "invalid_request", "invalid_body", "origin_refused", "method_refused", "not_found"]);
  const ERROR_CODES = new Set([...STOP_CODES, ...BUSINESS_CODES, "rate_limited", "busy", "mac_offline",
    "service_unavailable", "network_error", "request_timeout", "invalid_receipt", "request_refused"]);

  function failure(code) { return Object.assign(new Error(code), {code}); }
  function cleanSong(value) {
    const source = value?.source ?? "song";
    if (!value || typeof value.song_id !== "string" || !ID.test(value.song_id)
        || typeof value.title !== "string" || value.title.length > 120 || /[\x00-\x1f\x7f]/.test(value.title)
        || !SOURCES.has(source)) throw failure("invalid_request");
    // Deliberately keep only the add contract: no URL, credential or arbitrary metadata.
    return {song_id: value.song_id, title: value.title, source};
  }
  function songKey(value) { return typeof value === "string" ? value : `${value.source ?? "song"}:${value.song_id}`; }
  function classify(error) {
    const status = Number.isInteger(error?.status) ? error.status : 0;
    const code = ERROR_CODES.has(error?.code) ? error.code : "";
    if (STOP_CODES.has(code)) return {kind: "stop", code};
    if (status === 401 || status === 403 || status === 410) return {kind: "stop", code: "guest_unavailable"};
    if (BUSINESS_CODES.has(code)) return {kind: "failed", code};
    if (status === 429 || status >= 500 || ["rate_limited", "busy", "mac_offline", "service_unavailable"].includes(code)) {
      return {kind: "retrying", code: code || (status === 429 ? "rate_limited" : "service_unavailable")};
    }
    if (status >= 400) return {kind: "failed", code: code || "request_refused"};
    return {kind: "retrying", code: code || "network_error"};
  }
  function entryCopy(item) { return {...item, song: {...item.song}}; }

  class AddQueue {
    constructor({roomId, guestId, send, onChange = () => {}, onAccepted = () => {}, storage,
      now = () => Date.now(), requestId = () => crypto.randomUUID(),
      schedule = (fn, ms) => setTimeout(fn, ms), unschedule = timer => clearTimeout(timer),
      requestTimeoutMs = 15000} = {}) {
      if (!UUID.test(roomId || "") || !UUID.test(guestId || "") || typeof send !== "function") throw failure("invalid_scope");
      this.roomId = roomId; this.guestId = guestId; this.send = send;
      this.onChange = onChange; this.onAccepted = onAccepted; this.now = now; this.requestId = requestId;
      this.schedule = schedule; this.unschedule = unschedule; this.requestTimeoutMs = requestTimeoutMs;
      this.storageKey = `friday-ktv-add-queue:v1:${roomId}:${guestId}`;
      if (storage === undefined) { try { storage = scope.localStorage; } catch {} }
      this.storage = storage; this.persistence = storage ? "persistent" : "memory";
      this.items = []; this.stopped = ""; this.known = new Set(); this.accepted = new Map();
      this.started = false; this.closed = false; this.active = null; this.timer = null; this.generation = 0;
      this._restore();
    }

    _restore() {
      if (!this.storage) return;
      try {
        const raw = this.storage.getItem(this.storageKey);
        if (!raw) return;
        if (raw.length > 150000) throw failure("invalid_cache");
        const value = JSON.parse(raw);
        if (value?.version !== 1 || value.room !== this.roomId || value.guest !== this.guestId
            || !Array.isArray(value.items) || value.items.length > 80) throw failure("invalid_cache");
        const seen = new Set(), restored = [];
        for (const saved of value.items) {
          const song = cleanSong(saved.song), key = songKey(song);
          if (!UUID.test(saved.request_id || "") || seen.has(key) || !STATES.has(saved.state)
              || !Number.isSafeInteger(saved.attempts) || saved.attempts < 0
              || !Number.isSafeInteger(saved.created_at) || saved.created_at < 0
              || !Number.isSafeInteger(saved.retry_at) || saved.retry_at < 0) throw failure("invalid_cache");
          seen.add(key);
          restored.push({key, song, request_id: saved.request_id,
            state: saved.state === "sending" ? "queued" : saved.state, attempts: saved.attempts,
            retry_at: Math.min(saved.retry_at, this.now() + 300000), created_at: saved.created_at,
            code: ERROR_CODES.has(saved.code) ? saved.code : ""});
        }
        this.items = restored;
        this.stopped = STOP_CODES.has(value.stopped) ? value.stopped : "";
      } catch { this.persistence = "memory"; }
    }

    _save() {
      if (!this.storage) { this.persistence = "memory"; return; }
      try {
        this.storage.setItem(this.storageKey, JSON.stringify({version: 1, room: this.roomId,
          guest: this.guestId, stopped: this.stopped, items: this.items.map(entryCopy)}));
        this.persistence = "persistent";
      } catch { this.persistence = "memory"; }
    }
    _change() { this._save(); try { this.onChange(this.snapshot()); } catch {} }
    snapshot() { return {items: this.items.map(entryCopy), stopped: this.stopped, persistence: this.persistence}; }
    has(song) { const key = songKey(song); return this.items.some(item => item.key === key) || this.known.has(key) || this.accepted.has(key); }

    enqueue(value) {
      if (this.closed) throw failure("queue_closed");
      if (this.stopped) throw failure(this.stopped);
      const song = cleanSong(value), key = songKey(song), existing = this.items.find(item => item.key === key);
      if (existing) return {...entryCopy(existing), duplicate: true};
      if (this.known.has(key) || this.accepted.has(key)) return {key, song, state: "accepted", duplicate: true};
      if (this.items.length >= 80) throw failure("local_queue_full");
      const request_id = this.requestId();
      if (!UUID.test(request_id || "")) throw failure("invalid_request_id");
      const item = {key, song, request_id, state: "queued", attempts: 0, retry_at: 0,
        created_at: Math.max(0, Math.floor(this.now())), code: ""};
      this.items.push(item); this._change(); this._kick();
      return entryCopy(item);
    }

    start() { if (!this.closed) { this.started = true; this._change(); this._kick(); } }
    wake() { this._kick(); }
    stop(code) {
      if (this.closed || !STOP_CODES.has(code)) return false;
      this.stopped = code; ++this.generation;
      if (this.timer !== null) this.unschedule(this.timer);
      this.timer = null;
      if (this.active) {
        this.unschedule(this.active.timeout); this.active.controller.abort(); this.active = null;
      }
      for (const item of this.items) if (item.state === "sending") { item.state = "failed"; item.code = code; }
      this._change(); return true;
    }
    retry(key) {
      if (this.closed || this.stopped) return false;
      const item = this.items.find(candidate => candidate.key === songKey(key));
      if (!item || !["failed", "retrying"].includes(item.state)) return false;
      item.state = "queued"; item.retry_at = 0; item.code = "";
      this._change(); this._kick(); return true;
    }
    observe({songs = [], played = []} = {}) {
      const completed = new Set(played), active = new Map();
      for (const song of songs) if (song?.id && song.state !== "cancelled" && !completed.has(song.id)) active.set(songKey(song), song);
      this.known = new Set(active.keys());
      for (const [key, receipt] of this.accepted) {
        if (active.has(key)) receipt.observed = true;
        else if (completed.has(receipt.id) || songs.some(song => song.id === receipt.id && song.state === "cancelled")
            || receipt.observed) this.accepted.delete(key);
      }
    }
    close() {
      this.closed = true; this.started = false; ++this.generation;
      if (this.timer !== null) this.unschedule(this.timer);
      this.timer = null;
      if (this.active) {
        this.unschedule(this.active.timeout); this.active.controller.abort(); this.active = null;
      }
      for (const item of this.items) if (item.state === "sending") item.state = "queued";
      this._change();
    }

    _kick() {
      if (!this.started || this.closed || this.stopped || this.active) return;
      if (this.timer !== null) this.unschedule(this.timer);
      this.timer = null;
      const item = this.items.find(candidate => candidate.state !== "failed");
      if (!item) return;
      const wait = item.state === "retrying" ? item.retry_at - this.now() : 0;
      if (wait > 0) { this.timer = this.schedule(() => { this.timer = null; this._kick(); }, wait); return; }
      this._send(item);
    }

    _send(item) {
      const controller = new AbortController(), generation = ++this.generation;
      const active = {controller, generation, timeout: null}; this.active = active;
      item.state = "sending"; item.attempts++; item.retry_at = 0; item.code = ""; this._change();
      const timeout = new Promise((_, reject) => {
        active.timeout = this.schedule(() => { controller.abort(); reject(failure("request_timeout")); }, this.requestTimeoutMs);
      });
      const sending = Promise.resolve().then(() => {
        if (this.closed || this.active !== active) throw failure("queue_closed");
        return this.send({...item.song, request_id: item.request_id}, {signal: controller.signal});
      });
      Promise.race([sending, timeout]).then(receipt => {
        if (this.closed || this.active !== active || generation !== this.generation) return;
        if (!receipt || !UUID.test(receipt.id || "") || !RECEIPT_STATES.has(receipt.state)) throw failure("invalid_receipt");
        const accepted = {id: receipt.id, state: receipt.state, ...(receipt.duplicate === true ? {duplicate: true} : {})};
        this.accepted.set(item.key, {id: accepted.id, observed: false});
        this.items = this.items.filter(candidate => candidate !== item);
        this._change();
        try { this.onAccepted({song: {...item.song}, receipt: accepted, request_id: item.request_id}); } catch {}
      }).catch(error => {
        if (this.closed || this.active !== active || generation !== this.generation) return;
        const result = classify(error); item.code = result.code;
        if (result.kind === "stop") { this.stopped = result.code; item.state = "failed"; }
        else if (result.kind === "retrying") {
          item.state = "retrying";
          const retryAfter = Number.isFinite(error?.retryAfterMs) ? Math.max(0, Math.min(300000, error.retryAfterMs)) : 0;
          item.retry_at = Math.floor(this.now() + Math.max(retryAfter, Math.min(30000, 1000 * 2 ** Math.min(item.attempts - 1, 5))));
        } else item.state = "failed";
        this._change();
      }).finally(() => {
        this.unschedule(active.timeout);
        if (this.active === active) { this.active = null; this._kick(); }
      });
    }
  }

  function message(item) {
    if (item.state === "queued") return "已暂存，等待加入";
    if (item.state === "sending") return "正在加入歌单";
    if (item.state === "retrying") return item.code === "rate_limited" ? "点歌较多，稍后自动加入" : "连接恢复后自动加入";
    return ({queue_full: "歌单已满，稍后重试", cloud_song_unavailable: "歌曲已不在云盘，请换个版本",
      room_closed: "本场已结束，请重新扫码", room_unavailable: "房间已失效，请重新扫码",
      guest_unavailable: "加入信息已失效，请重新扫码", idempotency_conflict: "点歌请求不一致，请重新加入房间"})[item.code]
      || "未能加入，点此重试";
  }
  const api = {AddQueue, message};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else scope.FridayAddQueue = Object.freeze(api);
})(globalThis);
