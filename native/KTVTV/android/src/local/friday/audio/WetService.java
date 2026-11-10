package local.friday.audio;

import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.*;
import org.json.*;

/** Bound, non-exported service in :wet. Native crashes stay outside the dry owner.
 * The native input is a read-only authenticated platform cache endpoint.
 * No ordinary input constructor or global USB cache flag is used; no dry
 * START, STOP, gain or Guard call is made here.
 */
public final class WetService extends Service {
    public static final int START=1,CONFIGURE=2,HEARTBEAT=3,STOP=4,READ=5,BYPASS=6,REPLY=7;
    private HandlerThread worker;private Handler control;private Messenger incoming,owner;
    private IBinder ownerBinder;private NativeWet engine;private final WetPolicy policy=new WetPolicy();
    private String stage="OFF",reason="",nativeJson="{\"input_closed\":1,\"output_closed\":1,\"never_opened\":true}";private int profile=0,wet=0,gain=0;
    private boolean micMuted=true,bypassed=true;private long starts,closes,lastRequestId;private int lastCommand;
    private final IBinder.DeathRecipient death=()->control.post(()->{closeWet("dry_owner_died");detachOwner();});
    @Override public void onCreate(){super.onCreate();worker=new HandlerThread("friday-wet-control");worker.start();control=new Handler(worker.getLooper()){@Override public void handleMessage(Message message){receive(message);}};incoming=new Messenger(control);control.post(poll);}
    @Override public IBinder onBind(Intent intent){return incoming.getBinder();}
    @Override public boolean onUnbind(Intent intent){control.post(()->{closeWet("client_unbound");detachOwner();});return false;}
    private boolean claim(Message m){
        if(m.sendingUid!=android.os.Process.myUid()||m.replyTo==null)return false;
        IBinder binder=m.replyTo.getBinder();if(ownerBinder!=null)return ownerBinder.equals(binder);
        try{binder.linkToDeath(death,0);ownerBinder=binder;owner=m.replyTo;return true;}catch(RemoteException e){return false;}
    }
    private void detachOwner(){if(ownerBinder!=null)ownerBinder.unlinkToDeath(death,0);ownerBinder=null;owner=null;}
    private void receive(Message m){
        if(!claim(m))return;Bundle b=m.getData();lastRequestId=b.getLong("request_id");lastCommand=m.what;
        try{
            if(m.what==START)startWet(b);
            else if(m.what==STOP)closeWet("local_wet_stop");
            else if(m.what==CONFIGURE){if(b.getLong("lease")!=policy.lease()){closeWet("configuration_lease_changed");}
                else if(readSettings(b)){apply();}}
            else if(m.what==BYPASS){bypassed=true;if(engine!=null)engine.bypass();}
            else if(m.what==HEARTBEAT&&engine!=null){
                if(!policy.heartbeat(b.getLong("lease"),b.getInt("input"),b.getInt("output"),b.getBoolean("dry_active"),SystemClock.elapsedRealtime()))closeWet(policy.reason());
                else {gain=Math.max(0,Math.min(1000,b.getInt("gain")));micMuted=b.getBoolean("muted",true);
                    if(!policy.armed()&&WetProfiles.canArm(profile,micMuted,bypassed,gain))policy.confirm(b.getLong("arm_nonce"),b.getLong("sent_ms"),SystemClock.elapsedRealtime());
                    apply();if(policy.armed())stage="WET_RUNNING_STRICT_CACHE";}}
            reply();
        }catch(RuntimeException|LinkageError failure){closeWet("wet_control_failure");reply();}
    }
    private boolean readSettings(Bundle b){
        int p=b.getInt("profile",-1),w=b.getInt("wet",-1),g=b.getInt("gain",-1);
        if(!WetProfiles.valid(p)||w<0||w>1000||g<0||g>1000){reason="invalid_wet_configuration";return false;}
        profile=p;wet=w;gain=g;micMuted=b.getBoolean("muted",true);bypassed=b.getBoolean("bypass",true);return true;
    }
    private boolean devices(int input,int output){
        AudioManager audio=(AudioManager)getSystemService(AUDIO_SERVICE);boolean foundInput=false,foundOutput=false;
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_ALL)){
            if(d.getId()==input&&d.isSource()&&(d.getType()==AudioDeviceInfo.TYPE_USB_DEVICE||d.getType()==AudioDeviceInfo.TYPE_USB_HEADSET))foundInput=true;
            if(d.getId()==output&&d.isSink()&&d.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)foundOutput=true;
        }return foundInput&&foundOutput;
    }
    private void startWet(Bundle b){
        if(engine!=null){reason="wet_already_running";return;}
        if(!readSettings(b))return;
        if(micMuted||bypassed||gain==0||(profile>0&&wet==0)){reason="wet_disabled";return;}
        if(checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){reason="record_audio_permission_missing";return;}
        long lease=b.getLong("lease");int input=b.getInt("input"),output=b.getInt("output");
        if(!b.getBoolean("explicit_start")||!b.getBoolean("dry_active")||!devices(input,output)){reason="explicit_live_dry_and_devices_required";return;}
        if(!policy.start(lease,input,output,SystemClock.elapsedRealtime())){reason=policy.reason();return;}
        stage="OPENING_WET_ONLY";reason="";
        engine=NativeWet.open(input,output,lease);starts++;
        if(!engine.valid()){closeWet("native_contract_or_output_open_failed");return;}
        apply();if(engine.start()!=0){closeWet("native_start_failed");return;}
        policy.opened(SystemClock.elapsedRealtimeNanos(),SystemClock.elapsedRealtime());
        stage="WET_WAITING_CURRENT_HEARTBEAT";
    }
    private void apply(){if(engine!=null&&!engine.configure(WetProfiles.KEYS[profile],wet/1000f,gain/1000f,micMuted||!policy.armed(),bypassed||!policy.armed()))closeWet("native_configuration_failed");}
    private void closeWet(String why){
        policy.stop(why);reason=why;stage="STOPPING_WET_ONLY";
        final NativeWet current=engine;engine=null;
        if(current!=null){
            try{current.bypass();current.finish();JSONObject n=current.metrics();nativeJson=n.toString();
                if(n.optInt("input_closed")!=1||n.optInt("output_closed")!=1)reason="strict_cache_release_unverified";
            }finally{current.dispose();}
        }
        if(current!=null)closes++;
        stage="OFF";
    }
    private final Runnable poll=new Runnable(){public void run(){
        try{
            if(engine!=null){
                if(!policy.healthy(SystemClock.elapsedRealtime()))closeWet(policy.reason());
                else {JSONObject n=engine.metrics();nativeJson=n.toString();if(n.optInt("fault")!=0||n.optInt("running")!=1)closeWet("native_wet_fault_"+n.optInt("fault"));}
            }reply();
        }catch(RuntimeException|LinkageError failure){closeWet("wet_monitor_failure");reply();}
        control.postDelayed(this,100);
    }};
    private String receipt(){
        try{JSONObject nativeState=new JSONObject(nativeJson);
            boolean ready=engine!=null&&policy.armed()&&nativeState.optInt("fault")==0&&nativeState.optInt("running")==1
                &&nativeState.optLong("input_frames")>0&&nativeState.optLong("output_callbacks")>2
                &&(nativeState.optInt("inline_voice_mode")==1?nativeState.optBoolean("inline_output_uses_owned_dry_stream")&&nativeState.optBoolean("inline_output_io_ready"):nativeState.optInt("output_actual_device_id")==policy.output())
                &&nativeState.optLong("applied_epoch")==nativeState.optLong("command_epoch")
                &&nativeState.optInt("profile_id")==profile&&nativeState.optInt("wet_permille")==wet&&nativeState.optInt("monitor_gain_permille")==gain
                &&nativeState.optInt("mic_muted")== (micMuted?1:0)&&nativeState.optInt("bypass")== (bypassed?1:0)
                &&nativeState.optInt("profile_transition_frames")==0&&nativeState.optInt("gain_ramp_frames")==0;
            return new JSONObject().put("schema",1).put("component","friday-voice-0.60").put("pid",android.os.Process.myPid()).put("uid",android.os.Process.myUid())
            .put("stage",stage).put("reason",reason).put("active",engine!=null).put("lease",policy.lease()).put("input_device",policy.input()).put("output_device",policy.output())
            .put("request_id",lastRequestId).put("command",lastCommand)
            .put("awaiting_start_confirmation",engine!=null&&!policy.armed()).put("arm_nonce",policy.nonce()).put("opened_elapsed_ms",policy.openedMs())
            .put("profile",WetProfiles.KEYS[profile]).put("wet_mix",wet/1000.0).put("monitor_gain_reference",gain/1000.0).put("monitor_gain_acoustically_calibrated",false).put("mic_muted",micMuted).put("bypass",bypassed)
            .put("starts",starts).put("closes",closes).put("native",nativeState).put("native_ready",ready).put("wet_output_enabled",ready&&!micMuted&&!bypassed&&gain>0&&wet>0)
            .put("dsp_has_rendered_wet",nativeState.optLong("output_nonzero_samples")>0).put("rendered_wet_samples",nativeState.optLong("rendered_wet_samples"))
            .put("cache_enable_calls",0).put("cache_disable_calls",0).put("cache_hook_error",0).put("cache_hook_requested",false)
            .put("cache_hook_readback_verified",false).put("cache_hook_previous_state_known",false).put("cache_hook_native_crash_cleanup_guaranteed",false).put("legacy_cache_hook_used",false).put("cache_transport_closes_on_crash",true)
            .put("vendor_setters",0).put("dry_stop_requests",0).put("dry_audio_copied_to_output",false)
            .put("automatic_start",true).put("requires_requested_live_mic",true).put("strict_bridge_required",true)
            .put("cache_path_verified",nativeState.optBoolean("cache_path_verified")).put("physical_capture_reopen_excluded",true).put("wet_audition_verified",false).toString();}
        catch(JSONException e){return "{\"receipt_error\":true}";}
    }
    private void reply(){if(owner==null)return;try{Message m=Message.obtain(null,REPLY);Bundle b=new Bundle();b.putString("receipt",receipt());m.setData(b);owner.send(m);}catch(RemoteException e){closeWet("dry_owner_unreachable");detachOwner();}}
    @Override public void onDestroy(){control.removeCallbacks(poll);control.post(()->{closeWet("wet_service_destroyed");detachOwner();worker.quitSafely();});super.onDestroy();}
}
