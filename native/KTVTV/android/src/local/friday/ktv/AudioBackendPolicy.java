package local.friday.ktv;

/** Route admission and buffer hints; a HAL allocation is not a delivery batch. */
public final class AudioBackendPolicy {
    public static final long PLATFORM_FRESHNESS_MILLIS=1500;
    private AudioBackendPolicy(){}
    public static int duplexOutput(int usbCount,int soleUsbId,int selectedInputId,int speakerCount,int speakerId,int selectedOutputId){
        return usbCount==1&&soleUsbId>0&&soleUsbId==selectedInputId&&speakerCount==1&&speakerId>0
            &&(selectedOutputId==0||selectedOutputId==speakerId)?speakerId:0;
    }
    public static int outputDemandFrames(int backend,long[] metrics){
        if(backend!=2)return (int)metrics[4];
        if(metrics.length<AudioBackendIO.METRIC_COUNT||metrics[63]!=1||metrics[6]<=0||metrics[6]>3840)throw new IllegalArgumentException("output_batch_unavailable");
        return (int)metrics[6];
    }
    public static int inputDeliveryFrames(int backend,long[] metrics){
        long waitUs=metrics[backend==2?35:19];
        return (int)Math.max(240,Math.min(80000,Math.max(0,waitUs))*48/1000);
    }
    public static boolean duplexMetricsAvailable(int backend,long[] metrics){return backend==2&&metrics.length>=AudioBackendIO.METRIC_COUNT&&metrics[63]==1;}
    public static boolean outputXrunsAvailable(int backend,long[] metrics){return backend==0||duplexMetricsAvailable(backend,metrics)&&metrics[12]>=0;}
    // AAudio server timestamps and API xrun counters do not establish physical HAL timing.
    public static boolean hardwareTimingAvailable(int backend,long[] metrics){return false;}
    public static boolean hardwareXrunsAvailable(int backend,long[] metrics){return false;}
    public static boolean routeChanged(int backend,int current,int expected){return expected>0&&(backend==2||current>0)&&current!=expected;}
    public static boolean platformFresh(long observedMs,long nowMs,long startupGraceUntilMs){
        if(observedMs<=0)return nowMs<startupGraceUntilMs;
        return observedMs<=nowMs&&nowMs-observedMs<=PLATFORM_FRESHNESS_MILLIS;
    }
}
