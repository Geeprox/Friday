package local.friday.ktv;

import java.lang.management.ManagementFactory;
import java.util.Arrays;
import java.util.Random;
import java.util.concurrent.atomic.AtomicBoolean;
import local.friday.audio.WetProfiles;

/** JVM-only sample synthesis. Never loads JNI, Android audio, models or media. */
public final class CompatVocalEffectsTest {
    private static int checks;
    private static void check(boolean value,String text){checks++;if(!value)throw new AssertionError(text);}
    private static boolean zero(short[] values){for(short value:values)if(value!=0)return false;return true;}
    private static double energy(CompatVocalEffects fx,int profile,float amplitude,float frequency){
        fx.configure(profile,360);fx.clear();double total=0;
        for(int i=0;i<48000*3;i++){
            float x=amplitude*(float)Math.sin(2*Math.PI*frequency*i/48000);fx.process(x,-x,true);
            if(i>=48000*2)total+=(double)fx.left*fx.left+(double)fx.right*fx.right;
        }
        return total;
    }
    public static void main(String[] args)throws Exception{
        CompatVocalEffects fx=new CompatVocalEffects();
        check(VocalEffectsPolicy.KEYS.length==7&&WetProfiles.KEYS.length==7,"all Java routes expose seven stable IDs");
        for(int i=0;i<7;i++)check(WetProfiles.KEYS[i].equals(VocalEffectsPolicy.KEYS[i])&&WetProfiles.LABELS[i].equals(VocalEffectsPolicy.LABELS[i])
            &&Math.round(WetProfiles.DEFAULT_WET[i]*1000)==VocalEffectsPolicy.WET_PERMILLE[i],"preset metadata is identical across routes");
        int[] onset={-1,863,1247,1727,2063,527,3744},first=new int[7];Arrays.fill(first,-1);
        double[] early=new double[7],tail=new double[7],late=new double[7];
        for(int p=0;p<7;p++){
            fx.configure(WetProfiles.KEYS[p]);fx.clear();
            for(int i=0;i<48000*7;i++){
                fx.process(i==0?1:0,i==0?-1:0,true);float l=fx.left,r=fx.right;
                check(Float.isFinite(l)&&Float.isFinite(r)&&Math.abs(l)<=.9f&&Math.abs(r)<=.9f,"finite pure wet impulse");
                if(i==0)check(l==0&&r==0,"no current-sample dry copy");
                if(first[p]<0&&(l!=0||r!=0))first[p]=i;
                double e=(double)l*l+(double)r*r;
                if(i<24000)early[p]+=e;if(i>=24000&&i<96000)tail[p]+=e;if(i>48000*6)late[p]+=e;
            }
            check(first[p]==onset[p],"native-compatible first wet frame "+p);
            if(p==0)check(early[p]==0&&tail[p]==0,"dry preset emits no wet");
            else check(early[p]>0&&tail[p]>0&&late[p]<early[p]*.0001,"audible response and bounded decaying tail "+p);
        }
        check(tail[5]/early[5]<tail[1]/early[1]*.25,"studio keeps a shorter late tail");
        check(tail[4]/early[4]>tail[2]/early[2],"opera retains its longer decay");

        fx.configure("echo");fx.clear();int left=-1,right=-1;double original=0,repeat=0,next=0;
        for(int i=0;i<48000;i++){
            fx.process(i==0?.2f:0,0,true);if(left<0&&fx.left!=0)left=i;if(right<0&&fx.right!=0)right=i;
            if(i>=3744&&i<4224)original+=(double)fx.left*fx.left;
            if(i>=8736&&i<9216)repeat+=(double)fx.right*fx.right;
            if(i>=12480&&i<12960)next+=(double)fx.left*fx.left;
        }
        check(left==3744&&right==8736,"slapback has no early cross-channel dry path");
        check(repeat>original*.001&&repeat<original*.1&&next>0&&next<repeat*.1,"low-feedback repeats lose energy");
        double quiet=energy(fx,5,.04f,997),doubleQuiet=energy(fx,5,.08f,997),loud=energy(fx,5,.60f,997);
        double loudRatio=loud/(quiet*225);
        check(quiet>0&&Math.abs(doubleQuiet/quiet-4)<.01,"quiet input is linear");
        check(loudRatio>.1&&loudRatio<.65,"sustained loud input lowers only wet prominence");
        double phraseTail=0,tailEnd=0,recovered=0;
        for(int i=0;i<48000*2;i++){
            fx.process(0,0,true);double e=(double)fx.left*fx.left+(double)fx.right*fx.right;
            if(i<24000)phraseTail+=e;if(i>72000)tailEnd+=e;
        }
        check(phraseTail>quiet*.01&&tailEnd<quiet*.0001,"wet protection releases a real tail then decays");
        for(int i=0;i<48000*3;i++){
            float x=.04f*(float)Math.sin(2*Math.PI*997*i/48000);fx.process(x,-x,true);
            if(i>=48000*2)recovered+=(double)fx.left*fx.left+(double)fx.right*fx.right;
        }
        check(Math.abs(recovered/quiet-1)<.001,"quiet sensitivity returns after a loud phrase without reset");
        double mid=energy(fx,6,.04f,997);
        check(energy(fx,6,.04f,40)<mid*.3&&energy(fx,6,.04f,18000)<mid*.3,"rumble and harsh treble shaping");

        for(int p=1;p<7;p++){
            fx.configure(p,360);fx.clear();
            for(int i=0;i<48000;i++){fx.process(Float.NaN,Float.POSITIVE_INFINITY,true);check(fx.left==0&&fx.right==0,"invalid floats do not enter the tail");}
            for(int i=0;i<48000;i++){
                float x=i%5==0?Float.NaN:((i&1)==0?Float.MAX_VALUE:-Float.MAX_VALUE);fx.process(x,-x,true);
                check(Float.isFinite(fx.left)&&Float.isFinite(fx.right)&&Math.abs(fx.left)<=.9f&&Math.abs(fx.right)<=.9f,"corrupt finite input stays bounded");
            }
            fx.process(.5f,.5f,false);check(fx.left==0&&fx.right==0&&fx.activeWet()==0,"mute immediately clears wet output");
            for(int i=0;i<9000;i++){fx.process(0,0,true);check(fx.left==0&&fx.right==0,"unmute never revives old tail");}
        }
        for(int kind=0;kind<3;kind++){
            try{if(kind==0)fx.configure("missing");if(kind==1)fx.configure("studio",361);if(kind==2)fx.configure(-1,180);throw new AssertionError("invalid setting accepted");}
            catch(IllegalArgumentException expected){}
        }

        // Requested settings wait for fixed, amplitude-independent wet fades.
        fx.configure("pop");fx.clear();for(int i=0;i<48000;i++)fx.process(.1f,-.1f,true);
        long old=fx.status().appliedRevision,wanted=fx.configure("echo");
        check(!fx.status().applied()&&fx.status().appliedRevision==old,"setter ACK is not render application");
        for(int i=0;i<239;i++)fx.process(0,0,true);
        check(fx.status().transitionFrames==1&&!fx.status().applied(),"wet fade out is 240 samples");
        fx.process(0,0,true);check("echo".equals(fx.status().profile)&&!fx.status().applied(),"new profile needs its own gain fade");
        for(int i=0;i<239;i++)fx.process(0,0,true);check(!fx.status().applied(),"wet fade in is not an intent ACK");
        fx.process(0,0,true);check(fx.status().applied()&&fx.status().appliedRevision==wanted,"actual new revision completes after both fades");
        for(int i=0;i<9000;i++){fx.process(0,0,true);check(fx.left==0&&fx.right==0,"old tail cleared on profile switch");}
        wanted=fx.configure("echo",161);for(int i=0;i<239;i++)fx.process(0,0,true);check(!fx.status().applied(),"tiny wet change still takes 240 frames");
        fx.process(0,0,true);check(fx.status().appliedRevision==wanted&&fx.status().requestedWetPermille==161,"wet amount application is reported");
        fx.configure("dry");fx.process(.7f,-.7f,true);check(fx.left==0&&fx.right==0&&fx.status().gated&&fx.status().applied(),"dry disables and clears immediately");

        // One bounded control word prevents legacy/UI races from reverting a
        // user's explicit profile. Sample processing does not take a lock.
        final CompatVocalEffects concurrent=new CompatVocalEffects();AtomicBoolean done=new AtomicBoolean();
        Thread setter=new Thread(()->{for(int i=0;i<20000;i++)concurrent.configure(1+i%6,160+i%201);done.set(true);});setter.start();
        int rendered=0;while(!done.get()||rendered<20000){concurrent.configureLegacy(350);concurrent.process(.1f,-.1f,true);rendered++;}
        setter.join();wanted=concurrent.configure("studio");concurrent.configureLegacy(350);
        for(int i=0;i<720;i++)concurrent.process(0,0,true);
        check(concurrent.status().appliedRevision==wanted&&"studio".equals(concurrent.status().profile)&&concurrent.status().requestedWetPermille==180,"latest explicit profile wins over legacy slider");

        // Check zero steady-state allocations when the host VM exposes its
        // allocation counter. The measurement contains no diagnostics objects.
        java.lang.management.ThreadMXBean platform=ManagementFactory.getThreadMXBean();
        if(platform instanceof com.sun.management.ThreadMXBean){
            com.sun.management.ThreadMXBean bean=(com.sun.management.ThreadMXBean)platform;
            if(bean.isThreadAllocatedMemorySupported()){
                bean.setThreadAllocatedMemoryEnabled(true);long thread=Thread.currentThread().getId();
                for(int i=0;i<200000;i++)concurrent.process(.02f,-.02f,true);
                long allocated=bean.getThreadAllocatedBytes(thread);
                for(int i=0;i<200000;i++)concurrent.process(.02f,-.02f,true);
                check(bean.getThreadAllocatedBytes(thread)==allocated,"no heap allocation in steady wet processing");
            }
        }

        // The real mixer preserves the first backing sample and gives every
        // selected preset a fixed revision, stereo wet return and shared budget.
        PcmMixer mixer=new PcmMixer();mixer.musicGain=1;short[] music={1000,-1000},mic={0},output=new short[2];
        mixer.mix(music,mic,1,1,output);check(Math.abs(output[0]-1000)<=1&&Math.abs(output[1]+1000)<=1,"no dry/backing FIFO or lookahead");
        long gate=mixer.voiceGateState();check((gate>>>3)==1&&(gate&5)==5,"voice gate receipt follows a real muted render frame");
        mixer.reset();mixer.clearVoice();check(mixer.voiceGateState()==gate,"reset and clear cannot manufacture render progress");
        mixer.mix(music,mic,1,0,output);check(mixer.voiceGateState()==gate,"empty mix cannot acknowledge a voice gate");
        mixer.micMuted=false;mixer.safety.latched=true;mixer.mix(music,mic,1,1,output);
        check((mixer.voiceGateState()&7)==3,"observed safety latch and dry zero are distinct from a mute intent");
        Random random=new Random(58);short[] song=new short[480],voice=new short[480],out=new short[480];
        for(String key:WetProfiles.KEYS)for(float gain:new float[]{.01f,.2f,.7f,1})for(int wet:new int[]{Math.round(WetProfiles.DEFAULT_WET[WetProfiles.id(key)]*1000),360}){
            mixer=new PcmMixer();mixer.musicGain=0;mixer.micMuted=false;mixer.gainA=mixer.gainB=gain;
            long revision=mixer.setVocalPreset(key,wet);int maximum=(int)Math.ceil(gain*32767)+1;
            for(int b=0;b<64;b++){
                for(int i=0;i<voice.length;i++)voice[i]=(short)(random.nextInt(65536)-32768);
                mixer.mix(song,voice,2,240,out);for(short sample:out)check(Math.abs((int)sample)<=maximum,"dry plus wet stays in requested digital voice budget");
            }
            CompatVocalEffects.Status status=mixer.compatEffectsStatus();
            check(status.capable&&status.pureWet&&!status.acousticallyVerified&&status.applied()&&status.appliedRevision==revision&&key.equals(status.profile),"all seven software effects actually apply");
            gate=mixer.voiceGateState();check((gate&1)==0,"dry effect gating does not acknowledge dry voice mute");
            mixer.micMuted=true;check(mixer.voiceGateState()==gate,"mute intent alone cannot update the render receipt");
            Arrays.fill(voice,(short)0);mixer.mix(song,voice,2,1,out);
            check(mixer.compatEffectsStatus().gated&&mixer.compatEffectsStatus().wetPermille==0,"manual mute clears wet on first sample");
            check((mixer.voiceGateState()&1)==0,"one muted sample cannot claim a completed dry fade");
            mixer.mix(song,voice,2,240,out);mixer.mix(song,voice,2,240,out);
            check(zero(out),"muted mixer has no voice or wet tail after short dry fade");
            long mutedGate=mixer.voiceGateState();check((mutedGate>>>3)>(gate>>>3)&&(mutedGate&5)==5,"zero dry gate and observed mute require later render frames");
            for(int i=0;i<12;i++)mixer.mix(song,voice,2,240,out);
            mixer.micMuted=false;mixer.mix(song,voice,2,240,out);check(zero(out),"unmute does not replay a stopped wet tail");
        }
        // The same timeline split into arbitrary block sizes gives exactly the
        // same PCM. Effect delays do not become block-dependent buffering.
        short[] longSong=new short[8192],longMic=new short[8192],whole=new short[8192],split=new short[8192];
        for(int i=0;i<longSong.length;i++){longSong[i]=(short)(random.nextInt(10001)-5000);longMic[i]=(short)(random.nextInt(20001)-10000);}
        PcmMixer one=new PcmMixer(),many=new PcmMixer();one.micMuted=many.micMuted=false;one.setVocalPreset("studio");many.setVocalPreset("studio");
        one.mix(longSong,longMic,2,4096,whole);int at=0,block=0;int[] sizes={1,17,128,240,37};
        while(at<4096){int count=Math.min(sizes[block++%sizes.length],4096-at);short[] part=new short[count*2];
            many.mix(Arrays.copyOfRange(longSong,at*2,(at+count)*2),Arrays.copyOfRange(longMic,at*2,(at+count)*2),2,count,part);
            System.arraycopy(part,0,split,at*2,part.length);at+=count;}
        check(Arrays.equals(whole,split),"sample causality is invariant to callback block size");
        System.out.println("CompatVocalEffectsTest passed: "+checks+" checks; seven impulse/tail profiles, wet-only dynamics, finite input, exact fades/revisions, mute and budget, no sample allocations/FIFO; normalized loud wet="+loudRatio+"; no audio/device/native loading");
    }
}
