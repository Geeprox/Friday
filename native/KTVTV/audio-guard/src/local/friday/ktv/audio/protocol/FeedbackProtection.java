package local.friday.ktv.audio.protocol;

/** Lease-bound automatic protection. It accepts scalars, never vendor strings or PCM.
 * Guard owns this state under its existing policy lock. Attenuation never recovers
 * from a poll or a slider; only a fresh explicit reset/new lease can clear it. */
public final class FeedbackProtection {
    public static final long DEADLINE_MS=750;
    private long lease,monitorPid,frames,lastProgress=-1,sequence;
    private int gainCap=100,wet,level,reason;
    private boolean ready,latched,emergencyIssued;private long emergencySince=-1;
    private final int[] frequencies=new int[8],cuts=new int[8];
    public synchronized void begin(long lease){this.lease=lease;monitorPid=frames=sequence=0;lastProgress=-1;gainCap=100;wet=level=reason=0;ready=latched=emergencyIssued=false;emergencySince=-1;for(int i=0;i<8;i++)frequencies[i]=cuts[i]=0;}
    /** First identity is pinned for a dry lease. A replacement requires explicit reset. */
    public synchronized boolean heartbeat(long lease,long pid,long frames,boolean ready,int wet,long sent,long now){
        if(this.lease!=lease||lease<=0||pid<=0||frames<0||wet<0||wet>360||sent<0||now<sent||now-sent>DEADLINE_MS)return false;
        if(monitorPid!=0&&monitorPid!=pid){trip(4);return false;}
        if(frames<this.frames){trip(4);return false;}
        monitorPid=pid;this.wet=wet;
        if(frames>this.frames){this.frames=frames;lastProgress=now;}
        this.ready=ready&&frames>=2048&&!latched&&lastProgress>=0&&now-lastProgress<=DEADLINE_MS;
        return true;
    }
    public synchronized boolean healthy(long now){return ready&&!latched&&lastProgress>=0&&now>=lastProgress&&now-lastProgress<=DEADLINE_MS;}
    /** Stalled monitor is a protection failure, not silence and not permission to bypass. */
    public synchronized boolean watchdog(long now,boolean audible){if(audible&&!latched&&!healthy(now)){trip(4);return true;}return false;}
    public synchronized boolean observe(long lease,long pid,long seq,int severity,int hz,int currentGain){
        if(this.lease!=lease||pid!=monitorPid||seq<=sequence||seq<=0||severity<1||severity>3||hz<0||hz>12000||currentGain<0||currentGain>100)return false;
        if(severity<3&&hz<80)return false;
        sequence=seq;level=Math.max(level,severity);
        if(severity==1){
            int slot=-1;
            for(int i=0;i<8;i++)if(frequencies[i]>0&&Math.abs(frequencies[i]-hz)<=Math.max(30,hz/10)){slot=i;break;}
            if(slot<0)for(int i=0;i<8;i++)if(frequencies[i]==0){slot=i;break;}
            if(slot<0){gainCap=Math.min(gainCap,Math.max(1,currentGain/2));level=2;}
            else {frequencies[slot]=hz;cuts[slot]=Math.max(-12,cuts[slot]-6);}
            reason=1;
        }else if(severity==2){gainCap=Math.min(gainCap,Math.max(1,currentGain/2));reason=2;}
        else trip(hz==0?5:3);
        return true;
    }
    public synchronized void trip(int reason){latched=true;ready=false;gainCap=0;level=3;this.reason=reason;}
    public synchronized boolean emergencyStopDue(long now,boolean audible){
        if(!latched||!audible){emergencySince=-1;return false;}
        if(emergencySince<0)emergencySince=now;
        if(!emergencyIssued&&now-emergencySince>=500){emergencyIssued=true;return true;}return false;
    }
    public synchronized int referenceGain(int user){return latched?0:Math.min(Math.max(0,Math.min(100,user)),gainCap);}
    /** Reserve a deterministic wet-return budget; user value stays a separate setpoint. */
    public static int dryBudget(int reference,int wet){if(reference<=0)return 0;return Math.max(1,reference*1000/(1000+Math.max(0,Math.min(360,wet))));}
    public synchronized int effectiveGain(int user,long now){return healthy(now)?dryBudget(referenceGain(user),wet):0;}
    public synchronized int gainCap(){return gainCap;}public synchronized int level(){return level;}public synchronized int reason(){return reason;}public synchronized long sequence(){return sequence;}
    public synchronized long monitorPid(){return monitorPid;}public synchronized long frames(){return frames;}public synchronized int wet(){return wet;}public synchronized boolean latched(){return latched;}
    /** Native post-EQ coefficients use a 44.1 kHz model over the 48 kHz PCM path.
     * Broad Q=6 covers the observed nominal-rate uncertainty; acoustic calibration
     * is not claimed. Full tuples plus output unity prevent residual gain boosts. */
    public synchronized String eqCommand(){
        StringBuilder b=new StringBuilder("61699=");
        for(int i=0;i<8;i++){
            int f=frequencies[i]==0?1000:Math.max(80,Math.min(12000,(frequencies[i]*441+240)/480));
            if(i>0)b.append(',');int id=28+3*i;
            b.append(id).append(':').append(f).append(',').append(id+1).append(":6,").append(id+2).append(':').append(cuts[i]);
        }
        return b.append(",52:0").toString();
    }
    /** Exact client-rate scalars for the in-stream processor, not OEM 44.1k coefficients. */
    public synchronized int[][] inlineBands(){int[][] result=new int[2][8];for(int i=0;i<8;i++){result[0][i]=frequencies[i]==0?1000:Math.max(188,Math.min(6492,frequencies[i]));result[1][i]=-cuts[i];}return result;}
}
