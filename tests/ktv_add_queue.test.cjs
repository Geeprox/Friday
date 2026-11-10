const test = require('node:test');
const assert = require('node:assert/strict');
const {AddQueue, message} = require('../integrations/ktv_public/web/add-queue.js');
const uuid = n => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`;
const roomId = uuid(1), guestId = uuid(2);
const song = (id, source = 'song') => ({song_id: String(id), title: `歌曲 ${id}`, source});
const receipt = id => ({id: uuid(1000 + id), state: 'queued'});
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject}; };
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
function storage() { const values = new Map(); return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), values}; }
class Clock {
  constructor() { this.time = 100000; this.serial = 0; this.timers = new Map(); }
  now = () => this.time;
  schedule = (fn, delay) => { const id = ++this.serial; this.timers.set(id, {fn, at: this.time + delay}); return id; };
  unschedule = id => this.timers.delete(id);
  async tick(ms) {
    const end = this.time + ms;
    for (;;) {
      const next = [...this.timers].filter(([, value]) => value.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      this.time = next[1].at; this.timers.delete(next[0]); next[1].fn(); await flush();
    }
    this.time = end; await flush();
  }
}
function setup(options = {}) {
  const clock = new Clock(), cache = storage(); let serial = 50;
  const queue = new AddQueue({roomId, guestId, storage: cache, now: clock.now,
    schedule: clock.schedule, unschedule: clock.unschedule, requestId: () => uuid(++serial),
    send: async payload => receipt(Number(payload.song_id)), ...options});
  return {queue, clock, cache};
}

test('rapid distinct taps persist before dispatch, send in order and never overlap or duplicate a song', async () => {
  const sent = [], accepted = []; let active = 0, peak = 0;
  const {queue, cache} = setup({send: async payload => {
    sent.push(payload); peak = Math.max(peak, ++active); await Promise.resolve(); active--; return receipt(Number(payload.song_id));
  }, onAccepted: value => accepted.push(value)});
  for (let id = 1; id <= 20; id++) { queue.enqueue(song(id)); queue.enqueue(song(id)); }
  assert.equal(sent.length, 0);
  assert.equal(JSON.parse(cache.getItem(queue.storageKey)).items.length, 20);
  queue.start(); for (let i = 0; i < 20; i++) await flush();
  assert.deepEqual(sent.map(value => value.song_id), Array.from({length: 20}, (_, i) => String(i + 1)));
  assert.equal(peak, 1); assert.equal(accepted.length, 20); assert.equal(queue.snapshot().items.length, 0);
  assert.equal(new Set(sent.map(value => value.request_id)).size, 20);
  queue.close();
});

test('a server commit with a lost receipt retries the same request before later songs', async () => {
  const committed = new Map(), sent = [], accepted = []; let first = true;
  const {queue, clock} = setup({send: async payload => {
    sent.push(payload); if (!committed.has(payload.request_id)) committed.set(payload.request_id, receipt(Number(payload.song_id)));
    if (first) { first = false; throw new TypeError('private connection detail'); }
    return committed.get(payload.request_id);
  }, onAccepted: value => accepted.push(value)});
  queue.start(); const firstRequest = queue.enqueue(song(1)).request_id; queue.enqueue(song(2)); await flush();
  assert.equal(queue.snapshot().items[0].state, 'retrying');
  await clock.tick(999); assert.equal(sent.length, 1);
  await clock.tick(1);
  assert.deepEqual(sent.map(value => value.song_id), ['1', '1', '2']);
  assert.equal(sent[1].request_id, firstRequest); assert.equal(committed.size, 2); assert.equal(accepted.length, 2);
  queue.close();
});

test('429 and 5xx retain pending songs and honor Retry-After without serializing arbitrary errors', async () => {
  const sent = []; let failures = 2;
  const {queue, clock, cache} = setup({send: async payload => {
    sent.push(payload);
    if (failures-- === 2) throw Object.assign(new Error('private URL and key'), {status: 429, retryAfterMs: 5000});
    if (failures === 0) throw Object.assign(new Error('private SQL'), {status: 503, code: 'service_unavailable'});
    return receipt(1);
  }});
  queue.start(); queue.enqueue(song(1)); await flush();
  assert.equal(queue.snapshot().items[0].code, 'rate_limited');
  await clock.tick(4999); assert.equal(sent.length, 1);
  await clock.tick(1); assert.equal(queue.snapshot().items[0].state, 'retrying');
  assert.equal(queue.snapshot().items[0].code, 'service_unavailable');
  await clock.tick(2000); assert.equal(sent.length, 3);
  assert.equal(new Set(sent.map(value => value.request_id)).size, 1);
  assert(!cache.getItem(queue.storageKey).includes('private'));
  queue.close();
});

test('queue_full is a visible business failure, later songs continue, and manual retry keeps the request ID', async () => {
  const sent = [], accepted = []; let full = true;
  const {queue, clock} = setup({send: async payload => {
    sent.push(payload); if (payload.song_id === '1' && full) throw Object.assign(new Error(), {status: 429, code: 'queue_full'});
    return receipt(Number(payload.song_id));
  }, onAccepted: value => accepted.push(value)});
  queue.start(); const first = queue.enqueue(song(1)); queue.enqueue(song(2)); await flush();
  assert.deepEqual(sent.map(value => value.song_id), ['1', '2']);
  const failed = queue.snapshot().items[0]; assert.equal(failed.state, 'failed'); assert.match(message(failed), /歌单已满/);
  await clock.tick(120000); assert.equal(sent.length, 2);
  full = false; assert.equal(queue.retry(first.key), true); await flush();
  assert.equal(sent[2].request_id, first.request_id); assert.equal(accepted.length, 2);
  queue.close();
});

for (const code of ['room_closed', 'room_unavailable', 'guest_unavailable']) test(`${code} stops the whole persisted scope and cannot resend on reload`, async () => {
  let calls = 0;
  const {queue, cache, clock} = setup({send: async () => {calls++; throw Object.assign(new Error(), {status: 403, code});}});
  queue.start(); queue.enqueue(song(1)); queue.enqueue(song(2)); await flush();
  assert.equal(queue.snapshot().stopped, code); await clock.tick(120000); assert.equal(calls, 1);
  assert.equal(queue.retry('song:1'), false); assert.throws(() => queue.enqueue(song(3)), {code}); queue.close();
  const restored = setup({storage: cache, send: async () => {calls++; return receipt(1);}}).queue;
  restored.start(); await flush(); assert.equal(calls, 1); assert.equal(restored.snapshot().items.length, 2); restored.close();
});

test('reload restores the full pending payload and old callbacks cannot clear a replacement queue', async () => {
  const pending = deferred(), firstAccepted = [], secondAccepted = [];
  const {queue, cache} = setup({send: () => pending.promise, onAccepted: value => firstAccepted.push(value)});
  queue.start(); const entry = queue.enqueue(song(7, 'cloud')); await flush(); queue.close();
  let received;
  const replacement = setup({storage: cache, send: async payload => {received = payload; return receipt(7);},
    onAccepted: value => secondAccepted.push(value)}).queue;
  replacement.start(); await flush(); pending.resolve(receipt(7)); await flush();
  assert.deepEqual(received, {...song(7, 'cloud'), request_id: entry.request_id});
  assert.equal(firstAccepted.length, 0); assert.equal(secondAccepted.length, 1);
  assert.equal(JSON.parse(cache.getItem(queue.storageKey)).items.length, 0); replacement.close();
});

test('persistent caches are room and guest isolated and never contain keys or arbitrary media metadata', async () => {
  const {queue, cache} = setup();
  queue.enqueue({...song(1), key: 'DO-NOT-STORE', room_key: 'DO-NOT-STORE', artwork: 'https://private.invalid/file'});
  const original = cache.getItem(queue.storageKey);
  assert(!original.includes('DO-NOT-STORE')); assert(!original.includes('https://'));
  const newGuest = setup({storage: cache, guestId: uuid(3)}).queue;
  const newRoom = setup({storage: cache, roomId: uuid(4)}).queue;
  assert.equal(newGuest.snapshot().items.length, 0); assert.equal(newRoom.snapshot().items.length, 0);
  newGuest.start(); newRoom.start(); await flush(); assert.equal(cache.getItem(queue.storageKey), original);
  queue.close(); newGuest.close(); newRoom.close();
});

test('an accepted song remains deduplicated through delayed status, then can be ordered after it was sung', async () => {
  const sent = [];
  const {queue} = setup({send: async payload => {sent.push(payload); return receipt(1);}});
  queue.start(); const first = queue.enqueue(song(1)); await flush();
  assert.equal(queue.enqueue(song(1)).state, 'accepted'); queue.observe({songs: [], played: []});
  assert.equal(queue.enqueue(song(1)).state, 'accepted'); assert.equal(sent.length, 1);
  queue.observe({songs: [{...song(1), ...receipt(1)}], played: []});
  queue.observe({songs: [{...song(1), ...receipt(1)}], played: [receipt(1).id]});
  const next = queue.enqueue(song(1)); assert.notEqual(next.request_id, first.request_id); await flush(); assert.equal(sent.length, 2);
  queue.close();
});

test('same numeric song ID from different sources stays distinct', async () => {
  const sent = [], {queue} = setup({send: async payload => {sent.push(payload); return receipt(sent.length);}});
  for (const source of ['song', 'cloud', 'mv']) queue.enqueue(song(1, source));
  queue.start(); await flush(); assert.deepEqual(sent.map(value => value.source), ['song', 'cloud', 'mv']); queue.close();
});

test('storage refusal reports memory-only state while preserving and sending the current page queue', async () => {
  const states = [], {queue} = setup({storage: {getItem() {throw new Error('blocked');}, setItem() {throw new Error('full');}}, onChange: value => states.push(value)});
  queue.enqueue(song(1)); assert.equal(queue.snapshot().persistence, 'memory');
  queue.start(); await flush(); assert.equal(queue.snapshot().items.length, 0); assert(states.every(state => state.persistence === 'memory')); queue.close();
});

test('a hung request times out and retries the same ID; a late success cannot acknowledge twice', async () => {
  const hung = deferred(), sent = [], accepted = [];
  const {queue, clock} = setup({requestTimeoutMs: 100, send: payload => {sent.push(payload); return sent.length === 1 ? hung.promise : Promise.resolve(receipt(1));}, onAccepted: value => accepted.push(value)});
  queue.start(); queue.enqueue(song(1)); await flush();
  await clock.tick(100); assert.equal(queue.snapshot().items[0].code, 'request_timeout');
  await clock.tick(1000); assert.equal(sent.length, 2); assert.equal(sent[0].request_id, sent[1].request_id);
  hung.resolve(receipt(1)); await flush(); assert.equal(accepted.length, 1); queue.close();
});

test('invalid receipts are never marked accepted, and business failures remain manually retryable after reload', async () => {
  let count = 0; const accepted = [];
  const {queue, clock, cache} = setup({send: async () => ++count === 1 ? {id: 'https://invalid.example', state: 'queued'} : Promise.reject(Object.assign(new Error(), {status: 409, code: 'cloud_song_unavailable'})), onAccepted: value => accepted.push(value)});
  queue.start(); queue.enqueue(song(1)); await flush(); assert.equal(queue.snapshot().items[0].code, 'invalid_receipt');
  await clock.tick(1000); assert.equal(queue.snapshot().items[0].state, 'failed'); assert.equal(accepted.length, 0); queue.close();
  let called = 0; const restored = setup({storage: cache, send: async () => {called++; return receipt(1);}}).queue;
  restored.start(); await flush(); assert.equal(called, 0); restored.retry('song:1'); await flush(); assert.equal(called, 1); restored.close();
});

test('foreign or malformed stored scopes cannot become requests and snapshots cannot mutate live payloads', () => {
  const {queue, cache} = setup(); queue.enqueue(song(1));
  const snapshot = queue.snapshot(); snapshot.items[0].song.title = 'mutated';
  assert.equal(queue.snapshot().items[0].song.title, '歌曲 1');
  const value = JSON.parse(cache.getItem(queue.storageKey)); value.guest = uuid(9); cache.setItem(queue.storageKey, JSON.stringify(value));
  const restored = setup({storage: cache}).queue; assert.equal(restored.snapshot().items.length, 0);
  assert.throws(() => restored.enqueue(song(1, 'https://invalid.example')), {code: 'invalid_request'}); queue.close(); restored.close();
});

test('an expired-room status stops an in-flight add immediately and ignores its late receipt', async () => {
  const pending = deferred(), accepted = []; let aborted = false;
  const {queue, cache, clock} = setup({send: (payload, {signal}) => {signal.addEventListener('abort', () => {aborted = true;}); return pending.promise;},
    onAccepted: value => accepted.push(value)});
  queue.start(); queue.enqueue(song(1)); queue.enqueue(song(2)); await flush();
  assert.equal(queue.stop('room_closed'), true); assert.equal(aborted, true);
  pending.resolve(receipt(1)); await flush(); await clock.tick(120000);
  assert.equal(accepted.length, 0); assert.equal(queue.snapshot().items.length, 2);
  assert.equal(JSON.parse(cache.getItem(queue.storageKey)).stopped, 'room_closed'); queue.close();
});
