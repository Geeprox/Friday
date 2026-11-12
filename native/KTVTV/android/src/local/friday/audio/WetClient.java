package local.friday.audio;

import android.content.*;
import android.os.*;
import org.json.*;

/** Main-process Binder adapter; never loads NativeWet or carries PCM.
 * Binder death reports a wet-only failure; there is no dry stop callback/API.
 */
public final class WetClient {
    public interface Listener {void changed(String receipt);}
    private final Context context;private final Listener listener;private Messenger remote;private boolean bound;
    private long lease,confirmationNonce,requestSequence;private int input,output;private String profile="dry";private float mix,gain;private boolean muted=true,bypassed=true;
    private final Messenger replies;
    public WetClient(Context context,Listener listener){this.context=context.getApplicationContext();this.listener=listener;
        replies=new Messenger(new Handler(Looper.getMainLooper()){@Override public void handleMessage(Message m){if(m.what==WetService.REPLY&&m.sendingUid==android.os.Process.myUid()){
            String raw=m.getData().getString("receipt","{}");try{confirmationNonce=new JSONObject(raw).optLong("arm_nonce");}catch(JSONException ignored){}listener.changed(raw);}}});
    }
    private final ServiceConnection connection=new ServiceConnection(){
        public void onServiceConnected(ComponentName name,IBinder binder){remote=new Messenger(binder);send(WetService.READ,new Bundle());}
        public void onServiceDisconnected(ComponentName name){remote=null;notifyFailure("wet_process_disconnected");}
        @Override public void onBindingDied(ComponentName name){remote=null;notifyFailure("wet_binding_died");}
        @Override public void onNullBinding(ComponentName name){remote=null;notifyFailure("wet_null_binding");}
    };
    public void bind(){if(bound)return;try{bound=context.bindService(new Intent(context,WetService.class),connection,Context.BIND_AUTO_CREATE);if(!bound)notifyFailure("wet_bind_unavailable");}catch(RuntimeException failure){bound=false;remote=null;notifyFailure("wet_bind_failed");}}
    public boolean connected(){return remote!=null;}
    public long lastRequestId(){return requestSequence;}
    public boolean start(long lease,int input,int output,String profile,float wetMix,float monitorGain,boolean micMuted){
        if(WetProfiles.id(profile)<0||!connected())return false;
        WetProfiles.permille(wetMix);WetProfiles.permille(monitorGain);
        this.lease=lease;this.input=input;this.output=output;this.profile=profile;mix=wetMix;gain=monitorGain;muted=micMuted;bypassed=false;confirmationNonce=0;
        Bundle b=settings();b.putBoolean("explicit_start",true);b.putBoolean("dry_active",true);return send(WetService.START,b);
    }
    public void configure(String profile,float wetMix,float monitorGain,boolean micMuted,boolean bypass){
        if(WetProfiles.id(profile)<0)throw new IllegalArgumentException("unknown_wet_profile");WetProfiles.permille(wetMix);WetProfiles.permille(monitorGain);
        this.profile=profile;mix=wetMix;gain=monitorGain;muted=micMuted;bypassed=bypass;send(WetService.CONFIGURE,settings());
    }
    public void heartbeat(long lease,int input,int output,boolean dryActive,float monitorGain,boolean micMuted){
        this.gain=monitorGain;this.muted=micMuted;
        Bundle b=new Bundle();b.putLong("lease",lease);b.putInt("input",input);b.putInt("output",output);b.putBoolean("dry_active",dryActive);b.putInt("gain",WetProfiles.permille(monitorGain));b.putBoolean("muted",micMuted);b.putLong("arm_nonce",confirmationNonce);b.putLong("sent_ms",SystemClock.elapsedRealtime());send(WetService.HEARTBEAT,b);
    }
    public void bypass(){bypassed=true;send(WetService.BYPASS,new Bundle());}
    public void stop(){bypassed=true;send(WetService.STOP,new Bundle());}
    public void close(){stop();if(bound){context.unbindService(connection);bound=false;}remote=null;}
    private Bundle settings(){Bundle b=new Bundle();b.putLong("lease",lease);b.putInt("input",input);b.putInt("output",output);b.putInt("profile",WetProfiles.id(profile));b.putInt("wet",WetProfiles.permille(mix));b.putInt("gain",WetProfiles.permille(gain));b.putBoolean("muted",muted);b.putBoolean("bypass",bypassed);return b;}
    private boolean send(int command,Bundle data){Messenger current=remote;if(current==null)return false;try{data.putLong("request_id",++requestSequence);Message m=Message.obtain(null,command);m.replyTo=replies;m.setData(data);current.send(m);return true;}catch(RemoteException e){remote=null;notifyFailure("wet_binder_send_failed");return false;}}
    private void notifyFailure(String reason){try{listener.changed(new JSONObject().put("component","friday-voice-0.56").put("active",false).put("stage","WET_PROCESS_LOST").put("reason",reason).put("dry_stop_requests",0).put("native_ready",false).put("native_resource_release_observed",false).toString());}catch(JSONException ignored){}}
}
