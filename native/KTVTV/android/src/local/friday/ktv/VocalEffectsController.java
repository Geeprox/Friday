package local.friday.ktv;

import android.content.Context;
import android.os.Process;
import android.os.SystemClock;
import local.friday.audio.WetClient;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Isolated voice observer with an inline effect or legacy wet return. Protection opens only
 * for a requested live mic with a current dry lease; it never grants dry gain.
 * The two-argument adapter retains explicit-audition authorization. */
public final class VocalEffectsController {
    public interface Listener { void changed(); }
    private final Context context;private final Listener listener;private final boolean automaticProtection;
    private boolean mixReady;private final WetMixReservation reservation=new WetMixReservation();
    private final VocalEffectsPolicy policy=new VocalEffectsPolicy();
    private WetClient client;
    private long binding,startedRequest,stopRequest,lastHeartbeat,receivedAt;
    private boolean opening,active,closing,releaseBinding,everAttempted,closed=true;
    private int configuredProfile=-1,configuredGain=-1,configuredMute=-1,reasonCode=1;
    private JSONObject receipt=new JSONObject();
    public VocalEffectsController(Context context,Listener listener){this(context,listener,false);}
    public VocalEffectsController(Context context,Listener listener,boolean protect){this.context=context.getApplicationContext();this.listener=listener;automaticProtection=protect;}
    private void bind(){if(client!=null)return;long generation=++binding;client=new WetClient(context,value->received(generation,value));try{client.bind();}catch(RuntimeException e){failed(5);}}
    /** A live requested mic may open a zero-output observer. It never grants dry gain. */
    public synchronized void mixReady(boolean value){if(mixReady!=value){mixReady=value;drive();}}
    public synchronized void resetProtection(){policy.resetFault();mixReady=false;requestStop(true);if(client==null&&!closed){closing=true;stopRequest=0;bind();}}
    public synchronized int reservation(){reservation.request(inlinePath(receipt.optJSONObject("native"))?0:VocalEffectsPolicy.WET_PERMILLE[policy.profile()]);return reservation.value();}
    public synchronized long monitorToken(){JSONObject n=receipt.optJSONObject("native");return !identityMatches()||n==null?0:n.optLong("created_ns");}
    public synchronized long monitorFrames(){JSONObject n=receipt.optJSONObject("native");return !identityMatches()||n==null?0:n.optLong("feedback_frames");}
    public synchronized long feedbackSequence(){JSONObject n=receipt.optJSONObject("native");return !identityMatches()||n==null?0:n.optLong("feedback_sequence");}
    public synchronized int feedbackLevel(){JSONObject n=receipt.optJSONObject("native");return n==null?0:n.optInt("feedback_level");}
    public synchronized int feedbackHz(){JSONObject n=receipt.optJSONObject("native");return n==null?0:n.optInt("feedback_hz");}
    public synchronized boolean protectionReady(){JSONObject n=receipt.optJSONObject("native");return automaticProtection&&identityMatches()&&n!=null&&n.optInt("feedback_ready")==1&&n.optInt("feedback_latched")==0&&n.optInt("fault")==0&&n.optInt("running")==1&&(!inlinePath(n)||n.optBoolean("inline_output_io_ready"));}
    private int renderProfile(){return automaticProtection&&!mixReady?0:policy.profile();}


    public synchronized void environment(VocalEffectsPolicy.DryLease lease,boolean allowed,boolean muted,int gain){
        if(policy.environment(lease,allowed,muted,gain)){reasonCode=allowed?7:6;if(automaticProtection&&allowed&&!muted)policy.fail();requestStop(true);}
        if(automaticProtection&&allowed&&!muted&&!closing&&!policy.authorized()&&!policy.fault()&&policy.protect())bind();
        drive();
    }
    public synchronized boolean canAudition(){return policy.canAudition()&&!closing;}
    public synchronized boolean audition(int profile){
        if(automaticProtection&&profile!=policy.profile())mixReady=false;
        if(closing||!policy.audition(profile))return false;
        reasonCode=3;everAttempted=true;releaseBinding=false;
        bind();
        drive();changed();return !policy.fault();
    }
    public synchronized void select(String value){
        int next=VocalEffectsPolicy.id(value);if(next==policy.profile())return;
        if(automaticProtection)mixReady=false;
        policy.select(next);
        if(next==0&&!automaticProtection)requestStop(false);else drive();changed();
    }
    /** Close the observer/effect client. Loss of an inline observer closes its safety gate. */
    public synchronized void suspend(int reason){policy.revoke();reasonCode=reason;requestStop(true);changed();}
    public synchronized void close(){policy.revoke();if(client!=null){WetClient prior=client;client=null;++binding;prior.close();}opening=false;closing=false;active=false;}

