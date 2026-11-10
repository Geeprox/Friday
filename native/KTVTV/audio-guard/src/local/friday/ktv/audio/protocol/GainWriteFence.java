package local.friday.ktv.audio.protocol;

import java.util.HashMap;

/** Metadata only. Every method runs under the same monitor as ProbePolicy.
 * A reservation survives cancellation until its remote call has really exited.
 * No remote call or wait may run while that monitor is held. */
public final class GainWriteFence {
    private static final class Write {
        final long lease,epoch,ticket;
        Write(long lease,long epoch,long ticket){this.lease=lease;this.epoch=epoch;this.ticket=ticket;}
    }
    private final HashMap<Long,Write> pending=new HashMap<>();
    private long lease,epoch,serial;
    private boolean accepting;
    public boolean beginLease(long lease,long epoch){
        if(lease<=this.lease||lease<=0||!pending.isEmpty())return false;
        this.lease=lease;this.epoch=epoch;accepting=true;return true;
    }
    public long register(long lease,long epoch,long ticket){
        if(!accepting||this.lease!=lease||this.epoch!=epoch||ticket<=0||serial==Long.MAX_VALUE)return 0;
        long token=++serial;pending.put(token,new Write(lease,epoch,ticket));return token;
    }
    public void cancel(long lease){if(this.lease==lease)accepting=false;}
    /** True only for the final exit of this cancelled lease, never a stale exit. */
    public boolean finished(long lease,long token){
        if(this.lease!=lease)return false;
        Write write=pending.get(token);
        if(write==null||write.lease!=lease||write.epoch!=epoch||write.ticket<=0)return false;
        pending.remove(token);return !accepting&&pending.isEmpty();
    }
    public boolean settled(long lease){return this.lease==lease&&pending.isEmpty();}
    public int pending(long lease){return this.lease==lease?pending.size():0;}
}
