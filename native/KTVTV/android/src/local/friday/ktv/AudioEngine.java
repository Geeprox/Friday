package local.friday.ktv;

import android.content.Context;
import android.media.*;
import android.media.audiofx.*;
import android.os.Process;
import java.io.File;
import java.io.IOException;
import java.util.Arrays;
import java.util.concurrent.atomic.AtomicBoolean;

/** One output stream mixes locally cached backing with a separately clocked bounded USB input. */
public final class AudioEngine {
    public interface Listener {void ended(String code, boolean natural);}
    private final AudioManager manager;
    private final Listener listener;
    private final int inputRateMode;
    private final boolean playbackOnly;
    private final AtomicBoolean stop=new AtomicBoolean();
    private final AtomicBoolean completionSent=new AtomicBoolean();
    private volatile AudioTrack track;
    private volatile AudioRecord record;
    private volatile Thread render,capture;
    private AudioFocusRequest focus;
    public final PcmMixer mixer=new PcmMixer();
    public volatile long renderedFrames,capturedFrames,positionFrames,playheadFrames;
    public volatile int inputDevice=-1,outputDevice=-1,bufferFrames,performanceMode,underruns,micPeak,mixedPeak;
    public volatile long droppedMicFrames,queuedMicFrames;
    public volatile long missingMicFrames,dspNanosMax,outputQueuedFrames,inputAgeNanos;
    public volatile int inputBufferFrames,outputType,performanceModeActual,inputPeakA,inputPeakB;
    public volatile boolean running;
    private volatile String captureFault;
    private volatile SwitchingWavSource musicSource;
    private File desiredBacking;
    public volatile InputMonitor lastInput=new InputMonitor();
    public volatile int lastInputFault;
    public volatile int inputSource,inputEffectsDisabled,inputEffectsFailed;
    public volatile int backingSwitches,backingFailures;
    public volatile int inputSampleRate,outputSampleRate,micTargetFrames,micPeakQueueFrames,micRatePpm;
    public volatile long micRebuffers,inputAgeMaxNanos,lastSessionInputFrames,lastSessionDropped,lastSessionMissing,lastSessionInputAgeUs;
    public volatile int lastSessionOutputType,lastSessionFault;
    public volatile int inputOpenFailures,inputFallbacks;
    public volatile int inputReadP95Us,outputBufferRequested;
    public volatile boolean inputTimestampAvailable;
    public volatile long[] nativeProbe=new long[16];
    private boolean nativeProbed;
    private volatile AudioBackendIO nativeIO;
    public volatile int audioBackend,lastSessionBackend,nativeOpenFailures;
    /** Published arrays are immutable snapshots; readers can retain one reference. */
    public volatile long[] nativeMetrics=new long[AudioBackendIO.METRIC_COUNT];
    private long nativeMetricsPublishedAt;
    public volatile boolean duplexAttempted;
    public volatile long duplexOpenCode,duplexStartCode;
    public volatile int duplexOpenFailures,duplexStartFailures;
    private final java.util.Set<Long> unsupportedSources=new java.util.HashSet<>();
    public AudioEngine(Context context,Listener listener){this(context,listener,NativeDuplexIO.INPUT_RATE_DEFAULT,false);}
    public AudioEngine(Context context,Listener listener,boolean playbackOnly){this(context,listener,NativeDuplexIO.INPUT_RATE_DEFAULT,playbackOnly);}
    public AudioEngine(Context context,Listener listener,int inputRateMode){this(context,listener,inputRateMode,false);}
    private AudioEngine(Context context,Listener listener,int inputRateMode,boolean playbackOnly){
        if(inputRateMode!=NativeDuplexIO.INPUT_RATE_DEFAULT&&inputRateMode!=NativeDuplexIO.INPUT_RATE_NATIVE_48K)throw new IllegalArgumentException();
        manager=(AudioManager)context.getSystemService(Context.AUDIO_SERVICE);this.listener=listener;this.inputRateMode=inputRateMode;this.playbackOnly=playbackOnly;
    }
    private static boolean usb(AudioDeviceInfo d){return d!=null && d.isSource() && (d.getType()==AudioDeviceInfo.TYPE_USB_DEVICE || d.getType()==AudioDeviceInfo.TYPE_USB_HEADSET);}
    public static boolean wiredOutput(AudioDeviceInfo d){return d!=null && d.isSink() && (d.getType()==AudioDeviceInfo.TYPE_HDMI
            || d.getType()==AudioDeviceInfo.TYPE_HDMI_ARC || d.getType()==AudioDeviceInfo.TYPE_LINE_DIGITAL
            || d.getType()==AudioDeviceInfo.TYPE_LINE_ANALOG || d.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
            || d.getType()==AudioDeviceInfo.TYPE_WIRED_HEADPHONES || d.getType()==AudioDeviceInfo.TYPE_WIRED_HEADSET);}
    private boolean present(AudioDeviceInfo d,int flags){for(AudioDeviceInfo current:manager.getDevices(flags))if(current.getId()==d.getId() && current.getType()==d.getType())return true;return false;}
    private AudioRecord openInput(AudioDeviceInfo input,int channels,int mask,int minimum)throws IOException{
        int basic="true".equals(manager.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED))?MediaRecorder.AudioSource.UNPROCESSED:MediaRecorder.AudioSource.VOICE_RECOGNITION;
        int[] sources=android.os.Build.VERSION.SDK_INT>=29?new int[]{MediaRecorder.AudioSource.VOICE_PERFORMANCE,MediaRecorder.AudioSource.UNPROCESSED,basic}:new int[]{MediaRecorder.AudioSource.UNPROCESSED,basic};
        for(int source:sources){
            long key=((long)input.getId()<<32)|source;if(unsupportedSources.contains(key))continue;
            AudioRecord candidate=null;
            for(int bytes:new int[]{channels*960,Math.max(minimum,channels*1920)}){
            try{
                candidate=new AudioRecord.Builder().setAudioSource(source).setAudioFormat(new AudioFormat.Builder().setSampleRate(48000)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(mask).build()).setBufferSizeInBytes(bytes).build();
                if(candidate.getState()!=AudioRecord.STATE_INITIALIZED)throw new IOException("input_unavailable");
                if(!candidate.setPreferredDevice(input))throw new IOException("input_route_refused");
                candidate.startRecording();if(candidate.getRecordingState()!=AudioRecord.RECORDSTATE_RECORDING)throw new IOException("input_unavailable");
                inputSource=source;if(source!=sources[0])inputFallbacks++;return candidate;
            }catch(SecurityException error){if(candidate!=null)candidate.release();throw error;}
            catch(Exception error){inputOpenFailures++;if(candidate!=null){try{candidate.stop();}catch(Exception ignored){}candidate.release();candidate=null;}}
            }
            unsupportedSources.add(key);
        }
        throw new IOException("input_unavailable");
    }
    public synchronized void start(File backing,AudioDeviceInfo input,AudioDeviceInfo output,long seek) throws Exception {
        if(playbackOnly&&input!=null)throw new IOException("separate_microphone_owner");
        if(playbackOnly)mixer.micMuted=true;
        if(!released())throw new IOException("audio_busy");
        if(output!=null && (!output.isSink() || !present(output,AudioManager.GET_DEVICES_OUTPUTS)))throw new IOException("output_selection_required");
        int channels=0;
        if(input!=null){
            if(!usb(input) || !present(input,AudioManager.GET_DEVICES_INPUTS))throw new IOException("usb_input_required");
            for(int n:input.getChannelCounts())if(n==2)channels=2;
            if(channels==0)for(int n:input.getChannelCounts())if(n==1)channels=1;
            if(channels==0)throw new IOException("input_channels_unknown");
        }
        desiredBacking=backing;musicSource=null;
        final int micChannels=channels;stop.set(false);completionSent.set(false);renderedFrames=0;capturedFrames=0;micPeak=mixedPeak=0;
        // Publish the requested clock before the worker opens streams; the UI
        // must not scroll new lyrics using the previous song's playhead.
        positionFrames=playheadFrames=seek;
        duplexAttempted=false;duplexOpenCode=duplexStartCode=0;audioBackend=0;
        long[] emptyMetrics=new long[AudioBackendIO.METRIC_COUNT];emptyMetrics[12]=emptyMetrics[13]=-1;nativeMetrics=emptyMetrics;nativeMetricsPublishedAt=0;
        inputTimestampAvailable=false;inputDevice=-1;outputDevice=-1;inputBufferFrames=0;droppedMicFrames=0;queuedMicFrames=0;missingMicFrames=0;dspNanosMax=0;outputQueuedFrames=0;inputAgeNanos=0;inputPeakA=inputPeakB=0;captureFault=null;
        focus=new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN).setAudioAttributes(new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setOnAudioFocusChangeListener(change->{if(change<0)stop();}).build();
        if(manager.requestAudioFocus(focus)!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED){focus=null;throw new IOException("audio_focus_unavailable");}
        render=new Thread(()->render(backing,input,output,micChannels,seek),"ktv-render");running=true;
        try{render.start();}catch(RuntimeException error){running=false;releaseFocus();throw error;}
    }
    private synchronized void releaseFocus(){AudioFocusRequest current=focus;focus=null;if(current!=null)manager.abandonAudioFocusRequest(current);}
    private void completed(String code,boolean natural){if(completionSent.compareAndSet(false,true)){releaseFocus();running=false;listener.ended(code,natural);}}
    private int duplexOutput(AudioDeviceInfo input,AudioDeviceInfo selectedOutput){
        int inputs=0,inputId=0,speakers=0,speakerId=0;
        for(AudioDeviceInfo device:manager.getDevices(AudioManager.GET_DEVICES_INPUTS))if(usb(device)){inputs++;inputId=device.getId();}
        for(AudioDeviceInfo device:manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(device.isSink()&&device.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER){speakers++;speakerId=device.getId();}
        return AudioBackendPolicy.duplexOutput(inputs,inputId,input.getId(),speakers,speakerId,selectedOutput==null?0:selectedOutput.getId());
    }
    private void render(File backing,AudioDeviceInfo input,AudioDeviceInfo output,int channels,long seek){
        try{
            if(stop.get()){completed("stopped",false);return;}
            if(input!=null){int expectedOutput=duplexOutput(input,output);if(expectedOutput>0&&renderNative(backing,input,channels,seek,2,expectedOutput))return;}
            if(stop.get()){completed("stopped",false);return;}
            // An explicit diagnostic must fail with its own evidence. It cannot
            // become a different capture backend after a failed strict open.
            if(inputRateMode==NativeDuplexIO.INPUT_RATE_NATIVE_48K){completed("strict_input_unavailable",false);return;}
            if(input!=null&&output==null&&renderNative(backing,input,channels,seek,1,0))return;
            if(stop.get()){completed("stopped",false);return;}
            renderJava(backing,input,output,channels,seek);
        }catch(Exception error){stop.set(true);completed("audio_unavailable",false);}
        finally{releaseFocus();running=false;}
    }
    private boolean renderNative(File backing,AudioDeviceInfo input,int channels,long seek,int backend,int expectedOutput){
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
        AudioBackendIO io;
        if(backend==2){
            duplexAttempted=true;io=NativeDuplexIO.open(input.getId(),expectedOutput,channels,inputRateMode);duplexOpenCode=Math.abs((long)NativeDuplexIO.openCode());
            if(io==null){duplexOpenFailures++;publishNativeMetrics(NativeDuplexIO.openMetrics(),true);return false;}
        }else{
            int burst=240;try{burst=Integer.parseInt(manager.getProperty(AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER));}catch(Exception ignored){}
            io=NativeAudioIO.open(input.getId(),channels,burst);if(io==null){nativeOpenFailures++;return false;}
        }
        nativeIO=io;
        if(stop.get()){io.stop();io.close();nativeIO=null;completed("stopped",false);return true;}
        boolean started=false,startException=false;
        try{started=AudioBackendIO.startOrClose(io);}
        catch(RuntimeException error){startException=true;if(backend==2)duplexStartFailures++;else nativeOpenFailures++;throw error;}
        finally{if(backend==2){duplexStartCode=Math.abs((long)((NativeDuplexIO)io).startCode());if(startException&&duplexStartCode==0)duplexStartCode=9003;}if(!started)nativeIO=null;}
        if(!started){if(backend==2)publishNativeMetrics(NativeDuplexIO.openMetrics(),true);if(stop.get()){completed("stopped",false);return true;}if(backend==2)duplexStartFailures++;else nativeOpenFailures++;return false;}
        audioBackend=backend;String code="stopped";boolean natural=false;LiveVoiceBuffer ring=null;
        short[] song=new short[480],mic=new short[240*channels],mixed=new short[480];long[] metrics=new long[AudioBackendIO.METRIC_COUNT];
        mixer.reset();inputSource=MediaRecorder.AudioSource.UNPROCESSED;inputEffectsDisabled=inputEffectsFailed=0;inputReadP95Us=0;
        inputAgeMaxNanos=0;micRebuffers=0;micPeakQueueFrames=micRatePpm=0;
        try(SwitchingWavSource music=new SwitchingWavSource(backing)){
            installMusicSource(music,backing);music.seek(seek);positionFrames=playheadFrames=seek;io.metrics(metrics);
            inputBufferFrames=(int)metrics[5];bufferFrames=(int)metrics[4];outputBufferRequested=backend==2?0:bufferFrames;
            inputSampleRate=(int)metrics[17];outputSampleRate=(int)metrics[16];performanceModeActual=performanceMode=metrics[8]==(backend==2?12:1)?1:0;
            final int outputDemand=AudioBackendPolicy.outputDemandFrames(backend,metrics);
            ring=new LiveVoiceBuffer((int)metrics[7],outputDemand,channels);ring.configureNativeStartup((int)metrics[7],outputDemand);micTargetFrames=ring.targetFrames;publishNativeMetrics(metrics,true);
            final LiveVoiceBuffer captureRing=ring;capture=new Thread(()->captureNative(io,input,channels,captureRing,backend,outputDemand),"ktv-native-capture");capture.start();
            long prefill=System.nanoTime()+450000000L;while(!stop.get()&&ring.queuedFrames()<ring.targetFrames&&System.nanoTime()<prefill)Thread.sleep(1);
            io.routeDevice();io.metrics(metrics);outputDevice=(int)metrics[2];AudioDeviceInfo routed=null;for(AudioDeviceInfo device:manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(device.getId()==outputDevice)routed=device;
            if(routed==null)throw new IOException("output_route_unavailable");if(backend==2&&(routed.getId()!=expectedOutput||routed.getType()!=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER))throw new IOException("output_route_mismatch");outputType=routed.getType();
            long routeGraceUntil=System.nanoTime()/1000000+AudioBackendPolicy.PLATFORM_FRESHNESS_MILLIS;
            while(!stop.get()){
                int frames=music.read(song,240);backingSwitches=music.switches;backingFailures=music.failures;if(frames==0){natural=true;code="completed";break;}
                ring.take(mic,240);queuedMicFrames=ring.queuedFrames();missingMicFrames=ring.missingFrames;micRebuffers=ring.rebufferCount;micPeakQueueFrames=ring.peakFrames;micRatePpm=ring.ratePpm;
                long dspStarted=System.nanoTime();mixer.mix(song,mic,channels,frames,mixed);observeMixedPeak(mixed,frames);dspNanosMax=Math.max(dspNanosMax,System.nanoTime()-dspStarted);
                int written=0;while(written<frames*2&&!stop.get()){int n=io.write(mixed,written,frames*2-written);if(n<=0||n>frames*2-written||n%2!=0)throw new IOException("output_write_failed");written+=n;}
                renderedFrames+=written/2;positionFrames=music.position;io.metrics(metrics);publishNativeMetrics(metrics,false);
                if(metrics[2]!=outputDevice)throw new IOException("output_route_changed");if(metrics[3]!=input.getId())throw new IOException("input_route_changed");
                if(backend==2&&!AudioBackendPolicy.platformFresh(metrics[25],System.nanoTime()/1000000,routeGraceUntil))throw new IOException("audio_route_stale");
                playheadFrames=seek+Math.min(renderedFrames,metrics[0]);outputQueuedFrames=metrics[1];underruns=(int)Math.max(0,metrics[12]);droppedMicFrames=ring.droppedFrames+(backend==2?0:metrics[10]);
                if(capturedFrames==0&&System.nanoTime()>prefill+2000000000L)throw new IOException("no_microphone_frames");
            }
            if(natural){long deadline=System.nanoTime()+500000000L;while(!stop.get()&&System.nanoTime()<deadline){io.metrics(metrics);if(metrics[0]>=renderedFrames)break;Thread.sleep(2);}}
        }catch(Exception error){if(!stop.get()){String message=error.getMessage();code=message!=null&&message.matches("[a-z_]{1,50}")?message:"audio_unavailable";}}
        finally{
            natural=natural&&!stop.get();stop.set(true);io.stop();if(capture!=null)try{capture.join(1500);}catch(InterruptedException ignored){Thread.currentThread().interrupt();}
            if(capture!=null&&capture.isAlive())captureFault="input_stop_timeout";
            io.metrics(metrics);publishNativeMetrics(metrics,true);
            lastSessionBackend=backend;lastSessionInputFrames=capturedFrames;lastSessionDropped=droppedMicFrames;lastSessionMissing=missingMicFrames;lastSessionInputAgeUs=inputAgeMaxNanos/1000;lastSessionOutputType=outputType;lastSessionFault=lastInputFault!=0?3:mixer.safety.reason;
            io.close();nativeIO=null;if(ring!=null)ring.clear();queuedMicFrames=0;Arrays.fill(song,(short)0);Arrays.fill(mic,(short)0);Arrays.fill(mixed,(short)0);mixer.clearVoice();
            if(captureFault!=null){code=captureFault;natural=false;}musicSource=null;completed(code,natural&&code.equals("completed"));
        }return true;
    }
    private void publishNativeMetrics(long[] metrics,boolean force){
        long now=System.nanoTime();if(force||now-nativeMetricsPublishedAt>=100000000L){nativeMetrics=metrics.clone();nativeMetricsPublishedAt=now;}
    }
    private void observeMixedPeak(short[] mixed,int frames){int peak=mixedPeak;for(int i=0;i<frames*2;i++)peak=Math.max(peak,Math.abs((int)mixed[i]));mixedPeak=peak;}
    public void checkNativeRoute(){AudioBackendIO io=nativeIO;if(io!=null&&outputDevice>0){int current=io.routeDevice();if(AudioBackendPolicy.routeChanged(audioBackend,current,outputDevice)){captureFault="output_route_changed";stop();}}}
    private void captureNative(AudioBackendIO io,AudioDeviceInfo input,int channels,LiveVoiceBuffer ring,int backend,int outputDemand){
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);short[] block=new short[240*channels];long[] metrics=new long[AudioBackendIO.METRIC_COUNT];InputMonitor monitor=new InputMonitor();lastInput=monitor;lastInputFault=0;
        long deadline=System.nanoTime()+2000000000L;boolean timingConfigured=false;
        try{while(!stop.get()){
            int n=io.read(block,240);if(n<0){if(!stop.get()){captureFault="input_read_failed";stop.set(true);}break;}
            if(n==0){if(System.nanoTime()>deadline){captureFault="no_microphone_frames";stop.set(true);}if(backend==2)Thread.sleep(1);continue;}
            if(n>block.length||n%channels!=0){captureFault="input_format_unavailable";stop.set(true);break;}
            if(backend==2)deadline=System.nanoTime()+2000000000L;
            io.metrics(metrics);if(metrics[3]!=input.getId()){captureFault="input_route_changed";stop.set(true);break;}inputDevice=input.getId();
            inputAgeNanos=0;inputAgeMaxNanos=0;
            int peak=0;for(int i=0;i<n;i++){int level=Math.abs((int)block[i]);peak=Math.max(peak,level);if(i%channels==0)inputPeakA=Math.max(inputPeakA,level);else inputPeakB=Math.max(inputPeakB,level);}micPeak=Math.max(micPeak,peak);
            monitor.add(block,n,channels);capturedFrames+=n/channels;if(monitor.ready){if(!timingConfigured){inputReadP95Us=(int)Math.min(Integer.MAX_VALUE,Math.max(0,metrics[backend==2?35:19]));ring.configureNativeStartup(AudioBackendPolicy.inputDeliveryFrames(backend,metrics),outputDemand);micTargetFrames=ring.targetFrames;timingConfigured=true;}ring.put(block,n);}else if(monitor.frames>=48000){lastInputFault=1;captureFault="input_clipped";stop.set(true);break;}
        }}catch(Exception error){if(!stop.get()){captureFault="input_read_failed";stop.set(true);}}
        finally{Arrays.fill(block,(short)0);}
    }
    private void renderJava(File backing,AudioDeviceInfo input,AudioDeviceInfo output,int channels,long seek){
        audioBackend=0;
        String code="stopped";boolean natural=false;AudioTrack localTrack=null;AudioRecord localRecord=null;AudioEffect[] effects=new AudioEffect[3];
        LiveVoiceBuffer ring=null;short[] song=new short[480],mic=new short[240*Math.max(1,channels)],mixed=new short[480];
        mixer.reset();
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
        try(SwitchingWavSource music=new SwitchingWavSource(backing)){
            if(input!=null&&!nativeProbed&&mixer.musicGain==0&&mixer.gainA==0&&mixer.gainB==0){nativeProbe=NativeAudioProbe.run(input.getId(),channels);nativeProbed=true;}
            installMusicSource(music,backing);
            music.seek(seek);positionFrames=music.position;playheadFrames=music.position;
            int minimum=AudioTrack.getMinBufferSize(48000,AudioFormat.CHANNEL_OUT_STEREO,AudioFormat.ENCODING_PCM_16BIT);
            if(minimum<=0)throw new IOException("output_format_unavailable");
            localTrack=new AudioTrack.Builder().setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_GAME)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build()).setAudioFormat(new AudioFormat.Builder().setSampleRate(48000)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
                .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(Math.max(minimum,3840))
                .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY).build();
            track=localTrack;
            outputSampleRate=localTrack.getSampleRate();inputSampleRate=0;micRebuffers=0;micTargetFrames=micPeakQueueFrames=micRatePpm=0;inputAgeMaxNanos=0;
            if(output!=null && !localTrack.setPreferredDevice(output))throw new IOException("output_route_refused");
            localTrack.setBufferSizeInFrames(Math.max(480,minimum/4));localTrack.play();
            short[] silence=new short[480];long preflightFrames=0;
            for(int i=0;i<40 && !stop.get();i++){
                int n=localTrack.write(silence,0,silence.length,AudioTrack.WRITE_BLOCKING);
                if(n<=0)throw new IOException("output_write_failed");preflightFrames+=n/2;
                AudioDeviceInfo actual=localTrack.getRoutedDevice();
                if(actual!=null && actual.isSink() && (output==null || actual.getId()==output.getId())){outputDevice=actual.getId();outputType=actual.getType();break;}
            }
            if(stop.get())return;
            if(outputDevice<0 || (output!=null && outputDevice!=output.getId()))throw new IOException("output_route_mismatch");
            bufferFrames=localTrack.getBufferSizeInFrames();performanceMode=localTrack.getPerformanceMode();
            performanceModeActual=performanceMode;
            if(wiredOutput(localTrack.getRoutedDevice())){
                int burst=240;try{burst=Integer.parseInt(manager.getProperty(AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER));}catch(Exception ignored){}
                outputBufferRequested=performanceMode==AudioTrack.PERFORMANCE_MODE_LOW_LATENCY?Math.max(240,Math.min(480,burst*2)):Math.min(bufferFrames,1440);localTrack.setBufferSizeInFrames(outputBufferRequested);bufferFrames=localTrack.getBufferSizeInFrames();
            }
            if(input!=null){
                int mask=channels==2?AudioFormat.CHANNEL_IN_STEREO:AudioFormat.CHANNEL_IN_MONO;
                int min=AudioRecord.getMinBufferSize(48000,mask,AudioFormat.ENCODING_PCM_16BIT);
                if(min<=0)throw new IOException("input_format_unavailable");
                inputEffectsDisabled=inputEffectsFailed=0;
                localRecord=openInput(input,channels,mask,min);
                record=localRecord;
                int session=localRecord.getAudioSessionId();
                try{if(AutomaticGainControl.isAvailable())effects[0]=AutomaticGainControl.create(session);}catch(Exception ignored){inputEffectsFailed|=1;}
                try{if(NoiseSuppressor.isAvailable())effects[1]=NoiseSuppressor.create(session);}catch(Exception ignored){inputEffectsFailed|=2;}
                try{if(AcousticEchoCanceler.isAvailable())effects[2]=AcousticEchoCanceler.create(session);}catch(Exception ignored){inputEffectsFailed|=4;}
                for(int i=0;i<effects.length;i++)if(effects[i]!=null){try{if(effects[i].setEnabled(false)==AudioEffect.SUCCESS&&!effects[i].getEnabled())inputEffectsDisabled|=1<<i;else inputEffectsFailed|=1<<i;}catch(Exception ignored){inputEffectsFailed|=1<<i;}}
                inputBufferFrames=localRecord.getBufferSizeInFrames();
                inputSampleRate=localRecord.getSampleRate();
                final AudioRecord reader=localRecord;
                ring=new LiveVoiceBuffer(inputBufferFrames,bufferFrames,channels);micTargetFrames=ring.targetFrames;
                final LiveVoiceBuffer captureRing=ring;
                capture=new Thread(()->capture(reader,input,channels,captureRing),"ktv-usb-capture");capture.start();
                long prefill=System.nanoTime()+300000000L;
                while(!stop.get()&&ring.queuedFrames()<ring.targetFrames&&System.nanoTime()<prefill)Thread.sleep(1);
            }
            long started=System.nanoTime();int tunedUnderruns=localTrack.getUnderrunCount();
            while(!stop.get()){
                AudioDeviceInfo actual=localTrack.getRoutedDevice();
                if(actual==null || actual.getId()!=outputDevice)throw new IOException("output_route_changed");
                int frames=music.read(song,240);backingSwitches=music.switches;backingFailures=music.failures;if(frames==0){natural=true;code="completed";break;}
                if(ring!=null){ring.take(mic,240);missingMicFrames=ring.missingFrames;queuedMicFrames=ring.queuedFrames();droppedMicFrames=ring.droppedFrames;micRebuffers=ring.rebufferCount;micPeakQueueFrames=ring.peakFrames;micRatePpm=ring.ratePpm;}
                long dspStart=System.nanoTime();
                mixer.mix(song,mic,Math.max(1,channels),frames,mixed);observeMixedPeak(mixed,frames);
                dspNanosMax=Math.max(dspNanosMax,System.nanoTime()-dspStart);
                int written=0;
                while(written<frames*2 && !stop.get()){
                    int n=localTrack.write(mixed,written,frames*2-written,AudioTrack.WRITE_BLOCKING);
                    if(n<=0)throw new IOException("output_write_failed");written+=n;
                }
                renderedFrames+=written/2;positionFrames=music.position;int observedUnderruns=localTrack.getUnderrunCount();
                if(observedUnderruns-tunedUnderruns>=2&&bufferFrames<minimum/4){localTrack.setBufferSizeInFrames(Math.min(minimum/4,bufferFrames+480));bufferFrames=localTrack.getBufferSizeInFrames();if(ring!=null)ring.ensureOutputBurst(bufferFrames);tunedUnderruns=observedUnderruns;}
                underruns=observedUnderruns;
                playheadFrames=seek+Math.max(0,(localTrack.getPlaybackHeadPosition()&0xffffffffL)-preflightFrames);
                outputQueuedFrames=Math.max(0,preflightFrames+renderedFrames-(localTrack.getPlaybackHeadPosition()&0xffffffffL));
                if(input!=null && capturedFrames==0 && System.nanoTime()-started>2000000000L)throw new IOException("no_microphone_frames");
            }
            if(natural){
                // Drain the final output buffer before advancing to the next song.
                long deadline=System.nanoTime()+1000000000L;
                while(!stop.get() && (localTrack.getPlaybackHeadPosition()&0xffffffffL)<preflightFrames+renderedFrames && System.nanoTime()<deadline)Thread.sleep(5);
            }
        }catch(Exception error){if(!stop.get()){String message=error.getMessage();code=message!=null && message.matches("[a-z_]{1,50}")?message:"audio_unavailable";}}
        finally{
            natural=natural && !stop.get();
            stop.set(true);
            if(localRecord!=null){try{localRecord.stop();}catch(Exception ignored){} }
            if(capture!=null){try{capture.join(1500);}catch(InterruptedException interrupted){Thread.currentThread().interrupt();}}
            if(capture!=null&&capture.isAlive())captureFault="input_stop_timeout";
            if(input!=null){lastSessionBackend=0;lastSessionInputFrames=capturedFrames;lastSessionDropped=droppedMicFrames;lastSessionMissing=missingMicFrames;lastSessionInputAgeUs=inputAgeMaxNanos/1000;lastSessionOutputType=outputType;lastSessionFault=lastInputFault!=0?3:mixer.safety.reason;}
            for(AudioEffect effect:effects)if(effect!=null){try{effect.release();}catch(Exception ignored){}}
            if(localRecord!=null)localRecord.release();record=null;
            if(ring!=null)ring.clear();queuedMicFrames=0;Arrays.fill(song,(short)0);Arrays.fill(mic,(short)0);Arrays.fill(mixed,(short)0);mixer.clearVoice();
            if(localTrack!=null){try{localTrack.stop();}catch(Exception ignored){}localTrack.release();}track=null;
            if(captureFault!=null){code=captureFault;natural=false;}
            musicSource=null;completed(code,natural && code.equals("completed"));
        }
    }
    private void capture(AudioRecord source,AudioDeviceInfo input,int channels,LiveVoiceBuffer ring){
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);short[] block=new short[240*channels];
        long deadline=System.nanoTime()+2000000000L,readFrames=0;AudioTimestamp stamp=new AudioTimestamp();
        int[] waits=new int[101];int reads=0;boolean timingConfigured=false;inputReadP95Us=0;
        InputMonitor monitor=new InputMonitor();lastInput=monitor;lastInputFault=0;
        while(!stop.get()){
            long readStarted=System.nanoTime();int n=source.read(block,0,block.length,AudioRecord.READ_BLOCKING);
            int waited=(int)Math.min(100,(System.nanoTime()-readStarted)/1000000);waits[waited]++;reads++;
            if(n<0){captureFault="input_read_failed";stop.set(true);break;}
            if(n==0){try{Thread.sleep(1);}catch(InterruptedException ignored){}continue;}
            readFrames+=n/channels;
            AudioDeviceInfo actual=source.getRoutedDevice();
            if(actual==null){if(System.nanoTime()>deadline){captureFault="input_route_unavailable";stop.set(true);}continue;}
            if(actual.getId()!=input.getId()){captureFault="input_route_changed";stop.set(true);break;}
            inputDevice=actual.getId();n-=n%channels;
            int peak=0;for(int i=0;i<n;i++){int level=Math.abs((int)block[i]);peak=Math.max(peak,level);if(i%channels==0)inputPeakA=Math.max(inputPeakA,level);else inputPeakB=Math.max(inputPeakB,level);}micPeak=Math.max(micPeak,peak);
            if(source.getTimestamp(stamp,AudioTimestamp.TIMEBASE_MONOTONIC)==AudioRecord.SUCCESS){inputTimestampAvailable=true;long edge=stamp.nanoTime+(readFrames-stamp.framePosition)*1000000000L/48000;inputAgeNanos=Math.max(0,System.nanoTime()-edge);inputAgeMaxNanos=Math.max(inputAgeMaxNanos,inputAgeNanos);}
            monitor.add(block,n,channels);capturedFrames+=n/channels;
            if(monitor.ready){if(!timingConfigured){int cumulative=0,p95=0;for(int i=0;i<waits.length;i++){cumulative+=waits[i];if(cumulative>=Math.ceil(reads*.95)){p95=i;break;}}inputReadP95Us=(p95+1)*1000;ring.configureStartup((p95+1)*48,bufferFrames);micTargetFrames=ring.targetFrames;timingConfigured=true;}ring.put(block,n);}
            else if(monitor.frames>=48000){lastInputFault=1;captureFault="input_clipped";stop.set(true);break;}
        }
        Arrays.fill(block,(short)0);
    }
    private synchronized void installMusicSource(SwitchingWavSource source,File initial){musicSource=source;if(desiredBacking!=null&&!desiredBacking.equals(initial))source.request(desiredBacking);}
    public synchronized void switchBacking(File path){desiredBacking=path;SwitchingWavSource current=musicSource;if(current!=null)current.request(path);}
    public void stop(){stop.set(true);AudioBackendIO io=nativeIO;if(io!=null)io.stop();AudioTrack current=track;if(current!=null){try{current.pause();current.flush();current.stop();}catch(Exception ignored){}}}
    /** Playback completion alone does not release a still-blocked input owner. */
    public boolean released(){
        Thread output=render,input=capture;
        return !running&&(output==null||!output.isAlive())&&(input==null||!input.isAlive())
            &&track==null&&record==null&&nativeIO==null;
    }
    public void awaitStopped() throws Exception {
        Thread current=render;if(current!=null){current.join(2500);if(current.isAlive())throw new IOException("audio_stopping");}
        if(!released())throw new IOException(capture!=null&&capture.isAlive()?"input_stop_timeout":"audio_stopping");
    }
    public void devicesChanged(){if(running)stop();}
}
