package local.friday.ktv;

/** One immutable edition binds both PCM files to a shared frame timeline. */
public final class MediaTimeline {
    private MediaTimeline(){}
    public static boolean valid(long frames,long durationMs,long originalBytes,long backingBytes,double originalSeconds,double backingSeconds){
        if(frames<1||frames>86400000L||durationMs<1||durationMs>1800000||originalBytes!=44+frames*4||backingBytes<48||backingBytes>345600044L||(backingBytes-44)%4!=0)return false;
        long backingFrames=(backingBytes-44)/4;
        return Math.abs(backingFrames-frames)<=4800&&Math.abs(Math.round(frames/48.0)-durationMs)<=1
            &&Double.isFinite(originalSeconds)&&Double.isFinite(backingSeconds)
            &&Math.abs(originalSeconds-frames/48000.0)<.001&&Math.abs(backingSeconds-backingFrames/48000.0)<.001;
    }
}
