import local.friday.ktv.audio.protocol.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;

/** Real pure gates with paused remote-call substitutes. No Android, JNI or audio. */
public final class PositiveGainStopRaceTest {
    static int checks;
    static void yes(boolean value,String reason){checks++;if(!value)throw new AssertionError(reason);}
    static void await(CountDownLatch latch){DispatchRaceTest.await(latch);}
    static final class Harness {
        final ProbePolicy policy=ProbePolicyTest.started();
        final GainRequestGate requests=new GainRequestGate();
        final GainWriteFence writes=new GainWriteFence();
        final StopDispatch stops=new StopDispatch();
        final List<String> calls=Collections.synchronizedList(new ArrayList<String>());
        final CountDownLatch firstStop=new CountDownLatch(1),returnStop=new CountDownLatch(1);
        final boolean pauseFirstStop;
        int stopCalls;long clock=6000;
        Harness(boolean pause){
            pauseFirstStop=pause;yes(writes.beginLease(policy.leaseId,policy.cancellationEpoch),"lease fence begins with ownership");
            yes(policy.startFinished(5100,true,0,true,true),"START sequence finished");
        }
        long request(){synchronized(policy){return requests.request(policy.leaseId,40,40);}}
        long register(long lease,long epoch,long ticket){synchronized(policy){
            if(!policy.pendingAllowed(epoch)||!policy.owns||policy.leaseId!=lease||!requests.current(ticket)
                ||!policy.allowGain(lease,clock,clock,true,40))return 0;
            return writes.register(lease,epoch,ticket);
        }}
        void exited(long lease,long token){boolean finalStop;synchronized(policy){finalStop=writes.finished(lease,token);}if(finalStop)stop(lease);}
        void stop(long expectedLease){
            final long lease,ticket;
            synchronized(policy){
                if(!policy.owns||policy.leaseId!=expectedLease)return;
                policy.cancelPending();requests.cancel();writes.cancel(policy.leaseId);policy.requestStop(ProbePolicy.USER);
                lease=policy.leaseId;ticket=stops.begin(lease,policy.owns);
            }
            calls.add("END");if(ticket==0)return;
            final boolean afterStart,afterWrites;final int number;
            synchronized(policy){
                yes(stops.current(lease,ticket),"lease-bound STOP dispatch");
                afterStart=policy.startReturned;afterWrites=writes.settled(lease);number=++stopCalls;
            }
            calls.add(afterStart&&afterWrites?"STOP_FINAL":"STOP_EARLY");
            if(number==1){firstStop.countDown();if(pauseFirstStop)await(returnStop);}
            final boolean retry;
            synchronized(policy){
                policy.stopFinished(++clock,true,2,afterStart,true,afterWrites);
                retry=stops.finish(lease,ticket,policy.leaseId,policy.owns,policy.startReturned,writes.settled(lease),afterStart&&afterWrites);
            }
            if(retry)stop(lease);
        }
        void nextLease(){synchronized(policy){
            yes(!policy.owns,"previous lease actually closed");
            yes(policy.idleEvidence(ProbePolicyTest.target(),"1.1.3","single",2,ProbePolicyTest.cards(),true),"new idle evidence");
            yes(policy.beginStart(++clock,clock,true,true),"fresh local start");
            yes(writes.beginLease(policy.leaseId,policy.cancellationEpoch),"new fence generation");
            yes(policy.startFinished(++clock,true,0,true,true),"fresh START sequence");
        }}
    }
    static final class PausedWrite {
        final CountDownLatch registered=new CountDownLatch(1),resume=new CountDownLatch(1);
        final AtomicReference<Throwable> failure=new AtomicReference<>();
        final Thread thread;volatile long token;
        PausedWrite(Harness h,String name,boolean throwCall){
            long lease=h.policy.leaseId,epoch=h.policy.cancellationEpoch,ticket=h.request();
            thread=new Thread(()->{
                try{
                    token=h.register(lease,epoch,ticket);registered.countDown();await(resume);
                    if(token!=0)try{h.calls.add(name);if(throwCall)throw new IllegalStateException("synthetic remote failure");}
                    finally{h.exited(lease,token);}
                }catch(Throwable e){if(!throwCall||!(e instanceof IllegalStateException))failure.set(e);}
            },"fake-positive-write");
            thread.start();await(registered);yes(token>0,"positive call is registered before it can be paused");
        }
        void finish(){resume.countDown();DispatchRaceTest.join(thread);if(failure.get()!=null)throw new AssertionError(failure.get());}
    }
    static void pausedAfterCheck(String name){
        Harness h=new Harness(false);PausedWrite write=new PausedWrite(h,name,false);long lease=h.policy.leaseId;
        h.stop(lease);await(h.firstStop);
        synchronized(h.policy){yes(h.policy.owns&&h.policy.state==ProbePolicy.RECOVERY,"early successful STOP cannot publish released");yes(!h.writes.settled(lease),"paused call remains pending");}
        yes(h.calls.contains("STOP_EARLY"),"STOP did not wait for paused remote call");
        write.finish();
        yes(h.calls.indexOf("STOP_EARLY")<h.calls.indexOf(name),"test exercises the original check-to-call race");
        yes(h.calls.indexOf(name)<h.calls.indexOf("STOP_FINAL"),"final STOP is issued after the late positive write exited");
        yes(!h.policy.owns&&h.policy.stopAfterGainWrites&&h.stopCalls==2,"only final STOP closes owner");
        long another=h.request();yes(h.register(lease,h.policy.cancellationEpoch,another)==0,"no positive write after final STOP");
    }
    static void cancelledBeforeRegistration(){
        Harness h=new Harness(false);long lease=h.policy.leaseId,epoch=h.policy.cancellationEpoch,ticket=h.request();
        CountDownLatch checked=new CountDownLatch(1),resume=new CountDownLatch(1);long[] token={-1};
        Thread worker=new Thread(()->{checked.countDown();await(resume);token[0]=h.register(lease,epoch,ticket);});worker.start();await(checked);
        h.stop(lease);resume.countDown();DispatchRaceTest.join(worker);
        yes(token[0]==0&&!h.policy.owns&&h.stopCalls==1,"cancellation before atomic registration requires no second STOP");
    }
    static void completionDuringImmediateStop(){
        Harness h=new Harness(true);PausedWrite write=new PausedWrite(h,"SYSTEM_GAIN_100",false);long lease=h.policy.leaseId;
        Thread stop=new Thread(()->h.stop(lease));stop.start();await(h.firstStop);write.finish();
        yes(h.stopCalls==1&&h.policy.owns,"writer completion does not dispatch over an in-flight STOP");
        h.returnStop.countDown();DispatchRaceTest.join(stop);
        yes(h.stopCalls==2&&!h.policy.owns,"STOP completion observes settled writers and dispatches final STOP");
        yes(h.calls.lastIndexOf("SYSTEM_GAIN_100")<h.calls.lastIndexOf("STOP_FINAL"),"final ordering holds across opposite completion schedule");
    }
    static void onlyLastExitRetries(){
        Harness h=new Harness(false);long lease=h.policy.leaseId,epoch=h.policy.cancellationEpoch,ticket=h.request();
        long mic=h.register(lease,epoch,ticket),system=h.register(lease,epoch,ticket);yes(mic>0&&system>0,"both positive writer kinds can be tracked");
        h.stop(lease);h.calls.add("MIC_GAIN_40");h.exited(lease,mic);
        yes(h.stopCalls==1&&h.policy.owns&&h.writes.pending(lease)==1,"first completion cannot release second writer");
        h.calls.add("SYSTEM_GAIN_100");h.exited(lease,system);
        yes(h.stopCalls==2&&!h.policy.owns,"last completion alone dispatches final STOP");
        h.exited(lease,system);yes(h.stopCalls==2,"duplicate completion cannot dispatch more STOPs");
    }
    static void exceptionalExitStillSettles(){
        Harness h=new Harness(false);PausedWrite write=new PausedWrite(h,"MIC_GAIN_THROW",true);h.stop(h.policy.leaseId);write.finish();
        yes(!h.policy.owns&&h.stopCalls==2,"exceptional remote exit is fenced by finally");
    }
    static void oldLeaseCannotStopNewLease(){
        Harness h=new Harness(false);long old=h.policy.leaseId;h.stop(old);h.nextLease();long newer=h.policy.leaseId,ticket=h.request();
        long token=h.register(newer,h.policy.cancellationEpoch,ticket);yes(token>0,"new lease positive reservation");int calls=h.calls.size();
        h.exited(old,token);h.stop(old);
        yes(h.calls.size()==calls&&h.policy.owns&&h.writes.pending(newer)==1,"old writer/STOP callback cannot revoke or settle new lease");
        h.exited(newer,token);h.stop(newer);yes(!h.policy.owns,"current lease still closes normally");
    }
    static void staleStopTicketCannotFinishNewAttempt(){
        StopDispatch d=new StopDispatch();long old=d.begin(4,true);yes(old>0,"old dispatch");
        yes(!d.finish(4,old,4,false,true,true,true),"old attempt completes");long next=d.begin(5,true);
        yes(next>old&&!d.finish(4,old,5,true,true,true,false)&&d.current(5,next),"stale completion cannot alter active next-lease STOP");
    }
    public static void main(String[] args){
        cancelledBeforeRegistration();pausedAfterCheck("MIC_GAIN_40");pausedAfterCheck("SYSTEM_GAIN_100");completionDuringImmediateStop();
        onlyLastExitRetries();exceptionalExitStillSettles();oldLeaseCannotStopNewLease();staleStopTicketCannotFinishNewAttempt();
        System.out.println("PositiveGainStopRaceTest: "+checks+" checks passed; paused MIC/SYSTEM calls, immediate STOP, final fence and stale-lease isolation");
    }
}
