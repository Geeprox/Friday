package local.friday.ktv;

/** CPU-only synthetic work: no Android audio API, no device capture or speaker output. */
public final class DspBenchmark {
    public static long p95Micros(){
        PcmMixer mixer=new PcmMixer();short[] song=new short[480],voice=new short[480],output=new short[480];
        java.util.Random random=new java.util.Random(19);for(int i=0;i<480;i++){song[i]=(short)(random.nextInt(12001)-6000);voice[i]=(short)(random.nextInt(30001)-15000);}
        long[] times=new long[256];for(int i=0;i<384;i++){long start=System.nanoTime();mixer.mix(song,voice,2,240,output);if(i>=128)times[i-128]=System.nanoTime()-start;}
        java.util.Arrays.sort(times);java.util.Arrays.fill(voice,(short)0);java.util.Arrays.fill(output,(short)0);return times[243]/1000;
    }
}
