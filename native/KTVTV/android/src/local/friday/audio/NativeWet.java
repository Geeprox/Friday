package local.friday.audio;

import org.json.*;

/** Independent native wet-only engine. Use only inside the separate :wet process.
 * All methods belong on a control worker; finish may join the input reader.
 * metrics refreshes the public output route gate; call it at least every 250 ms.
 * No PCM array crosses JNI. Never load this class into the dry-lease owner process.
 */
public final class NativeWet {
    public static long[] cacheContract(){return cacheContractNative();}
    private static native long[] cacheContractNative();
    static {System.loadLibrary("friday_wet");}
    private long handle;
    private static final String[] KEYS={"input_open_result","input_start_result","input_stop_result","input_close_result","input_device_id","sample_rate","channels","format","sharing_mode","hardware_rate","hardware_channels","hardware_format","input_burst_frames","input_capacity_frames","input_buffer_frames","input_frames","input_nonzero_samples","input_peak_pcm16","input_zero_reads","input_read_error","input_last_ns","input_read_max_ns","input_frames_written","input_frames_read","input_xruns","input_timestamp_result","input_timestamp_position","input_timestamp_ns","input_timestamp_sample_ns","output_open_result","output_start_result","output_stage","output_performance","output_expected_device_id","output_actual_device_id","output_route_sample_ns","output_callbacks","output_last_callback_ns","output_callback_max_ns","output_frames","output_nonzero_samples","output_peak_pcm16","output_clipped_samples","queue_frames","queue_max_frames","producer_dropped_frames","consumer_dropped_frames","missing_input_frames","stale_input_frames","last_dequeue_age_ns","dsp_clear_count","profile_id","wet_permille","monitor_gain_permille","mic_muted","bypass","command_epoch","applied_epoch","fault","running","input_closed","output_closed","created_ns","started_ns","ended_ns","profile_transition_frames","gain_ramp_frames","rendered_wet_samples","queue_generation","feedback_frames","feedback_sequence","feedback_level","feedback_hz","feedback_peak","feedback_clipped","feedback_ready","feedback_latched","input_discarded_frames","input_backlog_frames","cache_server_pid","cache_server_uid","cache_generation","cache_capabilities","cache_sequence","inline_voice_mode","inline_flags","inline_callbacks","inline_frames","inline_peak_pcm16"};
    public static NativeWet open(int inputDeviceId,int outputDeviceId,long lease){return new NativeWet(inputDeviceId,outputDeviceId,lease);}
    private NativeWet(int input,int output,long lease){handle=openNative(input,output,lease);if(handle==0)throw new IllegalStateException("wet_native_allocation_failed");}
    public synchronized boolean valid(){return handle!=0&&validNative(handle);}
    public synchronized int start(){return handle==0?-1:startNative(handle);}
    public synchronized boolean configure(String profile,float wetMix,float monitorGain,boolean micMuted,boolean bypass){
        int id=WetProfiles.id(profile);if(id<0)throw new IllegalArgumentException("unknown_wet_profile");
        return handle!=0&&configureNative(handle,id,WetProfiles.permille(wetMix),WetProfiles.permille(monitorGain),micMuted,bypass);
    }
    public synchronized void bypass(){if(handle!=0)bypassNative(handle);}
    public synchronized void finish(){if(handle!=0)finishNative(handle);}
    public synchronized void dispose(){if(handle!=0){finishNative(handle);disposeNative(handle);handle=0;}}
    public synchronized JSONObject metrics(){
        JSONObject out=new JSONObject();if(handle==0)return out;
        long[] values=metricsNative(handle);
        try{
            for(int i=0;i<KEYS.length;i++)out.put(KEYS[i],i<values.length?values[i]:JSONObject.NULL);
            boolean inline=out.optInt("inline_voice_mode")==1;
            out.put("metric_count",values.length).put("output_api",inline?"OWNED_NATIVE_SINGLE_STREAM":"OpenSL_ES_MEDIA_NONE").put("output_rate",48000).put("output_channels",2).put("output_pcm_bits",16).put("output_queue_buffer_count",inline?0:2).put("output_callback_frames",inline?0:240);
            out.put("dry_signal_in_output",inline).put("pcm_saved",false).put("pcm_crosses_jni",false).put("metrics_snapshot_atomic",false).put("input_api","strict_cache_unix_v1").put("cache_path_verified",(out.optInt("cache_capabilities")==31||out.optInt("cache_capabilities")==63)&&out.optLong("cache_generation")>0&&out.optInt("fault")==0&&out.optInt("running")==1).put("physical_capture_reopen_excluded",true).put("input_device_id_is_dry_reference",true).put("wet_acoustic_quality_verified",false);
            out.put("inline_output_uses_owned_dry_stream",inline&&out.optInt("cache_server_uid")==1041&&out.optInt("cache_capabilities")==63&&(out.optInt("inline_flags")&77)==77)
                .put("inline_output_io_ready",inline&&(out.optInt("inline_flags")&256)!=0&&out.optInt("fault")==0)
                .put("inline_wet_sample_counter_available",false).put("separate_wet_output_stream",!inline).put("extra_dry_output_stream",false).put("additional_dry_fifo_frames",0);
            long age=-1;
            if(out.optLong("input_timestamp_result",-1)==0){long stamp=out.optLong("input_timestamp_ns"),sample=out.optLong("input_timestamp_sample_ns"),position=out.optLong("input_timestamp_position",-1),read=out.optLong("input_frames_read",-1);
                if(stamp>0&&position>=0&&read>=0){long estimate=sample-stamp-(read-position)*1000000000L/48000;if(estimate>=0&&estimate<10000000000L)age=estimate;}}
            out.put("input_age_estimate_ns",age).put("input_age_is_acoustic_measurement",false);
        }catch(JSONException ignored){}
        return out;
    }
    private static native long openNative(int input,int output,long lease);
    private static native boolean validNative(long handle);
    private static native int startNative(long handle);
    private static native boolean configureNative(long handle,int profile,int wet,int gain,boolean muted,boolean bypass);
    private static native void bypassNative(long handle);
    private static native void finishNative(long handle);
    private static native void disposeNative(long handle);
    private static native long[] metricsNative(long handle);
}
