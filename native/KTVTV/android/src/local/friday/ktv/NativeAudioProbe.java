package local.friday.ktv;
/** A bounded zero-output capability check, only called by an explicitly unmuted silent test. */
public final class NativeAudioProbe {
    private static boolean available;
    static {if(android.os.Build.VERSION.SDK_INT>=29)try{System.loadLibrary("ktv_probe");available=true;}catch(UnsatisfiedLinkError ignored){}}
    private static native long[] inspect(int input,int channels);
    public static long[] run(int input,int channels){if(!available)return new long[16];try{return inspect(input,channels);}catch(RuntimeException error){return new long[16];}}
}
