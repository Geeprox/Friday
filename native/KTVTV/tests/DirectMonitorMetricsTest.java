import local.friday.ktv.DirectMonitorActivity;
import local.friday.ktv.NativeVoiceMonitor;
import local.friday.ktv.NativeDuplexIO;
import local.friday.ktv.ProductionAudioCheckActivity;
import java.lang.reflect.Constructor;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Host JVM with the SDK stub jar only: validates the JNI/report contract, never an Activity instance. */
public final class DirectMonitorMetricsTest {
    private static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    private static void names(String[] actual,String suffixes){
        String[] expected=suffixes.split(" ");check(actual.length==expected.length,"metric count changed");
        for(int i=0;i<expected.length;i++)check(actual[i].equals("direct_monitor_"+expected[i]),"metric index "+i+" changed: "+actual[i]);
    }
    private static void zeroSnapshot(long[] snapshot){
        check(snapshot.length==NativeVoiceMonitor.METRIC_COUNT,"fallback snapshot length differs from JNI schema");
        for(long value:snapshot)check(value==0,"fallback retained metrics from an earlier run");
    }
    private static void collect(Set<String> keys,String source,String regex){
        Matcher matches=Pattern.compile(regex).matcher(source);while(matches.find())keys.add(matches.group(1));
    }
    private static int largestReport(Path root)throws Exception{
        String source=new String(Files.readAllBytes(root.resolve("native/KTVTV/android/src/local/friday/ktv/MainActivity.java")),StandardCharsets.UTF_8);
        int from=source.indexOf("private JSONObject telemetry(){"),to=source.indexOf("private void endParty()",from);
        check(from>=0&&to>from,"telemetry method missing");source=source.substring(from,to);
        Set<String> keys=new LinkedHashSet<>();
        collect(keys,source,"\\.put\\(\"([^\"]+)\"");
        for(String array:new String[]{"ioKeys","nativeKeys"}){
            Matcher declaration=Pattern.compile("String\\[\\] "+array+"=\\{([^}]+)\\}").matcher(source);
            check(declaration.find(),"native telemetry keys missing");collect(keys,declaration.group(1),"\"([^\"]+)\"");
        }
        keys.addAll(Arrays.asList(DirectMonitorActivity.METRICS));keys.addAll(Arrays.asList(DirectMonitorActivity.EXTRA));keys.addAll(Arrays.asList(NativeDuplexIO.METRICS));keys.addAll(Arrays.asList(ProductionAudioCheckActivity.METRICS));
        keys.removeAll(Arrays.asList("id","type","channels","vendor","product","interfaces","inputs","usb"));
        // Every scalar uses the service's largest accepted general value, even
        // where the actual field is a boolean or has a smaller specific bound.
        StringBuilder json=new StringBuilder("{");
        for(String key:keys){check(key.matches("[a-z0-9_]+"),"unexpected telemetry key encoding");json.append('"').append(key).append("\":1000000000000,");}
        json.append("\"inputs\":[");devices(json,"id","type","channels");json.append("],\"usb\":[");devices(json,"vendor","product","interfaces");json.append("]}");
        return json.toString().getBytes(StandardCharsets.UTF_8).length;
    }
    private static void devices(StringBuilder json,String first,String second,String list){
        for(int i=0;i<16;i++){
            if(i!=0)json.append(',');json.append("{\"").append(first).append("\":65535,\"").append(second).append("\":65535,\"").append(list).append("\":[");
            for(int j=0;j<16;j++){if(j!=0)json.append(',');json.append("65535");}json.append("]}");
        }
    }
    public static void main(String[] args)throws Exception{
        check(android.os.Build.VERSION.SDK_INT==0,"this test requires the host SDK stub jar; never run on a device");
        names(DirectMonitorActivity.METRICS,
            "input_frames output_frames voice_frames input_dropped trimmed missing peak native_error output_performance input_performance "+
            "output_buffer input_buffer error_stream quiet gap_max_us callback_count expired input_peak output_only mode "+
            "output_api_actual input_api_actual output_mmap input_mmap output_sharing input_sharing output_state input_state output_route input_route "+
            "output_capacity input_capacity output_burst input_burst output_frames_written output_frames_read input_frames_written input_frames_read "+
            "output_timestamp_available output_timestamp_error output_timestamp_position output_timestamp_age_us output_pending_frames "+
            "input_timestamp_available input_timestamp_error input_timestamp_position input_timestamp_age_us input_pending_frames snapshot_elapsed_ms snapshot_final last_callback_age_us "+
            "mmap_policy_available mmap_policy_before mmap_policy_set_error mmap_policy_restored output_pending_available input_pending_available output_timestamp_future input_timestamp_future "+
            "output_sample_rate_actual input_sample_rate_actual "+
            "steady_start_ms startup_discarded_frames steady_input_frames steady_output_frames steady_missing_frames steady_gap_max_us steady_short_read_callbacks "+
            "input_observed_samples input_full_scale_samples input_full_scale_run_max_frames audible_output_frames output_limited_frames output_limited_run_max_frames "+
            "audible_input_mono_abs_sum output_abs_sum steady_poll_count output_pending_valid_count output_pending_min_us output_pending_max_us output_pending_last_us "+
            "input_pending_valid_count input_pending_min_us input_pending_max_us input_pending_last_us output_counter_min_us output_counter_max_us input_counter_min_us input_counter_max_us "+
            "output_xrun_available output_xrun_delta input_xrun_available input_xrun_delta clock_span_ms input_produced_frames_delta output_presented_frames_delta legacy_gain_would_limit_frames");
        names(DirectMonitorActivity.EXTRA,"result finished_at output_type elapsed_ms output_api drop_counts_available baseline_input_frames baseline_output_frames baseline_voice_frames baseline_missing baseline_elapsed_ms gate_failures");
        check(DirectMonitorActivity.METRICS.length==97&&NativeVoiceMonitor.METRIC_COUNT==97,"JNI metrics must have 97 entries");
        Set<String> unique=new LinkedHashSet<>();unique.addAll(Arrays.asList(DirectMonitorActivity.METRICS));unique.addAll(Arrays.asList(DirectMonitorActivity.EXTRA));
        check(unique.size()==109,"duplicate metric would overwrite another field");
        long[] first=NativeVoiceMonitor.openMetrics();zeroSnapshot(first);first[96]=123;zeroSnapshot(NativeVoiceMonitor.openMetrics());
        check(NativeVoiceMonitor.openCode()==9001,"host fallback tried to call JNI");
        // Construct a zero handle locally; no native open or Android lifecycle runs.
        Constructor<NativeVoiceMonitor> constructor=NativeVoiceMonitor.class.getDeclaredConstructor(long.class);constructor.setAccessible(true);
        NativeVoiceMonitor closed=constructor.newInstance(0L);zeroSnapshot(closed.monitorMetrics());closed.close();closed.close();zeroSnapshot(closed.monitorMetrics());
        int bytes=largestReport(args.length==0?Paths.get(""):Paths.get(args[0]));
        check(bytes<=24576,"complete bounded diagnostics exceeds 24 KiB: "+bytes);
        System.out.println("97 native metrics and 12 extras keep their indices; unavailable/closed snapshots reset; complete bounded report "+bytes+"/24576 bytes; no device or native audio opened");
    }
}
