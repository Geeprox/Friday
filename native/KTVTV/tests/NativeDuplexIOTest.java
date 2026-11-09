package local.friday.ktv;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/** Exercises the real Java lifecycle wrapper with fake native bindings on the host JVM. */
public final class NativeDuplexIOTest {
    private static void check(boolean value,String why){if(!value)throw new AssertionError(why);}
    private static void await(CountDownLatch latch)throws Exception{check(latch.await(2,TimeUnit.SECONDS),"bounded test latch expired");}
    private static void join(Thread thread)throws Exception{thread.join(2000);check(!thread.isAlive(),"wrapper deadlocked");}
    private static void rejected(Runnable action){try{action.run();}catch(IllegalArgumentException expected){return;}throw new AssertionError("invalid JNI buffer bounds accepted");}
    private static final class Fake implements NativeDuplexIO.Calls {
        final List<String> events;final String name;final AtomicInteger closes=new AtomicInteger(),writes=new AtomicInteger();
        final CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1),stopped=new CountDownLatch(1),startEntered=new CountDownLatch(1);
        volatile boolean blockRead,blockStart,throwStart,throwStop,closed;int startResult;
        Fake(String name,List<String> events){this.name=name;this.events=events;}
        void event(String event){check(!closed,"JNI called after close");events.add(name+":"+event);}
        public int start(long h){event("start");startEntered.countDown();if(blockStart)try{await(release);}catch(Exception e){throw new AssertionError(e);}if(throwStart)throw new IllegalStateException("synthetic_start");return startResult;}
        public int read(long h,short[] data,int frames){event("read");entered.countDown();if(blockRead)try{await(release);}catch(Exception e){throw new AssertionError(e);}return frames*2;}
        public int write(long h,short[] data,int offset,int samples){event("write");writes.incrementAndGet();return samples;}
        public void metrics(long h,long[] data){event("metrics");data[63]=1;data[14]=Math.abs(startResult);}
        public int route(long h){event("route");return 2;}
        public void stop(long h){event("stop");stopped.countDown();if(throwStop)throw new IllegalStateException("synthetic_stop");}
        public void close(long h){event("close");closed=true;closes.incrementAndGet();}
    }
    private static void startupAndFallback(){
        List<String> events=Collections.synchronizedList(new ArrayList<>());Fake failing=new Fake("new",events);failing.startResult=-60002;
        NativeDuplexIO failed=new NativeDuplexIO(1,2,failing);check(!AudioBackendIO.startOrClose(failed),"failed startup accepted");check(failed.startCode()==-60002&&failing.closes.get()==1,"failed route lost code or ownership");
        Fake fallback=new Fake("old",events);NativeDuplexIO accepted=new NativeDuplexIO(2,2,fallback);check(AudioBackendIO.startOrClose(accepted),"fallback did not start");
        check(events.indexOf("new:close")<events.indexOf("old:start"),"fallback overlapped failed stream");accepted.close();accepted.close();check(fallback.closes.get()==1,"close was not idempotent");
        Fake exceptional=new Fake("exception",events);exceptional.throwStart=true;NativeDuplexIO exception=new NativeDuplexIO(3,2,exceptional);
        try{AudioBackendIO.startOrClose(exception);throw new AssertionError("synthetic start exception lost");}catch(IllegalStateException expected){}
        check(exceptional.closes.get()==1,"start exception leaked native owner");
        Fake stopFailure=new Fake("stop_failure",events);stopFailure.startResult=-1;stopFailure.throwStop=true;NativeDuplexIO cleanup=new NativeDuplexIO(4,2,stopFailure);
        try{AudioBackendIO.startOrClose(cleanup);throw new AssertionError("synthetic stop exception lost");}catch(IllegalStateException expected){}
        check(stopFailure.closes.get()==1,"stop exception skipped native close");
    }
    private static void concurrentClose()throws Exception{
        Fake fake=new Fake("concurrent",Collections.synchronizedList(new ArrayList<>()));fake.blockRead=true;
        NativeDuplexIO io=new NativeDuplexIO(7,2,fake);AtomicReference<Throwable> error=new AtomicReference<>();
        Thread read=new Thread(()->{try{check(io.read(new short[480],240)==480,"read count changed");}catch(Throwable t){error.set(t);}});read.start();await(fake.entered);
        check(io.write(new short[480],0,480)==480&&fake.writes.get()==1,"input read serialized output writes");
        Thread close=new Thread(()->{try{io.close();}catch(Throwable t){error.set(t);}});close.start();await(fake.stopped);
        check(fake.closes.get()==0,"native owner closed while read was active");fake.release.countDown();join(read);join(close);check(error.get()==null,"concurrent wrapper failed: "+error.get());
        check(fake.closes.get()==1&&io.read(new short[480],240)==-1&&io.write(new short[480],0,480)==-1&&io.routeDevice()==0,"closed wrapper reached JNI");
        long[] metrics=new long[91];Arrays.fill(metrics,123);io.metrics(metrics);for(int i=0;i<metrics.length;i++)check(metrics[i]==(i==12||i==13?-1:0),"closed snapshot retained data or fabricated xrun availability");
    }
    private static void cancelStartup()throws Exception{
        Fake fake=new Fake("startup",Collections.synchronizedList(new ArrayList<>()));fake.blockStart=true;
        NativeDuplexIO io=new NativeDuplexIO(8,2,fake);AtomicReference<Throwable> error=new AtomicReference<>();
        Thread start=new Thread(()->{try{check(!AudioBackendIO.startOrClose(io),"cancelled startup accepted");}catch(Throwable t){error.set(t);}});
        start.start();await(fake.startEntered);io.stop();await(fake.stopped);fake.release.countDown();join(start);
        check(error.get()==null&&fake.closes.get()==1,"startup cancellation leaked or raced the owner");
    }
    public static void main(String[] args)throws Exception{
        check(android.os.Build.VERSION.SDK_INT==0,"host SDK stub jar required; never run this test on a device");
        check(NativeDuplexIO.openCode()==9001&&NativeDuplexIO.openMetrics().length==91,"unavailable JNI fallback invalid");
        check(AudioBackendIO.METRIC_COUNT==91&&NativeDuplexIO.METRICS.length==64&&NativeDuplexIO.METRIC_START+NativeDuplexIO.METRICS.length==91,"duplex metric range changed");
        check(NativeDuplexIO.INPUT_RATE_DEFAULT==0&&NativeDuplexIO.INPUT_RATE_NATIVE_48K==1,"input-rate mode ABI changed");
        rejected(()->NativeDuplexIO.open(9,2,2,2));rejected(()->NativeDuplexIO.open(9,2,2,-1));
        check(NativeDuplexIO.open(9,2,2)==null&&NativeDuplexIO.open(9,2,2,1)==null,"host fallback invoked native audio");
        String[] suffixes=("input_read_frames startup_input_discarded_frames output_callback_count output_callback_min_frames output_callback_max_frames output_callback_batch_max_frames output_callback_batch_count "+
            "input_read_wait_max_us input_read_wait_p95_us output_queue_budget_frames output_raw_rate input_raw_rate output_raw_capacity input_raw_capacity output_raw_burst input_raw_burst "+
            "output_raw_written output_raw_read input_raw_written input_raw_read output_mmap input_mmap mmap_policy_restored output_timestamp_available output_pending_raw_frames input_timestamp_available input_pending_raw_frames "+
            "output_pending_available input_pending_available input_callback_metrics_available mmap_policy_available mmap_policy_before mmap_policy_set_error startup_drain_completed output_state input_state schema_version "+
            "sample_client_returned_frames sample_client_discarded_frames sample_begin_ms sample_span_us sample_input_timestamp_error sample_input_timestamp_position sample_input_timestamp_age_us sample_input_timestamp_future "+
            "read_call_count read_lock_wait_max_us read_total_max_us read_success_count delivery_gap_max_us delivery_gap_over_10ms_count delivery_gap_over_20ms_count delivery_gap_over_50ms_count "+
            "platform_refresh_count platform_refresh_max_us platform_refresh_last_us route_refresh_lock_wait_max_us sample_started_valid delivery_last_age_us read_return_min_frames read_return_max_frames "+
            "input_hardware_rate input_hardware_rate_available input_rate_mode").split(" ");
        for(int i=0;i<suffixes.length;i++)check(NativeDuplexIO.METRICS[i].equals("duplex_"+suffixes[i]),"duplex native index changed: "+(i+27));
        Fake fake=new Fake("bounds",new ArrayList<>());NativeDuplexIO io=new NativeDuplexIO(1,2,fake);
        rejected(()->io.read(new short[960],481));rejected(()->io.read(new short[1],1));rejected(()->io.read(new short[1],Integer.MAX_VALUE));rejected(()->io.read(new short[1],0));
        rejected(()->io.write(new short[4],1,2));rejected(()->io.write(new short[4],2,Integer.MAX_VALUE));rejected(()->io.write(new short[4],Integer.MAX_VALUE,2));rejected(()->io.write(new short[1000],0,1000));rejected(()->io.metrics(new long[90]));
        check(fake.events.isEmpty(),"invalid bounds reached JNI");io.close();startupAndFallback();concurrentClose();cancelStartup();
        System.out.println("Native wrapper schema, failed-start fallback, exceptions, concurrent read/write/stop/close and buffer bounds passed; fake bindings only");
    }
}
