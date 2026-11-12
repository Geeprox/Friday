package local.friday.ktv;

import android.Manifest;
import android.app.Activity;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.AudioManager;
import android.os.*;
import java.lang.ref.WeakReference;
import org.json.JSONObject;

/** Main-process owner of a signature-protected, independently stopped microphone lease. */
public final class VendorMicClient implements AutoCloseable {
    public interface Listener {void onChanged(JSONObject state);}
    private static final String PACKAGE="local.friday.ktv.audio",PERMISSION=PACKAGE+".CONTROL";
    private static final int READ=1,START=3,LOW=4,STOP=5,REPLY=6,MUTE=7,PROTECTION=8,PROTECTION_RESET=9;
    private static final int READY=2,STARTING=3,ZERO=4,ACTIVE=5,STOPPING=6,RECOVERY=8;
    private final Context app;
    private final WeakReference<Activity> activity;
    private final Handler ui=new Handler(Looper.getMainLooper());
    private final DesiredState intent=new DesiredState();
    private Listener listener;
    private Messenger service;
    private boolean bound,startPending,hardStopPending,notifyQueued;
    private int guardUid=-1,pendingGain=Integer.MIN_VALUE;
    private long pendingGainCalls,initialGainLease,requiredCancelEpoch,resetLease,resetCounter;private boolean protectionResetPending;
    private volatile long receiptAtMs;
    private String error="";
    private volatile JSONObject raw=new JSONObject(),published=new JSONObject();

