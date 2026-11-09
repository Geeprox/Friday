package local.friday.ktv;

/** Local intent, protection readiness and player pause are never mute ACKs. */
public final class MicMuteConfirmation {
    private MicMuteConfirmation(){}
    /** Guard also emits raw false while STARTING/RECOVERY. It is not an unmute ACK. */
    public static boolean ownedReadback(int phase,boolean hasMute,boolean rawMuted,int status,boolean zeroDone,boolean lowDone,int gain){
        return ownedReadback(phase,hasMute,rawMuted,status,zeroDone,lowDone,gain,false);
    }
    public static boolean ownedReadback(int phase,boolean hasMute,boolean rawMuted,int status,boolean zeroDone,boolean lowDone,int gain,boolean restoring){
        // A fresh restore first zeros the old live session. Its previous positive
        // ACK cannot acknowledge the new restore before that sequence finishes.
        if(restoring&&!rawMuted)return false;
        if(!hasMute||status!=0||gain<0||gain>100)return false;
        return (phase==4&&zeroDone&&gain==0&&rawMuted)
            ||(phase==5&&lowDone&&rawMuted==(gain==0));
    }
    public static boolean known(long age,long pid,long lease,boolean owned,boolean completed,boolean stopped){
        return age>=0&&age<=750&&pid>0&&(owned?(lease>0&&completed):stopped);
    }
    public static boolean muted(long age,long pid,long lease,boolean owned,boolean completed,boolean rawMuted,boolean stopped){
        return known(age,pid,lease,owned,completed,stopped)&&(owned?rawMuted:stopped);
    }
}