    private void drive(){
        if(client==null||!client.connected())return;
        if(!policy.authorized()){if(!closing&&(active||opening))requestStop(true);return;}
        if(policy.profile()==0&&!automaticProtection){if((active||opening)&&!closing)requestStop(false);return;}
        if(closing)return;
        if(policy.shouldStart(opening,active,closing)){
            VocalEffectsPolicy.DryLease lease=policy.lease();opening=true;closed=false;policy.startIssued();reasonCode=3;
            configuredProfile=renderProfile();configuredGain=policy.gain();configuredMute=0;
            boolean sent=client.start(lease.lease,lease.input,lease.output,VocalEffectsPolicy.KEYS[renderProfile()],mix(),gain(),false);
            if(client!=null&&sent)startedRequest=client.lastRequestId();else if(!policy.fault())failed(5);
        }
        if(client==null||closing||!policy.authorized())return;
        if(active||opening){
            int muted=policy.muted()?1:0;
            if(configuredProfile!=renderProfile()||configuredGain!=policy.gain()||configuredMute!=muted){
                // Send the zero gate before a muted configuration. The remote
                // callback clears tails; IPC delivery is not called instantaneous.
                if(muted!=0)client.bypass();
                if(client==null||!policy.authorized())return;
                configuredProfile=renderProfile();configuredGain=policy.gain();configuredMute=muted;
                client.configure(VocalEffectsPolicy.KEYS[renderProfile()],mix(),gain(),muted!=0,muted!=0);
            }
            long now=SystemClock.elapsedRealtime();
            if(client!=null&&now-lastHeartbeat>=250){lastHeartbeat=now;VocalEffectsPolicy.DryLease lease=policy.lease();client.heartbeat(lease.lease,lease.input,lease.output,true,gain(),policy.muted());}
        }
    }
    private float mix(){return VocalEffectsPolicy.WET_PERMILLE[renderProfile()]/1000f;}
    private float gain(){return policy.gain()/100f;}
    private void requestStop(boolean unbind){
        releaseBinding|=unbind;
        if(client==null){opening=false;active=false;closing=false;return;}
        if(!client.connected()){
            // A bind that has not delivered a service cannot have sent START.
            WetClient prior=client;client=null;++binding;prior.close();opening=false;active=false;closing=false;return;
        }
        if(closing)return;
        closing=true;client.bypass();
        if(client!=null){client.stop();if(client!=null)stopRequest=client.lastRequestId();}
    }
    private synchronized void received(long generation,String value){
        if(generation!=binding||client==null)return;
        JSONObject next;try{next=new JSONObject(value);}catch(JSONException e){failed(4);return;}
        receipt=next;receivedAt=SystemClock.elapsedRealtime();
        if("WET_PROCESS_LOST".equals(next.optString("stage"))){failed(5);return;}
        JSONObject nativeState=next.optJSONObject("native");
        boolean wasActive=active;active=next.optBoolean("active");
        closed=!active&&nativeState!=null&&nativeState.optInt("input_closed")==1&&nativeState.optInt("output_closed")==1;
        reservation.request(inlinePath(nativeState)?0:VocalEffectsPolicy.WET_PERMILLE[policy.profile()]);
        if(nativeState!=null)reservation.observe(inlinePath(nativeState)?0:nativeState.optInt("wet_permille"),nativeState.optLong("command_epoch"),nativeState.optLong("applied_epoch"),nativeState.optInt("profile_transition_frames"),nativeState.optInt("gain_ramp_frames"),nativeState.optLong("output_callbacks"),closed);
        if(closing){
            if(closed&&next.optLong("request_id")>=stopRequest){
                closing=false;opening=false;configuredProfile=-1;
                if(releaseBinding){WetClient prior=client;client=null;++binding;releaseBinding=false;prior.close();}
            }
        }else if(active){
            opening=false;
            if(nativeState!=null&&nativeState.optInt("fault")!=0){failed(4);return;}
            if(matchingNative()){policy.observeReady(policy.profile(),true);reasonCode=0;}
        }else if(opening&&next.optLong("request_id")>=startedRequest&&!next.optString("reason").isEmpty()){
            failed(4);return;
        }else if(!opening&&wasActive&&policy.authorized()&&policy.profile()>0){
            // A stopped/failed native engine is never restarted by a poll.
            failed(4);return;
        }
        drive();changed();
    }
    private void failed(int reason){
        policy.fail();reasonCode=reason;opening=false;active=false;closing=false;
        if(client!=null){WetClient prior=client;client=null;++binding;prior.close();}changed();
    }
    private static boolean inlinePath(JSONObject n){return n!=null&&n.optInt("inline_voice_mode")==1&&n.optInt("cache_server_uid")==1041&&n.optInt("cache_capabilities")==63&&n.optLong("cache_generation")>0;}
    private static boolean outputMatches(JSONObject n,int output){return inlinePath(n)?n.optBoolean("inline_output_uses_owned_dry_stream")&&n.optBoolean("inline_output_io_ready")&&(n.optInt("inline_flags")&333)==333:n.optInt("output_actual_device_id")==output;}
    private boolean identityMatches(){
        if(!policy.authorized()||closing||!active||SystemClock.elapsedRealtime()-receivedAt>500)return false;
        VocalEffectsPolicy.DryLease lease=policy.lease();JSONObject n=receipt.optJSONObject("native");
        return n!=null&&receipt.optLong("lease")==lease.lease&&receipt.optInt("input_device")==lease.input&&receipt.optInt("output_device")==lease.output
            &&receipt.optInt("pid")>0&&receipt.optInt("pid")!=Process.myPid()&&receipt.optInt("uid",-1)==Process.myUid()
            &&outputMatches(n,lease.output);
    }
    private boolean matchingNative(){
        if(!identityMatches()||!receipt.optBoolean("native_ready"))return false;
        VocalEffectsPolicy.DryLease lease=policy.lease();JSONObject n=receipt.optJSONObject("native");
        return n!=null&&receipt.optLong("lease")==lease.lease&&receipt.optInt("input_device")==lease.input&&receipt.optInt("output_device")==lease.output
            &&receipt.optInt("pid")>0&&receipt.optInt("pid")!=Process.myPid()&&receipt.optInt("uid",-1)==Process.myUid()
            &&(renderProfile()==0?monitorMatches(n):VocalEffectsPolicy.nativeMatches(renderProfile(),policy.gain(),policy.muted(),n.optInt("profile_id",-1),n.optInt("wet_permille",-1),n.optInt("monitor_gain_permille",-1),n.optInt("mic_muted",-1),n.optInt("bypass",-1),n.optLong("command_epoch",-1),n.optLong("applied_epoch",-2)))
            &&outputMatches(n,lease.output);
    }
    private boolean monitorMatches(JSONObject n){return n.optInt("profile_id",-1)==0&&n.optInt("wet_permille",-1)==0&&n.optInt("monitor_gain_permille",-1)==policy.gain()*10
        &&n.optInt("mic_muted",-1)==(policy.muted()?1:0)&&n.optInt("bypass",-1)==(policy.muted()?1:0)&&n.optLong("command_epoch")>0&&n.optLong("command_epoch")==n.optLong("applied_epoch");}
    private void changed(){if(listener!=null)listener.changed();}
    public synchronized boolean ready(){return renderProfile()==policy.profile()&&matchingNative();}
    public synchronized boolean available(){return automaticProtection?protectionReady():policy.available();}
    public synchronized boolean fault(){return policy.fault();}
    public synchronized boolean released(){return closed&&!active&&!opening&&!closing;}
    public synchronized int requestedProfile(){return policy.profile();}
    public synchronized JSONArray capabilities(){JSONArray result=new JSONArray();for(int i=0;i<(available()?VocalEffectsPolicy.KEYS.length:1);i++)result.put(VocalEffectsPolicy.KEYS[i]);return result;}
    public synchronized String reportProfile(){return VocalEffectsPolicy.KEYS[policy.profile()];}
    public synchronized String reportState(){if(policy.profile()==0&&automaticProtection&&protectionReady()&&matchingNative())return "applied";if(policy.profile()==0&&everAttempted&&closed&&!closing)return "applied";if(policy.fault())return "failed";if(policy.profile()==0)return closing?"pending":"unavailable";if(ready())return "applied";return policy.authorized()?"pending":"unavailable";}
    public synchronized String statusText(){
        if(automaticProtection&&feedbackLevel()>=3)return "人声保护已触发，请查看麦克风静音状态。";
        if(policy.fault()){JSONObject n=receipt.optJSONObject("native");return n!=null&&n.optInt("fault")==7?"混响输入规格不匹配，暂不可用。":"混响启动失败，暂不可用。";}
        if(closing)return "正在关闭混响…";
        if(policy.profile()==0&&closed&&everAttempted)return "混响已关闭";
        if(automaticProtection&&policy.profile()==0&&protectionReady())return "自动防啸叫监测中 · 混响已关闭";
        if(ready())return policy.muted()?"混响已静音并清除尾音":"正在试听 · "+VocalEffectsPolicy.LABELS[policy.profile()];
        if(policy.authorized())return automaticProtection?"正在准备语音保护…":"混响准备中…";
        return "开启麦克风并播放歌曲后，可在这里开始试听。";
    }
    /** Fixed scalars only; no audio, free text, URL, token or arbitrary JSON. */
    public synchronized void diagnostics(JSONObject out)throws JSONException{
        JSONObject n=receipt.optJSONObject("native");if(n==null)n=new JSONObject();
        boolean ready=ready(),output=active&&receipt.optBoolean("wet_output_enabled")&&SystemClock.elapsedRealtime()-receivedAt<=1200;
        long age=n.optLong("input_age_estimate_ns",-1);
        boolean timed=n.optInt("input_timestamp_result",-1)==0&&age>=0;
        out.put("wet_ready",ready).put("wet_active",active).put("wet_fault",policy.fault()).put("wet_explicit_trial",everAttempted)
            .put("wet_cache_verified",identityMatches()&&n.optBoolean("cache_path_verified")).put("wet_capture_reopen_excluded",n.optBoolean("physical_capture_reopen_excluded")).put("wet_audition_verified",false)
            .put("wet_process_isolated",receipt.optInt("pid")>0&&receipt.optInt("pid")!=Process.myPid()&&receipt.optInt("uid",-1)==Process.myUid())
            .put("wet_timestamp_available",timed).put("wet_input_timestamp_age_us",timed?age/1000:0)
            .put("wet_muted",n.optInt("mic_muted",-1)==1).put("wet_bypass",n.optInt("bypass",-1)==1).put("wet_rendered",ready&&n.optLong("rendered_wet_samples")>0)
            .put("wet_requested_profile",policy.profile()).put("wet_applied_profile",ready?policy.profile():0).put("wet_pid",Math.max(0,receipt.optLong("pid")))
            .put("wet_starts",Math.max(0,receipt.optLong("starts"))).put("wet_closes",Math.max(0,receipt.optLong("closes"))).put("wet_reason_code",reasonCode)
            .put("software_voice_processing_active",inlinePath(n)&&active||output).put("software_voice_processing_wet_only",!inlinePath(n))
            .put("software_dry_voice_processing_active",inlinePath(n)&&n.optBoolean("inline_output_uses_owned_dry_stream"))
            .put("software_voice_processing_same_stream",inlinePath(n)).put("wet_rendered_counter_available",!inlinePath(n));
        for(String key:new String[]{"input_frames","input_nonzero_samples","input_peak_pcm16","output_frames","output_nonzero_samples","output_peak_pcm16","queue_frames","queue_max_frames","producer_dropped_frames","consumer_dropped_frames","missing_input_frames","stale_input_frames","output_actual_device_id","command_epoch","applied_epoch"})out.put("wet_"+key,Math.max(0,n.optLong(key)));
        for(String key:new String[]{"feedback_frames","feedback_sequence","feedback_level","feedback_hz","feedback_peak","feedback_clipped","feedback_ready","feedback_latched","input_discarded_frames","input_backlog_frames"})out.put("voice_"+key,Math.max(0,n.optLong(key)));
        out.put("voice_protection_ready",protectionReady());
        for(String key:new String[]{"fault","input_device_id","sample_rate","channels","format","sharing_mode","hardware_rate","hardware_channels","hardware_format","output_stage"})out.put("wet_native_"+key,Math.max(0,n.optLong(key)));
        for(String key:new String[]{"input_open_result","input_start_result","output_open_result","output_start_result"})out.put("wet_native_"+key+"_error",Math.max(0,-n.optLong(key)));
        for(String key:new String[]{"cache_enable_calls","cache_disable_calls","cache_hook_error"})out.put("wet_"+key,Math.max(0,receipt.optLong(key)));
        out.put("wet_cache_hook_requested",receipt.optBoolean("cache_hook_requested"));
    }
}
