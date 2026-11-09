package android.media;

import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

/** No audio device: the test advances the playback head explicitly. */
public final class AudioTrack {
    public static final int MODE_STATIC=0,STATE_NO_STATIC_DATA=2;
    public static final CopyOnWriteArrayList<AudioTrack> outputs=new CopyOnWriteArrayList<>();
    public final AtomicInteger reads=new AtomicInteger();
    public volatile int head;
    public volatile boolean played,released;
    private int bytes;
    public static final class Builder {
        private int bytes;
        public Builder setAudioAttributes(AudioAttributes value){return this;}
        public Builder setAudioFormat(AudioFormat value){return this;}
        public Builder setBufferSizeInBytes(int value){bytes=value;return this;}
        public Builder setTransferMode(int value){if(value!=MODE_STATIC)throw new AssertionError("transfer mode");return this;}
        public AudioTrack build(){AudioTrack track=new AudioTrack();track.bytes=bytes;outputs.add(track);return track;}
    }
    public int getState(){return STATE_NO_STATIC_DATA;}
    public int write(byte[] pcm,int offset,int length){if(offset!=0||length!=pcm.length||length!=bytes)throw new AssertionError("PCM write");return length;}
    public void setVolume(float value){if(value<0||value>1)throw new AssertionError("gain");}
    public void play(){if(released)throw new AssertionError("play after release");played=true;}
    public int getPlaybackHeadPosition(){reads.incrementAndGet();if(released)throw new IllegalStateException("released");return head;}
    public void pause(){}
    public void flush(){}
    public void stop(){}
    public void release(){released=true;}
}
