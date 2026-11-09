package local.friday.ktv.audio.protocol;

/** Latest signed owner request wins; a queued nonzero gain cannot overtake mute or hard stop. */
public final class GainRequestGate {
    private long revision,lease;private int wanted,effective;private boolean inFlight;
    public long request(){return ++revision;}
    public long request(long lease,int wanted,int effective){
        if(inFlight&&this.lease==lease&&this.wanted==wanted&&this.effective==effective)return 0;
        this.lease=lease;this.wanted=wanted;this.effective=effective;inFlight=true;return ++revision;
    }
    public void finished(long ticket){if(current(ticket))inFlight=false;}
    public void cancel(){revision++;inFlight=false;}
    public boolean current(long captured){return captured==revision;}
}
