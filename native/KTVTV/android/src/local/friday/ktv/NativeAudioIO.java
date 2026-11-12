package local.friday.ktv;
/** Callback-backed native I/O; Java DSP stays outside the real-time callbacks. */
public final class NativeAudioIO implements AudioBackendIO {
    private static boolean available;
    static{if(android.os.Build.VERSION.SDK_INT>=29)try{System.loadLibrary("ktv_probe");available=true;}catch(UnsatisfiedLinkError ignored){}}
    private volatile long handle;
    private NativeAudioIO(long handle){this.handle=handle;}
    public static NativeAudioIO open(int input,int channels,int burst){if(!available)return null;long value=openNative(input,channels,burst);return value==0?null:new NativeAudioIO(value);}
    public static NativeAudioIO openMonitor(int input,int channels,int burst){if(!available)return null;long value=openMonitorAAudioNative(input,channels,burst);return value==0?null:new NativeAudioIO(value);}
    public synchronized boolean armMonitor(int output){return handle!=0&&armMonitorNative(handle,output);}
    public synchronized void cancelMonitor(){if(handle!=0)cancelMonitorNative(handle);}
    public synchronized long[] monitorMetrics(){return handle==0?new long[12]:monitorMetricsNative(handle);}
    public synchronized int routeDevice(){return handle==0?0:routeNative(handle);}
    public static int openCode(){return available?openCodeNative():9001;}
    public boolean start(){return startNative(handle)==0;}
    public int read(short[] data,int frames){return readNative(handle,data,frames);}
    public int write(short[] data,int offset,int samples){return writeNative(handle,data,offset,samples);}
    public void metrics(long[] data){if(data.length>27)java.util.Arrays.fill(data,27,data.length,0);metricsNative(handle,data);}
    public synchronized void stop(){if(handle!=0)stopNative(handle);}
    public synchronized void close(){long old=handle;handle=0;if(old!=0)closeNative(old);}
    private static native int openCodeNative();
    private static native long openMonitorNative(int input,int channels,int burst);
    private static native long openMonitorAAudioNative(int input,int channels,int burst);
    private static native boolean armMonitorNative(long handle,int output);
    private static native void cancelMonitorNative(long handle);
    private static native long[] monitorMetricsNative(long handle);
    private static native long openNative(int input,int channels,int burst);
    private static native int routeNative(long handle);
    private static native int startNative(long handle);
    private static native int readNative(long handle,short[] data,int frames);
    private static native int writeNative(long handle,short[] data,int offset,int samples);
    private static native void metricsNative(long handle,long[] data);
    private static native void stopNative(long handle);
    private static native void closeNative(long handle);
}
