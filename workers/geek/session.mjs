#!/usr/bin/env node
// Long-lived, explicitly started read-only session. Uses the pinned upstream
// handshake; reconnect authenticates with the saved PIN, not an invented token.
import {readFile, lstat, mkdir, chmod} from 'node:fs/promises';
import {resolve} from 'node:path';
import {parseArgs} from 'node:util';
import {once} from 'node:events';
import {loadClient} from './load-client.mjs';
import {normalizeURL} from './policy.mjs';
import {classifyFailure} from './diagnostics.mjs';
import {REVISION} from './session-reader.mjs';
import {watchSession} from './session-watch.mjs';

const abort = new AbortController();
let phase = 'local_setup';
const end = () => { abort.abort(); };
process.on('SIGTERM',end); process.on('SIGINT',end);
process.stdin.resume(); process.stdin.on('end',end);
async function emit(status, fields = {}) {
  const record = {schema_version:1,kind:'status',provider:'oh-my-sage-readonly',revision:REVISION,
    status,simulated:false,writes_attempted:false,graph_semantics:'opaque',
    checked_at:new Date().toISOString(),reconnects:0,...fields};
  if (!process.stdout.write(JSON.stringify(record)+'\n')) await once(process.stdout,'drain');
}
try {
  const {values} = parseArgs({options:{url:{type:'string'},'passcode-file':{type:'string'},output:{type:'string'}}});
  const url = normalizeURL(values.url ?? '');
  if (!values['passcode-file'] || !values.output) throw Error('configuration_required');
  const secretPath = resolve(values['passcode-file']), info = await lstat(secretPath);
  if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || info.mode & 0o077 || info.size > 1024) throw Error('unsafe_passcode_file');
  const passcode = (await readFile(secretPath,'utf8')).trim();
  if (!passcode || passcode.length > 128 || [...passcode].some(c=>c.charCodeAt(0)<32)) throw Error('invalid_passcode_file');
  const output = resolve(values.output);
  await mkdir(output,{mode:0o700}); await chmod(output,0o700);
  const GatewayClient = await loadClient();
  await watchSession({clientFactory:()=>{
    const client = new GatewayClient();
    // Adapt only the existing connect argument, not the handshake or transport.
    return {connect:()=>client.connect(url),authenticate:pin=>client.authenticate(pin),
      close:()=>client.close(),callApi:(...args)=>client.callApi(...args),
      get onRawResponse(){return client.onRawResponse;},set onRawResponse(value){client.onRawResponse=value;}};
  },passcode,output,emit,signal:abort.signal});
} catch (error) {
  if (!abort.signal.aborted) await emit('blocked',{failure_phase:phase,failure_code:classifyFailure(error)});
} finally {
  if (abort.signal.aborted) await emit('stopped');
  process.exit(0);
}
