import local.friday.ktv.audio.protocol.CarrierGate;

public final class CarrierGateTest {
    static int checks;
    static void yes(boolean value){checks++;if(!value)throw new AssertionError("check "+checks);}
    public static void main(String[] args){
        CarrierGate a=new CarrierGate();yes(a.attach(1));yes(!a.allowVendorStart(1));yes(a.ready(1));yes(a.allowVendorStart(1));
        a.cancel();yes(!a.allowVendorStart(1));a.sequenceFinished(1);yes(a.beginClose(1,true));yes(!a.attach(2));a.finishClose(1,true);yes(!a.occupied());yes(a.attach(2));
        CarrierGate cancelledBeforeReady=new CarrierGate();yes(cancelledBeforeReady.attach(3));cancelledBeforeReady.cancel();yes(!cancelledBeforeReady.ready(3));yes(!cancelledBeforeReady.allowVendorStart(3));cancelledBeforeReady.sequenceFinished(3);yes(cancelledBeforeReady.beginClose(3,true));
        CarrierGate lateStart=new CarrierGate();yes(lateStart.attach(4));yes(lateStart.ready(4));yes(lateStart.allowVendorStart(4));lateStart.cancel();yes(!lateStart.beginClose(4,false));lateStart.sequenceFinished(4);yes(!lateStart.beginClose(4,false));yes(lateStart.beginClose(4,true));yes(!lateStart.beginClose(4,true));lateStart.finishClose(4,true);yes(!lateStart.occupied());
        CarrierGate failedRelease=new CarrierGate();yes(failedRelease.attach(5));failedRelease.cancel();failedRelease.sequenceFinished(5);yes(failedRelease.beginClose(5,true));failedRelease.finishClose(5,false);yes(failedRelease.occupied());yes(!failedRelease.attach(6));
        CarrierGate stale=new CarrierGate();yes(stale.attach(7));yes(stale.ready(7));stale.cancel();stale.sequenceFinished(7);yes(stale.beginClose(7,true));stale.finishClose(7,true);yes(stale.attach(8));yes(!stale.ready(7));yes(!stale.allowVendorStart(7));stale.sequenceFinished(7);yes(!stale.beginClose(8,true));stale.finishClose(7,true);yes(stale.occupied());
        System.out.println("CarrierGateTest: "+checks+" checks passed (normal, cancel-before-ready, late START, release failure, stale lease)");
    }
}
