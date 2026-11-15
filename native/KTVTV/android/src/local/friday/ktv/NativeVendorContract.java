package local.friday.ktv;

import android.content.Context;
import android.media.AudioManager;
import java.util.Locale;
import local.friday.audio.NativeWet;

/** 只读检查系统音频桥及控制路径是否属于同一个运行实例。 */
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
    public static Result unavailable(){return unavailable("bridge_probe_unavailable");}
    private static Result unavailable(String reason){return new Result(false,0,-1,0,reason);}
    public static Result inspect(Context context){
        if(context==null)return unavailable();
        final long[] value;
        try{value=NativeWet.cacheContract();}catch(RuntimeException|LinkageError unavailable){return unavailable();}
        Result metadata=checkBridge(value);
        if(!metadata.ready())return metadata;
        try{
            AudioManager audio=(AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
            if(audio==null)return unavailable("framework_route_unavailable");
            Object raw=AudioManager.class.getMethod("getParameters",String.class).invoke(audio,"61961");
            String reply=raw instanceof String?(String)raw:"";
            return checkControlRoute(metadata,value[7],reply.startsWith("61961=")?reply.substring(6).trim():"");
        }catch(ReflectiveOperationException|RuntimeException|LinkageError unavailable){return unavailable("framework_route_unavailable");}
    }
    // 顺序为状态、对端 PID、内核 UID、能力、采样率、声道、PCM 格式、控制实例标记。
    public static Result fromReceipt(long[] value,String controlVersion){
        Result metadata=checkBridge(value);
        return metadata.ready()?checkControlRoute(metadata,value[7],controlVersion):metadata;
    }
    private static Result checkControlRoute(Result metadata,long cookie,String controlVersion){
        return String.format(Locale.ROOT,"1.1.3+friday.%016x",cookie).equals(controlVersion)
            ?metadata:unavailable("framework_route_mismatch");
    }
    private static Result checkBridge(long[] value){
        if(value==null||value.length!=8)return unavailable();
        // Android Linux errno 仅归入固定诊断项，不记录设备标识或系统原始回包。
        if(value[0]==-2||value[0]==-111)return unavailable("bridge_not_listening");
        if(value[0]==-1||value[0]==-13)return unavailable("bridge_access_denied");
        if(value[0]==-110)return unavailable("bridge_timed_out");
        if(value[0]!=0||value[1]<=0||value[1]>Integer.MAX_VALUE
            ||value[2]!=1041||(value[3]!=REQUIRED&&value[3]!=63)||value[4]!=48000||value[5]!=2||value[6]!=1||value[7]==0)
            return unavailable("bridge_contract_invalid");
        return new Result(true,(int)value[1],(int)value[2],(int)value[3],"ready");
    }
}
