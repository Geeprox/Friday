package local.friday.ktv;
public final class ImmediateControlTest {
 static void yes(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args){
  LocalControlIntent intent=new LocalControlIntent();
  long a=intent.request(1,100);yes(intent.resolve(0,5,120)==1);
  long b=intent.request(0,130);yes(!intent.isCurrent(a,140)&&intent.isCurrent(b,140));intent.accepted(a,6);yes(intent.resolve(1,6,140)==0);
  intent.accepted(b,7);yes(intent.resolve(1,6,150)==0);
  yes(intent.resolve(0,7,160)==0&&!intent.pending());
  yes(intent.resolve(1,8,170)==1);
  a=intent.request(0,200);intent.clear();intent.accepted(a,9);yes(intent.resolve(1,8,210)==1);
  a=intent.request(0,300);intent.failed(a);yes(intent.resolve(1,8,310)==1);yes(intent.takeReconcile());yes(!intent.takeReconcile());
  intent.request(0,400);yes(intent.resolve(1,8,10400)==1);yes(intent.takeReconcile());
  System.out.println("ImmediateControlTest passed: immediate keys, stale ACK, newer phone revision, room reset and failed synchronization");
 }
}