    /** No audio or service start occurs in the constructor or availability check. */
    public VendorMicClient(Activity activity,Listener listener){this.app=activity.getApplicationContext();this.activity=new WeakReference<>(activity);this.listener=listener;publish();}
    public static boolean available(Context context){
        if(Build.VERSION.SDK_INT<34||!"mtktv".equals(Build.HARDWARE))return false;
        try{
            AudioManager.class.getMethod("getParameters",String.class);AudioManager.class.getMethod("setParameters",String.class);
            PackageManager pm=context.getPackageManager();android.content.pm.ApplicationInfo target=pm.getApplicationInfo(PACKAGE,0);
            ServiceInfo info=pm.getServiceInfo(new ComponentName(PACKAGE,PACKAGE+".GuardService"),PackageManager.MATCH_DISABLED_COMPONENTS);
            // Automatic protection requires the signed 0.5 companion. Older guards lack
            // the fail-closed heartbeat and attenuation protocol.
            return pm.getPackageInfo(PACKAGE,0).versionCode>=5&&target.enabled&&target.uid!=android.os.Process.myUid()&&info.enabled&&info.exported&&PERMISSION.equals(info.permission)
                &&pm.checkSignatures(context.getPackageName(),PACKAGE)==PackageManager.SIGNATURE_MATCH
                &&pm.getApplicationEnabledSetting(PACKAGE)<=PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                &&pm.getComponentEnabledSetting(new ComponentName(PACKAGE,PACKAGE+".GuardService"))<=PackageManager.COMPONENT_ENABLED_STATE_ENABLED;
        }catch(Exception|LinkageError denied){return false;}
    }
    public boolean permissionReady(){return available(app)&&app.getPackageManager().checkPermission(Manifest.permission.RECORD_AUDIO,PACKAGE)==PackageManager.PERMISSION_GRANTED;}
    public void openPermission(){onMain(()->{Activity owner=activity.get();if(intent.closing||!intent.foreground||owner==null||!available(app))return;try{owner.startActivity(new Intent().setComponent(new ComponentName(PACKAGE,PACKAGE+".PermissionActivity")));}catch(RuntimeException failure){error="permission_entry_unavailable";publish();}});}
    public void setForeground(boolean value){onMain(()->{if(intent.closing)return;intent.foreground(value);if(value){ensureBound();pollSoon();}else hardStop();publish();});}
    public void setEnabled(boolean enabled,int gain){onMain(()->{
        if(intent.closing)return;boolean wasDesired=intent.desired;intent.enabled(enabled,gain);
        if(!intent.desired&&(wasDesired||startPending)){startPending=false;sendMute();}
        if(intent.foreground)ensureBound();drive();pollSoon();publish();
    });}
    public void protection(boolean ready,long token,long frames,long sequence,int level,int frequency,int wet){onMain(()->{
        if(service==null||intent.closing||!intent.foreground||!raw.optBoolean("owns_unresolved_lease"))return;
        Bundle b=new Bundle();b.putLong("lease_id",raw.optLong("lease_id"));b.putLong("guard_pid",raw.optLong("guard_pid"));b.putLong("monitor_token",token);
        b.putLong("frames",frames);b.putLong("sequence",sequence);b.putInt("severity",level);b.putInt("frequency",frequency);b.putInt("wet",wet);b.putBoolean("ready",ready);b.putLong("action_ms",SystemClock.elapsedRealtime());send(PROTECTION,b);
    });}
    /** This is called only by a fresh unmute gesture, never from gain/poll callbacks. */
    public void resetProtection(){onMain(()->{if(!intent.foreground||intent.closing)return;pendingGain=Integer.MIN_VALUE;resetLease=raw.optLong("lease_id");resetCounter=raw.optLong("protection_reset_count")+1;protectionResetPending=raw.optBoolean("owns_unresolved_lease");Bundle b=new Bundle();b.putLong("lease_id",resetLease);send(PROTECTION_RESET,b);});}
    /** Hard stop: unlike setEnabled(false), releases native capture and the ordinary carrier. */
    public void stop(){onMain(()->{intent.stop();hardStop();publish();});}
    @Override public void close(){onMain(()->{if(intent.closing)return;intent.closing=true;intent.stop();listener=null;hardStop();if(service==null&&!bound)detach();publish();});}
    private boolean completedMuteReadback(JSONObject r){return MicMuteConfirmation.ownedReadback(r.optInt("state",-1),r.has("mic_muted"),r.optBoolean("mic_muted"),r.optInt("hal_status",-99),r.optBoolean("zero_calls_returned"),r.optBoolean("low_calls_returned"),r.optInt("gain_setpoint",-1),protectionResetPending);}
    public boolean muteConfirmed(){JSONObject r=raw;return MicMuteConfirmation.known(readbackAgeMs(),r.optLong("guard_pid"),r.optLong("lease_id"),r.optBoolean("owns_unresolved_lease"),completedMuteReadback(r),cleanupVerified(r));}
    public boolean confirmedMuted(){JSONObject r=raw;return MicMuteConfirmation.muted(readbackAgeMs(),r.optLong("guard_pid"),r.optLong("lease_id"),r.optBoolean("owns_unresolved_lease"),completedMuteReadback(r),r.optBoolean("mic_muted"),cleanupVerified(r));}
    public JSONObject state(){return copy(published);}
    public JSONObject readback(){return copy(raw);}
    public JSONObject telemetry(){return copy(raw);}
    /** Age of an authenticated Guard receipt, not an audio latency measurement. */
    public long readbackAgeMs(){return receiptAtMs==0?Long.MAX_VALUE:Math.max(0,SystemClock.elapsedRealtime()-receiptAtMs);}
    public static int normalizeGain(int value){return Math.max(0,Math.min(100,value));}
    static int bootstrapGain(int value){return Math.min(5,normalizeGain(value));}

