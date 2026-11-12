package local.friday.ktv.audio;

import android.media.*;
import android.os.Process;
import org.json.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;

/** An ordinary MEDIA/MUSIC track whose private samples are always exactly zero. */
final class ZeroOutputCarrier implements AutoCloseable {
    private final int expectedRoute;private final Runnable changed,failed;
    private final short[] zeros=new short[960];
    private final AtomicBoolean stop=new AtomicBoolean();
    private final CountDownLatch readySignal=new CountDownLatch(1),stopSignal=new CountDownLatch(1);
    private final ScheduledExecutorService observer=Executors.newSingleThreadScheduledExecutor(r->new Thread(r,"zero-carrier-observer"));
    private volatile AudioTrack track;private Thread writer;
    private volatile String state="NOT_STARTED",error="";private volatile boolean ready,released;
    private volatile int actualRoute,actualType,underruns=-1,bufferFrames,capacityFrames,performanceMode=-1,playState=-1;
    private volatile long written,head,initialHead,readyNs,playNs,releaseNs,observedNs;private long lastRaw,wraps,lastPublish;
    ZeroOutputCarrier(int route,Runnable changed,Runnable failed){expectedRoute=route;this.changed=changed;this.failed=failed;}
    synchronized void start(){if(stop.get()){released=true;state="STOPPED";readySignal.countDown();return;}state="CREATING";observer.scheduleWithFixedDelay(this::observe,0,100,TimeUnit.MILLISECONDS);writer=new Thread(this::run,"zero-carrier-writer");writer.start();}
    boolean awaitReady(BooleanSupplier cancelled){
        try{while(!cancelled.getAsBoolean()&&!stop.get()){if(readySignal.await(50,TimeUnit.MILLISECONDS))return ready&&error.isEmpty()&&!cancelled.getAsBoolean();}}
        catch(InterruptedException e){Thread.currentThread().interrupt();}return false;
    }
    boolean readyForVendor(){return ready&&!stop.get()&&error.isEmpty()&&actualRoute==expectedRoute&&actualType==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER&&playState==AudioTrack.PLAYSTATE_PLAYING&&head>initialHead;}
    boolean releaseReturned(){return released;}
    private void fail(String code){if(error.isEmpty()){error=code;state="FAILED";readySignal.countDown();failed.run();}}
    private void run(){
        AudioTrack current=null;
        try{
            if(stop.get())return;
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
            int minimum=AudioTrack.getMinBufferSize(48000,AudioFormat.CHANNEL_OUT_STEREO,AudioFormat.ENCODING_PCM_16BIT);
            if(minimum<=0)throw new IllegalStateException("carrier_min_buffer_failed");
            current=new AudioTrack.Builder().setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
                .setTransferMode(AudioTrack.MODE_STREAM).setPerformanceMode(AudioTrack.PERFORMANCE_MODE_NONE).setBufferSizeInBytes(Math.max(minimum,480*2*2*8)).build();
            if(current.getState()!=AudioTrack.STATE_INITIALIZED)throw new IllegalStateException("carrier_uninitialized");
            if(current.setVolume(1f)!=AudioTrack.SUCCESS)throw new IllegalStateException("carrier_gain_failed");
            bufferFrames=current.getBufferSizeInFrames();capacityFrames=current.getBufferCapacityInFrames();performanceMode=current.getPerformanceMode();
            initialHead=((long)current.getPlaybackHeadPosition())&0xffffffffL;lastRaw=initialHead;head=initialHead;track=current;
            if(stop.get())return;
            int first=current.write(zeros,0,zeros.length,AudioTrack.WRITE_NON_BLOCKING);if(first<0||first%2!=0)throw new IllegalStateException("carrier_prefill_failed");written+=first/2;
            if(stop.get())return;
            current.play();playNs=System.nanoTime();state="PLAYING";
            while(!stop.get()){
                if(!error.isEmpty()){stopSignal.await();break;}
                int count=current.write(zeros,0,zeros.length,AudioTrack.WRITE_NON_BLOCKING);
                if(count<0||count>zeros.length||count%2!=0){fail("carrier_write_failed");continue;}
                written+=count/2;if(count==0)Thread.sleep(1);
            }
        }catch(InterruptedException e){Thread.currentThread().interrupt();}
        catch(RuntimeException e){if(!stop.get()){fail(e instanceof IllegalStateException?e.getMessage():"carrier_exception");try{stopSignal.await();}catch(InterruptedException interrupted){Thread.currentThread().interrupt();}}}
        finally{
            // Guard closes only after the final vendor STOP/disable attempt for this lease.
            track=null;boolean releaseReturned=true;if(current!=null){try{current.stop();}catch(RuntimeException ignored){}try{current.flush();}catch(RuntimeException ignored){}try{current.release();}catch(RuntimeException failure){releaseReturned=false;error="carrier_release_failed";}}
            releaseNs=System.nanoTime();released=releaseReturned;state=error.isEmpty()?"STOPPED":"FAILED";readySignal.countDown();
        }
    }
    private void observe(){
        if(stop.get())return;AudioTrack current=track;
        try{
            if(current!=null){
                playState=current.getPlayState();AudioDeviceInfo routed=current.getRoutedDevice();actualRoute=routed==null?0:routed.getId();actualType=routed==null?0:routed.getType();
                long raw=((long)current.getPlaybackHeadPosition())&0xffffffffL;if(raw<lastRaw&&lastRaw-raw>0x80000000L)wraps++;lastRaw=raw;head=(wraps<<32)+raw;underruns=current.getUnderrunCount();
                if(actualRoute>0&&(actualRoute!=expectedRoute||actualType!=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER))fail("carrier_route_changed");
                else if(ready&&actualRoute==0)fail("carrier_route_lost");
                else if(ready&&playState!=AudioTrack.PLAYSTATE_PLAYING)fail("carrier_not_playing");
                else if(!ready&&error.isEmpty()&&actualRoute==expectedRoute&&head>initialHead&&playState==AudioTrack.PLAYSTATE_PLAYING){readyNs=System.nanoTime();ready=true;state="READY";readySignal.countDown();}
            }
        }catch(RuntimeException e){if(!stop.get())fail("carrier_observation_failed");}
        long now=System.nanoTime();observedNs=now;if(now-lastPublish>=1000000000L){lastPublish=now;changed.run();}
    }
    JSONObject metrics(){
        JSONObject out=new JSONObject();try{out.put("state",state).put("error",error).put("ready",ready).put("released",released).put("expected_route",expectedRoute).put("actual_route",actualRoute).put("actual_route_type",actualType)
            .put("play_state",playState).put("frames_written",written).put("playback_head_frames",head).put("initial_playback_head_frames",initialHead).put("head_advanced",head>initialHead).put("underruns",underruns).put("buffer_frames",bufferFrames).put("capacity_frames",capacityFrames).put("performance_mode",performanceMode)
            .put("sample_ns",observedNs).put("snapshot_atomic",false).put("play_ns",playNs).put("ready_ns",readyNs).put("release_ns",releaseNs).put("sample_rate",48000).put("channels",2).put("encoding",AudioFormat.ENCODING_PCM_16BIT).put("usage",AudioAttributes.USAGE_MEDIA).put("content_type",AudioAttributes.CONTENT_TYPE_MUSIC)
            .put("track_gain",1).put("samples_always_zero",true).put("preferred_device_set",false).put("system_volume_changed",false).put("automatic_time_limit",false);
        }catch(JSONException ignored){}return out;
    }
    public void close(){Thread worker;synchronized(this){stop.set(true);worker=writer;if(worker==null){released=true;releaseNs=System.nanoTime();state="STOPPED";}}stopSignal.countDown();readySignal.countDown();observer.shutdownNow();if(worker!=null&&worker!=Thread.currentThread())try{worker.join();}catch(InterruptedException e){Thread.currentThread().interrupt();}}
}
