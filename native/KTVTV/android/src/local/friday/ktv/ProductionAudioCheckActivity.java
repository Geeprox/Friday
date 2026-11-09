package local.friday.ktv;

import android.Manifest;
import android.app.Activity;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.File;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/** Explicit, zero-output check of the real AudioEngine chain. No recorded PCM or network. */
public final class ProductionAudioCheckActivity extends Activity {
    public static final String[] METRICS=ProductionCheckGate.KEYS;
    private static final int PERMISSION=74;
    private final Handler ui=new Handler(Looper.getMainLooper());
    private AudioManager audio;
    private SharedPreferences prefs;
    private TextView status;
    private Button start;
    private volatile boolean foreground;
    private volatile Session active;
    private long sequence;
    private static final class Session {
        final long id,prepareDeadlineMs;
        final int inputId,outputId;
        final AtomicBoolean cancelled=new AtomicBoolean();
        final Object finishLock=new Object();
        volatile AudioEngine engine;
        volatile long startedMs;
        volatile boolean stopping,completed,engineEnded;
        volatile int cancelReason,result=ProductionCheckGate.RUNNING;
        volatile int engineEndCode;
        Session(long id,int inputId,int outputId){this.id=id;this.inputId=inputId;this.outputId=outputId;
            prepareDeadlineMs=SystemClock.elapsedRealtime()+ProductionCheckGate.PREPARE_MS;}
    }
    @Override public void onCreate(Bundle saved){
        super.onCreate(saved);
        audio=(AudioManager)getSystemService(AUDIO_SERVICE);
        prefs=getSharedPreferences("ktv-private",MODE_PRIVATE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout panel=new LinearLayout(this);panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER);panel.setPadding(48,32,48,32);panel.setBackgroundColor(0xff111513);
        TextView title=new TextView(this);title.setText("演唱通道检查");title.setTextSize(32);title.setTextColor(0xfff8f7f2);panel.addView(title);
        status=new TextView(this);status.setText("全程零输出 · 最多 15 秒\n检查麦克风与电视扬声器连接，不播放歌曲或人声\n不保存麦克风音频，也不测量实际演唱延迟");
        status.setTextSize(20);status.setTextColor(0xffd2d7d2);status.setPadding(0,24,0,24);panel.addView(status);
        start=new Button(this);start.setText("开始检查");start.setOnClickListener(v->begin());panel.addView(start);
        Button back=new Button(this);back.setText("返回");back.setOnClickListener(v->{cancel(active,ProductionCheckGate.CANCEL_USER);finish();});panel.addView(back);
        setContentView(panel);start.requestFocus();audio.registerAudioDeviceCallback(devices,ui);
    }
    private AudioDeviceInfo[] selectedDevices(){
        AudioDeviceInfo input=null,output=null;int inputs=0,outputs=0;
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_INPUTS))if(d.isSource()
                &&(d.getType()==AudioDeviceInfo.TYPE_USB_DEVICE||d.getType()==AudioDeviceInfo.TYPE_USB_HEADSET)){input=d;inputs++;}
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(d.isSink()
                &&d.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER){output=d;outputs++;}
        return inputs==1&&outputs==1?new AudioDeviceInfo[]{input,output}:null;
    }
    private boolean selectionUnchanged(Session s){
        AudioDeviceInfo[] selected=selectedDevices();
        return selected!=null&&selected[0].getId()==s.inputId&&selected[1].getId()==s.outputId;
    }
    private void begin(){
        Session previous=active;
        if(!foreground||!hasWindowFocus()||previous!=null&&!previous.completed)return;
        if(checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},PERMISSION);
            status.setText("允许麦克风权限后，按“开始检查”\n检查全程零输出，不保存麦克风音频");return;
        }
        AudioDeviceInfo[] selected=selectedDevices();
        if(selected==null){status.setText("请连接一个 USB 麦克风，并使用电视内置扬声器后重试");return;}
        Session s=new Session(++sequence,selected[0].getId(),selected[1].getId());active=s;
        save(ProductionCheckGate.receipt(ProductionCheckGate.RUNNING,0,0,null,null,s.inputId,s.outputId,false,false,0));
        start.setEnabled(false);status.setText("正在准备 · 全程零输出");
        ui.postDelayed(()->watchDeadline(s),ProductionCheckGate.PREPARE_MS);
        new Thread(()->runCheck(s,selected[0]),"ktv-production-check").start();
    }
    /** UI watchdog is independent of the worker's own deadline checks. */
    private void watchDeadline(Session s){
        if(active!=s||s.completed)return;
        long deadline=s.startedMs==0?s.prepareDeadlineMs:s.startedMs+ProductionCheckGate.CHECK_MS;
        long remaining=deadline-SystemClock.elapsedRealtime();
        if(remaining<=0){cancel(s,ProductionCheckGate.CANCEL_DEADLINE);return;}
        ui.postDelayed(()->watchDeadline(s),remaining);
    }
    private boolean unavailable(Session s){return s.cancelled.get()||!foreground||active!=s;}
    private void runCheck(Session s,AudioDeviceInfo input){
        File wave=null;AudioEngine engine=null;ProductionCheckGate.Snapshot baseline=null,last=null;
        int result=ProductionCheckGate.PREPARE_FAILED,failures=0;
        boolean beforeStop=false,stopped=false;
        try{
            wave=File.createTempFile("production-check-",".wav",getCacheDir());
            long remaining=s.prepareDeadlineMs-SystemClock.elapsedRealtime();
            ProductionCheckGate.writeSilentWave(wave,System.nanoTime()+Math.max(0,remaining)*1000000L,()->unavailable(s));
            if(unavailable(s)||SystemClock.elapsedRealtime()>=s.prepareDeadlineMs)throw new IllegalStateException();
            if(!selectionUnchanged(s)){result=ProductionCheckGate.ROUTE_FAILED;throw new IllegalStateException();}
            engine=new AudioEngine(this,(code,natural)->{
                s.engineEndCode=ProductionCheckGate.endCode(code);
                s.engineEnded=true;
                if(!s.stopping&&!s.completed)cancel(s,ProductionCheckGate.CANCEL_ENGINE);
            },NativeDuplexIO.INPUT_RATE_NATIVE_48K);s.engine=engine;
            engine.mixer.musicGain=0;engine.mixer.gainA=0;engine.mixer.gainB=0;engine.mixer.wet=0;engine.mixer.micMuted=true;
            if(unavailable(s))throw new IllegalStateException();
            s.startedMs=SystemClock.elapsedRealtime();
            engine.start(wave,input,null,0);
            if(unavailable(s)){engine.stop();throw new IllegalStateException();}
            result=ProductionCheckGate.GATE_FAILED;
            while(!unavailable(s)){
                long elapsed=SystemClock.elapsedRealtime()-s.startedMs;
                if(elapsed>=ProductionCheckGate.CHECK_MS){result=ProductionCheckGate.DEADLINE;break;}
                if(!selectionUnchanged(s)){result=ProductionCheckGate.ROUTE_FAILED;break;}
                engine.checkNativeRoute(); // Refresh the cached raw platform routes at 200 ms.
                last=snapshot(engine,SystemClock.elapsedRealtime()-s.startedMs);
                if(s.engineEnded||!last.running){result=ProductionCheckGate.ENGINE_FAILED;break;}
                if(!last.silent||last.outputPeak!=0){failures|=ProductionCheckGate.NOT_SILENT;break;}
                if(ProductionCheckGate.metric(last,14)>0||last.inputFault!=0||last.safetyReason!=0){
                    result=ProductionCheckGate.ENGINE_FAILED;failures|=ProductionCheckGate.ERROR;break;
                }
                if(baseline==null){
                    if(last.elapsedMs>=ProductionCheckGate.WARMUP_MS
                            &&ProductionCheckGate.readyFailures(last,s.inputId,s.outputId)==0)baseline=last;
                    else if(last.elapsedMs>=ProductionCheckGate.LAST_BASELINE_MS){
                        failures|=ProductionCheckGate.readyFailures(last,s.inputId,s.outputId)|ProductionCheckGate.DURATION;break;
                    }
                }else{
                    boolean finalSample=last.elapsedMs-baseline.elapsedMs>=ProductionCheckGate.STABLE_MS
                            &&ProductionCheckGate.metric(last,25)-ProductionCheckGate.metric(baseline,25)>=ProductionCheckGate.STABLE_MS;
                    failures|=ProductionCheckGate.failures(baseline,last,s.inputId,s.outputId,finalSample);
                    if(failures!=0)break;
                    if(finalSample){result=ProductionCheckGate.PASS;break;}
                }
                final long remainingMs=ProductionCheckGate.CHECK_MS-last.elapsedMs;
                ui.post(()->{if(active==s&&!s.completed&&!s.cancelled.get())status.setText("正在检查 · 全程零输出 · "+Math.max(1,(remainingMs+999)/1000)+" 秒");});
                Thread.sleep(ProductionCheckGate.SAMPLE_MS);
            }
        }catch(Exception failure){
            if(s.startedMs!=0&&result!=ProductionCheckGate.ROUTE_FAILED)result=ProductionCheckGate.ENGINE_FAILED;
        }finally{
            if(engine!=null){
                // Preserve values before our stop can clear queues or catch counters up.
                ProductionCheckGate.Snapshot finalSample=snapshot(engine,s.startedMs==0?0:SystemClock.elapsedRealtime()-s.startedMs);
                if(last==null||finalSample.running){last=finalSample;beforeStop=finalSample.running;}
                if(result==ProductionCheckGate.PASS){
                    failures|=ProductionCheckGate.failures(baseline,last,s.inputId,s.outputId,true);
                    if(!beforeStop)failures|=ProductionCheckGate.NOT_RUNNING;
                }
                s.stopping=true;engine.stop();
                try{engine.awaitStopped();stopped=true;}catch(Exception failure){stopped=false;}
                // Failed open/start information may arrive while stop is joining render.
                // Preserve it separately without replacing the before-stop queue evidence.
                if(last!=null){last.duplexAttempted=engine.duplexAttempted;last.duplexOpenCode=engine.duplexOpenCode;
                    last.duplexStartCode=engine.duplexStartCode;last.engineEndCode=s.engineEndCode;}
            }else stopped=true;
            if(wave!=null&&!wave.delete()&&wave.exists())result=ProductionCheckGate.PREPARE_FAILED;
            synchronized(s.finishLock){
                if(!foreground||active!=s){s.cancelled.set(true);if(s.cancelReason==0)s.cancelReason=ProductionCheckGate.CANCEL_BACKGROUND;}
                if(s.startedMs!=0&&SystemClock.elapsedRealtime()-s.startedMs>=ProductionCheckGate.CHECK_MS
                        &&s.cancelReason==0){s.cancelReason=ProductionCheckGate.CANCEL_DEADLINE;result=ProductionCheckGate.DEADLINE;}
                result=ProductionCheckGate.finalResult(result,s.cancelled.get(),stopped,failures);
                if(s.cancelReason==ProductionCheckGate.CANCEL_DEADLINE)result=ProductionCheckGate.DEADLINE;
                if(s.cancelReason==ProductionCheckGate.CANCEL_ENGINE)result=ProductionCheckGate.ENGINE_FAILED;
                s.result=result;s.completed=true;
                save(ProductionCheckGate.receipt(result,s.cancelReason,failures,baseline,last,s.inputId,s.outputId,beforeStop,stopped,System.currentTimeMillis()/1000));
            }
            ui.post(()->{
                if(active!=s||isFinishing())return;start.setEnabled(foreground);
                status.setText(s.result==ProductionCheckGate.PASS?"静音通道检查通过\n仅验证供帧与混音稳定，未测量实际演唱延迟":
                        s.result==ProductionCheckGate.CANCELLED?"检查已取消，结果无效":
                        s.result==ProductionCheckGate.ROUTE_FAILED?"音频连接已变化，检查未通过":
                        s.result==ProductionCheckGate.DEADLINE?"检查已到时停止，结果未通过":"演唱通道未通过静音检查，可返回后重试");
            });
        }
    }
    private static ProductionCheckGate.Snapshot snapshot(AudioEngine engine,long elapsed){
        ProductionCheckGate.Snapshot s=new ProductionCheckGate.Snapshot();
        long[] published=engine.nativeMetrics;s.nativeMetrics=published==null?new long[0]:published.clone();
        s.elapsedMs=elapsed;s.running=engine.running;s.backend=engine.audioBackend;
        s.inputRoute=engine.inputDevice;s.outputRoute=engine.outputDevice;s.inputRate=engine.inputSampleRate;s.outputRate=engine.outputSampleRate;
        s.captured=engine.capturedFrames;s.rendered=engine.renderedFrames;s.missing=engine.missingMicFrames;
        s.dropped=engine.droppedMicFrames;s.rebuffers=engine.micRebuffers;s.outputPeak=engine.mixedPeak;
        s.target=engine.micTargetFrames;s.queue=engine.queuedMicFrames;s.queuePeak=engine.micPeakQueueFrames;
        s.outputQueue=engine.outputQueuedFrames;s.dspNanos=engine.dspNanosMax;s.readP95Us=engine.inputReadP95Us;s.ratePpm=engine.micRatePpm;
        s.inputFault=engine.lastInputFault;s.safetyReason=engine.mixer.safety.reason;s.inputReady=engine.lastInput.ready;
        s.inputPeak=engine.micPeak;s.outputType=engine.outputType;
        s.silent=engine.mixer.musicGain==0&&engine.mixer.gainA==0&&engine.mixer.gainB==0&&engine.mixer.wet==0&&engine.mixer.micMuted;
        return s;
    }
    private void save(Map<String,Long> values){
        SharedPreferences.Editor edit=prefs.edit();for(String key:METRICS)edit.putLong(key,values.get(key));edit.apply();
    }
    private void cancel(Session s,int reason){
        if(s==null)return;
        // An interrupted run cannot later pass. A completed receipt remains history;
        // leaving this page only invalidates its live-page flag, never unlocks audio.
        synchronized(s.finishLock){
            if(s.completed)prefs.edit().putLong("production_check_valid",0).apply();
            else{s.cancelled.set(true);if(s.cancelReason==0)s.cancelReason=reason;}
        }
        AudioEngine engine=s.engine;if(engine!=null)engine.stop();
    }
    private final AudioDeviceCallback devices=new AudioDeviceCallback(){
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] changed){cancel(active,ProductionCheckGate.CANCEL_ROUTE);}
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] changed){cancel(active,ProductionCheckGate.CANCEL_ROUTE);}
    };
    @Override public void onWindowFocusChanged(boolean hasFocus){super.onWindowFocusChanged(hasFocus);if(!hasFocus)cancel(active,ProductionCheckGate.CANCEL_FOCUS);}
    @Override protected void onResume(){super.onResume();foreground=true;if(start!=null)start.setEnabled(active==null||active.completed);}
    @Override protected void onPause(){foreground=false;cancel(active,ProductionCheckGate.CANCEL_BACKGROUND);super.onPause();}
    @Override protected void onStop(){foreground=false;cancel(active,ProductionCheckGate.CANCEL_BACKGROUND);super.onStop();}
    @Override protected void onDestroy(){cancel(active,ProductionCheckGate.CANCEL_BACKGROUND);audio.unregisterAudioDeviceCallback(devices);super.onDestroy();}
    @Override public void onBackPressed(){cancel(active,ProductionCheckGate.CANCEL_USER);super.onBackPressed();}
}
