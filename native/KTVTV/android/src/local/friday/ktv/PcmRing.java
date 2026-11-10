package local.friday.ktv;

/** Small bounded live-voice queue; old frames are discarded rather than growing latency. */
public final class PcmRing {
    private final short[] samples;
    private final int channels;
    private int read, count;
    public volatile long droppedFrames;
    public PcmRing(int frames, int channels) {
        if (frames < 1 || channels < 1 || channels > 2) throw new IllegalArgumentException();
        this.channels = channels; samples = new short[frames * channels];
    }
    public synchronized void put(short[] source, int length) {
        if (length < 0 || length > source.length || length % channels != 0) throw new IllegalArgumentException();
        int skip = Math.max(0, length - samples.length);
        droppedFrames += skip / channels;
        int kept = length - skip;
        int overflow = Math.max(0, count + kept - samples.length);
        read = (read + overflow) % samples.length; count -= overflow;
        droppedFrames += overflow / channels;
        for (int i = skip; i < length; i++) {
            samples[(read + count) % samples.length] = source[i]; count++;
        }
    }
    public synchronized int take(short[] target, int frames) {
        int length = Math.min(count, frames * channels);
        for (int i = 0; i < length; i++) { target[i] = samples[read]; read = (read + 1) % samples.length; }
        java.util.Arrays.fill(target, length, frames * channels, (short)0); count -= length;
        return length / channels;
    }
    public synchronized int queuedFrames() { return count / channels; }
    public synchronized void clear(){java.util.Arrays.fill(samples,(short)0);read=0;count=0;}
}
