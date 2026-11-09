package local.friday.ktv;
/** Oboe owns duplex synchronization. Only aggregate counters cross JNI. */
public final class NativeVoiceMonitor implements AutoCloseable {
    public static final int METRIC_COUNT=97;
    private static boolean available;
    static{if(android.os.Build.VERSION.SDK_INT>=29)try{System.loadLibrary("ktv_probe");available=true;}catch(UnsatisfiedLinkError ignored){}}
    private long handle;
    private NativeVoiceMonitor(long value){handle=value;}
    public static NativeVoiceMonitor open(int input,int output,int channels,boolean quiet,boolean outputOnly,int mode){if(!available)return null;long value=openNative(input,output,channels,quiet,outputOnly,mode);return value==0?null:new NativeVoiceMonitor(value);}
    public static int openCode(){return available?openCodeNative():9001;}
    public static long[] openMetrics(){return available?openMetricsNative():new long[METRIC_COUNT];}
    public synchronized boolean start(){return handle!=0&&startNative(handle)==0;}
    public synchronized int routeDevice(){return handle==0?0:routeNative(handle);}
    public synchronized boolean armMonitor(int output){return handle!=0&&armNative(handle,output);}
    public synchronized void cancelMonitor(){if(handle!=0)cancelNative(handle);}
    public synchronized long[] monitorMetrics(){return handle==0?new long[METRIC_COUNT]:metricsNative(handle);}
    public synchronized void close(){if(handle!=0){long old=handle;handle=0;closeNative(old);}}
    private static native long openNative(int input,int output,int channels,boolean quiet,boolean outputOnly,int mode);
    private static native int openCodeNative();
    private static native long[] openMetricsNative();
    private static native int startNative(long handle);
    private static native int routeNative(long handle);
    private static native boolean armNative(long handle,int output);
    private static native void cancelNative(long handle);
    private static native long[] metricsNative(long handle);
    private static native void closeNative(long handle);
}