    static final class DesiredState {
        boolean foreground,desired,closing;int gain=20;
        void foreground(boolean value){foreground=value;if(!value)stop();}
        void enabled(boolean value,int level){gain=normalizeGain(level);desired=value&&gain>0&&foreground&&!closing;}
        void stop(){desired=false;}
        boolean mayStart(){return foreground&&desired&&!closing;}
    }
    private void onMain(Runnable action){if(Looper.myLooper()==Looper.getMainLooper())action.run();else ui.post(action);}
    private Intent serviceIntent(){return new Intent().setComponent(new ComponentName(PACKAGE,PACKAGE+".GuardService"));}
    private final ServiceConnection connection=new ServiceConnection(){
        @Override public void onServiceConnected(ComponentName name,IBinder binder){
            if(!PACKAGE.equals(name.getPackageName())||!available(app)){error="companion_identity_mismatch";detach();publish();return;}
            try{guardUid=app.getPackageManager().getApplicationInfo(PACKAGE,0).uid;service=new Messenger(binder);error="";if(hardStopPending||!intent.foreground||intent.closing)send(STOP,new Bundle());send(READ,new Bundle());pollSoon();}catch(Exception failure){error="companion_connection_failed";publish();}
        }
        @Override public void onServiceDisconnected(ComponentName name){service=null;startPending=false;pendingGain=Integer.MIN_VALUE;intent.stop();error="companion_disconnected";hardStopPending=true;requiredCancelEpoch=0;publish();}
        @Override public void onBindingDied(ComponentName name){onServiceDisconnected(name);detach();}
        @Override public void onNullBinding(ComponentName name){error="companion_binding_unavailable";detach();publish();}
    };
    private final Messenger replies=new Messenger(new Handler(Looper.getMainLooper()){
        @Override public void handleMessage(Message message){if(message.what==REPLY&&message.sendingUid==guardUid)accept(message.getData().getString("receipt","{}"));}
    });
    private void ensureBound(){
        if(bound||!intent.foreground||intent.closing)return;
        if(!available(app)){error="companion_unavailable";return;}
        try{app.startForegroundService(serviceIntent());bound=app.bindService(serviceIntent(),connection,Context.BIND_AUTO_CREATE);if(!bound)error="companion_bind_failed";}
        catch(RuntimeException failure){error="companion_start_failed";bound=false;}
    }
    private boolean send(int what,Bundle data){
        if(service==null)return false;
        try{data.putInt("pid",android.os.Process.myPid());if(what==START||what==LOW||what==MUTE||what==PROTECTION_RESET){data.putLong("action_ms",SystemClock.elapsedRealtime());data.putBoolean("local_action",intent.foreground&&!intent.closing);}
            Message message=Message.obtain(null,what);message.replyTo=replies;message.setData(data);service.send(message);return true;
        }catch(RemoteException failure){service=null;intent.stop();error="companion_send_failed";return false;}
    }
    private void hardStop(){
        intent.stop();startPending=false;pendingGain=Integer.MIN_VALUE;
        hardStopPending=true;requiredCancelEpoch=Math.max(requiredCancelEpoch,raw.optLong("cancellation_epoch",0)+1);
        send(STOP,new Bundle());pollSoon();
    }
    private void sendMute(){
        if(service==null||pendingGain==0)return;
        pendingGainCalls=raw.optLong("gain_calls",0);if(send(MUTE,new Bundle()))pendingGain=0;
    }
    private void accept(String json){
        try{
            JSONObject next=new JSONObject(json);long oldPid=raw.optLong("guard_pid",0),newPid=next.optLong("guard_pid",0),priorStops=raw.optLong("stop_calls",0);raw=next;if(protectionResetPending&&(next.optLong("lease_id")!=resetLease||next.optLong("protection_reset_count")>=resetCounter))protectionResetPending=false;receiptAtMs=SystemClock.elapsedRealtime();
            if(oldPid!=0&&newPid!=oldPid){intent.stop();startPending=false;pendingGain=Integer.MIN_VALUE;hardStopPending=true;requiredCancelEpoch=next.optLong("cancellation_epoch",0)+1;send(STOP,new Bundle());}
            int state=next.optInt("state",-1),gain=next.optInt("gain_setpoint",0);long lease=next.optLong("lease_id",0);
            if(!hardStopPending&&(state==STOPPING||state==RECOVERY)){intent.stop();hardStopPending=true;requiredCancelEpoch=next.optLong("cancellation_epoch",0)+1;startPending=false;send(STOP,new Bundle());}
            if(!hardStopPending&&oldPid!=0&&oldPid==newPid&&next.optLong("stop_calls",0)>priorStops&&cleanupVerified(next)){intent.stop();startPending=false;}
            if(state!=READY)startPending=false;
            if(state==ACTIVE&&next.optBoolean("low_calls_returned")&&gain>0)initialGainLease=lease;
            if(pendingGain!=Integer.MIN_VALUE&&state==ACTIVE&&next.optBoolean("low_calls_returned")&&next.optInt("applied_user_gain_setpoint",-1)==pendingGain&&next.optLong("gain_calls",0)>pendingGainCalls)pendingGain=Integer.MIN_VALUE;
            if(!next.optBoolean("owns_unresolved_lease")||state==STOPPING||state==RECOVERY)pendingGain=Integer.MIN_VALUE;
            if(hardStopPending&&next.optLong("cancellation_epoch",0)>=requiredCancelEpoch&&cleanupVerified(next)){hardStopPending=false;if(intent.closing){detach();publish();return;}}
            drive();publish();
        }catch(Exception ignored){error="companion_receipt_unreadable";intent.stop();publish();}
    }
    private void drive(){
        if(service==null||hardStopPending||intent.closing||!intent.foreground)return;
        boolean owns=raw.optBoolean("owns_unresolved_lease");int state=raw.optInt("state",-1);
        if(!intent.desired){if(owns&&(state==ZERO||state==ACTIVE)&&!raw.optBoolean("mic_muted"))sendMute();return;}
        if(!permissionReady()||!raw.optBoolean("foreground_ready")||!raw.optBoolean("independent_uid"))return;
        if(!owns&&state==READY&&!raw.optBoolean("carrier_resources_pending")&&!startPending){if(send(START,new Bundle()))startPending=true;return;}
        if(!owns||(state!=ZERO&&state!=ACTIVE)||pendingGain!=Integer.MIN_VALUE)return;
        long lease=raw.optLong("lease_id",0);int target=!protectionResetPending&&raw.optBoolean("protection_ready")?(initialGainLease==lease?intent.gain:bootstrapGain(intent.gain)):0;
        if(state==ACTIVE&&raw.optBoolean("low_calls_returned")&&raw.optInt("applied_user_gain_setpoint",-1)==target)return;
        Bundle data=new Bundle();data.putLong("lease_id",lease);data.putInt("gain",target);pendingGainCalls=raw.optLong("gain_calls",0);if(send(LOW,data))pendingGain=target;
    }
    private static boolean cleanupVerified(JSONObject value){
        if(value.optBoolean("owns_unresolved_lease")||value.optBoolean("carrier_resources_pending"))return false;
        if(value.optLong("start_calls",0)==0&&value.optLong("stop_calls",0)==0)return true;
        JSONObject carrier=value.optJSONObject("zero_output_carrier");
        return value.optBoolean("stop_call_returned")&&value.optBoolean("flag_disable_returned")&&value.optInt("hal_status",-99)==2&&(carrier==null||carrier.length()==0||carrier.optBoolean("released"));
    }
    private final Runnable poll=new Runnable(){public void run(){if(service!=null&&(intent.foreground||hardStopPending)){send(READ,new Bundle());ui.postDelayed(this,200);}publish();}};
    private void pollSoon(){ui.removeCallbacks(poll);if(service!=null&&(intent.foreground||hardStopPending))ui.post(poll);}
    private void detach(){ui.removeCallbacks(poll);service=null;if(bound){try{app.unbindService(connection);}catch(RuntimeException ignored){}bound=false;}}
    private void publish(){
        JSONObject result=new JSONObject();boolean installed=available(app),permission=permissionReady(),owns=raw.optBoolean("owns_unresolved_lease");int phase=raw.optInt("state",-1),gain=raw.optInt("gain_setpoint",0);
        boolean enabled=intent.mayStart()&&service!=null&&!hardStopPending&&owns&&phase==ACTIVE&&raw.optBoolean("low_calls_returned")&&gain>0&&raw.optBoolean("protection_ready");
        String name=!installed?"unavailable":!permission?"permission_required":hardStopPending?"stopping":service==null?"disconnected":enabled?"enabled":owns&&raw.optBoolean("mic_muted")?"muted":intent.desired?"starting":"ready";
        String guardError=raw.optString("last_error","");if(guardError.isEmpty())guardError=raw.optString("reject","");
        try{result.put("protection_reset_pending",protectionResetPending).put("state",name).put("available",installed).put("permission_ready",permission).put("connected",service!=null).put("foreground",intent.foreground).put("desired",intent.desired).put("desired_enabled",intent.desired).put("desired_gain",intent.gain).put("enabled",enabled).put("stopping",hardStopPending||phase==STOPPING||phase==RECOVERY).put("native_capture_owned",owns).put("mic_muted",muteConfirmed()?confirmedMuted():JSONObject.NULL).put("effective_gain_setpoint",gain).put("gain_readback_available",false).put("stop_verified",service!=null&&cleanupVerified(raw)).put("last_error",!error.isEmpty()?error:guardError).put("guard",copy(raw));}catch(Exception ignored){}
        published=result;
        if(listener!=null&&!notifyQueued){notifyQueued=true;ui.post(()->{notifyQueued=false;Listener current=listener;if(current!=null)current.onChanged(state());});}
    }
    private static JSONObject copy(JSONObject value){try{return new JSONObject(value.toString());}catch(Exception ignored){return new JSONObject();}}
}
