package local.friday.ktv;

import java.util.Arrays;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/** AAudio output callback plus bounded synchronous input reads on the Java capture thread. */
public final class NativeDuplexIO implements AudioBackendIO {
    public static final int INPUT_RATE_DEFAULT=0, INPUT_RATE_NATIVE_48K=1;
    public static final int METRIC_START=27;
    public static final String[] METRICS={
        "duplex_input_read_frames","duplex_startup_input_discarded_frames","duplex_output_callback_count",
        "duplex_output_callback_min_frames","duplex_output_callback_max_frames","duplex_output_callback_batch_max_frames","duplex_output_callback_batch_count",
        "duplex_input_read_wait_max_us","duplex_input_read_wait_p95_us","duplex_output_queue_budget_frames",
        "duplex_output_raw_rate","duplex_input_raw_rate","duplex_output_raw_capacity","duplex_input_raw_capacity","duplex_output_raw_burst","duplex_input_raw_burst",
        "duplex_output_raw_written","duplex_output_raw_read","duplex_input_raw_written","duplex_input_raw_read","duplex_output_mmap","duplex_input_mmap",
        "duplex_mmap_policy_restored","duplex_output_timestamp_available","duplex_output_pending_raw_frames","duplex_input_timestamp_available","duplex_input_pending_raw_frames",
        "duplex_output_pending_available","duplex_input_pending_available","duplex_input_callback_metrics_available","duplex_mmap_policy_available",
        "duplex_mmap_policy_before","duplex_mmap_policy_set_error","duplex_startup_drain_completed","duplex_output_state","duplex_input_state","duplex_schema_version",
        "duplex_sample_client_returned_frames","duplex_sample_client_discarded_frames","duplex_sample_begin_ms","duplex_sample_span_us",
        "duplex_sample_input_timestamp_error","duplex_sample_input_timestamp_position","duplex_sample_input_timestamp_age_us","duplex_sample_input_timestamp_future",
        "duplex_read_call_count","duplex_read_lock_wait_max_us","duplex_read_total_max_us","duplex_read_success_count",
        "duplex_delivery_gap_max_us","duplex_delivery_gap_over_10ms_count","duplex_delivery_gap_over_20ms_count","duplex_delivery_gap_over_50ms_count",
        "duplex_platform_refresh_count","duplex_platform_refresh_max_us","duplex_platform_refresh_last_us","duplex_route_refresh_lock_wait_max_us",
        "duplex_sample_started_valid","duplex_delivery_last_age_us","duplex_read_return_min_frames","duplex_read_return_max_frames",
        "duplex_input_hardware_rate","duplex_input_hardware_rate_available","duplex_input_rate_mode"};
    private static boolean available;
    static{if(android.os.Build.VERSION.SDK_INT>=29)try{System.loadLibrary("ktv_probe");available=true;}catch(UnsatisfiedLinkError ignored){}}
    interface Calls {
        int start(long handle);
        int read(long handle,short[] data,int frames);
        int write(long handle,short[] data,int offset,int samples);
        void metrics(long handle,long[] data);
        int route(long handle);
        void stop(long handle);
        void close(long handle);
    }
    private static final Calls JNI=new Calls(){
        public int start(long handle){return startNative(handle);}
        public int read(long handle,short[] data,int frames){return readNative(handle,data,frames);}
        public int write(long handle,short[] data,int offset,int samples){return writeNative(handle,data,offset,samples);}
        public void metrics(long handle,long[] data){metricsNative(handle,data);}
        public int route(long handle){return routeNative(handle);}
        public void stop(long handle){stopNative(handle);}
        public void close(long handle){closeNative(handle);}
    };
    private final ReentrantReadWriteLock lifecycle=new ReentrantReadWriteLock(true);
    private final AtomicBoolean stopped=new AtomicBoolean();
    private final int channels;
    private final Calls calls;
    private long handle;
    private volatile int startCode;
    NativeDuplexIO(long handle,int channels,Calls calls){this.handle=handle;this.channels=channels;this.calls=calls;}
    public static NativeDuplexIO open(int input,int expectedOutput,int channels){
        return open(input,expectedOutput,channels,INPUT_RATE_DEFAULT);
    }
    public static NativeDuplexIO open(int input,int expectedOutput,int channels,int inputRateMode){
        if(input<=0||expectedOutput<=0||channels<1||channels>2
                ||inputRateMode!=INPUT_RATE_DEFAULT&&inputRateMode!=INPUT_RATE_NATIVE_48K)throw new IllegalArgumentException();
        if(!available)return null;
        try{long value=openNative(input,expectedOutput,channels,inputRateMode);return value==0?null:new NativeDuplexIO(value,channels,JNI);}
        catch(UnsatisfiedLinkError missing){available=false;return null;}
    }
    public static int openCode(){return available?openCodeNative():9001;}
    public static long[] openMetrics(){if(available)return openMetricsNative();long[] empty=new long[METRIC_COUNT];empty[12]=empty[13]=-1;return empty;}
    public int startCode(){return startCode;}
    public boolean start(){
        lifecycle.readLock().lock();try{if(handle==0||stopped.get()){startCode=-9002;return false;}startCode=calls.start(handle);return startCode==0&&!stopped.get();}
        finally{lifecycle.readLock().unlock();}
    }
    public int read(short[] data,int frames){
        if(data==null||frames<1||frames>480||frames>data.length/channels)throw new IllegalArgumentException();
        lifecycle.readLock().lock();try{return handle==0||stopped.get()?-1:calls.read(handle,data,frames);}finally{lifecycle.readLock().unlock();}
    }
    public int write(short[] data,int offset,int samples){
        if(data==null||offset<0||samples<2||samples>960||offset>data.length||samples>data.length-offset||offset%2!=0||samples%2!=0)throw new IllegalArgumentException();
        lifecycle.readLock().lock();try{return handle==0||stopped.get()?-1:calls.write(handle,data,offset,samples);}finally{lifecycle.readLock().unlock();}
    }
    public void metrics(long[] data){
        if(data==null||data.length<METRIC_COUNT)throw new IllegalArgumentException();
        lifecycle.readLock().lock();try{Arrays.fill(data,0);data[12]=data[13]=-1;if(handle!=0)calls.metrics(handle,data);}finally{lifecycle.readLock().unlock();}
    }
    public int routeDevice(){lifecycle.readLock().lock();try{return handle==0?0:calls.route(handle);}finally{lifecycle.readLock().unlock();}}
    public void stop(){
        stopped.set(true);lifecycle.readLock().lock();try{if(handle!=0)calls.stop(handle);}finally{lifecycle.readLock().unlock();}
    }
    public void close(){
        try{stop();}finally{lifecycle.writeLock().lock();try{long old=handle;handle=0;if(old!=0)calls.close(old);}finally{lifecycle.writeLock().unlock();}}
    }
    private static native long openNative(int input,int expectedOutput,int channels,int inputRateMode);
    private static native int startNative(long handle);
    private static native int readNative(long handle,short[] data,int frames);
    private static native int writeNative(long handle,short[] data,int offset,int samples);
    private static native void metricsNative(long handle,long[] data);
    private static native int routeNative(long handle);
    private static native void stopNative(long handle);
    private static native void closeNative(long handle);
    private static native int openCodeNative();
    private static native long[] openMetricsNative();
}
