import local.friday.ktv.audio.protocol.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;

/** Combined carrier/policy/stopper schedules, without Android or audio. */
public final class CarrierStopRaceTest {
    static int checks;
    static void yes(boolean value){checks++;if(!value)throw new AssertionError("check "+checks);}
    static void beforeCarrierReady(){
        ProbePolicy p=ProbePolicyTest.started();CarrierGate carrier=new CarrierGate();StopDispatch dispatch=new StopDispatch();long lease=p.leaseId;
        yes(carrier.attach(lease));p.cancelPending();carrier.cancel();p.requestStop(ProbePolicy.USER);long first=dispatch.begin(lease,p.owns);yes(first>0);
        p.stopFinished(5010,true,2,false,true,true);yes(p.owns);yes(!carrier.beginClose(lease,false));yes(!dispatch.finish(lease,first,lease,p.owns,p.startReturned,true,false));
        carrier.sequenceFinished(lease);yes(!p.startFinished(5020,false,Integer.MIN_VALUE,false,false));yes(!carrier.ready(lease));yes(!carrier.allowVendorStart(lease));
        long last=dispatch.begin(lease,p.owns);yes(last>0);p.stopFinished(5030,true,2,true,true,true);yes(!dispatch.finish(lease,last,lease,p.owns,p.startReturned,true,true));yes(carrier.beginClose(lease,true));carrier.finishClose(lease,true);yes(!p.owns&&!carrier.occupied());
    }
    static void lateStartFinishesDuringStop(){
        ProbePolicy p=ProbePolicyTest.started();CarrierGate carrier=new CarrierGate();StopDispatch dispatch=new StopDispatch();long lease=p.leaseId;
        yes(carrier.attach(lease));yes(carrier.ready(lease));yes(carrier.allowVendorStart(lease));
        p.cancelPending();carrier.cancel();p.requestStop(ProbePolicy.USER);final long first=dispatch.begin(lease,p.owns);yes(first>0);
        CountDownLatch stopInFlight=new CountDownLatch(1),startReturned=new CountDownLatch(1);AtomicInteger closes=new AtomicInteger(),stops=new AtomicInteger(1);
        Thread stop=new Thread(()->{
            stopInFlight.countDown();DispatchRaceTest.await(startReturned);
            synchronized(p){p.stopFinished(5100,true,2,false,true,true);yes(p.owns);yes(!carrier.beginClose(lease,false));yes(dispatch.finish(lease,first,lease,p.owns,p.startReturned,true,false));long last=dispatch.begin(lease,p.owns);yes(last>0);stops.incrementAndGet();p.stopFinished(5110,true,2,true,true,true);yes(!dispatch.finish(lease,last,lease,p.owns,p.startReturned,true,true));yes(carrier.beginClose(lease,true));closes.incrementAndGet();carrier.finishClose(lease,true);}
        });
        Thread start=new Thread(()->{DispatchRaceTest.await(stopInFlight);synchronized(p){carrier.sequenceFinished(lease);yes(!p.startFinished(5090,true,0,true,true));yes(dispatch.begin(lease,p.owns)==0);}startReturned.countDown();});
        stop.start();start.start();DispatchRaceTest.join(stop);DispatchRaceTest.join(start);yes(stops.get()==2&&closes.get()==1);yes(!p.owns&&!carrier.occupied());
    }
    static void noNewLeaseBeforeRelease(){
        ProbePolicy p=ProbePolicyTest.started();CarrierGate carrier=new CarrierGate();long lease=p.leaseId;yes(carrier.attach(lease));yes(carrier.ready(lease));carrier.sequenceFinished(lease);yes(p.startFinished(5100,true,0,true,true));
        p.requestStop(ProbePolicy.USER);carrier.cancel();p.stopFinished(5200,true,2,true,true,true);yes(!p.owns&&carrier.occupied());yes(carrier.beginClose(lease,true));yes(!carrier.attach(lease+1));carrier.finishClose(lease,true);yes(carrier.attach(lease+1));
    }
    public static void main(String[] args){beforeCarrierReady();lateStartFinishesDuringStop();noNewLeaseBeforeRelease();System.out.println("CarrierStopRaceTest: "+checks+" checks passed across three combined stop schedules");}
}
