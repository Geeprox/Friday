package local.friday.ktv;

import java.lang.management.ManagementFactory;
import java.util.Random;

/** Synthetic arrays only. Tonal surrogates are not confirmed acoustic feedback. */
public final class FeedbackNotchTest {
    private static int checks;
    private static void check(boolean value,String text){checks++;if(!value)throw new AssertionError(text);}
    private static double tone(int i,double hz){return Math.sin(2*Math.PI*hz*i/48000);}
    private interface Signal {double sample(int frame);}
    private static void run(FeedbackNotch f,int frames,Signal signal){
        for(int i=0;i<frames;i++){float out=f.process((float)signal.sample(i));check(Float.isFinite(out)&&Math.abs(out)<=4,"finite bounded IIR output");}
    }
    private static void none(String name,Signal signal,int frames){
        FeedbackNotch f=new FeedbackNotch();run(f,frames,signal);
        check(f.count==0&&f.severity==0&&!f.attenuated&&!f.latched,name);
    }
    private static void fillBudget(FeedbackNotch f){
        for(int p=0;p<4;p++){final int hz=900+p*600;run(f,12000,i->.25*tone(i,hz));run(f,4800,i->0);check(f.count==p+1&&!f.attenuated,"one bounded notch for each distinct risk");}
    }
    public static void main(String[] args)throws Exception{
        FeedbackNotch f=new FeedbackNotch();double normal=0;
        for(int i=0;i<48000;i++){double x=.13*tone(i,220)+.05*tone(i,440)+.03*tone(i,660);normal+=Math.abs(f.process((float)x)-x);}
        check(f.count==0&&normal<.01,"harmonic singing is passed unchanged");
        double vocal=0,isolated=0;
        for(int i=0;i<48000;i++){
            double y=f.process((float)(.12*tone(i,220)+.4*tone(i,2000)));
            if(i>24000){vocal+=y*tone(i,220);isolated+=y*tone(i,2000);}
        }
        check(f.count==1&&!f.attenuated&&!f.latched,"a successful notch does not escalate to whole-voice mute");
        check(Math.abs(isolated)/12000<.08,"isolated risk attenuated more than 14 dB");
        check(Math.abs(vocal)/12000>.10,"unrelated singer fundamental is preserved");

        none("high fundamental harmonic vowel",i->.22*tone(i,900)+.13*tone(i,1800)+.09*tone(i,2700)+.05*tone(i,3600),48000);
        none("dominant second harmonic vowel",i->.075*tone(i,1200)+.25*tone(i,2400)+.03*tone(i,3600),48000);
        none("quiet sustained pure note",i->.075*tone(i,1800),48000);
        none("decaying harmonic instrument",i->Math.exp(-3*i/48000.)*(.3*tone(i,1200)+.08*tone(i,2400)+.04*tone(i,3600)),48000);
        none("moving sweep",i->{double t=i/48000.;return .3*Math.sin(2*Math.PI*(500*t+500*t*t));},96000);
        none("vibrato",i->{double t=i/48000.;return .25*Math.sin(2*Math.PI*1800*t+2*Math.sin(2*Math.PI*6*t));},96000);
        none("100 ms short pure tone",i->i<4800?.3*tone(i,1500):0,48000);
        none("80 ms growing transient",i->i<3840?Math.min(.7,.025*Math.exp(i/48000.*50))*tone(i,1500):0,48000);
        none("below observation band",i->.25*tone(i,80),48000);
        none("above observation band",i->.25*tone(i,9000),48000);
        Random noise=new Random(20261103);none("broad noise",i->(noise.nextDouble()-.5)*.8,144000);

        f=new FeedbackNotch();long firstAction=0;
        for(int i=0;i<48000;i++){
            f.process((float)(Math.min(.45,.045*Math.exp(i/48000.*4))*tone(i,1000)));
            if(firstAction==0&&f.count>0)firstAction=f.frames;
        }
        check(firstAction>0&&firstAction<=12000&&f.count==1&&!f.latched,"stable growing risk receives a bounded narrow mitigation");
        check(Math.abs(f.peakHz-1000)<3,"interpolated frequency is bounded and accurate for the surrogate");
        long growthAction=firstAction;

        // Four notches are a per-session budget, not a silent failure branch.
        f=new FeedbackNotch();fillBudget(f);long cutFrame=0,latchFrame=0;
        for(int i=0;i<12000;i++){f.process((float)(.30*tone(i,3300)));if(cutFrame==0&&f.attenuated)cutFrame=f.frames;}
        check(f.count==4&&f.events==4&&f.attenuated&&f.gainCap==50&&!f.latched&&f.severity==2,"fifth persistent frequency falls back to a gain cap");
        for(int i=0;i<16000;i++){f.process((float)(.30*tone(i,3300)));if(latchFrame==0&&f.latched)latchFrame=f.frames;}
        check(f.latched&&f.gainCap==0&&f.severity==3&&f.sequence==6,"continued risk after gain reduction locks mute");
        check(latchFrame-cutFrame>=12000&&latchFrame-cutFrame<=12512,"final latch waits 250 ms of persistent risk after the gain cut");
        long sequence=f.sequence,frames=f.frames;f.clearState();run(f,48000,i->0);
        check(f.latched&&f.sequence==sequence&&f.frames==frames+48000&&f.count==4,"silence and stream clear never unlock or replenish notches");
        f.reset();check(!f.latched&&!f.attenuated&&f.count==0&&f.gainCap==100&&f.frames==frames+48000,"only explicit reset grants a new protection budget");

        f=new FeedbackNotch();run(f,12000,i->.6*tone(i,1100));run(f,48000,i->.6*tone(i,1112));
        check(f.latched&&f.count==1,"a strong residual inside an existing notch escalates rather than adding duplicate notches");
        f=new FeedbackNotch();run(f,12000,i->.25*tone(i,1100));run(f,48000,i->.02*tone(i,1100));
        check(f.count==1&&!f.attenuated&&!f.latched,"a suppressed residual never triggers unnecessary global reduction");

        FeedbackNotch moved=new FeedbackNotch(0);run(moved,22000,i->.25*tone(i,900));
        check(moved.attenuated&&!moved.latched,"unavailable notch stage first lowers gain");
        run(moved,4800,i->0);run(moved,16000,i->.25*tone(i,2100));
        check(moved.latched,"moving to another stable frequency cannot evade the existing gain-cut latch");

        // No stereo averaging: antiphase and noisy-other-channel risks are both
        // observed independently, while successful notches retain voice.
        for(int kind=0;kind<3;kind++){
            PcmMixer mixer=new PcmMixer();mixer.micMuted=false;mixer.musicGain=0;
            short[] music=new short[480],mic=new short[480],out=new short[480];Random other=new Random(42);
            for(int block=0;block<200;block++){
                for(int i=0;i<240;i++){short v=(short)Math.round(.25*tone(block*240+i,1300)*32767);mic[i*2]=kind==2?0:v;mic[i*2+1]=kind==0?(short)-v:kind==1?(short)Math.round((other.nextDouble()-.5)*1.2*32767):v;}
                mixer.mix(music,mic,2,240,out);
            }
            check((kind==2?mixer.feedbackB.count:mixer.feedbackA.count)>0&&!mixer.safety.latched,"antiphase and either-side risks reach their own notch");
            check(mixer.safety.ready&&mixer.safety.frames==48000&&Math.abs(mixer.safety.peakHz-1300)<3,"stereo status retains actual processed frames and the risk frequency");
        }

        // The real mixer applies the global gain cap to dry and old wet tails.
        PcmMixer mixer=new PcmMixer();mixer.micMuted=false;mixer.musicGain=0;mixer.gainA=mixer.gainB=1;mixer.setVocalPreset("studio");
        short[] music=new short[480],mic=new short[480],out=new short[480];
        for(int p=0;p<4;p++){
            int hz=900+p*600;for(int block=0;block<50;block++){for(int i=0;i<240;i++)mic[i*2]=mic[i*2+1]=(short)Math.round(.25*tone(block*240+i,hz)*32767);mixer.mix(music,mic,2,240,out);}
            java.util.Arrays.fill(mic,(short)0);for(int block=0;block<20;block++)mixer.mix(music,mic,2,240,out);
        }
        check(mixer.feedbackA.count==4&&mixer.feedbackB.count==4,"both real streaming notch budgets are full");
        double beforeCut=0,afterCut=0;int beforeSamples=0,afterSamples=0;
        for(int block=0;block<50;block++){
            for(int i=0;i<240;i++)mic[i*2]=mic[i*2+1]=(short)Math.round(.30*tone(block*240+i,3300)*32767);
            mixer.mix(music,mic,2,240,out);
            if(block>=20&&block<30)for(short sample:out){beforeCut+=(double)sample*sample;beforeSamples++;}
            if(mixer.safety.attenuated&&block>=47)for(short sample:out){afterCut+=(double)sample*sample;afterSamples++;}
        }
        check(mixer.safety.attenuated&&!mixer.safety.latched&&mixer.safety.gainCap==50,"the software mixer receives a real gain-cut state");
        double gainRatio=Math.sqrt(afterCut/afterSamples/(beforeCut/beforeSamples));
        check(gainRatio>.3&&gainRatio<.7,"the gain cut actually lowers mixed voice including wet return");
        for(int block=0;block<80;block++){for(int i=0;i<240;i++)mic[i*2]=mic[i*2+1]=(short)Math.round(.30*tone(block*240+i,3300)*32767);mixer.mix(music,mic,2,240,out);}
        check(mixer.safety.latched&&mixer.safety.reason==2&&mixer.safety.gainCap==0,"persistent post-budget risk locks the real mixer");
        for(short sample:out)check(sample==0,"locked mixer voice is zero after its short dry fade");
        check((mixer.voiceGateState()&3)==3,"rendered zero gate and observed safety latch are published together");
        mixer.clearVoice();check(mixer.safety.latched,"ordinary stream clear preserves the latch");
        long oldFrames=mixer.safety.frames;mixer.resetVoiceSafety();check(mixer.safety.latched,"UI reset request cannot mutate the detector");
        mixer.mix(music,mic,2,0,out);check(mixer.safety.latched,"empty block cannot apply a protection reset");
        java.util.Arrays.fill(mic,(short)0);mixer.mix(music,mic,2,240,out);
        check(!mixer.safety.latched&&!mixer.safety.attenuated&&mixer.feedbackA.count==0&&mixer.feedbackB.count==0&&mixer.safety.frames==oldFrames+240,"render owner alone applies an explicit reset and preserves frame progress");

        VoiceSafety safety=new VoiceSafety();
        for(int i=0;i<48000;i++)safety.accept(i<480?(i%2==0?1:-1):0,i<480);
        check(!safety.latched&&safety.clippedSamples==480,"10 ms clipping transient is not a feedback or overload verdict");
        safety.reset();for(int i=0;i<8192;i++)safety.accept(i%2==0?1:-1,true);
        check(safety.latched&&safety.reason==1&&safety.peakHz==0,"persistent digital overload is a distinct safety reason");
        for(int i=0;i<48000;i++)safety.accept(0,false);check(safety.latched,"quiet input never clears overload");
        FeedbackNotch corrupt=new FeedbackNotch();
        for(int i=0;i<48000;i++){float value=i%3==0?Float.NaN:i%3==1?Float.POSITIVE_INFINITY:Float.MAX_VALUE;check(Float.isFinite(corrupt.process(value)),"nonfinite input never poisons IIR or analysis");}

        java.lang.management.ThreadMXBean platform=ManagementFactory.getThreadMXBean();
        if(platform instanceof com.sun.management.ThreadMXBean){
            com.sun.management.ThreadMXBean bean=(com.sun.management.ThreadMXBean)platform;
            if(bean.isThreadAllocatedMemorySupported()){
                bean.setThreadAllocatedMemoryEnabled(true);long thread=Thread.currentThread().getId();FeedbackNotch steady=new FeedbackNotch();
                for(int i=0;i<48000;i++)steady.process((float)(.22*tone(i,900)+.13*tone(i,1800)));
                long allocated=bean.getThreadAllocatedBytes(thread);
                for(int i=0;i<48000;i++)steady.process((float)(.22*tone(i,900)+.13*tone(i,1800)));
                check(bean.getThreadAllocatedBytes(thread)==allocated,"overlapped FFT plus IIR processing allocates no per-sample heap");
                PcmMixer bounded=new PcmMixer();bounded.micMuted=false;bounded.musicGain=0;bounded.setVocalPreset("studio");
                short[] backing=new short[480],voice=new short[480],mixed=new short[480];Random input=new Random(31);
                for(int i=0;i<voice.length;i++)voice[i]=(short)(input.nextInt(16001)-8000);
                for(int i=0;i<1000;i++)bounded.mix(backing,voice,2,240,mixed);
                allocated=bean.getThreadAllocatedBytes(thread);
                for(int i=0;i<1000;i++)bounded.mix(backing,voice,2,240,mixed);
                check(bean.getThreadAllocatedBytes(thread)==allocated,"the full mixer plus stereo analysis allocates no steady callback heap");
            }
        }
        System.out.println("Adaptive notch contracts passed: "+checks+" checks; harmonics/high vowel/sweep/vibrato/transients rejected, growing risk action at "+growthAction+" frames, residual notch preservation, stereo isolation, fourth/fifth-frequency budget, actual mixer gain ratio="+gainRatio+", gain-cut-to-latch="+(latchFrame-cutFrame)+" frames, latch/reset/overload, bounded no-allocation analysis; no acoustic feedback or latency claim");
    }
}
