import local.friday.ktv.audio.protocol.GainRequestGate;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;

public final class GainCoalescingTest {
    public static void main(String[] args)throws Exception{
        GainRequestGate g=new GainRequestGate();long first=g.request(4,70,53);
        CountDownLatch inCall=new CountDownLatch(1),release=new CountDownLatch(1);AtomicInteger finished=new AtomicInteger();
        Thread worker=new Thread(()->{inCall.countDown();try{release.await();}catch(InterruptedException e){throw new AssertionError(e);}if(g.current(first)){g.finished(first);finished.incrementAndGet();}});
        worker.start();inCall.await();
        for(int i=0;i<100;i++)if(g.request(4,70,53)!=0||!g.current(first))throw new AssertionError("same heartbeat cancelled blocked LOW");
        release.countDown();worker.join(2000);if(worker.isAlive()||finished.get()!=1)throw new AssertionError("gain ACK starvation");
        long cut=g.request(4,70,26);if(cut==0||g.current(first))throw new AssertionError("stricter cap must replace target");
        long mute=g.request(4,0,0);if(mute==0||g.current(cut))throw new AssertionError("user mute takes priority");
        g.cancel();if(g.current(mute))throw new AssertionError("hard stop cancels");
        long next=g.request(5,70,53);if(next==0)throw new AssertionError("new lease cannot coalesce old target");
        System.out.println("GainCoalescingTest passed: blocked setter + repeated heartbeat, stricter cap, mute and lease replacement");
    }
}
