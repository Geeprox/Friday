package local.friday.ktv;

/** Keeps the dry headroom until the wet callback has applied the lower target
 * and its two application output buffers have advanced. A requested epoch is
 * not an applied epoch, nor is this an acoustic/device-buffer measurement. */
public final class WetMixReservation {
    private int requested,held;
    private long releaseEpoch=-1,releaseCallbacks;
    public void request(int wet){requested=Math.max(0,Math.min(360,wet));held=Math.max(held,requested);}
    public void observe(int wet,long command,long applied,int transition,int ramp,long callbacks,boolean closed){
        wet=Math.max(0,Math.min(360,wet));held=Math.max(held,wet);
        if(closed){held=requested;releaseEpoch=-1;return;}
        int target=Math.max(requested,wet);
        if(command<=0||command!=applied||transition!=0||ramp!=0){releaseEpoch=-1;return;}
        if(target>=held){releaseEpoch=-1;return;}
        if(releaseEpoch!=command||callbacks<releaseCallbacks){releaseEpoch=command;releaseCallbacks=callbacks;return;}
        if(callbacks-releaseCallbacks>=2){held=target;releaseEpoch=-1;}
    }
    public int value(){return held;}
}
