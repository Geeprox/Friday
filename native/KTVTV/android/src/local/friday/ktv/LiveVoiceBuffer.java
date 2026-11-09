package local.friday.ktv;

/** Bounded USB/HAL burst buffer. Slow clock correction never skips whole voice blocks. */
public final class LiveVoiceBuffer {
    private final short[] samples;
    public final int channels, capacityFrames;
    public volatile int targetFrames;
    private int read, count;
    private double fraction, rate=1, filteredError;
    private boolean primed,everPrimed;
    private final float[] last=new float[2];
    private float fade;
    public volatile long droppedFrames, missingFrames, rebufferCount;
    public volatile int peakFrames, ratePpm;
    public LiveVoiceBuffer(int inputFrames,int outputFrames,int channels){
        if(channels<1||channels>2||inputFrames<0||outputFrames<0)throw new IllegalArgumentException();
        this.channels=channels;
        // Device buffers are a conservative bound on delivery bursts, not a latency measurement.
        int requested=startupTarget(inputFrames,outputFrames,960);
        capacityFrames=(int)Math.min(11520,(long)requested+Math.max(960,inputFrames)+Math.max(960,outputFrames));
        targetFrames=reachableTarget(requested);
        samples=new short[capacityFrames*channels];
    }
    private static int startupTarget(int inputFrames,int outputFrames,int minimum){
        if(inputFrames<0||outputFrames<0)throw new IllegalArgumentException();
        return (int)Math.max(minimum,Math.min(3840,(long)Math.max(inputFrames,outputFrames)+240));
    }
    private int reachableTarget(int requested){
        // Keep the target attainable in the fixed allocation, with two frames
        // of lookahead for interpolation; measured bursts may grow after open.
        return Math.min(requested,capacityFrames-2);
    }
    public synchronized void put(short[] source,int length){
        if(length<0||length>source.length||length%channels!=0)throw new IllegalArgumentException();
        int frames=length/channels,skip=Math.max(0,frames-capacityFrames);
        int overflow=Math.max(0,count+frames-skip-capacityFrames);
        if(skip+overflow>0){read=(read+overflow)%capacityFrames;count-=overflow;droppedFrames+=skip+overflow;fraction=0;fade=0;}
        for(int f=skip;f<frames;f++){
            int at=(read+count)%capacityFrames;
            for(int c=0;c<channels;c++)samples[at*channels+c]=source[f*channels+c];
            count++;
        }
        peakFrames=Math.max(peakFrames,count);
    }
    public synchronized void configureStartup(int inputBurstFrames,int outputFrames){
        int requested=startupTarget(inputBurstFrames,outputFrames,960);
        if(!primed&&count==0)targetFrames=reachableTarget(requested);
    }
    public synchronized void configureNativeStartup(int inputBurstFrames,int outputFrames){
        int requested=startupTarget(inputBurstFrames,outputFrames,480);
        if(!primed&&count==0)targetFrames=reachableTarget(requested);
    }
    public synchronized void ensureOutputBurst(int frames){targetFrames=reachableTarget(Math.max(targetFrames,startupTarget(frames,0,480)));}
    public synchronized int take(short[] target,int frames){
        if(frames<0||frames>target.length/channels)throw new IllegalArgumentException();
        if(!primed&&count>=targetFrames){primed=everPrimed=true;fade=0;}
        if(!primed){if(everPrimed)missingFrames+=frames;silence(target,0,frames);return 0;}
        // Correct oscillator drift only, at at most 0.1% (under two cents), with a slow servo.
        // HAL packet bursts are absorbed by capacity rather than interpreted as a rate change.
        filteredError+=.001*(count-targetFrames-filteredError);
        double wanted=1+Math.max(-.001,Math.min(.001,filteredError/480000));
        rate+=Math.max(-.000002,Math.min(.000002,wanted-rate));ratePpm=(int)Math.round((rate-1)*1000000);
        int produced=0;
        for(;produced<frames;produced++){
            if(count<2)break;
            int next=(read+1)%capacityFrames;fade=Math.min(1,fade+1f/240);
            for(int c=0;c<channels;c++){
                float value=(float)(samples[read*channels+c]*(1-fraction)+samples[next*channels+c]*fraction);
                last[c]=value*fade;target[produced*channels+c]=(short)Math.round(last[c]);
            }
            fraction+=rate;int consumed=(int)fraction;fraction-=consumed;
            consumed=Math.min(consumed,count);read=(read+consumed)%capacityFrames;count-=consumed;
        }
        if(produced<frames){missingFrames+=frames-produced;if(frames-produced>48){rebufferCount++;primed=false;fraction=0;}silence(target,produced,frames);}
        return produced;
    }
    private void silence(short[] target,int start,int end){
        for(int f=start;f<end;f++)for(int c=0;c<channels;c++){
            last[c]*=.97f;if(Math.abs(last[c])<.5f)last[c]=0;target[f*channels+c]=(short)Math.round(last[c]);
        }
    }
    public synchronized int queuedFrames(){return count;}
    public synchronized void clear(){java.util.Arrays.fill(samples,(short)0);java.util.Arrays.fill(last,0);read=count=0;fraction=fade=0;primed=everPrimed=false;}
}
