import {createHash} from 'node:crypto';
export const READ_METHODS = Object.freeze(['getDevList', 'getGraphList', 'getGraph', 'getVarScopeList', 'getVarList']);
export function validateCall(method, params = {}) {
  if (!READ_METHODS.includes(method)) throw new Error('unsupported_read_method');
  if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('invalid_params');
  const fields = method === 'getGraph' ? ['id'] : method === 'getVarList' ? ['scope'] : [];
  if (Object.keys(params).some(k => !fields.includes(k)) || fields.some(k => typeof params[k] !== 'string' || !params[k] || params[k].length > 256)) throw new Error('invalid_params');
}
export function normalizeURL(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || !['', '/'].includes(url.pathname) || !['', '#/'].includes(url.hash)) throw new Error('invalid_gateway_url');
  // No discovery, redirect, or arbitrary destination: explicit private IPv4 only.
  const parts = url.hostname.split('.').map(Number);
  if (parts.length !== 4 || parts.some(n => !Number.isInteger(n) || n < 0 || n > 255) || !(parts[0] === 10 || (parts[0] === 192 && parts[1] === 168) || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31))) throw new Error('private_ipv4_required');
  return url.origin;
}
export function rawRecord(raw) {
  const bytes = Buffer.from(raw);
  if (bytes.length > 4 * 1024 * 1024) throw new Error('response_too_large');
  const parsed = JSON.parse(bytes.toString('utf8'));
  return {bytes, parsed, sha256:createHash('sha256').update(bytes).digest('hex')};
}
