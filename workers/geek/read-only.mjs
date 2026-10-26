#!/usr/bin/env node
// Single isolated read-only session. No retries, writes, graphs normalization or prompts.
import {readFile, stat, mkdir, writeFile, chmod} from 'node:fs/promises';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {parseArgs} from 'node:util';
import {loadClient} from './load-client.mjs';
import {normalizeURL, rawRecord, validateCall} from './policy.mjs';
import {classifyFailure} from './diagnostics.mjs';

const revision = '4fe2c03ce16c365838b58f70e955e987d1a23531';
let client;
let timer;
let report = {schema_version:1, provider:'oh-my-sage-readonly', revision, simulated:false,
  started_at:new Date().toISOString(), authorization:'explicit pairing and read-only inventory',
  status:'blocked', writes_attempted:false, firmware:null, graph_semantics:'opaque',
  methods:[], errors:[], retention_days:7, sensitivity:'sensitive',
  limitations:['Only Geek-visible inventory; normal Mi Home rules may be invisible.', 'No hardware action, firmware compatibility or offline behavior verified.']};
let output;
let phase = 'local_setup';
try {
  const {values} = parseArgs({options:{url:{type:'string'}, 'passcode-file':{type:'string'}, output:{type:'string'}}});
  const url = normalizeURL(values.url ?? '');
  if (!values['passcode-file'] || !values.output) throw new Error('passcode_file_and_output_required');
  const secretPath = resolve(values['passcode-file']);
  const metadata = await stat(secretPath);
  if (!metadata.isFile() || metadata.mode & 0o077 || metadata.size > 1024) throw new Error('passcode_file_must_be_private_0600');
  const passcode = (await readFile(secretPath, 'utf8')).trim();
  if (!passcode || passcode.length > 128) throw new Error('invalid_passcode_file');
  output = resolve(values.output, 'session-' + randomUUID());
  await mkdir(output, {recursive:true,mode:0o700}); await chmod(output, 0o700);
  phase = 'load_client';
  const GatewayClient = await loadClient();
  client = new GatewayClient();
  // Parent process is expected to supervise; this worker also has a hard session deadline.
  timer = setTimeout(() => { console.error('geek_readonly_session_deadline'); process.exit(2); }, 120000);
  let lastRaw = null;
  let totalRawBytes = 0;
  client.onRawResponse = raw => { lastRaw = rawRecord(raw); };
  phase = 'websocket_connect';
  await client.connect(url);
  phase = 'authenticate';
  await client.authenticate(passcode);
  report.status = 'authenticated';
  async function read(method, params={}) {
    validateCall(method, params);
    phase = method;
    lastRaw = null;
    const result = await client.callApi(method, params, 10000);
    if (!lastRaw || !Object.hasOwn(lastRaw.parsed,'result')) throw new Error('raw_response_unavailable');
    totalRawBytes += lastRaw.bytes.length;
    if (totalRawBytes > 32 * 1024 * 1024) throw new Error('raw_session_storage_budget');
    const index = report.methods.length + 1;
    const filename = String(index).padStart(3,'0') + '-' + method + '.json';
    await writeFile(join(output, filename), lastRaw.bytes, {mode:0o600,flag:'wx'});
    report.methods.push({method, file:filename, bytes:lastRaw.bytes.length, sha256:lastRaw.sha256, result_kind:Array.isArray(result)?'array':typeof result});
    return result;
  }
  const devices = await read('getDevList');
  report.device_count = devices && typeof devices === 'object' && devices.devList ? Object.keys(devices.devList).length : null;
  const graphs = await read('getGraphList');
  if (!Array.isArray(graphs) || graphs.length > 64 || graphs.some(g=>typeof g?.id !== 'string')) throw new Error('graph_inventory_shape_or_budget');
  report.graph_count=graphs.length;
  for (const graph of graphs) await read('getGraph',{id:graph.id});
  const scopes = await read('getVarScopeList');
  const names = Array.isArray(scopes) ? scopes : scopes?.scopes;
  if (!Array.isArray(names) || names.length > 32 || names.some(n=>typeof n !== 'string')) throw new Error('scope_inventory_shape_or_budget');
  report.scope_count=names.length;
  for (const scope of names) await read('getVarList',{scope});
  report.status = 'readonly_inventory_collected';
  phase = 'complete';
} catch(error) {
  // Do not echo gateway text, URL, passcodes or raw household response on terminal.
  report.status = report.methods.length ? 'partial' : 'blocked';
  report.failure_phase = phase;
  report.failure_code = classifyFailure(error);
  report.errors.push({code:'read_only_spike_failed', error_type:error?.constructor?.name ?? 'Error'});
} finally {
  if(client) await client.close();
  if(timer) clearTimeout(timer);
  report.finished_at=new Date().toISOString();
  if(output) await writeFile(join(output,'report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  console.log(JSON.stringify({...report, output_directory:output ?? null},null,2));
  process.exit(report.status === 'readonly_inventory_collected' ? 0 : 2);
}
