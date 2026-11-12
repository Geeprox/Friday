import local.friday.ktv.audio.protocol.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

/** Deterministic schedules for the two identified cancellation bugs; no Android/audio. */
public final class DispatchRaceTest {
    static void await(CountDownLatch latch){try{if(!latch.await(2,TimeUnit.SECONDS))throw new AssertionError("latch timeout");}catch(InterruptedException e){throw new AssertionError(e);}}
    static void join(Thread thread){try{thread.join(2000);if(thread.isAlive())throw new AssertionError("thread timeout");}catch(InterruptedException e){throw new AssertionError(e);}}
    static void check(boolean value){if(!value)throw new AssertionError();}
    static void startCompletesWhileStopInFlight(){
        Object lock=new Object();StopDispatch gate=new StopDispatch();boolean[] returned={false};AtomicInteger dispatches=new AtomicInteger(1);
        final long ticket;synchronized(lock){ticket=gate.begin(1,true);check(ticket>0);}
        CountDownLatch resultComputed=new CountDownLatch(1),startFinished=new CountDownLatch(1);
        Thread stop=new Thread(()->{
            // The hardware call returned. Hold its final state commit until START's completion.
            resultComputed.countDown();await(startFinished);boolean retry;
            synchronized(lock){retry=gate.finish(1,ticket,1,true,returned[0],true,false);}
            if(retry)synchronized(lock){if(gate.begin(1,true)>0)dispatches.incrementAndGet();}
        });
        Thread start=new Thread(()->{await(resultComputed);synchronized(lock){returned[0]=true;if(gate.begin(1,true)>0)dispatches.incrementAndGet();}startFinished.countDown();});
        stop.start();start.start();join(stop);join(start);check(dispatches.get()==2);
    }
    static void stopCompletesBeforeStart(){
        Object lock=new Object();StopDispatch gate=new StopDispatch();AtomicInteger dispatches=new AtomicInteger(1);
        final long ticket;synchronized(lock){ticket=gate.begin(1,true);check(ticket>0);}
        CountDownLatch released=new CountDownLatch(1);
        Thread stop=new Thread(()->{synchronized(lock){check(!gate.finish(1,ticket,1,true,false,true,false));}released.countDown();});
        Thread start=new Thread(()->{await(released);synchronized(lock){if(gate.begin(1,true)>0)dispatches.incrementAndGet();}});
        stop.start();start.start();join(stop);join(start);check(dispatches.get()==2);
    }
    static void cancelledQueuedStart(){
        ProbePolicy p=ProbePolicyTest.ready();long epoch=p.cancellationEpoch;AtomicInteger starts=new AtomicInteger();
        CountDownLatch queued=new CountDownLatch(1),released=new CountDownLatch(1);
        Thread command=new Thread(()->{queued.countDown();await(released);synchronized(p){if(p.pendingAllowed(epoch)&&p.beginStart(5000,5000,true,true))starts.incrementAndGet();}});
        command.start();await(queued);synchronized(p){p.cancelPending();check(!p.owns);}released.countDown();join(command);check(starts.get()==0&&!p.owns);
    }
    static void lateLowCannotReviveStoppedState(){
        ProbePolicy p=ProbePolicyTest.started();long lease=p.leaseId;p.startFinished(5100,true,0,true,true);check(p.allowGain(lease,5200,5200,true,5));
        p.requestStop(ProbePolicy.USER);p.stopFinished(5300,true,2,true,true,true);p.gainFinished(lease,5,true,0);
        check(!p.owns&&p.state==ProbePolicy.STOPPED&&p.halStatus==2);
    }
    public static void main(String[] args){startCompletesWhileStopInFlight();stopCompletesBeforeStart();cancelledQueuedStart();lateLowCannotReviveStoppedState();System.out.println("DispatchRaceTest: four deterministic schedules passed");}
}
