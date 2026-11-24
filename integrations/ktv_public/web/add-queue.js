"use strict";
(function (scope) {
  const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  const ID = /^[1-9][0-9]{0,17}$/;
  const SOURCES = new Set(["song", "cloud", "mv"]);
  const MAX_ADDS = 80;
  const STATES = new Set(["queued", "sending", "retrying", "failed"]);
  const RECEIPT_STATES = new Set(["queued", "acquiring", "separating", "transcoding", "ready", "failed", "cancelled"]);
  const STOP_CODES = new Set(["room_closed", "room_unavailable", "guest_unavailable"]);
  const BUSINESS_CODES = new Set(["queue_full", "cloud_song_unavailable", "idempotency_conflict",
    "invalid_request", "invalid_body", "origin_refused", "method_refused", "not_found"]);
  const WITHDRAW_REFUSALS = new Set(["song_already_started", "song_not_owned", "song_unavailable", "song_start_unavailable"]);
  const ERROR_CODES = new Set([...STOP_CODES, ...BUSINESS_CODES, "rate_limited", "busy", "mac_offline",
    "service_unavailable", "network_error", "request_timeout", "invalid_receipt", "request_refused"]);

  function failure(code) { return Object.assign(new Error(code), {code}); }
  function cleanSong(value) {
    const source = value?.source ?? "song";
    if (!value || typeof value.song_id !== "string" || !ID.test(value.song_id)
        || typeof value.title !== "string" || Array.from(value.title).length > 120 || /[\x00-\x1f\x7f]/.test(value.title)
        || !SOURCES.has(source)) throw failure("invalid_request");
    // Deliberately keep only the add contract: no URL, credential or arbitrary metadata.
    return {song_id: value.song_id, title: value.title, source};
  }
  function songKey(value) { return typeof value === "string" ? value : `${value.source ?? "song"}:${value.song_id}`; }
  function classify(error) {
    const status = Number.isInteger(error?.status) ? error.status : 0;
    const code = ERROR_CODES.has(error?.code) ? error.code : "";
    if (STOP_CODES.has(code)) return {kind: "stop", code};
    if (BUSINESS_CODES.has(code)) return {kind: "failed", code};
    if (status === 401 || status === 403 || status === 410) return {kind: "stop", code: "guest_unavailable"};
    if (status === 429 || status >= 500 || ["rate_limited", "busy", "mac_offline", "service_unavailable"].includes(code)) {
      return {kind: "retrying", code: code || (status === 429 ? "rate_limited" : "service_unavailable")};
    }
    if (status >= 400) return {kind: "failed", code: code || "request_refused"};
    return {kind: "retrying", code: code || "network_error"};
  }
  function entryCopy(item) {
    return {...item, song: {...item.song}, ...(item.withdraw ? {withdraw: {...item.withdraw}} : {})};
  }
  function cleanWithdrawal(value) {
    const keys = Object.keys(value || {});
    if (keys.length !== 1 || !["withdraw_id", "withdraw_request_id"].includes(keys[0])
        || typeof value[keys[0]] !== "string" || !UUID.test(value[keys[0]])) throw failure("invalid_cache");
    return {[keys[0]]: value[keys[0]]};
  }
  function restoreEntry(saved, now) {
    const song = cleanSong(saved.song);
    if (!UUID.test(saved.request_id || "") || !STATES.has(saved.state)
        || !Number.isSafeInteger(saved.attempts) || saved.attempts < 0
        || !Number.isSafeInteger(saved.created_at) || saved.created_at < 0
        || !Number.isSafeInteger(saved.retry_at) || saved.retry_at < 0) throw failure("invalid_cache");
    const withdraw = saved.withdraw ? cleanWithdrawal(saved.withdraw) : null;
    if (withdraw?.withdraw_request_id && withdraw.withdraw_request_id !== saved.request_id) throw failure("invalid_cache");
    return {key: songKey(song), song, request_id: saved.request_id,
      state: saved.state === "sending" ? "queued" : saved.state, attempts: saved.attempts,
      retry_at: Math.min(saved.retry_at, now + 300000), created_at: saved.created_at,
      code: ERROR_CODES.has(saved.code) ? saved.code : "",
      ...(withdraw ? {withdraw} : {})};
  }

  class AddQueue {
    constructor({roomId, guestId, send, onChange = () => {}, onAccepted = () => {}, onWithdrawn = () => {}, storage,
      now = () => Date.now(), requestId = () => crypto.randomUUID(),
      schedule = (fn, ms) => setTimeout(fn, ms), unschedule = timer => clearTimeout(timer),
      requestTimeoutMs = 15000} = {}) {
      if (!UUID.test(roomId || "") || !UUID.test(guestId || "") || typeof send !== "function") throw failure("invalid_scope");
      this.roomId = roomId; this.guestId = guestId; this.send = send;
      this.onChange = onChange; this.onAccepted = onAccepted; this.onWithdrawn = onWithdrawn;
      this.now = now; this.requestId = requestId;
      this.schedule = schedule; this.unschedule = unschedule; this.requestTimeoutMs = requestTimeoutMs;
      this.legacyStorageKey = `friday-ktv-add-queue:v1:${roomId}:${guestId}`;
      this.storagePrefix = `friday-ktv-add-queue:v2:${roomId}:${guestId}:`;
      if (storage === undefined) { try { storage = scope.localStorage; } catch {} }
      this.storage = storage; this.persistence = storage ? "persistent" : "memory";
      this.items = []; this.stopped = ""; this.known = new Set(); this.accepted = new Map();
      this.started = false; this.closed = false; this.active = null; this.timer = null; this.generation = 0;
      this._cache(cache => this._migrate(cache));
      this._sync();
      this.storageChanged = event => {
        if (event.storageArea === this.storage && (event.key === null || event.key?.startsWith(this.storagePrefix))) this.wake();
      };
      if (this.storage) scope.addEventListener?.("storage", this.storageChanged);
    }

    _cache(work) {
      if (this.persistence !== "persistent") return;
      try { return work(this.storage); }
      catch { this.persistence = "memory"; }
    }
    _key(phase, id) { return `${this.storagePrefix}${phase}:${id}`; }
    _phase(item) { return item.withdraw ? "withdraw" : "add"; }
    _obsolete(cache, phase, id) {
      return cache.getItem(this._key(`done:${phase}`, id)) === "1" ||
        phase === "add" && (cache.getItem(this._key("withdraw", id)) !== null ||
          cache.getItem(this._key("done:withdraw", id)) === "1");
    }
    _write(cache, item) {
      cache.setItem(this._key(this._phase(item), item.request_id), JSON.stringify({
        room: this.roomId, guest: this.guestId, item,
      }));
    }
    _migrate(cache) {
      const raw = cache.getItem(this.legacyStorageKey);
      if (!raw || raw.length > 150000) return;
      let entries, stopped;
      try {
        const value = JSON.parse(raw);
        if (value?.version !== 1 || value.room !== this.roomId || value.guest !== this.guestId
            || !Array.isArray(value.items) || value.items.length > MAX_ADDS * 2) return;
        entries = value.items.map(saved => restoreEntry(saved, this.now()));
        if (new Set(entries.map(item => item.key)).size !== entries.length ||
            new Set(entries.map(item => item.request_id)).size !== entries.length) return;
        stopped = value.stopped;
      } catch { return; }
      this.items = entries.filter(item => !this._obsolete(cache, this._phase(item), item.request_id));
      this.stopped = STOP_CODES.has(stopped) ? stopped : "";
      const migratedKey = this.storagePrefix + "v1-migrated";
      if (this.stopped && cache.getItem(migratedKey) === null && cache.getItem(this._stopKey(stopped)) === null) {
        cache.setItem(this._stopKey(stopped), stopped);
      }
      for (const item of this.items) {
        const phase = this._phase(item), id = item.request_id;
        if (!this._obsolete(cache, phase, id) && cache.getItem(this._key(phase, id)) === null) this._write(cache, item);
      }
      cache.setItem(migratedKey, "1");
      cache.removeItem(this.legacyStorageKey);
    }

    _stopKey(code) { return this.storagePrefix + (code === "guest_unavailable" ? "stop:guest" : "stop:room"); }
    _sync() {
      const previousPersistence = this.persistence;
      const cached = this._cache(cache => {
        const roomStop = cache.getItem(this._stopKey("room_closed"));
        const guestStop = cache.getItem(this._stopKey("guest_unavailable"));
        const stopped = STOP_CODES.has(roomStop) ? roomStop : guestStop === "guest_unavailable" ? guestStop : "";
        const items = new Map();
        for (let index = 0; index < cache.length; index++) {
          const key = cache.key(index);
          if (!key?.startsWith(this.storagePrefix)) continue;
          const [phase, id] = key.slice(this.storagePrefix.length).split(":");
          if (!["add", "withdraw"].includes(phase) || !UUID.test(id || "") ||
              key !== this._key(phase, id) || this._obsolete(cache, phase, id)) continue;
          try {
            const record = JSON.parse(cache.getItem(key));
            if (record?.room !== this.roomId || record.guest !== this.guestId) continue;
            const item = restoreEntry(record.item, this.now());
            if (item.request_id === id && this._phase(item) === phase && (item.withdraw || !items.has(id))) items.set(id, item);
          } catch { /* 忽略损坏的单条记录。 */ }
        }
        return {stopped, items};
      });
      if (!cached) return this.persistence !== previousPersistence;
      const active = this.active?.item;
      if (active) {
        const cachedActive = cached.items.get(active.request_id);
        if (cached.stopped || !cachedActive || this._phase(cachedActive) !== this._phase(active)) this._abortActive();
        else cached.items.set(active.request_id, active);
      }
      const items = [...cached.items.values()].sort((a, b) => a.created_at - b.created_at);
      const changed = this.stopped !== cached.stopped || JSON.stringify(this.items) !== JSON.stringify(items);
      this.stopped = cached.stopped; this.items = items;
      return changed;
    }
    _abortActive() {
      if (!this.active) return;
      const {controller, timeout} = this.active; this.active = null;
      this.unschedule(timeout); controller.abort();
    }
    _save(item) {
      this._cache(cache => {
        const phase = this._phase(item);
        if (!this._obsolete(cache, phase, item.request_id)) this._write(cache, item);
      });
    }
    _retire(item) {
      this._cache(cache => {
        const phase = this._phase(item), id = item.request_id;
        // 只清理操作内容。独立的终态标记阻止其他标签页的旧写入恢复该请求。
        cache.setItem(this._key(`done:${phase}`, id), "1");
        cache.removeItem(this._key(phase, id));
        if (item.withdraw) cache.removeItem(this._key("add", id));
      });
      this.items = this.items.filter(candidate => candidate !== item);
    }
    _superseded(item) {
      if (!this._cache(cache => this._obsolete(cache, this._phase(item), item.request_id))) return false;
      this.wake();
      return true;
    }
    _newRequestId() {
      const id = this.requestId();
      if (!UUID.test(id || "")) throw failure("invalid_request_id");
      return id;
    }
    _newCreatedAt() { return Math.max(0, Math.floor(this.now()), (this.items.at(-1)?.created_at ?? -1) + 1); }
    _change() { try { this.onChange(this.snapshot()); } catch {} }
    snapshot() { return {items: this.items.map(entryCopy), stopped: this.stopped, persistence: this.persistence}; }
    has(song) { const key = songKey(song); return this.items.some(item => item.key === key) || this.known.has(key) || this.accepted.has(key); }

    enqueue(value) {
      if (this.closed) throw failure("queue_closed");
      if (this._sync()) this._change();
      if (this.stopped) throw failure(this.stopped);
      const song = cleanSong(value), key = songKey(song), existing = this.items.find(item => item.key === key);
      if (existing) return {...entryCopy(existing), duplicate: true};
      if (this.known.has(key) || this.accepted.has(key)) return {key, song, state: "accepted", duplicate: true};
      if (this.items.filter(item => !item.withdraw).length >= MAX_ADDS) throw failure("local_queue_full");
      const item = {key, song, request_id: this._newRequestId(), state: "queued", attempts: 0, retry_at: 0,
        created_at: this._newCreatedAt(), code: ""};
      this.items.push(item); this._save(item); this._change(); this._kick();
      return entryCopy(item);
    }

    start() { if (!this.closed) { this.started = true; this._sync(); this._change(); this._kick(); } }
    wake() {
      if (this.closed) return;
      if (this._sync()) this._change();
      this._kick();
    }
    recoverGuest() {
      if (this._sync()) this._change();
      if (this.closed || this.stopped !== "guest_unavailable") return false;
      this.stopped = "";
      for (const item of this.items) if (item.code === "guest_unavailable") {
        item.state = "queued"; item.retry_at = 0; item.code = "";
        this._save(item);
      }
      this._cache(cache => cache.removeItem(this._stopKey("guest_unavailable")));
      this._change(); this._kick(); return true;
    }
    stop(code) {
      if (this.closed || !STOP_CODES.has(code)) return false;
      this.stopped = code; ++this.generation;
      if (this.timer !== null) this.unschedule(this.timer);
      this.timer = null;
      this._abortActive();
      this._cache(cache => cache.setItem(this._stopKey(code), code));
      for (const item of this.items) if (item.state === "sending") {
        item.state = "failed"; item.code = code; this._save(item);
      }
      this._change(); return true;
    }
    retry(key) {
      if (this._sync()) this._change();
      if (this.closed || this.stopped) return false;
      const item = this.items.find(candidate => candidate.key === songKey(key));
      if (!item || !["failed", "retrying"].includes(item.state)) return false;
      item.state = "queued"; item.retry_at = 0; item.code = "";
      this._save(item); this._change(); this._kick(); return true;
    }
    withdraw(value, queueId = "") {
      if (this._sync()) this._change();
      if (this.closed || this.stopped) return false;
      const song = cleanSong(value), key = songKey(song);
      const original = this.items.find(candidate => candidate.key === key);
      if (original?.withdraw || (queueId ? !UUID.test(queueId) : !original) ||
          (!original && this.items.length >= MAX_ADDS * 2)) return false;
      const id = original?.request_id || this._newRequestId();
      const withdraw = queueId ? {withdraw_id: queueId} : {withdraw_request_id: id};
      const item = {key, song, request_id: id, created_at: original?.created_at ?? this._newCreatedAt(),
        withdraw, state: "queued", attempts: 0, retry_at: 0, code: ""};
      if (this.active?.item === original) this._abortActive();
      if (original) this.items[this.items.indexOf(original)] = item;
      else this.items.push(item);
      this._save(item);
      // 撤回写入后再确认其他标签页是否开始发送，避免错把在途点歌当成未发送。
      const mayHaveSent = !queueId && !original.attempts && this._cache(cache => {
        const added = cache.getItem(this._key("add", id));
        return cache.getItem(this._key("done:add", id)) !== null || (added && JSON.parse(added).item.attempts > 0);
      });
      if (!queueId && !original.attempts && !mayHaveSent) {
        this.completeWithdrawal(item, {withdrawn: true, queue_removed: false});
      } else {
        this._change();
      }
      this._kick();
      return true;
    }
    completeWithdrawal(item, receipt, code = "") {
      if (receipt) {
        this.accepted.delete(item.key);
        if (receipt.queue_removed) this.known.delete(item.key);
      }
      this._retire(item);
      this._change();
      try { this.onWithdrawn({song: {...item.song}, receipt, code}); } catch {}
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
      this._abortActive();
      scope.removeEventListener?.("storage", this.storageChanged);
      for (const item of this.items) if (item.state === "sending") item.state = "queued";
      this._change();
    }

    _kick() {
      if (!this.started || this.closed || this.stopped || this.active) return;
      if (this.timer !== null) this.unschedule(this.timer);
      this.timer = null;
      const add = this.items.find(candidate => !candidate.withdraw && candidate.state !== "failed");
      let withdrawal = null;
      for (const candidate of this.items) {
        if (candidate.withdraw && candidate.state !== "failed" &&
            (!withdrawal || candidate.retry_at < withdrawal.retry_at)) withdrawal = candidate;
      }
      const item = !add || withdrawal && withdrawal.retry_at <= add.retry_at ? withdrawal : add;
      if (!item) return;
      const wait = item.state === "retrying" ? item.retry_at - this.now() : 0;
      if (wait > 0) { this.timer = this.schedule(() => { this.timer = null; this.wake(); }, wait); return; }
      this._send(item);
    }

    _send(item) {
      const controller = new AbortController(), generation = ++this.generation;
      const active = {controller, generation, item, timeout: null}; this.active = active;
      item.state = "sending"; item.attempts++; item.retry_at = 0; item.code = ""; this._save(item); this._change();
      const timeout = new Promise((_, reject) => {
        active.timeout = this.schedule(() => { controller.abort(); reject(failure("request_timeout")); }, this.requestTimeoutMs);
      });
      const sending = Promise.resolve().then(() => {
        if (this.closed || this.active !== active || this._superseded(item)) throw failure("queue_closed");
        return this.send(item.withdraw || {...item.song, request_id: item.request_id}, {signal: controller.signal});
      });
      Promise.race([sending, timeout]).then(receipt => {
        if (this.closed || this.active !== active || generation !== this.generation || this._superseded(item)) return;
        if (item.withdraw) {
          if (!receipt || receipt.withdrawn !== true || !UUID.test(receipt.id || "")
              || typeof receipt.queue_removed !== "boolean") throw failure("invalid_receipt");
          this.completeWithdrawal(item, receipt);
          return;
        }
        if (!receipt || !UUID.test(receipt.id || "") || !RECEIPT_STATES.has(receipt.state)) throw failure("invalid_receipt");
        if (receipt.state === "cancelled") {
          this.completeWithdrawal(item, {withdrawn: true, id: receipt.id, queue_removed: false});
          return;
        }
        const accepted = {id: receipt.id, state: receipt.state, ...(receipt.duplicate === true ? {duplicate: true} : {})};
        this.accepted.set(item.key, {id: accepted.id, observed: false});
        this._retire(item);
        this._change();
        try { this.onAccepted({song: {...item.song}, receipt: accepted, request_id: item.request_id}); } catch {}
      }).catch(error => {
        if (this.closed || this.active !== active || generation !== this.generation || this._superseded(item)) return;
        if (item.withdraw && WITHDRAW_REFUSALS.has(error?.code)) {
          this.completeWithdrawal(item, null, error.code);
          return;
        }
        const result = classify(error); item.code = result.code;
        if (result.kind === "stop") {
          this.stopped = result.code; item.state = "failed";
          this._cache(cache => cache.setItem(this._stopKey(result.code), result.code));
        }
        else if (result.kind === "retrying") {
          item.state = "retrying";
          const retryAfter = Number.isFinite(error?.retryAfterMs) ? Math.max(0, Math.min(300000, error.retryAfterMs)) : 0;
          item.retry_at = Math.floor(this.now() + Math.max(retryAfter, Math.min(30000, 1000 * 2 ** Math.min(item.attempts - 1, 5))));
        } else item.state = "failed";
        this._save(item); this._change();
      }).finally(() => {
        this.unschedule(active.timeout);
        if (this.active === active) { this.active = null; this._kick(); }
      });
    }
  }

  function message(item) {
    if (item.code === "guest_unavailable") return "正在恢复本场连接";
    if (item.withdraw) return item.state === "failed" ? "撤回未完成，可重试"
      : item.state === "retrying" ? "连接恢复后继续撤回" : "正在撤回";
    if (item.state === "queued") return "已暂存，等待加入";
    if (item.state === "sending") return "正在加入歌单";
    if (item.state === "retrying") return item.code === "rate_limited" ? "点歌较多，稍后自动加入" : "连接恢复后自动加入";
    return ({queue_full: "歌单已满，稍后重试", cloud_song_unavailable: "歌曲已不在云盘，请换个版本",
      room_closed: "本场已结束，请重新扫码", room_unavailable: "房间已失效，请重新扫码",
      idempotency_conflict: "点歌请求未完成，可重新点歌"})[item.code]
      || "未能加入，点此重试";
  }
  const api = {AddQueue, message};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else scope.FridayAddQueue = Object.freeze(api);
})(globalThis);
