package local.friday.ktv;
public final class ImmediateControlTest {
 static void yes(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args){
  LocalControlIntent intent=new LocalControlIntent();
  long a=intent.request(1,1);yes(intent.resolve(0,5,1)==1);
  long b=intent.request(0,1);yes(!intent.isCurrent(a)&&intent.isCurrent(b));
  intent.accepted(a,6);yes(intent.resolve(1,6,1)==0);
  intent.accepted(b,7);yes(intent.resolve(1,6,1)==0);
  yes(intent.resolve(0,7,2)==0&&!intent.pending()&&intent.takeReconcile());
  yes(intent.resolve(1,8,3)==1);
  a=intent.request(0,3);intent.clear();intent.accepted(a,9);yes(intent.resolve(1,8,3)==1);
  intent.request(0,3);
  yes(intent.resolve(1,8,3)==0&&intent.resolve(1,99,3)==0&&intent.pending());
  yes(intent.resolve(1,100,4)==1&&intent.takeReconcile()&&!intent.takeReconcile());
  System.out.println("ImmediateControlTest passed: local controls survive stale snapshots, and fresh field revisions still take effect");
 }
}
