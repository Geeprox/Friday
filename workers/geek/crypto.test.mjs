import test from 'node:test';
import assert from 'node:assert/strict';
import {createECDH, randomBytes} from 'node:crypto';
import BN from 'bn.js';
import elliptic from './curve-adapter.mjs';
import {runtimeSource} from './load-client.mjs';

const ec = new elliptic.ec('secp256k1');
const encoded = point => Buffer.concat([Buffer.of(4),Buffer.from(point.getX().toArray('be',32)),Buffer.from(point.getY().toArray('be',32))]);
test('point arithmetic matches independent OpenSSL public keys', () => {
  for (let i=0;i<16;i++) {
    const secret = randomBytes(32), native = createECDH('secp256k1');native.setPrivateKey(secret);
    const point = ec.g.mul(new BN(secret));
    assert.deepEqual(encoded(point),native.getPublicKey(null,'uncompressed'));
    const restored = ec.keyFromPublic({x:point.getX().toString(16),y:point.getY().toString(16)}).getPublic();
    assert(restored.eq(point));
    assert(point.add(point).eq(ec.g.mul(new BN(secret).muln(2).umod(ec.n))));
    assert(point.add(point.neg()).eq(ec.g.mul(new BN(0))));
  }
});
test('invalid peer points and unsupported curves are rejected', () => {
  assert.throws(()=>ec.keyFromPublic({x:'1',y:'1'}));
  assert.throws(()=>ec.keyFromPublic({x:'0',y:'0'}));
  assert.throws(()=>new elliptic.ec('unknown'));
});
async function protocol() {
  let source = (await runtimeSource()).replace('class ECJPAKE {','export class ECJPAKE {');
  for(const name of ['ws','bn.js'])source=source.replace("from '"+name+"'","from '"+import.meta.resolve(name)+"'");
  for(const name of ['./curve-adapter.mjs','./policy.mjs'])source=source.replace("from '"+name+"'","from '"+new URL(name,import.meta.url).href+"'");
  return (await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'))).ECJPAKE;
}
test('original ECJPAKE framing derives equal keys and rejects altered proof', async () => {
  const Protocol = await protocol();
  for(const mismatch of [false,true]) {
    const client=new Protocol('SYNTHETIC-PAIRING','client'),server=new Protocol(mismatch?'DIFFERENT-SYNTHETIC':'SYNTHETIC-PAIRING','server');
    const first=client.writeRoundOne(),second=server.writeRoundOne();
    client.readRoundOne(second);server.readRoundOne(first);
    const c2=client.writeRoundTwo(),s2=server.writeRoundTwo();
    const ckey=client.readRoundTwo(Buffer.concat([Buffer.alloc(3),s2])),skey=server.readRoundTwo(c2);
    assert.equal(ckey.equals(skey),!mismatch);
    assert.throws(()=>client.writeRoundOne());
  }
  const c=new Protocol('SYNTHETIC','client'),s=new Protocol('SYNTHETIC','server');
  c.writeRoundOne();const altered=s.writeRoundOne();altered[90]^=1;
  assert.throws(()=>c.readRoundOne(altered));
});
