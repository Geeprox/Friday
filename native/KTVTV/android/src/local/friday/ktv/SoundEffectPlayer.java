package local.friday.ktv;

import android.content.Context;
import android.media.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;

/** Offline room effects; no capture, focus takeover, network URL or microphone processing. */
public final class SoundEffectPlayer {
    private final Map<String,byte[]> sounds=new HashMap<>();
    private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private final Object lock=new Object();
    private AudioTrack track;
    private long generation;
    private boolean closed;
    private volatile int gain=60;
    public volatile long sequence;
    public volatile String action="",state="idle";
    public volatile boolean ready;
    public SoundEffectPlayer(Context context){
        worker.execute(()->{try{
            for(String name:new String[]{"applause","cheer"}){
                try(InputStream input=context.getAssets().open("sfx/"+name+".wav");ByteArrayOutputStream out=new ByteArrayOutputStream()){
                    byte[] block=new byte[8192];int n;while((n=input.read(block))!=-1){if(out.size()+n>1600000)throw new IOException("effect_size");out.write(block,0,n);}
                    byte[] wav=out.toByteArray();if(wav.length<=44||(wav.length-44)%4!=0)throw new IOException("effect_format");
                    sounds.put("sfx_"+name,Arrays.copyOfRange(wav,44,wav.length));
                }
            }
            ready=true;
        }catch(Exception ignored){ready=false;}});
    }
    public static boolean fresh(long created,long expires,long now){return expires-created==5&&created<=now&&now<expires;}
    public void gain(int value){gain=Math.max(0,Math.min(100,value));synchronized(lock){if(track!=null)track.setVolume(gain/100f);}}
    public void command(long seq,String name,long created,long expires){
        synchronized(lock){
            if(closed||seq<=sequence)return;
            sequence=seq;action=name;
            if(!fresh(created,expires,System.currentTimeMillis()/1000)){state="expired";return;}
            final long token=++generation;release();
            if(name.equals("sfx_stop")){state="stopped";return;}
            state="pending";
            worker.execute(()->play(token,seq,name,created,expires));
        }
    }
    private void play(long token,long seq,String name,long created,long expires){
        AudioTrack output=null;
        try{
            byte[] pcm=sounds.get(name);if(pcm==null)throw new IOException("effect_unavailable");
            synchronized(lock){if(closed||token!=generation)return;}
            output=new AudioTrack.Builder().setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
                .setBufferSizeInBytes(pcm.length).setTransferMode(AudioTrack.MODE_STATIC).build();
            if(output.getState()!=AudioTrack.STATE_NO_STATIC_DATA||output.write(pcm,0,pcm.length)!=pcm.length)throw new IOException("effect_output");
            synchronized(lock){
                if(closed||token!=generation)return;
                if(!fresh(created,expires,System.currentTimeMillis()/1000)){state="expired";return;}
                track=output;output.setVolume(gain/100f);output.play();
            }
            long started=System.nanoTime();
            while(true){
                synchronized(lock){
                    if(closed||token!=generation)return;
                    long head=output.getPlaybackHeadPosition()&0xffffffffL;
                    if(head>0&&seq==sequence)state="playing";
                    if(head>=pcm.length/4){if(seq==sequence)state="stopped";return;}
                    if(System.nanoTime()-started>1000000000L&&head==0)throw new IOException("effect_not_advancing");
                }
                Thread.sleep(15);
            }
        }catch(Exception ignored){synchronized(lock){if(token==generation&&seq==sequence)state="failed";}}
        finally{synchronized(lock){if(track==output){release();output=null;}}if(output!=null)try{output.release();}catch(Exception ignored){}}
    }
    private void release(){AudioTrack active=track;track=null;if(active!=null){try{active.pause();active.flush();active.stop();}catch(Exception ignored){}active.release();}}
    public void stop(){synchronized(lock){++generation;release();if(sequence>0)state="stopped";}}
    public void resetRoom(){synchronized(lock){stop();sequence=0;action="";state="idle";}}
    public void close(){synchronized(lock){closed=true;stop();}worker.shutdownNow();}
}
