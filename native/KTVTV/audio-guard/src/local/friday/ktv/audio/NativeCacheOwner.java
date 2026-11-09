package local.friday.ktv.audio;

import java.util.Locale;

/** Bounded metadata/lease IPC only. No PCM crosses this APK or JNI surface. */
final class NativeCacheOwner {
    private static final boolean LOADED=load();
    private static boolean load(){try{System.loadLibrary("friday_cache_owner");return true;}catch(RuntimeException|LinkageError unavailable){return false;}}
    private static native long[] contractNative();
    private static native int beginNative(long lease,int card,int device,long expectedCookie);
    private static native int bindNative(long lease);
    private static native int heartbeatNative(long lease);
    private static native void endNative();
    private static native int muteInlineNative(long lease,boolean muted);
    private static native int notchesInlineNative(long lease,int[] frequencies,int[] cuts);
    private static volatile long verifiedCookie;
    private static volatile boolean inline;
    static boolean available(String controlVersion){
        if(!LOADED){verifiedCookie=0;inline=false;return false;}
        try{long[] v=contractNative();boolean valid=v!=null&&v.length==8&&v[0]==0&&v[1]>0&&v[2]==1041&&(v[3]==31||v[3]==63)&&v[4]==48000&&v[5]==2&&v[6]==1&&v[7]!=0
                &&String.format(Locale.ROOT,"1.1.3+friday.%016x",v[7]).equals(controlVersion);
            verifiedCookie=valid?v[7]:0;inline=valid&&v[3]==63;return valid;}
        catch(RuntimeException|LinkageError unavailable){verifiedCookie=0;inline=false;return false;}
    }
    static String baseVersion(String version){
        if(version==null||version.length()!=29||!version.startsWith("1.1.3+friday."))return version;
        for(int i=13;i<29;i++){char c=version.charAt(i);if(!((c>='0'&&c<='9')||(c>='a'&&c<='f')))return version;}
        return "1.1.3";
    }
    static boolean begin(long lease,int card,int device){try{return LOADED&&lease>0&&verifiedCookie!=0&&beginNative(lease,card,device,verifiedCookie)==0;}catch(RuntimeException|LinkageError unavailable){return false;}}
    static boolean bind(long lease){try{return LOADED&&lease>0&&bindNative(lease)==0;}catch(RuntimeException|LinkageError unavailable){return false;}}
    static boolean heartbeat(long lease){try{return LOADED&&lease>0&&heartbeatNative(lease)==0;}catch(RuntimeException|LinkageError unavailable){return false;}}
    static boolean inline(){return inline;}
    static boolean muteInline(long lease,boolean muted){try{return !inline||(LOADED&&lease>0&&muteInlineNative(lease,muted)==0);}catch(RuntimeException|LinkageError unavailable){return false;}}
    static boolean notchesInline(long lease,int[] frequencies,int[] cuts){try{return inline&&LOADED&&lease>0&&notchesInlineNative(lease,frequencies,cuts)==0;}catch(RuntimeException|LinkageError unavailable){return false;}}
    static void end(){if(LOADED)try{endNative();}catch(RuntimeException|LinkageError unavailable){}}
}
