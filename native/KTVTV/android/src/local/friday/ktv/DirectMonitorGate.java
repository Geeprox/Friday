package local.friday.ktv;

/** A quiet duplex check authorizes only the unchanged diagnostic route and mode. */
public final class DirectMonitorGate {
    public static final int MODE_COUNT=3;
    private int mode;
    private long revision,verifiedRevision=-1;
    public int mode(){return mode;}
    public long revision(){return revision;}
    public boolean ready(){return verifiedRevision==revision;}
    public void invalidate(){revision++;verifiedRevision=-1;}
    public void selectMode(int next){
        if(next<0||next>=MODE_COUNT)throw new IllegalArgumentException("invalid_monitor_mode");
        if(next!=mode){mode=next;invalidate();}
    }
    public void completed(long checkedRevision,int checkedMode,boolean quiet,boolean outputOnly,boolean success){
        if(checkedRevision!=revision||checkedMode!=mode)return;
        if(quiet||!success)verifiedRevision=quiet&&!outputOnly&&success?revision:-1;
    }
}
