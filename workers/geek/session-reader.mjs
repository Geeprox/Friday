// Inventory content is opaque. Keep the exact RPC bytes alongside a separate
// order-independent content fingerprint; changing an RPC id is not a rule edit.
import {createHash, randomUUID} from 'node:crypto';
import {mkdir, writeFile, rename, rm, readdir, lstat} from 'node:fs/promises';
import {join} from 'node:path';
import {rawRecord, validateCall} from './policy.mjs';

export const REVISION = '4fe2c03ce16c365838b58f70e955e987d1a23531';
function canonical(value, depth = 0) {
  if (depth > 64) throw Error('inventory_nesting_budget');
  if (Array.isArray(value)) return '[' + value.map(v => canonical(v, depth + 1)).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k], depth + 1)).join(',') + '}';
  return JSON.stringify(value);
}
export function fingerprint(value) {
  return createHash('sha256').update(canonical(value)).digest('hex');
}
export function graphChanges(before, after) {
  if (!before) return {added:0, removed:0, modified:0};
  return {
    added:Object.keys(after).filter(id => !Object.hasOwn(before,id)).length,
    removed:Object.keys(before).filter(id => !Object.hasOwn(after,id)).length,
    modified:Object.keys(after).filter(id => Object.hasOwn(before,id) && before[id] !== after[id]).length,
  };
}
export async function pollInventory(client) {
  const records = [], graphsById = Object.create(null), content = Object.create(null);
  let raw = null, bytes = 0;
  const previousHook = client.onRawResponse;
  client.onRawResponse = incoming => { raw = rawRecord(incoming); };
  try {
    async function read(method, params = {}) {
      validateCall(method, params);
      raw = null;
      const value = await client.callApi(method, params, 10000);
      if (!raw || !Object.hasOwn(raw.parsed,'result') || fingerprint(raw.parsed.result) !== fingerprint(value)) throw Error('raw_response_unavailable');
      bytes += raw.bytes.length;
      if (bytes > 32 * 1024 * 1024) throw Error('raw_session_storage_budget');
      const file = String(records.length + 1).padStart(3,'0') + '-' + method + '.json';
      records.push({method, file, bytes:raw.bytes, raw_sha256:raw.sha256, content_sha256:fingerprint(value)});
      return value;
    }
    const devices = await read('getDevList');
    if (!devices || typeof devices !== 'object' || Array.isArray(devices) || !devices.devList || typeof devices.devList !== 'object') throw Error('device_inventory_shape');
    const ids = Object.keys(devices.devList);
    if (ids.length > 4096) throw Error('device_inventory_budget');
    const graphs = await read('getGraphList');
    if (!Array.isArray(graphs) || graphs.length > 64 || graphs.some(g => typeof g?.id !== 'string' || !g.id || g.id.length > 256) || new Set(graphs.map(g => g.id)).size !== graphs.length) throw Error('graph_inventory_shape_or_budget');
    for (const graph of graphs) graphsById[graph.id] = fingerprint(await read('getGraph',{id:graph.id}));
    const scopes = await read('getVarScopeList');
    const names = Array.isArray(scopes) ? scopes : scopes?.scopes;
    if (!Array.isArray(names) || names.length > 32 || names.some(n => typeof n !== 'string' || !n || n.length > 256) || new Set(names).size !== names.length) throw Error('scope_inventory_shape_or_budget');
    const variables = Object.create(null);
    for (const scope of names) variables[scope] = await read('getVarList',{scope});
    content.devices = fingerprint(devices);
    content.graph_list = fingerprint(graphs);
    content.graphs = graphsById;
    content.variables = fingerprint(variables);
    return {records, bytes, graphs:graphsById, fingerprint:fingerprint(content), device_count:ids.length, graph_count:graphs.length, scope_count:names.length};
  } finally { client.onRawResponse = previousHook; }
}

export async function saveInventory(root, inventory) {
  const metadata = await lstat(root);
  if (!metadata.isDirectory() || metadata.isSymbolicLink() || metadata.uid !== process.getuid() || metadata.mode & 0o077) throw Error('unsafe_evidence_directory');
  const id = randomUUID(), staging = join(root, '.snapshot-' + id), destination = join(root,'snapshot-' + id);
  await mkdir(staging,{mode:0o700});
  try {
    for (const record of inventory.records) await writeFile(join(staging,record.file),record.bytes,{mode:0o600,flag:'wx'});
    const manifest = {schema_version:1, provider:'oh-my-sage-readonly', revision:REVISION,
      simulated:false, graph_semantics:'opaque', fingerprint:inventory.fingerprint, graphs:inventory.graphs,
      observed_at:new Date().toISOString(), sensitivity:'sensitive', retention_days:7,
      records:inventory.records.map(({bytes,...record})=>({...record,byte_count:bytes.length}))};
    await writeFile(join(staging,'manifest.json'),JSON.stringify(manifest,null,2)+'\n',{mode:0o600,flag:'wx'});
    await rename(staging,destination);
  } finally { await rm(staging,{recursive:true,force:true}); }
  // At most four complete snapshots (<=128 MiB raw) per worker; unchanged
  // polls do not create snapshots. Only our UUID directories are eligible.
  const snapshots = [];
  for (const name of await readdir(root)) {
    if (!/^snapshot-[a-f0-9-]{36}$/.test(name)) continue;
    const entry = await lstat(join(root,name));
    if (entry.isDirectory() && !entry.isSymbolicLink() && entry.uid === process.getuid()) snapshots.push({name,time:entry.mtimeMs});
  }
  snapshots.sort((a,b)=>b.time-a.time);
  for (const entry of snapshots.slice(4)) await rm(join(root,entry.name),{recursive:true});
}
