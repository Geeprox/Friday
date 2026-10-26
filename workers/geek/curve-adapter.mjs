// Narrow ECJPAKE point API over the maintained noble implementation.
// The original protocol source and BN scalar arithmetic remain separately pinned.
import {secp256k1} from '@noble/curves/secp256k1.js';
import BN from 'bn.js';

const Point = secp256k1.Point;
const ORDER = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;
class ProtocolPoint {
  constructor(point) { this.point = point; }
  mul(scalar) {
    const value = ((BigInt(scalar.toString(10)) % ORDER) + ORDER) % ORDER;
    return new ProtocolPoint(value === 0n ? Point.ZERO : this.point.multiply(value));
  }
  add(other) { return new ProtocolPoint(this.point.add(other.point)); }
  neg() { return new ProtocolPoint(this.point.negate()); }
  eq(other) { return other instanceof ProtocolPoint && this.point.equals(other.point); }
  coordinate(name) {
    this.point.assertValidity();
    return new BN(this.point.toAffine()[name].toString(16), 16);
  }
  getX() { return this.coordinate('x'); }
  getY() { return this.coordinate('y'); }
}
class EC {
  constructor(curve) {
    if (curve !== 'secp256k1') throw Error('unsupported_protocol_curve');
    this.n = new BN(ORDER.toString(16), 16);
    this.g = new ProtocolPoint(Point.BASE);
  }
  keyFromPublic({x, y}) {
    const point = Point.fromAffine({x: BigInt('0x' + x), y: BigInt('0x' + y)});
    point.assertValidity();
    return {getPublic: () => new ProtocolPoint(point)};
  }
}
export default {ec: EC};
