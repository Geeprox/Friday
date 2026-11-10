package local.friday.ktv;

import java.io.*;
import java.nio.*;
import java.nio.file.*;
import java.util.*;

/** Host-side behavioral checks: no Android runtime, speaker, microphone or real music required. */
public final class PcmContractTest {
    private static void check(boolean value,String label){if(!value)throw new AssertionError(label);}
    public static void main(String[] args) throws Exception {
        PcmMixer mixer=new PcmMixer();mixer.musicGain=1;mixer.wet=0;
        short[] song=new short[480],mic=new short[480],out=new short[480];
        Arrays.fill(song,(short)1000);mixer.mix(song,mic,2,240,out);
        check(Math.abs(out[0]-1000)<=1 && Math.abs(out[100]-1000)<=1,"causal mixer preserves the first backing sample without a dry FIFO");
        Arrays.fill(song,(short)0);mixer.gainA=0;mixer.gainB=1;
        for(int i=0;i<240;i++){mic[i*2]=0;mic[i*2+1]=(short)(Math.sin(i*.3)*6000);}
        mixer.micMuted=false;for(int i=0;i<12;i++)mixer.mix(song,mic,2,240,out);check(Arrays.stream(toInts(out)).anyMatch(v->Math.abs(v)>100),"channel B independently audible after safe fade-in");
        Arrays.fill(song,Short.MAX_VALUE);Arrays.fill(mic,Short.MAX_VALUE);mixer.gainA=2;mixer.gainB=2;
        mixer.mix(song,mic,2,240,out);for(short sample:out)check(Math.abs((int)sample)<=31456,"limiter bounded");
        check(mixer.limitedSamples>0,"limiter accounting");
        mixer.reset();mixer.micMuted=true;Arrays.fill(song,(short)0);
        for(int i=0;i<16;i++)mixer.mix(song,mic,2,240,out);
        for(short sample:out)check(sample==0,"manual mute removes dry voice and reverb");
        mixer.reset();Arrays.fill(song,(short)0);Arrays.fill(mic,(short)0);mixer.mix(song,mic,2,240,out);
        for(short sample:out)check(sample==0,"stopped-session voice/reverb must not leak into next playback");
        VoiceSafety guard=new VoiceSafety();
        for(int i=0;i<48000;i++)guard.accept((float)(Math.sin(i*2*Math.PI*440/48000)*.3+Math.sin(i*2*Math.PI*880/48000)*.13+Math.sin(i*2*Math.PI*1320/48000)*.08),false);
        check(!guard.latched,"a sustained harmonic singing note is not isolated tonal risk");
        Random random=new Random(7);for(int i=0;i<48000;i++)guard.accept((random.nextFloat()-.5f)*.8f,false);
        check(!guard.latched,"broad noise must not be classified as sustained tonal feedback");
        for(int i=0;i<48000;i++)guard.accept((float)(Math.sin(i*2*Math.PI*2000/48000)*.4),false);
        check(guard.latched,"unfiltered persistent strong tonal risk escalates through gain cap to mute");
        for(int i=0;i<48000;i++)guard.accept(0,false);
        check(guard.latched,"safety mute never clears automatically");guard.reset();
        for(int i=0;i<4096;i++)guard.accept(1,true);
        check(!guard.latched,"brief clipped syllable does not lock voice mute");guard.reset();
        for(int i=0;i<96000;i++)guard.accept((float)(Math.sin(i*2*Math.PI*220/48000)*.25+Math.sin(i*2*Math.PI*440/48000)*.08+Math.sin(i*2*Math.PI*660/48000)*.06),i%1024<128);
        check(!guard.latched,"intermittent clipping is not sustained feedback");guard.reset();
        for(int i=0;i<8192;i++)guard.accept(1,true);
        check(guard.latched&&guard.reason==1,"sustained nearly railed input still locks voice mute");
        PcmRing ring=new PcmRing(2,2);ring.put(new short[]{1,2,3,4,5,6},6);
        short[] taken=new short[6];check(ring.take(taken,3)==2,"bounded queue");check(Arrays.equals(taken,new short[]{3,4,5,6,0,0}),"oldest dropped and underflow silent");check(ring.droppedFrames==1,"drop count");
        ring.put(new short[]{7,8},2);ring.clear();check(ring.take(taken,3)==0 && Arrays.equals(taken,new short[6]),"capture queue cleared at stop");
        byte[] h=new byte[44+960];ByteBuffer b=ByteBuffer.wrap(h).order(ByteOrder.LITTLE_ENDIAN);
        b.put("RIFF".getBytes("US-ASCII")).putInt(h.length-8).put("WAVEfmt ".getBytes("US-ASCII"));
        b.putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(192000).putShort((short)4).putShort((short)16).put("data".getBytes("US-ASCII")).putInt(960);
        File file=new File(args[0],"contract.wav");Files.write(file.toPath(),h);
        try(WavSource source=new WavSource(file)){check(source.frames==240,"duration contract");check(source.read(out,240)==240,"PCM block read");source.seek(20);check(source.position==20,"seek");}
        h[24]=0;Files.write(file.toPath(),h);boolean rejected=false;
        try(WavSource source=new WavSource(file)){}catch(IOException error){rejected=true;}check(rejected,"wrong sample rate rejected");
        if(args.length>1){try(WavSource actual=new WavSource(new File(args[1]))){check(actual.frames>0,"server artifact accepted");actual.seek(48000);check(actual.read(out,240)==240,"server artifact seek/read");}}
        System.out.println("PCM contracts passed; no audio device started");
    }
    private static int[] toInts(short[] value){int[] result=new int[value.length];for(int i=0;i<value.length;i++)result[i]=value[i];return result;}
}
