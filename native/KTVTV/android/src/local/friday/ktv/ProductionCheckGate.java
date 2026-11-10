package local.friday.ktv;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.BooleanSupplier;

/** Pure, aggregate-only contracts for a silent check of the production audio chain. */
public final class ProductionCheckGate {
    private ProductionCheckGate() {}
    public static final int PREPARE_MS=2000, CHECK_MS=15000, WARMUP_MS=1000,
            LAST_BASELINE_MS=1500, STABLE_MS=13000, SAMPLE_MS=200, RATE=48000;
    public static final int RUNNING=0, PASS=1, CANCELLED=2, ROUTE_FAILED=3,
            PREPARE_FAILED=4, ENGINE_FAILED=5, GATE_FAILED=6, DEADLINE=7;
    public static final int CANCEL_USER=1, CANCEL_BACKGROUND=2, CANCEL_FOCUS=3,
            CANCEL_ROUTE=4, CANCEL_DEADLINE=5, CANCEL_ENGINE=6;
    public static final int SCHEMA=1, DURATION=2, BACKEND=4, ROUTE=8, NOT_SILENT=16,
            ERROR=32, INPUT_RATE=64, OUTPUT_RATE=128, COUNTER_RESET=256,
            JAVA_MISSING=512, JAVA_DROP=1024, REBUFFER=2048, OUTPUT_MISSING=4096,
            CALLBACK_GAP=8192, CONFIGURATION=16384, STALE=32768, NOT_RUNNING=65536;
    public static final int NATIVE_COUNT=91;
    private static final String PREFIX="production_check_";
    private static final String[] SUMMARY={
        "result","valid","quiet","finished_at_s","elapsed_ms","steady_ms","baseline_elapsed_ms",
        "gate_failures","cancel_reason","backend","expected_input_route","expected_output_route",
        "actual_input_route","actual_output_route","input_rate","output_rate","input_delta_frames",
        "output_delta_frames","render_delta_frames","native_input_delta_frames","java_missing_delta_frames",
        "java_drop_delta_frames","rebuffer_delta","native_output_missing_delta_frames","output_peak",
        "input_fault","safety_reason","native_error","target_frames","queue_frames","queue_peak_frames",
        "output_queue_frames","dsp_max_us","raw_input_rate","raw_output_rate","input_drop_available",
        "input_dropped_frames","java_drop_available","snapshot_before_stop","stop_completed","schema_version",
        "captured_frames","rendered_frames","java_missing_frames","java_dropped_frames","rebuffer_count",
        "input_read_p95_us","rate_ppm","native_baseline_snapshot_ms","native_final_snapshot_ms",
        "input_monitor_ready","input_peak","output_type","duplex_attempted","duplex_open_code","duplex_start_code","engine_end_code"
    };
    /** Fixed allowlist: summary keys followed by exactly native_00 ... native_90, schema 1. */
    public static final String[] KEYS=makeKeys();
    private static String[] makeKeys(){
        String[] keys=new String[SUMMARY.length+NATIVE_COUNT];
        for(int i=0;i<SUMMARY.length;i++)keys[i]=PREFIX+SUMMARY[i];
        for(int i=0;i<NATIVE_COUNT;i++)keys[SUMMARY.length+i]=PREFIX+"native_"+(i<10?"0":"")+i;
        return keys;
    }
    public static final class Snapshot {
        public long elapsedMs,captured,rendered,missing,dropped,rebuffers,queue,outputQueue,dspNanos;
        public int backend,inputRoute,outputRoute,inputRate,outputRate,outputPeak,inputFault,safetyReason;
        public int target,queuePeak,readP95Us,ratePpm,inputPeak,outputType;
        public boolean running,silent,inputReady,duplexAttempted;
        public long duplexOpenCode,duplexStartCode;
        public int engineEndCode;
        public long[] nativeMetrics=new long[0];
    }
    public static long metric(Snapshot s,int index){
        return s!=null&&s.nativeMetrics!=null&&index>=0&&index<s.nativeMetrics.length?s.nativeMetrics[index]:-1;
    }
    /** Fixed diagnostic codes only; no exception text or private paths leave this process. */
    public static int endCode(String code){
        if("stopped".equals(code))return 1;
        if("strict_input_unavailable".equals(code))return 2;
        if("audio_unavailable".equals(code))return 3;
        if("completed".equals(code))return 4;
        if("output_route_unavailable".equals(code)||"output_route_mismatch".equals(code)||"output_route_changed".equals(code))return 5;
        if("input_route_unavailable".equals(code)||"input_route_changed".equals(code))return 6;
        if("no_microphone_frames".equals(code)||"input_read_failed".equals(code)||"input_clipped".equals(code))return 7;
        if("audio_route_stale".equals(code)||"output_write_failed".equals(code))return 8;
        if("input_stop_timeout".equals(code))return 9;
        return 10;
    }
    public static int readyFailures(Snapshot s,int expectedInput,int expectedOutput){
        if(s==null||s.nativeMetrics==null||s.nativeMetrics.length!=NATIVE_COUNT||metric(s,63)!=1)return SCHEMA;
        int result=0;
        if(!s.running)result|=NOT_RUNNING;
        if(s.backend!=2)result|=BACKEND;
        if(expectedInput<=0||expectedOutput<=0||s.inputRoute!=expectedInput||s.outputRoute!=expectedOutput
                ||metric(s,3)!=expectedInput||metric(s,2)!=expectedOutput)result|=ROUTE;
        if(!s.silent||s.outputPeak!=0)result|=NOT_SILENT;
        if(metric(s,14)!=0||s.inputFault!=0||s.safetyReason!=0)result|=ERROR;
        if(s.inputRate!=RATE||s.outputRate!=RATE||metric(s,16)!=RATE||metric(s,17)!=RATE
                ||metric(s,37)!=RATE||metric(s,38)!=RATE||metric(s,9)!=10&&metric(s,9)!=12
                ||metric(s,90)!=1 // Explicit strict 48 kHz diagnostic mode, not normal production.
                ||metric(s,89)!=0&&metric(s,89)!=1
                ||metric(s,89)==1&&metric(s,88)!=RATE||metric(s,89)==0&&metric(s,88)!=0
                ||metric(s,8)!=10&&metric(s,8)!=12||metric(s,47)!=1||metric(s,48)!=1
                ||metric(s,49)!=1||metric(s,57)!=1||metric(s,59)!=0||metric(s,60)!=1
                ||metric(s,61)!=4||metric(s,62)!=4||!s.inputReady
                ||s.target<480||s.target>3840||s.queuePeak<s.target||s.queuePeak>11520
                ||metric(s,36)<=0||metric(s,36)>1440)result|=CONFIGURATION;
        if(s.captured<=0||s.rendered<=0||metric(s,21)<=0||metric(s,27)<=0)result|=COUNTER_RESET;
        if(metric(s,23)<0||metric(s,23)>50000)result|=CALLBACK_GAP;
        if(metric(s,25)<=0)result|=STALE;
        return result;
    }
    private static long delta(long first,long last){return first<0||last<first?-1:last-first;}
    private static boolean rateMatches(long frames,long ms){
        if(frames<0||ms<=0||ms>CHECK_MS)return false;
        long expected=ms*48;
        return frames>=(expected*98+99)/100&&frames<=expected*102/100;
    }
    /** Checks every stable sample for regressions/loss, and checks duration/rate at the end. */
    public static int failures(Snapshot first,Snapshot last,int expectedInput,int expectedOutput,boolean finalSample){
        int result=readyFailures(first,expectedInput,expectedOutput)|readyFailures(last,expectedInput,expectedOutput);
        if(first==null||last==null)return result|DURATION;
        long stable=last.elapsedMs-first.elapsedMs;
        if(first.elapsedMs<WARMUP_MS||first.elapsedMs>LAST_BASELINE_MS||last.elapsedMs>=CHECK_MS
                ||stable<0||finalSample&&stable<STABLE_MS)result|=DURATION;
        long input=delta(first.captured,last.captured), rendered=delta(first.rendered,last.rendered);
        long output=delta(metric(first,21),metric(last,21)), nativeInput=delta(metric(first,27),metric(last,27));
        if(input<0||rendered<0||output<0||nativeInput<0)result|=COUNTER_RESET;
        if(finalSample){
            if(!rateMatches(input,stable)||!rateMatches(nativeInput,stable))result|=INPUT_RATE;
            if(!rateMatches(output,stable)||!rateMatches(rendered,stable))result|=OUTPUT_RATE;
            long nativeSpan=delta(metric(first,25),metric(last,25));
            // Platform snapshots refresh at 200 ms, separately from fast counters.
            if(nativeSpan<STABLE_MS||Math.abs(nativeSpan-stable)>500)result|=STALE;
        }
        if(delta(first.missing,last.missing)!=0)result|=JAVA_MISSING;
        if(delta(first.dropped,last.dropped)!=0)result|=JAVA_DROP;
        if(delta(first.rebuffers,last.rebuffers)!=0)result|=REBUFFER;
        if(delta(metric(first,11),metric(last,11))!=0)result|=OUTPUT_MISSING;
        // Native input[10] is unsupported. Its placeholder zero is deliberately never a gate.
        return result;
    }
    public static int finalResult(int requested,boolean cancelled,boolean stopped,int failures){
        if(cancelled)return CANCELLED;
        if(!stopped)return ENGINE_FAILED;
        return requested==PASS&&failures!=0?GATE_FAILED:requested;
    }
    private static void put(Map<String,Long> values,String suffix,long value){values.put(PREFIX+suffix,value);}
    public static Map<String,Long> receipt(int result,int cancelReason,int failures,Snapshot first,Snapshot last,
            int expectedInput,int expectedOutput,boolean beforeStop,boolean stopped,long finishedAtSeconds){
        Map<String,Long> values=new LinkedHashMap<>();for(String key:KEYS)values.put(key,-1L);
        put(values,"result",result);put(values,"valid",result==PASS?1:0);put(values,"quiet",1);
        put(values,"finished_at_s",finishedAtSeconds);put(values,"gate_failures",failures);put(values,"cancel_reason",cancelReason);
        put(values,"expected_input_route",expectedInput);put(values,"expected_output_route",expectedOutput);
        put(values,"input_drop_available",0);put(values,"input_dropped_frames",-1);put(values,"java_drop_available",last!=null&&last.backend==2?1:0);
        put(values,"snapshot_before_stop",beforeStop?1:0);put(values,"stop_completed",stopped?1:0);put(values,"schema_version",1);
        if(last!=null){
            put(values,"elapsed_ms",last.elapsedMs);put(values,"backend",last.backend);
            put(values,"actual_input_route",last.inputRoute);put(values,"actual_output_route",last.outputRoute);
            put(values,"input_rate",last.inputRate);put(values,"output_rate",last.outputRate);put(values,"output_peak",last.outputPeak);
            put(values,"input_fault",last.inputFault);put(values,"safety_reason",last.safetyReason);put(values,"native_error",metric(last,14));
            put(values,"target_frames",last.target);put(values,"queue_frames",last.queue);put(values,"queue_peak_frames",last.queuePeak);
            put(values,"output_queue_frames",last.outputQueue);put(values,"dsp_max_us",last.dspNanos/1000);
            put(values,"raw_input_rate",metric(last,38));put(values,"raw_output_rate",metric(last,37));
            put(values,"captured_frames",last.captured);put(values,"rendered_frames",last.rendered);
            put(values,"java_missing_frames",last.missing);put(values,"java_dropped_frames",last.backend==2?last.dropped:-1);put(values,"rebuffer_count",last.rebuffers);
            put(values,"input_read_p95_us",last.readP95Us);put(values,"rate_ppm",last.ratePpm);
            put(values,"native_final_snapshot_ms",metric(last,25));put(values,"input_monitor_ready",last.inputReady?1:0);
            put(values,"input_peak",last.inputPeak);put(values,"output_type",last.outputType);
            put(values,"duplex_attempted",last.duplexAttempted?1:0);put(values,"duplex_open_code",last.duplexOpenCode);
            put(values,"duplex_start_code",last.duplexStartCode);put(values,"engine_end_code",last.engineEndCode);
            for(int i=0;i<NATIVE_COUNT;i++)values.put(KEYS[SUMMARY.length+i],metric(last,i));
        }
        if(first!=null&&last!=null){
            put(values,"steady_ms",last.elapsedMs-first.elapsedMs);put(values,"baseline_elapsed_ms",first.elapsedMs);
            put(values,"input_delta_frames",delta(first.captured,last.captured));
            put(values,"output_delta_frames",delta(metric(first,21),metric(last,21)));
            put(values,"render_delta_frames",delta(first.rendered,last.rendered));
            put(values,"native_input_delta_frames",delta(metric(first,27),metric(last,27)));
            put(values,"java_missing_delta_frames",delta(first.missing,last.missing));
            put(values,"java_drop_delta_frames",first.backend==2&&last.backend==2?delta(first.dropped,last.dropped):-1);
            put(values,"rebuffer_delta",delta(first.rebuffers,last.rebuffers));
            put(values,"native_output_missing_delta_frames",delta(metric(first,11),metric(last,11)));
            put(values,"native_baseline_snapshot_ms",metric(first,25));
        }
        return values;
    }
    /** Writes only generated zeros: 18 s, stereo, PCM16, 48 kHz; never accepts microphone data. */
    public static void writeSilentWave(File file,long deadlineNanos,BooleanSupplier cancelled)throws IOException{
        final int bytes=18*RATE*4;
        try(OutputStream out=new BufferedOutputStream(new FileOutputStream(file))){
            checkPreparation(deadlineNanos,cancelled);
            out.write(new byte[]{'R','I','F','F'});little(out,bytes+36,4);out.write(new byte[]{'W','A','V','E','f','m','t',' '});
            little(out,16,4);little(out,1,2);little(out,2,2);little(out,RATE,4);little(out,RATE*4,4);
            little(out,4,2);little(out,16,2);out.write(new byte[]{'d','a','t','a'});little(out,bytes,4);
            byte[] zeros=new byte[8192];
            for(int left=bytes;left>0;){checkPreparation(deadlineNanos,cancelled);int count=Math.min(left,zeros.length);out.write(zeros,0,count);left-=count;}
        }
        checkPreparation(deadlineNanos,cancelled);
    }
    private static void little(OutputStream out,int value,int bytes)throws IOException{for(int i=0;i<bytes;i++)out.write(value>>>(i*8)&255);}
    private static void checkPreparation(long deadline,BooleanSupplier cancelled)throws IOException{
        if(cancelled.getAsBoolean()||System.nanoTime()>=deadline)throw new IOException("check_preparation_cancelled");
    }
}
