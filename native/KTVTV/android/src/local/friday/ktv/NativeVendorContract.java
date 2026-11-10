package local.friday.ktv;

import android.content.Context;
import android.media.AudioManager;
import java.util.Locale;
import local.friday.audio.NativeWet;

/** Read-only, bounded capability handshake. Package presence, SDK version and
 * a setter ACK cannot substitute for the running platform bridge's proof. */
public final class NativeVendorContract {
    private NativeVendorContract(){}
    public static final int REQUIRED=31;
    public static final class Result {
        private final boolean available;
        private final int pid,uid,caps;
        private final String reason;
        private Result(boolean available,int pid,int uid,int caps,String reason){this.available=available;this.pid=pid;this.uid=uid;this.caps=caps;this.reason=reason;}
        public boolean ready(){return available;}
        public boolean silentStart(){return available&&(caps&1)!=0;}
        public boolean cacheOnly(){return available&&(caps&2)!=0;}
        public int bridgePid(){return pid;}
        public int bridgeUid(){return uid;}
        public boolean inlineVoice(){return available&&caps==63;}
        public String reason(){return reason;}
    }
    public static Result unavailable(){return new Result(false,0,-1,0,"strict_platform_bridge_unavailable");}
    public static Result inspect(Context context){
        if(context==null)return unavailable();
        try{
            long[] value=NativeWet.cacheContract();
            if(value==null||value.length!=8||value[0]!=0)return unavailable();
            AudioManager audio=(AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
            if(audio==null)return unavailable();
            Object raw=AudioManager.class.getMethod("getParameters",String.class).invoke(audio,"61961");
            String reply=raw instanceof String?(String)raw:"";
            return fromReceipt(value,reply.startsWith("61961=")?reply.substring(6).trim():"");
        }catch(ReflectiveOperationException|RuntimeException|LinkageError unavailable){return unavailable();}
    }
    // status, peer/server PID, kernel peer UID, closed capabilities, rate,
    // channels, PCM format, per-host route cookie. These are software capabilities, not acoustics.
    public static Result fromReceipt(long[] value,String controlVersion){
        if(value==null||value.length!=8||value[0]!=0||value[1]<=0||value[1]>Integer.MAX_VALUE
            ||value[2]!=1041||(value[3]!=REQUIRED&&value[3]!=63)||value[4]!=48000||value[5]!=2||value[6]!=1||value[7]==0
            ||!String.format(Locale.ROOT,"1.1.3+friday.%016x",value[7]).equals(controlVersion))return unavailable();
        return new Result(true,(int)value[1],(int)value[2],(int)value[3],"ready");
    }
}
