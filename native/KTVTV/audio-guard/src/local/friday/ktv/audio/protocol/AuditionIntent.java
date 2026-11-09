package local.friday.ktv.audio.protocol;

/** One explicit foreground user gesture may request LOW only for its newly started lease. */
public final class AuditionIntent {
    private boolean armed;
    private long requestedMs,expectedLease;
    public void arm(long now,long priorLease){armed=true;requestedMs=now;expectedLease=priorLease+1;}
    public void cancel(){armed=false;}
    public boolean isArmed(){return armed;}
    public boolean consumeZero(long now,long lease,long started,boolean zero,boolean owns,boolean focused,boolean confirmed){
        if(!armed)return false;
        if(!focused||!confirmed||now<requestedMs){cancel();return false;}
        if(!zero||!owns)return false;
        if(lease!=expectedLease||started<requestedMs||started>now){cancel();return false;}
        cancel();return true;
    }
}
