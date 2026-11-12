package local.friday.ktv;

/** Audio-master video correction; no platform or playback side effects. */
public final class VideoSync {
    private long lastSeek=-10000;
    private int largeDriftSamples;
    public boolean seek;
    public float speed=1;
    public long drift;
    public void reset(){lastSeek=-10000;largeDriftSamples=0;seek=false;speed=1;drift=0;}
    public void update(long target,long current,long now,boolean force){
        drift=target-current;seek=false;
        largeDriftSamples=Math.abs(drift)>650?largeDriftSamples+1:0;
        if(force || (largeDriftSamples>=3 && now-lastSeek>=10000)){
            seek=true;lastSeek=now;largeDriftSamples=0;speed=1;return;
        }
        // A small decoder/clock wobble changes silent video speed, never its timeline.
        speed=Math.abs(drift)<=80?1:Math.max(.98f,Math.min(1.02f,1+drift/5000f));
    }
}
