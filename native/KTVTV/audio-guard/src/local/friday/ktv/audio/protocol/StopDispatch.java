package local.friday.ktv.audio.protocol;

/** All methods are called under the same policy monitor as startReturned/owns. */
public final class StopDispatch {
    private long lease,serial;
    private boolean inFlight;
    public long begin(long lease,boolean owns){
        if(!owns||lease<0||inFlight||serial==Long.MAX_VALUE)return 0;
        this.lease=lease;inFlight=true;return ++serial;
    }
    public boolean current(long lease,long ticket){return inFlight&&this.lease==lease&&serial==ticket;}
    public boolean finish(long lease,long ticket,long currentLease,boolean owns,boolean startReturned,boolean writesSettled,boolean issuedAfterSettled){
        if(!current(lease,ticket))return false;
        inFlight=false;
        // An early STOP never spins while a call is blocked. The final writer
        // or START completion will request another STOP; if it completed during
        // this STOP, the completion below schedules that final attempt instead.
        return currentLease==lease&&owns&&startReturned&&writesSettled&&!issuedAfterSettled;
    }
}
