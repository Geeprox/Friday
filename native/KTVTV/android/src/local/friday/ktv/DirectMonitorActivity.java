package local.friday.ktv;

import android.Manifest;
import android.app.Activity;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Local, explicit, bounded monitor; no media files, network or recordings. */
public final class DirectMonitorActivity extends Activity {
    public static final String[] METRICS={"direct_monitor_input_frames","direct_monitor_output_frames",
        "direct_monitor_voice_frames","direct_monitor_input_dropped","direct_monitor_trimmed",
        "direct_monitor_missing","direct_monitor_peak","direct_monitor_native_error",
        "direct_monitor_output_performance","direct_monitor_input_performance",
        "direct_monitor_output_buffer","direct_monitor_input_buffer",
        "direct_monitor_error_stream","direct_monitor_quiet","direct_monitor_gap_max_us",
        "direct_monitor_callback_count","direct_monitor_expired","direct_monitor_input_peak","direct_monitor_output_only",
        "direct_monitor_mode","direct_monitor_output_api_actual","direct_monitor_input_api_actual",
        "direct_monitor_output_mmap","direct_monitor_input_mmap","direct_monitor_output_sharing","direct_monitor_input_sharing",
        "direct_monitor_output_state","direct_monitor_input_state","direct_monitor_output_route","direct_monitor_input_route",
        "direct_monitor_output_capacity","direct_monitor_input_capacity","direct_monitor_output_burst","direct_monitor_input_burst",
        "direct_monitor_output_frames_written","direct_monitor_output_frames_read","direct_monitor_input_frames_written","direct_monitor_input_frames_read",
        "direct_monitor_output_timestamp_available","direct_monitor_output_timestamp_error","direct_monitor_output_timestamp_position","direct_monitor_output_timestamp_age_us","direct_monitor_output_pending_frames",
        "direct_monitor_input_timestamp_available","direct_monitor_input_timestamp_error","direct_monitor_input_timestamp_position","direct_monitor_input_timestamp_age_us","direct_monitor_input_pending_frames",
        "direct_monitor_snapshot_elapsed_ms","direct_monitor_snapshot_final","direct_monitor_last_callback_age_us",
        "direct_monitor_mmap_policy_available","direct_monitor_mmap_policy_before","direct_monitor_mmap_policy_set_error","direct_monitor_mmap_policy_restored",
        "direct_monitor_output_pending_available","direct_monitor_input_pending_available","direct_monitor_output_timestamp_future","direct_monitor_input_timestamp_future",
        "direct_monitor_output_sample_rate_actual","direct_monitor_input_sample_rate_actual",
        "direct_monitor_steady_start_ms","direct_monitor_startup_discarded_frames",
        "direct_monitor_steady_input_frames","direct_monitor_steady_output_frames","direct_monitor_steady_missing_frames",
        "direct_monitor_steady_gap_max_us","direct_monitor_steady_short_read_callbacks",
        "direct_monitor_input_observed_samples","direct_monitor_input_full_scale_samples","direct_monitor_input_full_scale_run_max_frames",
        "direct_monitor_audible_output_frames","direct_monitor_output_limited_frames","direct_monitor_output_limited_run_max_frames",
        "direct_monitor_audible_input_mono_abs_sum","direct_monitor_output_abs_sum","direct_monitor_steady_poll_count",
        "direct_monitor_output_pending_valid_count","direct_monitor_output_pending_min_us","direct_monitor_output_pending_max_us","direct_monitor_output_pending_last_us",
        "direct_monitor_input_pending_valid_count","direct_monitor_input_pending_min_us","direct_monitor_input_pending_max_us","direct_monitor_input_pending_last_us",
        "direct_monitor_output_counter_min_us","direct_monitor_output_counter_max_us","direct_monitor_input_counter_min_us","direct_monitor_input_counter_max_us",
        "direct_monitor_output_xrun_available","direct_monitor_output_xrun_delta","direct_monitor_input_xrun_available","direct_monitor_input_xrun_delta",
        "direct_monitor_clock_span_ms","direct_monitor_input_produced_frames_delta","direct_monitor_output_presented_frames_delta","direct_monitor_legacy_gain_would_limit_frames"};
    public static final String[] EXTRA={"direct_monitor_result","direct_monitor_finished_at",
        "direct_monitor_output_type","direct_monitor_elapsed_ms","direct_monitor_output_api","direct_monitor_drop_counts_available",
        "direct_monitor_baseline_input_frames","direct_monitor_baseline_output_frames","direct_monitor_baseline_voice_frames",
        "direct_monitor_baseline_missing","direct_monitor_baseline_elapsed_ms","direct_monitor_gate_failures"};
    private final Handler ui=new Handler(Looper.getMainLooper());
    private AudioManager audio;private SharedPreferences prefs;
    private TextView status;private Button start,check,outputCheck,channel;
    private final DirectMonitorGate gate=new DirectMonitorGate();
    private static final String[] CHANNELS={"标准","兼容","独占"};
    private volatile boolean cancelled,running,foreground;
    private volatile NativeVoiceMonitor active;
    private long sequence;
    @Override public void onCreate(Bundle state){
        super.onCreate(state);audio=(AudioManager)getSystemService(AUDIO_SERVICE);
        prefs=getSharedPreferences("ktv-private",MODE_PRIVATE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout panel=new LinearLayout(this);panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER);panel.setPadding(48,32,48,32);panel.setBackgroundColor(0xff111513);
        TextView title=new TextView(this);title.setText("人声试听");title.setTextSize(32);title.setTextColor(0xfff8f7f2);panel.addView(title);
        status=new TextView(this);status.setText("仅使用电视扬声器 · 15 秒后停止");status.setTextSize(20);status.setTextColor(0xffd2d7d2);status.setPadding(0,24,0,24);panel.addView(status);
        channel=new Button(this);channel.setText("音频通道："+CHANNELS[gate.mode()]);channel.setOnClickListener(v->{if(running)return;gate.selectMode((gate.mode()+1)%DirectMonitorGate.MODE_COUNT);start.setEnabled(false);channel.setText("音频通道："+CHANNELS[gate.mode()]);status.setText("通道已更换，请重新检查输出和连接");});panel.addView(channel);
        outputCheck=new Button(this);outputCheck.setText("检查输出");outputCheck.setOnClickListener(v->begin(true,true));panel.addView(outputCheck);
        check=new Button(this);check.setText("检查连接");check.setOnClickListener(v->begin(true,false));panel.addView(check);
        start=new Button(this);start.setText("开始试听");start.setEnabled(false);start.setOnClickListener(v->begin(false,false));panel.addView(start);
        Button back=new Button(this);back.setText("返回");back.setOnClickListener(v->finish());panel.addView(back);
        setContentView(panel);outputCheck.requestFocus();audio.registerAudioDeviceCallback(devices,ui);
    }
    private void begin(boolean quiet,boolean outputOnly){
        if(!foreground||running||!quiet&&!gate.ready())return;
        if(!outputOnly&&checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},72);status.setText("允许麦克风后，再按开始试听");return;}
        AudioDeviceInfo input=null,speaker=null;int count=0,speakers=0;
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_INPUTS))if(d.getType()==AudioDeviceInfo.TYPE_USB_DEVICE||d.getType()==AudioDeviceInfo.TYPE_USB_HEADSET){input=d;count++;}
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(d.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER){speaker=d;speakers++;}
        if(!outputOnly&&count!=1||speakers!=1){status.setText("请连接一个 USB 麦克风，并使用电视扬声器后重试");return;}
        int channels=outputOnly?2:0;if(!outputOnly)for(int c:input.getChannelCounts())if(c==2)channels=2;else if(c==1&&channels==0)channels=1;
        if(channels==0){status.setText("暂不支持这个麦克风，请重新连接后重试");return;}
        final int inputId=outputOnly?0:input.getId(),outputId=speaker.getId(),inputChannels=channels;
        running=true;cancelled=false;start.setEnabled(false);check.setEnabled(false);outputCheck.setEnabled(false);channel.setEnabled(false);status.setText("正在准备…");final long run=++sequence,checkedRevision=gate.revision();final int mode=gate.mode();
        SharedPreferences.Editor clear=prefs.edit();for(String key:METRICS)clear.putLong(key,0);for(String key:EXTRA)clear.putLong(key,0);clear.apply();
        new Thread(()->runMonitor(inputId,outputId,inputChannels,run,checkedRevision,quiet,outputOnly,mode),"ktv-direct-monitor").start();
    }
    private void runMonitor(int inputId,int outputId,int channels,long run,long checkedRevision,boolean quiet,boolean outputOnly,int mode){
        NativeVoiceMonitor io=null;AudioFocusRequest focus=null;int result=4;long started=0,elapsed=0,baselineMs=0;boolean armed=false;long[] metrics=new long[METRICS.length],baseline=null;metrics[13]=quiet?1:0;metrics[18]=outputOnly?1:0;metrics[19]=mode;
        try{
            focus=new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT).setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()).setOnAudioFocusChangeListener(change->{if(change<0)cancel();},ui).build();
            if(audio.requestAudioFocus(focus)!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED)throw new IllegalStateException();
            io=NativeVoiceMonitor.open(inputId,outputId,channels,quiet,outputOnly,mode);if(io==null){metrics=NativeVoiceMonitor.openMetrics();metrics[7]=NativeVoiceMonitor.openCode();metrics[12]=metrics[7]>=50000?0:metrics[7]>=30000?1:metrics[7]>=10000?2:0;throw new IllegalStateException();}
            active=io;if(cancelled||!foreground){result=2;throw new IllegalStateException();}
            if(!io.start())throw new IllegalStateException();
            long deadline=SystemClock.elapsedRealtime()+1500;int routed=0;
            while(!cancelled&&foreground&&SystemClock.elapsedRealtime()<deadline){metrics=io.monitorMetrics();if(metrics[7]!=0){result=5;throw new IllegalStateException();}routed=io.routeDevice();if(routed>0)break;Thread.sleep(20);}
            if(cancelled||!foreground){result=2;throw new IllegalStateException();}
            if(routed!=outputId){result=3;throw new IllegalStateException();}
            if(!io.armMonitor(outputId))throw new IllegalStateException();
            armed=true;started=SystemClock.elapsedRealtime();result=1;
            while(!cancelled&&foreground&&(elapsed=SystemClock.elapsedRealtime()-started)<15000){
                metrics=io.monitorMetrics();if(metrics[7]!=0){result=5;break;}
                if(io.routeDevice()!=outputId){metrics=io.monitorMetrics();result=metrics[7]!=0?5:3;break;}
                if(quiet&&baseline==null&&elapsed>=1000){baseline=metrics.clone();baselineMs=elapsed;}
                final int remaining=(int)Math.max(1,(15000-elapsed+999)/1000);
                ui.post(()->{if(run==sequence&&running)status.setText((quiet?"正在检查 · ":"正在试听 · ")+remaining+" 秒");});Thread.sleep(200);
            }
            if(cancelled||!foreground)result=2;
        }catch(Exception error){if(cancelled||!foreground)result=2;else if(result==1)result=5;}
        finally{
            if(io!=null){io.cancelMonitor();metrics=io.monitorMetrics();elapsed=started==0?0:Math.min(20000,SystemClock.elapsedRealtime()-started);io.close();}active=null;
            if(focus!=null)audio.abandonAudioFocusRequest(focus);
            if(metrics[7]!=0&&result!=2)result=5;
            if(result==1&&(!armed||!outputOnly&&metrics[2]==0||!quiet&&metrics[6]==0))result=quiet?7:6;
            int gateFailures=quiet?MonitorStability.failures(baseline,metrics,elapsed-baselineMs,elapsed,!outputOnly):0;
            if(result==1&&quiet&&gateFailures!=0)result=7;
            SharedPreferences.Editor saved=prefs.edit();for(int i=0;i<METRICS.length;i++)saved.putLong(METRICS[i],Math.abs(metrics[i]));
            saved.putLong(EXTRA[0],result).putLong(EXTRA[1],System.currentTimeMillis()/1000).putLong(EXTRA[2],armed?AudioDeviceInfo.TYPE_BUILTIN_SPEAKER:0).putLong(EXTRA[3],elapsed).putLong(EXTRA[4],armed?1:0).putLong(EXTRA[5],0);
            int[] baselineIndices={0,1,2,5,48};for(int i=0;i<baselineIndices.length;i++)saved.putLong(EXTRA[6+i],baseline==null?0:baseline[baselineIndices[i]]);
            saved.putLong(EXTRA[11],gateFailures).apply();
            running=false;final int code=result;ui.post(()->{if(run==sequence&&!isFinishing()){if(foreground)gate.completed(checkedRevision,mode,quiet,outputOnly,code==1);check.setEnabled(true);outputCheck.setEnabled(true);channel.setEnabled(true);start.setEnabled(foreground&&gate.ready());status.setText(code==1?(outputOnly?"输出正常，请继续检查连接":quiet?"连接正常，可以开始试听":"试听结束"):code==2?"试听已停止":code==3?"请切换到电视扬声器后重试":code==6?"未收到声音，请取消话筒静音后重试":code==7?"声音通道未通过检查，暂时无法试听":"声音通道不可用，请返回后重试");}});
        }
    }
    private void cancel(){cancelled=true;NativeVoiceMonitor io=active;if(io!=null)io.cancelMonitor();}
    private final AudioDeviceCallback devices=new AudioDeviceCallback(){
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] d){gate.invalidate();start.setEnabled(false);if(running)cancel();}
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] d){gate.invalidate();start.setEnabled(false);if(running)cancel();}
    };
    @Override protected void onStart(){super.onStart();foreground=true;}
    @Override protected void onStop(){foreground=false;gate.invalidate();start.setEnabled(false);cancel();super.onStop();}
    @Override protected void onDestroy(){cancel();audio.unregisterAudioDeviceCallback(devices);super.onDestroy();}
}
