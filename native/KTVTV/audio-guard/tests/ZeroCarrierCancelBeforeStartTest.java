package local.friday.ktv.audio;

import java.util.concurrent.atomic.AtomicInteger;

/** Exercises actual unopened carrier cleanup; no Android method or audio object is invoked. */
public final class ZeroCarrierCancelBeforeStartTest {
    public static void main(String[] args){
        AtomicInteger callbacks=new AtomicInteger();ZeroOutputCarrier carrier=new ZeroOutputCarrier(42,callbacks::incrementAndGet,callbacks::incrementAndGet);
        if(carrier.releaseReturned())throw new AssertionError("not yet closed");
        carrier.close();if(!carrier.releaseReturned())throw new AssertionError("unopened carrier remains owned");
        carrier.start();if(!carrier.releaseReturned()||carrier.readyForVendor())throw new AssertionError("late start revived cancelled carrier");
        carrier.close();if(!carrier.releaseReturned()||callbacks.get()!=0)throw new AssertionError("cancelled carrier executed callbacks");
        System.out.println("ZeroCarrierCancelBeforeStartTest: close-before-start, late-start rejection and idempotent close passed; no audio invoked");
    }
}
