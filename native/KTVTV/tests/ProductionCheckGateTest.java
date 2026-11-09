import local.friday.ktv.ProductionCheckGate;
import local.friday.ktv.ProductionCheckGate.Snapshot;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.HashSet;
import java.util.Map;
import java.util.function.Consumer;

/** Pure aggregate/fixture tests; no Android classes, devices, microphones, or audio playback. */
public final class ProductionCheckGateTest {
    private static void check(boolean ok,String why){if(!ok)throw new AssertionError(why);}
    private static Snapshot healthy(long ms){
        Snapshot s=new Snapshot();s.elapsedMs=ms;s.running=s.silent=s.inputReady=true;
        s.backend=2;s.inputRoute=17;s.outputRoute=19;s.inputRate=s.outputRate=48000;
        s.captured=s.rendered=ms*48;s.target=1200;s.queue=720;s.queuePeak=1440;
        s.nativeMetrics=new long[ProductionCheckGate.NATIVE_COUNT];long[] n=s.nativeMetrics;
        n[2]=19;n[3]=17;n[8]=10;n[9]=12;n[12]=n[13]=-1;n[16]=n[17]=48000;
        n[21]=n[27]=ms*48;n[23]=22000;n[25]=100000+ms;n[36]=1200;n[37]=48000;n[38]=48000;
        n[47]=n[48]=n[49]=n[57]=n[60]=n[63]=1;n[61]=n[62]=4;
        n[88]=48000;n[89]=n[90]=1;
        return s;
    }
    private static void reject(Consumer<Snapshot> change,int bit,String why){
        Snapshot first=healthy(1000),last=healthy(14200);change.accept(last);
        check((ProductionCheckGate.failures(first,last,17,19,true)&bit)!=0,why);
    }
    private static void gate(){
        Snapshot first=healthy(1000),last=healthy(14200);
        check(ProductionCheckGate.failures(first,last,17,19,true)==0,"healthy production transport rejected");
        check((ProductionCheckGate.failures(first,healthy(13999),17,19,true)&ProductionCheckGate.DURATION)!=0,"short stable interval accepted");
        check((ProductionCheckGate.failures(healthy(999),last,17,19,true)&ProductionCheckGate.DURATION)!=0,"early baseline accepted");
        check((ProductionCheckGate.failures(healthy(1600),healthy(14900),17,19,true)&ProductionCheckGate.DURATION)!=0,"late baseline accepted");
        reject(s->s.elapsedMs=15000,ProductionCheckGate.DURATION,"deadline treated as permission to continue");
        reject(s->s.nativeMetrics=new long[27],ProductionCheckGate.SCHEMA,"old backend schema accepted");
        reject(s->s.nativeMetrics=java.util.Arrays.copyOf(s.nativeMetrics,64),ProductionCheckGate.SCHEMA,"old 64-field evidence accepted as current diagnostics");
        reject(s->s.nativeMetrics=java.util.Arrays.copyOf(s.nativeMetrics,88),ProductionCheckGate.SCHEMA,"old 88-field evidence accepted as strict 48 kHz diagnostics");
        reject(s->s.nativeMetrics[63]=2,ProductionCheckGate.SCHEMA,"unknown metric schema accepted");
        reject(s->s.backend=1,ProductionCheckGate.BACKEND,"legacy fallback accepted as new route");
        reject(s->s.outputRoute=0,ProductionCheckGate.ROUTE,"unknown output route accepted");
        reject(s->s.inputRoute=18,ProductionCheckGate.ROUTE,"changed input accepted");
        reject(s->s.nativeMetrics[2]=20,ProductionCheckGate.ROUTE,"cached Java route overrode actual raw route");
        reject(s->s.outputPeak=1,ProductionCheckGate.NOT_SILENT,"nonzero mixed sample accepted");
        reject(s->s.silent=false,ProductionCheckGate.NOT_SILENT,"changed gain/mute accepted");
        reject(s->s.inputFault=1,ProductionCheckGate.ERROR,"input health fault accepted");
        reject(s->s.nativeMetrics[14]=885,ProductionCheckGate.ERROR,"native error accepted");
        reject(s->s.missing=240,ProductionCheckGate.JAVA_MISSING,"new Java silence ignored");
        reject(s->s.dropped=1,ProductionCheckGate.JAVA_DROP,"new Java dropped frame ignored");
        reject(s->s.rebuffers=1,ProductionCheckGate.REBUFFER,"rebuffer ignored");
        reject(s->s.nativeMetrics[11]=1,ProductionCheckGate.OUTPUT_MISSING,"native output silence ignored");
        reject(s->s.nativeMetrics[23]=50001,ProductionCheckGate.CALLBACK_GAP,"callback stall accepted");
        reject(s->s.nativeMetrics[25]-=600,ProductionCheckGate.STALE,"stale platform evidence accepted");
        reject(s->s.nativeMetrics[25]-=300,ProductionCheckGate.STALE,"less than 13 s of platform observations accepted");
        reject(s->s.nativeMetrics[47]=0,ProductionCheckGate.CONFIGURATION,"unavailable MMAP query treated as legacy");
        reject(s->s.nativeMetrics[48]=2,ProductionCheckGate.CONFIGURATION,"MMAP capture accepted");
        reject(s->s.nativeMetrics[49]=0,ProductionCheckGate.CONFIGURATION,"unrestored policy accepted");
        reject(s->s.nativeMetrics[9]=0,ProductionCheckGate.CONFIGURATION,"unknown performance mode accepted");
        reject(s->s.nativeMetrics[38]=96000,ProductionCheckGate.CONFIGURATION,"96 kHz raw stream accepted as strict 48 kHz");
        reject(s->s.nativeMetrics[90]=0,ProductionCheckGate.CONFIGURATION,"normal production mode accepted as explicit experiment");
        reject(s->s.nativeMetrics[88]=96000,ProductionCheckGate.CONFIGURATION,"known 96 kHz HAL conversion accepted");
        reject(s->s.nativeMetrics[89]=0,ProductionCheckGate.CONFIGURATION,"unavailable flag retained a known hardware rate");
        reject(s->s.nativeMetrics[89]=2,ProductionCheckGate.CONFIGURATION,"invalid hardware availability accepted");
        Snapshot ordinary=healthy(14200);ordinary.nativeMetrics[9]=10;
        check(ProductionCheckGate.failures(first,ordinary,17,19,true)==0,"performance flag substituted for actual stable transport");
        Snapshot unknownHardware=healthy(14200);unknownHardware.nativeMetrics[88]=unknownHardware.nativeMetrics[89]=0;
        check(ProductionCheckGate.failures(first,unknownHardware,17,19,true)==0,"unknown HAL rate prevented explicit two-layer 48 kHz evidence");
        reject(s->s.queuePeak=1000,ProductionCheckGate.CONFIGURATION,"never-primed voice target accepted");
        reject(s->s.nativeMetrics[36]=2880,ProductionCheckGate.CONFIGURATION,"60 ms output FIFO accepted");
        reject(s->s.running=false,ProductionCheckGate.NOT_RUNNING,"stopped snapshot accepted");
        reject(s->s.captured=0,ProductionCheckGate.COUNTER_RESET,"counter reset accepted");
        reject(s->s.captured=Long.MAX_VALUE,ProductionCheckGate.INPUT_RATE,"overflow counter accepted");
        reject(s->s.nativeMetrics[27]-=15000,ProductionCheckGate.INPUT_RATE,"native input rate deficit accepted");
        reject(s->s.nativeMetrics[21]-=15000,ProductionCheckGate.OUTPUT_RATE,"callback rate deficit accepted");
        reject(s->s.rendered-=15000,ProductionCheckGate.OUTPUT_RATE,"Java render deficit accepted");
        for(int percent:new int[]{98,102}){
            Snapshot edge=healthy(14200);long delta=13200L*48*percent/100;
            edge.captured=edge.nativeMetrics[27]=first.captured+delta;
            edge.rendered=edge.nativeMetrics[21]=first.rendered+delta;
            check(ProductionCheckGate.failures(first,edge,17,19,true)==0,"documented rate boundary rejected");
        }
        first.missing=last.missing=240;first.dropped=last.dropped=120;first.rebuffers=last.rebuffers=1;
        first.nativeMetrics[11]=last.nativeMetrics[11]=480;
        check(ProductionCheckGate.failures(first,last,17,19,true)==0,"unchanged startup counts treated as steady loss");
        last.missing=0;check((ProductionCheckGate.failures(first,last,17,19,true)&ProductionCheckGate.JAVA_MISSING)!=0,"loss counter reset hidden");
        check(ProductionCheckGate.failures(null,null,17,19,true)!=0,"missing evidence passed");
    }
    private static void receipts(){
        check(ProductionCheckGate.finalResult(1,true,true,0)==ProductionCheckGate.CANCELLED,"late cancellation can retain pass");
        check(ProductionCheckGate.finalResult(1,false,false,0)==ProductionCheckGate.ENGINE_FAILED,"unconfirmed stop can pass");
        check(ProductionCheckGate.finalResult(1,false,true,1)==ProductionCheckGate.GATE_FAILED,"failed gate can pass");
        Map<String,Long> result=ProductionCheckGate.receipt(1,0,0,healthy(1000),healthy(14200),17,19,true,true,1000);
        check(new HashSet<>(java.util.Arrays.asList(ProductionCheckGate.KEYS)).size()==ProductionCheckGate.KEYS.length,"duplicate fixed key");
        check(result.size()==ProductionCheckGate.KEYS.length,"unlisted preference key");
        for(String key:ProductionCheckGate.KEYS)check(result.containsKey(key)&&key.startsWith("production_check_"),"unbounded receipt key");
        check(result.get("production_check_input_drop_available")==0&&result.get("production_check_input_dropped_frames")==-1,"unknown native drop reported zero");
        check(result.get("production_check_java_drop_available")==1&&result.get("production_check_java_drop_delta_frames")==0,"Java drop evidence lost");
        check(result.get("production_check_raw_input_rate")==48000&&result.get("production_check_input_rate")==48000,"strict raw/client rate lost");
        check(result.get("production_check_native_12")==-1,"unknown XRun sentinel converted to measured zero");
        check(ProductionCheckGate.NATIVE_COUNT==91&&result.containsKey("production_check_native_90"),"new diagnostics omitted from receipt");
        Snapshot fallback=healthy(14200);fallback.backend=1;
        Map<String,Long> old=ProductionCheckGate.receipt(6,0,4,healthy(1000),fallback,17,19,true,true,1000);
        check(old.get("production_check_java_drop_available")==0&&old.get("production_check_java_drop_delta_frames")==-1,"legacy combined drops claimed as Java-only evidence");
        Map<String,Long> empty=ProductionCheckGate.receipt(0,0,0,null,null,17,19,false,false,0);
        check(empty.get("production_check_valid")==0&&empty.get("production_check_native_63")==-1,"old success evidence survives reset");
        check(empty.get("production_check_native_87")==-1,"unavailable appended metric fabricated zero");
        check(empty.get("production_check_native_90")==-1,"unavailable experiment mode fabricated default");
        Snapshot failed=healthy(3);failed.duplexAttempted=true;failed.duplexOpenCode=9001;
        failed.engineEndCode=ProductionCheckGate.endCode("strict_input_unavailable");
        Map<String,Long> failure=ProductionCheckGate.receipt(5,6,0,null,failed,17,19,false,true,1000);
        check(failure.get("production_check_duplex_open_code")==9001&&failure.get("production_check_engine_end_code")==2,
            "failed startup evidence lost after worker stop");
        check(empty.get("production_check_engine_end_code")==-1&&ProductionCheckGate.endCode("private exception text")==10,
            "unobserved or unbounded engine reason retained");
    }
    private static long little(byte[] b,int at,int count){long n=0;for(int i=0;i<count;i++)n|=(long)(b[at+i]&255)<<(8*i);return n;}
    private static void wave(File directory)throws Exception{
        File file=File.createTempFile("check-fixture-",".wav",directory);
        try{
            ProductionCheckGate.writeSilentWave(file,System.nanoTime()+2000000000L,()->false);
            byte[] bytes=Files.readAllBytes(file.toPath());
            check(bytes.length==44+18*48000*4,"silent fixture length wrong");
            check(new String(bytes,0,4,"US-ASCII").equals("RIFF")&&new String(bytes,8,4,"US-ASCII").equals("WAVE"),"WAV signature invalid");
            check(little(bytes,20,2)==1&&little(bytes,22,2)==2&&little(bytes,24,4)==48000&&little(bytes,34,2)==16,"WAV format invalid");
            check(little(bytes,4,4)==bytes.length-8&&little(bytes,40,4)==bytes.length-44,"WAV sizes inconsistent");
            for(int i=44;i<bytes.length;i++)check(bytes[i]==0,"fixture contained nonzero audio");
            try{ProductionCheckGate.writeSilentWave(file,System.nanoTime()+2000000000L,()->true);throw new AssertionError("cancelled preparation wrote fixture");}catch(IOException expected){}
            try{ProductionCheckGate.writeSilentWave(file,System.nanoTime()-1,()->false);throw new AssertionError("expired preparation wrote fixture");}catch(IOException expected){}
        }finally{check(file.delete()||!file.exists(),"temporary fixture retained");}
    }
    public static void main(String[] args)throws Exception{
        if(args.length!=1)throw new IllegalArgumentException("isolated receipt directory required");
        gate();receipts();wave(new File(args[0]));
        System.out.println("Production quiet-gate and generated-silence contracts passed; no device or audio started");
    }
}
