package local.friday.ktv;
/** Synthetic HAL delivery and playback clocks; never opens a device or records sound. */
public final class LiveVoiceBufferTest {
    private static void check(boolean ok,String why){if(!ok)throw new AssertionError(why);}
    private static void rejected(Runnable action,String why){try{action.run();}catch(IllegalArgumentException expected){return;}throw new AssertionError(why);}
    private static void boundedRetarget(){
        for(int channels=1;channels<=2;channels++)for(int mode=0;mode<3;mode++){
            LiveVoiceBuffer voice=new LiveVoiceBuffer(960,240,channels);
            check(voice.capacityFrames==3120,"real small-buffer capacity changed");
            if(mode==0)voice.configureNativeStartup(6000,480);
            else if(mode==1)voice.configureStartup(6000,480);
            else voice.ensureOutputBurst(6000);
            short[] input=new short[(voice.capacityFrames+960)*channels];java.util.Arrays.fill(input,(short)5000);
            voice.put(input,input.length);short[] out=new short[(voice.capacityFrames-2)*channels];
            check(voice.take(out,voice.capacityFrames-2)==voice.capacityFrames-2,"above-capacity delivery never primes, update="+mode+" channels="+channels);
            check(voice.targetFrames==voice.capacityFrames-2,"target lost its interpolation reserve");
            check(out[out.length-1]==5000&&voice.queuedFrames()>=2,"full bounded take did not retain interpolation lookahead");
            voice.take(new short[240*channels],240);check(voice.rebufferCount==1,"starvation must enter refill once");
            voice.put(input,input.length);check(voice.take(new short[240*channels],240)==240,"bounded target cannot re-prime after starvation");
            check(voice.peakFrames==voice.capacityFrames&&voice.droppedFrames>0,"oversized delivery must stay bounded and account for discarded input");
        }
    }
    private static void boundaryConfiguration(){
        LiveVoiceBuffer huge=new LiveVoiceBuffer(Integer.MAX_VALUE,Integer.MAX_VALUE,2);
        check(huge.capacityFrames==11520&&huge.targetFrames==3840,"large constructor dimensions overflowed or shrank normal target");
        LiveVoiceBuffer small=new LiveVoiceBuffer(960,240,1);
        small.configureStartup(Integer.MAX_VALUE,0);check(small.targetFrames==small.capacityFrames-2,"large startup burst overflowed");
        small.configureNativeStartup(0,Integer.MAX_VALUE);check(small.targetFrames==small.capacityFrames-2,"large native output burst overflowed");
        small.configureNativeStartup(240,240);check(small.targetFrames==480,"native startup floor changed");
        small.ensureOutputBurst(Integer.MAX_VALUE);check(small.targetFrames==small.capacityFrames-2,"large output burst overflowed");
        small.ensureOutputBurst(0);check(small.targetFrames==small.capacityFrames-2,"output adaptation lowered a valid target");
        rejected(()->new LiveVoiceBuffer(-1,240,1),"negative constructor input accepted");
        rejected(()->new LiveVoiceBuffer(240,-1,1),"negative constructor output accepted");
        rejected(()->small.configureStartup(-1,240),"negative startup input accepted");
        rejected(()->small.configureStartup(240,-1),"negative startup output accepted");
        rejected(()->small.configureNativeStartup(-1,240),"negative native input accepted");
        rejected(()->small.configureNativeStartup(240,-1),"negative native output accepted");
        rejected(()->small.ensureOutputBurst(-1),"negative output adaptation accepted");
        rejected(()->huge.take(new short[4],Integer.MAX_VALUE),"large take length overflowed its sample bound");
    }
    private static void reconfiguration(){
        LiveVoiceBuffer voice=new LiveVoiceBuffer(960,240,1);int initial=voice.targetFrames;
        voice.put(new short[]{5000},1);voice.configureStartup(6000,480);voice.configureNativeStartup(240,240);
        check(voice.targetFrames==initial&&voice.queuedFrames()==1,"startup configuration changed queued, unprimed audio");
        short[] input=new short[initial];java.util.Arrays.fill(input,(short)5000);voice.put(input,input.length);
        check(voice.take(new short[240],240)==240,"normal initial target failed");
        int queued=voice.queuedFrames();voice.configureStartup(6000,480);voice.configureNativeStartup(240,240);
        check(voice.targetFrames==initial&&voice.queuedFrames()==queued,"startup configuration changed active playback");
        voice.ensureOutputBurst(6000);check(voice.targetFrames==voice.capacityFrames-2&&voice.queuedFrames()==queued,"output adaptation cleared queued audio");
        check(voice.take(new short[240],240)==240,"output adaptation unprimed active playback");
        voice.clear();check(voice.queuedFrames()==0,"clear retained queued input");voice.configureStartup(288,480);
        check(voice.targetFrames==960,"clear did not allow fresh ordinary startup configuration");
        voice.configureNativeStartup(288,480);check(voice.targetFrames==720,"clear did not allow fresh native startup configuration");
        short[] out=new short[240];voice.take(out,240);for(short sample:out)check(sample==0,"clear retained an audible tail");
    }
    private static void burst(int driftPpm){
        LiveVoiceBuffer voice=new LiveVoiceBuffer(2880,2308,2);
        short[] input=new short[2880*2],out=new short[480];
        long delivered=0;boolean started=false;double nextCapture=0;int oldDrops=0,oldMissing=0;
        PcmRing old=new PcmRing(960,2);
        for(int ms=0;ms<120000;ms++){
            if(ms>=nextCapture){
                for(int i=0;i<2880;i++){short sample=(short)Math.round(Math.sin((delivered+i)*2*Math.PI*311/48000)*5000);input[i*2]=input[i*2+1]=sample;}
                for(int i=0;i<12;i++){short[] block=new short[480];System.arraycopy(input,i*480,block,0,480);voice.put(block,480);old.put(block,480);}
                delivered+=2880;nextCapture+=60.0/(1+driftPpm/1000000.0);
            }
            if(!started&&voice.queuedFrames()>=voice.targetFrames+2304)started=true;
            if(started&&ms%48==0){
                for(int i=0;i<9;i++){voice.take(out,240);oldMissing+=240-old.take(new short[480],240);}
                // 48 ms / 48 kHz = 2304 frames, including a final fractional block.
                voice.take(new short[288],144);oldMissing+=144-old.take(new short[288],144);
            }
        }
        oldDrops=(int)old.droppedFrames;
        check(oldDrops>100000&&oldMissing>100000,"old 20 ms FIFO reproduces substantial burst loss");
        check(voice.droppedFrames==0,"HAL bursts must not discard voice, drift="+driftPpm+" drops="+voice.droppedFrames);
        check(voice.missingFrames==0,"HAL bursts must not insert silence, drift="+driftPpm+" missing="+voice.missingFrames);
        check(Math.abs(voice.ratePpm)<=1000,"clock correction bounded below two cents");
        check(voice.peakFrames<=voice.capacityFrames,"queue remains bounded");
        voice.clear();voice.take(out,240);for(short x:out)check(x==0,"stop erases voice");
    }
    public static void main(String[] args){boundedRetarget();boundaryConfiguration();reconfiguration();burst(0);burst(400);burst(-400);
        LiveVoiceBuffer fast=new LiveVoiceBuffer(2880,480,1);fast.configureStartup(288,480);check(fast.targetFrames==960,"measured short delivery selects 20 ms target without assuming capacity is latency");
        short[] fastInput=new short[3000];fast.put(fastInput,3000);fast.take(new short[240],240);fast.configureStartup(3840,2308);check(fast.targetFrames==960,"never jump voice timeline after playback has begun");
        LiveVoiceBuffer voice=new LiveVoiceBuffer(2880,2308,1);short[] input=new short[6000];java.util.Arrays.fill(input,(short)5000);voice.put(input,input.length);short[] out=new short[240];
        for(int i=0;i<40;i++)voice.take(out,240);
        check(voice.rebufferCount==1,"one starvation enters refill instead of repeatedly breaking blocks");
        long before=voice.missingFrames;for(int i=0;i<10;i++)voice.take(out,240);check(voice.missingFrames==before+2400,"refill silence must be counted, never hidden");for(short x:out)check(x==0,"starvation fades to zero");
        System.out.println("Live USB burst/clock contracts passed; no audio device started");
    }
}
