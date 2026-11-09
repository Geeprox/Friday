package local.friday.ktv;
/** A quiet transport gate; not an acoustic latency or singing-quality result. */
public final class MonitorStability {
    private static long interval(long[] first,long[] last,long fallbackMs){
        return first.length>48&&last.length>48?last[48]-first[48]:fallbackMs;
    }
    public static boolean outputHealthy(long[] first,long[] last,long steadyMs,long elapsedMs){
        return failures(first,last,steadyMs,elapsedMs,false)==0;
    }
    public static boolean healthy(long[] first,long[] last,long steadyMs,long elapsedMs){
        return failures(first,last,steadyMs,elapsedMs,true)==0;
    }
    // Bitmask retains which unchanged transport requirement rejected a run:
    // 1 missing samples, 2 duration, 4 native error, 8 audible quiet check,
    // 16 performance, 32 callback stall, 64 output rate, 128 input rate,
    // 256 new missing voice frames, 512 voice/output rate mismatch.
    public static int failures(long[] first,long[] last,long steadyMs,long elapsedMs,boolean duplex){
        if(first==null||last==null||first.length<18||last.length<18)return 1;
        steadyMs=interval(first,last,steadyMs);
        int failures=steadyMs<10000||elapsedMs<14500?2:0;
        if(last[7]!=0)failures|=4;
        if(last[6]!=0)failures|=8;
        if(last[8]!=12&&last[8]!=10)failures|=16;
        if(last[14]>50000||last.length>50&&last[50]>50000)failures|=32;
        long expected=steadyMs*48,output=last[1]-first[1],input=last[0]-first[0],voice=last[2]-first[2];
        if(output<expected*98/100||output>expected*102/100)failures|=64;
        if(duplex){
            if(input<expected*98/100||input>expected*102/100)failures|=128;
            if(last[5]!=first[5])failures|=256;
            if(voice<output*995/1000||voice>output*1005/1000)failures|=512;
        }
        return failures;
    }
}
