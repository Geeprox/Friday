package local.friday.ktv.audio;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.hardware.usb.*;
import android.media.*;
import android.os.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.*;
import local.friday.ktv.audio.protocol.*;

/** Separate APK/UID, not a :process belonging to the controlling APK. */
public final class GuardService extends Service {
    private final ProbePolicy policy=new ProbePolicy();
    private final Handler main=new Handler(Looper.getMainLooper());
    private final ExecutorService commands=Executors.newSingleThreadExecutor(),stopper=Executors.newSingleThreadExecutor(),bridgeKeeper=Executors.newSingleThreadExecutor();
    private final AtomicBoolean refreshInFlight=new AtomicBoolean(),bridgeBeatInFlight=new AtomicBoolean();
    private volatile boolean bridgeReserved,bridgeBound,bridgeContractVerified;private volatile long lastBridgeBeat;
    private final StopDispatch stopDispatch=new StopDispatch();
    private final CarrierGate carrierGate=new CarrierGate();
    private final GainRequestGate gainDispatch=new GainRequestGate();
    private final GainWriteFence gainWrites=new GainWriteFence();
    private final FeedbackProtection protection=new FeedbackProtection();
    private boolean resetZeroPending;private long resetZeroSince;
    private int userGain,appliedUserGain;private long protectionResetCount;
    private ZeroOutputCarrier carrier;
    private JSONObject carrierFinal=new JSONObject(),carrierBeforeStart=new JSONObject();
    private final ArrayList<String> carrierSamples=new ArrayList<>();
    private long carrierLease;
    private final ArrayDeque<String> events=new ArrayDeque<>();
    private final ArrayList<String> calls=new ArrayList<>();
    private AudioManager audio;
    private VendorBridge vendor;
    private PowerManager.WakeLock wake;
    private Messenger client;
    private IBinder clientBinder;
    private IBinder.DeathRecipient death;
    private int controllerUid=-1,controllerPid=-1,commandExceptions,preflightStopCalls;
    private long lastRefresh,stopIssueMs,stopReturnMs,startIssueMs,startReturnMs,commandMaxMs;
    private long selfTestStopIssueMs,selfTestStopReturnMs;
    private long startCallReturnMs;
    private int actualStartCalls,flagEnableCalls,flagDisableCalls;
    private ProbePolicy.Target observedTarget;
    private String usbInterfaceClasses="",lastError="",targetReason="unknown";
    private volatile boolean foreground,serviceEnding;
    private final Messenger messenger=new Messenger(new Handler(Looper.getMainLooper()){
        @Override public void handleMessage(Message message){receive(message);}
    });
    private long now(){return SystemClock.elapsedRealtime();}
    private void event(String code){synchronized(policy){events.addLast(now()+":"+code);}}
    @Override public void onCreate(){
        super.onCreate();audio=(AudioManager)getSystemService(AUDIO_SERVICE);vendor=new VendorBridge(this,audio);
        wake=((PowerManager)getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"friday:globalmic-guard");wake.setReferenceCounted(false);
        if(getSharedPreferences("lease",MODE_PRIVATE).getBoolean("unresolved",false))policy.recoverOwnedMarker();
        audio.registerAudioDeviceCallback(devices,main);main.post(protectionWatchdog);event("guard_created_no_start");
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        NotificationManager nm=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel("watchdog","麦克风返听",NotificationManager.IMPORTANCE_LOW));
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,PermissionActivity.class),PendingIntent.FLAG_IMMUTABLE);
        Notification note=new Notification.Builder(this,"watchdog").setSmallIcon(android.R.drawable.ic_lock_silent_mode)
            .setContentTitle("Friday KTV 麦克风").setContentText("由 Friday KTV 控制，离开或停止后关闭返听。").setContentIntent(open).setOngoing(true).build();
        try{startForeground(1,note,ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);foreground=true;wake.acquire();}
        catch(RuntimeException failure){lastError="foreground_unavailable";foreground=false;}
        synchronized(policy){policy.foregroundReady=foreground;}
        return START_NOT_STICKY;
    }
    @Override public IBinder onBind(Intent intent){return messenger.getBinder();}
    private boolean callerAllowed(int uid){
        if(uid==android.os.Process.myUid())return true;
        String[] packages=getPackageManager().getPackagesForUid(uid);
        return packages!=null&&Arrays.asList(packages).contains(Protocol.CONTROLLER)
            &&getPackageManager().checkSignatures(android.os.Process.myUid(),uid)==PackageManager.SIGNATURE_MATCH;
    }
    private void receive(Message m){
        if(!callerAllowed(m.sendingUid))return;
        boolean external=m.sendingUid!=android.os.Process.myUid();
        if(external&&m.replyTo!=null&&!bindClient(m))return;
        Bundle b=m.getData();
        if(m.what==Protocol.READ){refresh();reply(m.replyTo);return;}
        if(m.what==Protocol.STOP){requestStop(ProbePolicy.USER);reply(m.replyTo);return;}
        if(!external||clientBinder==null||!clientBinder.isBinderAlive()){lastError="separate_controller_required";reply(m.replyTo);return;}
        if(m.what==Protocol.START)beginStart(b);
        else if(m.what==Protocol.LOW)low(b);
        else if(m.what==Protocol.MUTE)mute(b);
        else if(m.what==Protocol.PROTECTION)protection(b);
        else if(m.what==Protocol.PROTECTION_RESET)resetProtection(b);
        reply(m.replyTo);
    }
    private boolean bindClient(Message m){
        IBinder binder=m.replyTo.getBinder();if(binder==clientBinder)return true;
        synchronized(policy){if(policy.owns){lastError="controller_changed_during_lease";return false;}}
        if(clientBinder!=null&&death!=null)clientBinder.unlinkToDeath(death,0);
        client=m.replyTo;clientBinder=binder;controllerUid=m.sendingUid;controllerPid=m.getData().getInt("pid",-1);
        death=()->{synchronized(policy){policy.clientDied(now());}event("controller_binder_died");requestStop(ProbePolicy.CLIENT_DIED);};
        try{binder.linkToDeath(death,0);}catch(RemoteException failure){clientBinder=null;return false;}
        synchronized(policy){policy.independentUid=controllerUid!=android.os.Process.myUid();}
        return true;
    }
    private void reply(Messenger recipient){if(recipient==null)return;try{Message m=Message.obtain(null,Protocol.REPLY);Bundle b=new Bundle();b.putString("receipt",receipt(false));m.setData(b);recipient.send(m);}catch(RemoteException ignored){}}
    private static boolean contains(int[] array,int value){for(int v:array)if(v==value)return true;return false;}
    private ProbePolicy.Target target(){
        ArrayList<TargetResolver.Input> inputs=new ArrayList<>();
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_INPUTS))
            inputs.add(new TargetResolver.Input(d.getId(),d.getType(),d.isSource(),d.getAddress(),d.getChannelCounts(),d.getSampleRates(),d.getEncodings()));
        UsbManager usb=(UsbManager)getSystemService(USB_SERVICE);
        if(usb==null){targetReason="invalid_device_inventory";return null;}
        ArrayList<TargetResolver.Usb> descriptors=new ArrayList<>();StringBuilder classes=new StringBuilder();
        for(UsbDevice d:usb.getDeviceList().values()){
            TargetResolver.Interface[] faces=new TargetResolver.Interface[d.getInterfaceCount()];
            for(int i=0;i<faces.length;i++){
                UsbInterface face=d.getInterface(i);TargetResolver.Endpoint[] endpoints=new TargetResolver.Endpoint[face.getEndpointCount()];
                for(int k=0;k<endpoints.length;k++){UsbEndpoint e=face.getEndpoint(k);endpoints[k]=new TargetResolver.Endpoint(e.getDirection(),e.getType());}
                faces[i]=new TargetResolver.Interface(face.getInterfaceClass(),face.getInterfaceSubclass(),endpoints);
                if(classes.length()>0)classes.append(',');classes.append(face.getInterfaceClass());
            }
            descriptors.add(new TargetResolver.Usb(d.getDeviceName(),d.getVendorId(),d.getProductId(),d.getDeviceClass(),faces));
        }
        TargetResolver.Result result=TargetResolver.resolve(inputs.toArray(new TargetResolver.Input[0]),descriptors.toArray(new TargetResolver.Usb[0]),0);
        synchronized(policy){usbInterfaceClasses=classes.toString();targetReason=result.reason();}
        return result.target;
    }
    private String get(String key){long start=now();try{return vendor.read(key);}finally{duration(start);}}
    private void duration(long start){synchronized(policy){commandMaxMs=Math.max(commandMaxMs,now()-start);}}
    private void error(String fixedCode){synchronized(policy){commandExceptions++;lastError=fixedCode;}event(fixedCode);}
    private boolean readIdle(){
        try{
            synchronized(policy){if(carrierGate.occupied())return false;}
            ProbePolicy.Target target=target();String version=Protocol.value("61961",get("61961")),model=Protocol.value("61958",get("61958"));
            int status=Protocol.status(get("61959"));String cards=Protocol.value("61957",get("61957"));
            boolean safePlatform=NativeCacheOwner.available(version);bridgeContractVerified=safePlatform;
            synchronized(policy){observedTarget=target;lastRefresh=now();
                boolean idle=policy.idleEvidence(target,NativeCacheOwner.baseVersion(version),model,status,cards,Build.VERSION.SDK_INT>=34&&"mtktv".equals(Build.HARDWARE));
                if(!safePlatform){lastError="strict_platform_bridge_unavailable";policy.watchdogVerified=false;}
                return idle&&safePlatform;}
        }catch(RuntimeException failure){bridgeContractVerified=false;error("idle_read_failed");return false;}
    }
    private void refresh(){
        synchronized(policy){if(policy.owns||now()-lastRefresh<1000)return;}
        if(!refreshInFlight.compareAndSet(false,true))return;
        commands.execute(()->{try{readIdle();}finally{refreshInFlight.set(false);main.post(()->reply(client));}});
    }
    private void call(String name,Runnable command){
        long issued=now();boolean returned=false;
        synchronized(policy){if(name.equals("GlobalMicLowLatency=enable"))flagEnableCalls++;if(name.equals("GlobalMicLowLatency=disable"))flagDisableCalls++;}
        try{command.run();returned=true;}finally{
            duration(issued);
            sampleCarrier("after_"+name);
            synchronized(policy){try{calls.add(new JSONObject().put("command",name).put("lease_id",policy.leaseId)
                .put("issued_ms",issued).put("returned_ms",now()).put("returned",returned).put("readback_verified",false).toString());}catch(JSONException ignored){}}
        }
    }
    private boolean startAllowed(long epoch){synchronized(policy){return policy.pendingAllowed(epoch)&&policy.owns&&policy.state==ProbePolicy.STARTING;}}
    private int builtInOutput(){
        int id=0,count=0;for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(d.isSink()&&d.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER){id=d.getId();count++;}
        return count==1?id:0;
    }
    private void sampleCarrier(String step){
        synchronized(policy){if(carrier==null)return;try{carrierSamples.add(new JSONObject().put("step",step).put("lease_id",carrierLease).put("cancellation_epoch",policy.cancellationEpoch).put("elapsed_ms",now()).put("carrier",carrier.metrics()).toString());}catch(JSONException ignored){}}
    }
    private void carrierChanged(long lease){synchronized(policy){if(carrierLease!=lease)return;}sampleCarrier("progress");main.post(()->reply(client));}
    private void closeCarrier(long lease,boolean stopIssuedAfterSequence){
        ZeroOutputCarrier current;
        synchronized(policy){if(!carrierGate.beginClose(lease,stopIssuedAfterSequence))return;current=carrier;}
        sampleCarrier("before_release");boolean released=current==null;
        try{if(current!=null){current.close();released=current.releaseReturned();}}
        catch(RuntimeException failure){error("carrier_close_failed");}
        synchronized(policy){
            if(current!=null)carrierFinal=current.metrics();
            carrierGate.finishClose(lease,released);
            if(released&&carrierLease==lease)carrier=null;
        }
        if(!released)error("carrier_release_unverified");event(released?"carrier_release_returned":"carrier_release_unknown");
    }
    private void beginStart(Bundle data){
        final long action=data.getLong("action_ms",-1);final boolean local=data.getBoolean("local_action");
        final long epoch;synchronized(policy){epoch=policy.cancellationEpoch;}
        commands.execute(()->{
            synchronized(policy){if(!policy.pendingAllowed(epoch)||carrierGate.occupied())return;}
            if(!readIdle())return;
            final int output=builtInOutput();if(output<=0){error("built_in_output_unknown");main.post(()->reply(client));return;}
            final ProbePolicy.Target selected;final long lease;final ZeroOutputCarrier current;
            synchronized(policy){
                if(!policy.pendingAllowed(epoch)||carrierGate.occupied()||clientBinder==null||!clientBinder.isBinderAlive()||!policy.beginStart(now(),action,local,
                    checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED))return;
                selected=policy.target;lastError="";lease=policy.leaseId;
                if(!gainWrites.beginLease(lease,epoch)){policy.requestStop(ProbePolicy.COMMAND_ERROR);main.post(()->requestStop(ProbePolicy.COMMAND_ERROR,lease));return;}
                protection.begin(lease);resetZeroPending=false;startIssueMs=0;userGain=appliedUserGain=0;carrierLease=lease;carrierGate.attach(lease);carrierBeforeStart=new JSONObject();
                current=new ZeroOutputCarrier(output,()->carrierChanged(lease),()->{error("carrier_failed");requestStop(ProbePolicy.COMMAND_ERROR);});carrier=current;
            }
            boolean returned=false,zero=false,enabled=false;int status=Integer.MIN_VALUE;
            try{
                if(!getSharedPreferences("lease",MODE_PRIVATE).edit().putBoolean("unresolved",true).commit())throw new IllegalStateException("lease_marker_failed");
                if(!startAllowed(epoch)||!foreground)throw new IllegalStateException("start_cancelled");
                if(!NativeCacheOwner.begin(lease,selected.card,selected.device))throw new IllegalStateException("strict_bridge_reservation_failed");
                bridgeReserved=true;bridgeBound=false;lastBridgeBeat=now();
                if(!startAllowed(epoch))throw new IllegalStateException("start_cancelled");
                event("carrier_start_requested");current.start();sampleCarrier("starting");
                if(!current.awaitReady(()->!startAllowed(epoch)||!foreground||serviceEnding))throw new IllegalStateException("carrier_not_ready_or_cancelled");
                synchronized(policy){
                    if(!startAllowed(epoch)||!foreground||!current.readyForVendor()||!carrierGate.ready(lease)||!carrierGate.allowVendorStart(lease))throw new IllegalStateException("carrier_start_gate_rejected");
                    carrierBeforeStart=current.metrics();startIssueMs=now();actualStartCalls++;
                }
                sampleCarrier("before_vendor_start");event("carrier_playing_head_advanced_before_vendor_start");
                call("START",()->vendor.start(selected));returned=true;synchronized(policy){startCallReturnMs=now();}
                if(!startAllowed(epoch))throw new IllegalStateException();
                call("MIC_ZERO",vendor::micZero);synchronized(policy){policy.gainCalls++;}
                if(!startAllowed(epoch))throw new IllegalStateException();
                call("SYSTEM_ZERO",vendor::systemZero);synchronized(policy){policy.gainCalls++;}zero=true;
                if(!startAllowed(epoch))throw new IllegalStateException();
                call("GlobalMicLowLatency=enable",vendor::enableFlag);enabled=true;
                status=Protocol.status(get("61959"));
                if(status!=0||!startAllowed(epoch)||!NativeCacheOwner.bind(lease))throw new IllegalStateException("strict_bridge_bind_failed");
                bridgeBound=true;lastBridgeBeat=now();
            }catch(RuntimeException failure){zero=false;error("carrier_start_zero_or_bridge_failed");}
            boolean active;
            synchronized(policy){startReturnMs=now();carrierGate.sequenceFinished(lease);active=policy.startFinished(now(),returned,status,zero,enabled);}
            event(active?"zero_and_flag_calls_returned_no_gain_or_flag_readback":"start_needs_stop");
            if(!active)requestStop(ProbePolicy.COMMAND_ERROR);main.post(()->reply(client));
        });
    }
    private void mute(Bundle data){
        final long lease;
        synchronized(policy){
            if(!policy.owns){policy.cancelPending();return;}
            if(policy.state!=ProbePolicy.STARTING&&policy.state!=ProbePolicy.ZERO&&policy.state!=ProbePolicy.LOW)return;
            data.putLong("lease_id",policy.leaseId);data.putInt("gain",0);
            lease=policy.leaseId;
        }
        if(NativeCacheOwner.inline()&&!NativeCacheOwner.muteInline(lease,true)){error("inline_mute_failed");requestStop(ProbePolicy.COMMAND_ERROR);return;}
        low(data);
    }
    private boolean gainAllowed(long epoch,long lease){synchronized(policy){return policy.pendingAllowed(epoch)&&policy.owns&&policy.leaseId==lease&&(policy.state==ProbePolicy.ZERO||policy.state==ProbePolicy.LOW);}}
    private int protectedGain(int wanted){return NativeCacheOwner.inline()?(protection.healthy(now())?protection.referenceGain(wanted):0):protection.effectiveGain(wanted,now());}
    private boolean positiveGain(String name,Runnable command,long epoch,long lease,long ticket){
        final long write;
        synchronized(policy){
            if(!gainAllowed(epoch,lease)||!gainDispatch.current(ticket))return false;
            write=gainWrites.register(lease,epoch,ticket);
        }
        if(write==0)return false;
        // Cancellation can happen at any point after registration. STOP is
        // independent and immediate; only its final confirmation waits for this
        // reservation to exit. Never hold policy across a vendor Binder call.
        try{call(name,command);return true;}
        finally{
            final boolean finalStop;synchronized(policy){finalStop=gainWrites.finished(lease,write);}
            if(finalStop)requestStop(ProbePolicy.COMMAND_ERROR,lease);
        }
    }
    private void low(Bundle data){
        final long action=data.getLong("action_ms",-1),lease=data.getLong("lease_id",-1);
        final int wanted=data.getInt("gain",0);final boolean local=data.getBoolean("local_action");
        if(!Protocol.gainAllowed(wanted))return;
        final long epoch,ticket;synchronized(policy){if(!policy.owns||lease!=policy.leaseId||!local||action<0||now()<action)return;userGain=wanted;epoch=policy.cancellationEpoch;ticket=gainDispatch.request(lease,wanted,resetZeroPending?0:protectedGain(wanted));if(ticket==0)return;}
        commands.execute(()->{try{
            final int gain;
            synchronized(policy){if(!gainDispatch.current(ticket)||!policy.pendingAllowed(epoch)||!policy.allowGain(lease,now(),action,local,wanted))return;gain=resetZeroPending?0:protectedGain(wanted);}
            boolean returned=false;int status=Integer.MIN_VALUE;
            try{
                if(Protocol.status(get("61959"))!=0||!gainAllowed(epoch,lease))throw new IllegalStateException();
                synchronized(policy){if(gain>0&&(!protection.healthy(now())||protectedGain(wanted)<gain))return;}
                if(gain==0&&!NativeCacheOwner.muteInline(lease,true))throw new IllegalStateException("inline_mute_failed");
                if(gain==0)call("MIC_ZERO",vendor::micZero);else if(!positiveGain("MIC_GAIN_"+gain,()->vendor.micGain(gain),epoch,lease,ticket))return;synchronized(policy){policy.gainCalls++;if(!gainDispatch.current(ticket))return;}
                if(!gainAllowed(epoch,lease))throw new IllegalStateException();
                if(gain==0)call("SYSTEM_ZERO",vendor::systemZero);else if(!positiveGain("SYSTEM_GAIN_100",vendor::systemLow,epoch,lease,ticket))return;synchronized(policy){policy.gainCalls++;if(!gainDispatch.current(ticket))return;}
                if(gain>0&&NativeCacheOwner.inline()){
                    synchronized(policy){if(!gainDispatch.current(ticket)||!protection.healthy(now()))return;}
                    if(!positiveGain("INLINE_OWNER_GATE",()->{if(!NativeCacheOwner.muteInline(lease,false))throw new IllegalStateException("inline_enable_failed");},epoch,lease,ticket))return;
                }
                returned=true;status=Protocol.status(get("61959"));
            }catch(RuntimeException failure){synchronized(policy){if(!gainDispatch.current(ticket)||!policy.pendingAllowed(epoch))return;}error("gain_call_failed");}
            boolean stopping;
            synchronized(policy){if(!gainDispatch.current(ticket))return;policy.gainFinished(lease,gain,returned,status);if(returned&&status==0)appliedUserGain=wanted;stopping=policy.state==ProbePolicy.STOPPING||policy.state==ProbePolicy.RECOVERY;}
            event("gain_setpoint_calls_finished");if(stopping)requestStop(ProbePolicy.COMMAND_ERROR);main.post(()->reply(client));
        }finally{synchronized(policy){gainDispatch.finished(ticket);}}});
    }
    private Bundle protectionGainRequest(){Bundle b=new Bundle();synchronized(policy){b.putLong("lease_id",policy.leaseId);b.putInt("gain",userGain);}b.putLong("action_ms",now());b.putBoolean("local_action",true);return b;}
    private void protection(Bundle b){
        long lease=b.getLong("lease_id"),token=b.getLong("monitor_token"),seq=b.getLong("sequence");
        final long epoch;final boolean changed;boolean accepted;
        synchronized(policy){
            if(resetZeroPending||!policy.owns||lease!=policy.leaseId||b.getLong("guard_pid")!=android.os.Process.myPid())return;
            epoch=policy.cancellationEpoch;
            accepted=protection.heartbeat(lease,token,b.getLong("frames"),b.getBoolean("ready"),b.getInt("wet"),b.getLong("action_ms",-1),now());
            changed=accepted&&protection.observe(lease,token,seq,b.getInt("severity"),b.getInt("frequency"),protection.referenceGain(userGain));
            if(protection.watchdog(now(),policy.requestedGain>0)||changed)gainDispatch.cancel();
        }
        if(changed)commands.execute(()->{
            if(!gainAllowed(epoch,lease))return;
            try{call("FEEDBACK_EQ",()->applyProtectionEq(lease));}
            catch(RuntimeException failure){synchronized(policy){protection.trip(6);}error("feedback_eq_failed");}
            main.post(()->{low(protectionGainRequest());reply(client);});
        });
        else {boolean adjust;synchronized(policy){adjust=policy.requestedGain!=protectedGain(userGain);}if(adjust)low(protectionGainRequest());}
    }
    private void resetProtection(Bundle b){
        final long lease=b.getLong("lease_id"),action=b.getLong("action_ms",-1),epoch;
        synchronized(policy){
            if(resetZeroPending||!b.getBoolean("local_action")||action<0||now()<action||now()-action>750||!policy.owns||lease!=policy.leaseId)return;
            epoch=policy.cancellationEpoch;resetZeroPending=true;resetZeroSince=now();gainDispatch.cancel();
        }
        commands.execute(()->{
            if(!gainAllowed(epoch,lease))return;
            try{
                if(!NativeCacheOwner.muteInline(lease,true))throw new IllegalStateException("inline_mute_failed");
                call("PROTECTION_RESET_MIC_ZERO",vendor::micZero);
                if(!gainAllowed(epoch,lease))return;
                call("PROTECTION_RESET_SYSTEM_ZERO",vendor::systemZero);
                if(!gainAllowed(epoch,lease))return;
                int status=Protocol.status(get("61959"));if(status!=0)throw new IllegalStateException();
                if(!gainAllowed(epoch,lease)||!NativeCacheOwner.bind(lease))throw new IllegalStateException("strict_bridge_rebind_failed");
                synchronized(policy){
                    if(!gainAllowed(epoch,lease))return;
                    bridgeBound=true;lastBridgeBeat=now();policy.gainCalls+=2;policy.gainFinished(lease,0,true,status);userGain=appliedUserGain=0;protection.begin(lease);
                }
                // Only after zero setter completion can old attenuation be removed.
                call("FEEDBACK_EQ_RESET",()->applyProtectionEq(lease));
                synchronized(policy){if(!policy.pendingAllowed(epoch))return;gainDispatch.cancel();resetZeroPending=false;protectionResetCount++;}
                main.post(()->reply(client));
            }catch(RuntimeException failure){synchronized(policy){protection.trip(6);}error("protection_reset_failed");requestStop(ProbePolicy.COMMAND_ERROR);}
        });
    }
    private void applyProtectionEq(long lease){
        if(NativeCacheOwner.inline()){
            int[][] bands=protection.inlineBands();
            if(!NativeCacheOwner.notchesInline(lease,bands[0],bands[1]))throw new IllegalStateException("inline_notch_failed");
        }else vendor.protectionEq(protection);
    }
    private final Runnable protectionWatchdog=new Runnable(){public void run(){
        if(serviceEnding)return;boolean tripped,emergency;
        final long bridgeLease,bridgeEpoch;
        synchronized(policy){bridgeLease=policy.leaseId;bridgeEpoch=policy.cancellationEpoch;}
        if(bridgeReserved&&now()-lastBridgeBeat>=200&&bridgeBeatInFlight.compareAndSet(false,true)){
            bridgeKeeper.execute(()->{
                try{boolean healthy=NativeCacheOwner.heartbeat(bridgeLease);boolean current;
                    synchronized(policy){current=policy.owns&&policy.leaseId==bridgeLease&&policy.cancellationEpoch==bridgeEpoch;}
                    if(current){if(healthy)lastBridgeBeat=now();else{error("strict_bridge_owner_lost");requestStop(ProbePolicy.COMMAND_ERROR);}}
                }finally{bridgeBeatInFlight.set(false);}
            });
        }
        synchronized(policy){tripped=policy.owns&&protection.watchdog(now(),policy.requestedGain>0);if(tripped)gainDispatch.cancel();emergency=policy.owns&&(protection.emergencyStopDue(now(),policy.requestedGain>0)
                ||(resetZeroPending&&now()-resetZeroSince>=500)
                ||(policy.state==ProbePolicy.STARTING&&startIssueMs>0&&!policy.zeroCallsReturned&&now()-startIssueMs>=750));}
        if(emergency)requestStop(ProbePolicy.COMMAND_ERROR);else if(tripped){low(protectionGainRequest());reply(client);}main.postDelayed(this,100);
    }};
    private void requestStop(int reason){requestStop(reason,-1);}
    private void requestStop(int reason,long expectedLease){
        final long lease,ticket;
        synchronized(policy){
            if(expectedLease>=0&&(!policy.owns||policy.leaseId!=expectedLease))return;
            policy.cancelPending();carrierGate.cancel();gainDispatch.cancel();gainWrites.cancel(policy.leaseId);
            if(!policy.owns)return;
            policy.requestStop(reason);lease=policy.leaseId;ticket=stopDispatch.begin(lease,true);
        }
        // Cancel before revoking the bridge. In-flight positive calls may still
        // return later, so they remain fenced until a STOP issued after them.
        bridgeReserved=bridgeBound=bridgeContractVerified=false;NativeCacheOwner.end();
        if(ticket==0)return;
        stopper.execute(()->{
            final boolean afterStart,afterWrites;
            synchronized(policy){
                if(!policy.owns||policy.leaseId!=lease||!stopDispatch.current(lease,ticket))return;
                afterStart=policy.startReturned;afterWrites=gainWrites.settled(lease);stopIssueMs=now();
            }
            boolean returned=false,disabled=false;int status=Integer.MIN_VALUE;
            try{call("STOP",vendor::stop);returned=true;}
            catch(RuntimeException failure){error("stop_call_failed");}
            finally{try{call("GlobalMicLowLatency=disable",vendor::disableFlag);disabled=true;}catch(RuntimeException failure){error("flag_disable_failed");}}
            try{status=Protocol.status(get("61959"));}catch(RuntimeException failure){error("stop_status_read_failed");}
            boolean finished,retry;
            synchronized(policy){
                if(policy.leaseId!=lease||!stopDispatch.current(lease,ticket))return;
                stopReturnMs=now();policy.stopFinished(now(),returned,status,afterStart,disabled,afterWrites);finished=!policy.owns;
                retry=stopDispatch.finish(lease,ticket,policy.leaseId,policy.owns,policy.startReturned,gainWrites.settled(lease),afterStart&&afterWrites);
                if(finished)getSharedPreferences("lease",MODE_PRIVATE).edit().putBoolean("unresolved",false).commit();}
            closeCarrier(lease,afterStart&&afterWrites);
            event("stop_and_flag_disable_sequence_finished");
            if(retry)requestStop(reason,lease);main.post(()->reply(client));
        });
    }
    private final AudioDeviceCallback devices=new AudioDeviceCallback(){
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] changed){changed();}
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] changed){changed();}
        private void changed(){synchronized(policy){policy.cancelPending();if(!policy.owns){policy.watchdogVerified=false;return;}}event("device_changed");requestStop(ProbePolicy.DEVICE_CHANGED);}
    };
    String receipt(){return receipt(true);}
    private String receipt(boolean history){
        synchronized(policy){try{
            JSONArray commandRows=new JSONArray();if(history)for(String row:calls)commandRows.put(new JSONObject(row));
            JSONObject j=new JSONObject();j.put("schema",3).put("guard_version","0.5").put("guard_package",getPackageName()).put("guard_uid",android.os.Process.myUid()).put("guard_pid",android.os.Process.myPid())
                .put("controller_uid",controllerUid).put("controller_pid",controllerPid).put("state",policy.state).put("reject",policy.reject).put("last_error",lastError)
                .put("cancellation_epoch",policy.cancellationEpoch).put("elapsed_ms",now()).put("continuous_mode",true).put("automatic_expiry",false).put("hal_status",policy.halStatus).put("version",policy.version).put("play_model",policy.model).put("foreground_ready",policy.foregroundReady).put("foreground_service_active",foreground)
                .put("independent_uid",policy.independentUid).put("watchdog_verified",policy.watchdogVerified).put("watchdog_verified_ms",policy.verifiedAtMs)
                .put("controller_death_ms",policy.controllerDeathMs).put("strict_bridge_available",bridgeContractVerified).put("strict_cache_required",true).put("strict_bridge_reserved",bridgeReserved).put("strict_bridge_bound",bridgeBound).put("native_zero_initialization_verified",bridgeReserved).put("self_test_required",false).put("preflight_stop_calls",preflightStopCalls)
                .put("self_test_stop_issue_ms",selfTestStopIssueMs).put("self_test_stop_return_ms",selfTestStopReturnMs)
                .put("owns_unresolved_lease",policy.owns).put("started_ms",policy.startedMs).put("deadline_ms",policy.deadlineMs).put("stopped_ms",policy.stoppedMs)
                .put("start_calls",actualStartCalls).put("accepted_start_requests",policy.startCalls).put("lease_id",policy.leaseId).put("start_call_returned",policy.startCallSucceeded).put("start_sequence_finished",policy.startReturned)
                .put("zero_calls_returned",policy.zeroCallsReturned).put("low_calls_returned",policy.lowCallsReturned).put("gain_setpoint",policy.requestedGain).put("flag_enable_returned",policy.flagEnableReturned).put("flag_disable_returned",policy.flagDisableReturned).put("flag_readback_available",false).put("flag_enable_calls",flagEnableCalls).put("flag_disable_calls",flagDisableCalls).put("gain_calls",policy.gainCalls)
                .put("stop_calls",policy.stopCalls).put("stop_call_returned",policy.stopReturned).put("stop_issued_after_start_returned",policy.stopAfterStart).put("stop_reason",policy.stopReason)
                .put("stop_issued_after_gain_writes",policy.stopAfterGainWrites).put("positive_gain_writes_pending",gainWrites.pending(policy.leaseId)).put("inline_voice_mode",NativeCacheOwner.inline())
                .put("start_issue_ms",startIssueMs).put("start_return_ms",startCallReturnMs).put("start_sequence_finish_ms",startReturnMs).put("stop_issue_ms",stopIssueMs).put("stop_return_ms",stopReturnMs)
                .put("command_max_ms",commandMaxMs).put("command_exceptions",commandExceptions).put("gain_readback_available",false).put("mixer_restore_verified",false)
                .put("permission_ready",checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED).put("native_capture_owned",policy.owns).put("mic_muted",policy.owns&&((policy.state==ProbePolicy.ZERO&&policy.zeroCallsReturned)||(policy.state==ProbePolicy.LOW&&policy.lowCallsReturned&&policy.requestedGain==0))).put("soft_mute_keeps_capture",true).put("physical_latency_measured",false).put("raw_audio_saved",false).put("usb_interface_classes",usbInterfaceClasses).put("target_capability_reason",targetReason);
            j.put("protection_reset_count",protectionResetCount).put("requested_gain_setpoint",userGain).put("applied_user_gain_setpoint",appliedUserGain).put("protection_required",true).put("protection_ready",protection.healthy(now())).put("protection_latched",protection.latched())
                .put("protection_gain_cap",protection.gainCap()).put("protection_level",protection.level()).put("protection_reason",protection.reason()).put("protection_sequence",protection.sequence())
                .put("protection_monitor_token",protection.monitorPid()).put("protection_frames",protection.frames()).put("protection_wet",protection.wet()).put("protection_audio_verified",false);
            j.put("zero_output_carrier",carrier==null?carrierFinal:carrier.metrics()).put("carrier_before_vendor_start",carrierBeforeStart).put("carrier_lease_id",carrierLease).put("carrier_resources_pending",carrierGate.occupied()).put("carrier_start_gate_required",true).put("stop_order","final_vendor_stop_and_disable_attempt_then_carrier_release").put("ordinary_zero_samples_saved",false);
            if(history){JSONArray samples=new JSONArray();for(String row:carrierSamples)samples.put(new JSONObject(row));j.put("events",new JSONArray(events)).put("vendor_calls",commandRows).put("carrier_samples",samples);}
            ProbePolicy.Target t=policy.target;if(t!=null)j.put("input_id",t.inputId).put("card",t.card).put("device",t.device).put("usb_vendor",t.vendor).put("usb_product",t.product).put("usb_device_class",t.usbClass);
            ProbePolicy.Target seen=observedTarget;if(seen!=null)j.put("observed_input_id",seen.inputId).put("observed_card",seen.card).put("observed_device",seen.device)
                .put("observed_usb_count",seen.usbCount).put("observed_usb_matches",seen.usbMatches).put("observed_usb_device_class",seen.usbClass).put("observed_target_valid",seen.valid());
            return j.toString();
        }catch(JSONException failure){return "{\"schema\":1,\"receipt_error\":true}";}}
    }
    @Override public void onDestroy(){
        serviceEnding=true;main.removeCallbacks(protectionWatchdog);foreground=false;synchronized(policy){policy.foregroundReady=false;}
        requestStop(ProbePolicy.SERVICE_END);audio.unregisterAudioDeviceCallback(devices);
        // Do not shut down queued STOP work; Android can still kill this guard, which is an explicit limitation.
        if(wake.isHeld())wake.release();super.onDestroy();
    }
}
