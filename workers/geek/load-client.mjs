// Exact MIT upstream source remains unchanged. Instrument only the isolated runtime copy.
import {readFile, writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {stripTypeScriptTypes} from 'node:module';
const expected = 'b383efcdf8bfdf21d4ed7c36aeb1eda1953a4e479effdb2ffdc7a4c27044c7a4';
export async function runtimeSource() {
  const source = await readFile(new URL('./vendor/client.ts', import.meta.url), 'utf8');
  if (createHash('sha256').update(source).digest('hex') !== expected) throw new Error('upstream_source_hash_mismatch');
  let patched = source;
  const patches = [
    ["import elliptic from 'elliptic';", "import elliptic from './curve-adapter.mjs';"],
    ['export class GatewayClient {', 'export class GatewayClient {\n public onRawResponse: ((raw: Buffer) => void) | null = null;'],
    ['new WebSocket(url)', 'new WebSocket(url, {maxPayload: 4194304, handshakeTimeout: 8000, followRedirects: false})'],
    ['inflateRawSync(data.slice(4))', 'inflateRawSync(data.slice(4), {maxOutputLength: 4194304})'],
    ["const response = JSON.parse(decompressed.toString('utf8'));", "const response = JSON.parse(decompressed.toString('utf8'));\n            this.onRawResponse?.(decompressed);"],
    ["console.error('[!] Error handling message:', e);", "console.error('gateway_response_decode_failed');"],
    ['if (!this.secureEstablished) {\n            throw new Error(\'Secure Session not established\');', "validateCall(method, params);\n        if (!this.secureEstablished) {\n            throw new Error('Secure Session not established');"]
  ];
  for (const [before, after] of patches) {
    if (patched.split(before).length !== 2) throw new Error('upstream_patch_anchor_changed');
    patched = patched.replace(before, after);
  }
  return "import {validateCall} from './policy.mjs';\n" + stripTypeScriptTypes(patched);
}
export async function loadClient() {
  const javascript = await runtimeSource();
  const output = new URL('./.runtime-client.mjs', import.meta.url);
  await writeFile(output, javascript, {mode:0o600});
  return (await import(output.href)).GatewayClient;
}
