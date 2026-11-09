import local.friday.ktv.MonitorStability;
public final class MonitorStabilityTest {
    public static void main(String[] args){
        long[] first=new long[18],last=new long[18];first[0]=first[1]=first[2]=48000;
        last[0]=last[1]=last[2]=720000;last[8]=12;last[14]=6000;
        if(!MonitorStability.healthy(first,last,14000,15000))throw new AssertionError("healthy continuous transport rejected");
        long[] ordinary=last.clone();ordinary[8]=10;
        if(!MonitorStability.healthy(first,ordinary,14000,15000))throw new AssertionError("stable ordinary path rejected as if stability guaranteed latency");
        long[] oneGap=last.clone();oneGap[5]=256;
        if(MonitorStability.failures(first,oneGap,14000,15000,true)!=256)throw new AssertionError("missing-frame rejection lost its distinct reason");
        if(MonitorStability.failures(first,oneGap,14000,15000,false)!=0)throw new AssertionError("output-only result includes input condition");
        long[] bad=last.clone();bad[1]=31680;if(MonitorStability.healthy(first,bad,14000,15000))throw new AssertionError("stalled output accepted");
        bad=last.clone();bad[5]=192;if(MonitorStability.healthy(first,bad,14000,15000))throw new AssertionError("voice gaps accepted");
        bad=last.clone();bad[7]=885;if(MonitorStability.healthy(first,bad,14000,15000))throw new AssertionError("native failure accepted");
        bad=last.clone();bad[6]=1;if(MonitorStability.healthy(first,bad,14000,15000))throw new AssertionError("audible quiet check accepted");
        bad=last.clone();bad[14]=100000;if(MonitorStability.healthy(first,bad,14000,15000))throw new AssertionError("callback stall accepted");
        bad=last.clone();bad[0]=0;if(MonitorStability.healthy(first,bad,14000,15000))throw new AssertionError("input reset accepted");
        long[] output=last.clone();output[0]=output[2]=0;
        if(!MonitorStability.outputHealthy(first,output,14000,15000))throw new AssertionError("output-only check requires microphone");
        if(MonitorStability.healthy(first,output,14000,15000))throw new AssertionError("output-only check enabled voice audition");
        output[7]=885;if(MonitorStability.outputHealthy(first,output,14000,15000))throw new AssertionError("output-only timeout accepted");
        long[] recent=java.util.Arrays.copyOf(last,59);recent[50]=10000;
        if(!MonitorStability.outputHealthy(first,recent,14000,15000))throw new AssertionError("recent callback rejected");
        // A 200 ms stall at the end still fits the 2% frame-rate tolerance. No next
        // callback arrives to increase gap_max_us, so callback recency must reject it.
        recent[1]-=9600;recent[50]=200000;
        if(MonitorStability.outputHealthy(first,recent,14000,15000))throw new AssertionError("stalled final callback accepted by average rate");
        long[] nativeFirst=java.util.Arrays.copyOf(first,59),nativeLast=java.util.Arrays.copyOf(last,59);
        nativeFirst[48]=1000;nativeLast[48]=15000;nativeLast[50]=10000;
        if(!MonitorStability.healthy(nativeFirst,nativeLast,16000,17000))throw new AssertionError("cleanup time polluted the native sampling interval");
        nativeLast[48]=9000;
        if(MonitorStability.healthy(nativeFirst,nativeLast,14000,15000))throw new AssertionError("short native observation accepted using Java wall time");
        nativeLast[48]=500;
        if(MonitorStability.outputHealthy(nativeFirst,nativeLast,14000,15000))throw new AssertionError("reset native clock accepted");
        // New observations are diagnostic only; they must not relax or add a
        // transport gate condition. Keep a populated 0.41 tail in both cases.
        long[] extendedFirst=java.util.Arrays.copyOf(first,97),extendedLast=java.util.Arrays.copyOf(last,97);
        extendedFirst[48]=1000;extendedLast[48]=15000;extendedLast[50]=10000;
        java.util.Arrays.fill(extendedLast,61,97,Long.MAX_VALUE);
        if(!MonitorStability.healthy(extendedFirst,extendedLast,14000,15000))throw new AssertionError("diagnostic fields changed the quiet gate");
        extendedLast[5]=1;
        if(MonitorStability.failures(extendedFirst,extendedLast,14000,15000,true)!=256)throw new AssertionError("diagnostic fields hid the existing missing-frame rejection");
    }
}
