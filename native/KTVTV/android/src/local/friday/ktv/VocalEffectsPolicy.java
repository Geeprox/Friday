package local.friday.ktv;

/** Pure authorization policy for the isolated voice observer/wet-only process.
 * It has no dry start/stop operation. A changed lease revokes authorization;
 * the controller chooses explicit audition or protected-mic authorization. */
public final class VocalEffectsPolicy {
    /** Inline mixing owns its one headroom split; a second legacy reservation
     * would both attenuate the voice twice and prevent leaving the dry preset. */
    public static boolean mixReady(boolean inline,boolean healthy,int appliedUser,int wantedUser,int appliedGain,int referenceGain,int reservation,int profile){
        if(!healthy||appliedUser!=wantedUser||appliedGain<0||referenceGain<0||profile<0||profile>=WET_PERMILLE.length)return false;
        return inline?appliedGain==referenceGain&&reservation==0:
            appliedGain<=dryBudget(referenceGain,WET_PERMILLE[profile])&&reservation>=WET_PERMILLE[profile];
    }
    public static final String[] KEYS={"dry","pop","ktv","power","opera","studio","echo"};
    public static final String[] LABELS={"关闭混响","流行","KTV","唱将","歌剧","清晰小房间","短回声"};
    public static final int[] WET_PERMILLE={0,250,320,280,360,180,160};
    public static int dryBudget(int gain,int wet){return gain<=0?0:Math.max(1,Math.max(0,Math.min(100,gain))*1000/(1000+Math.max(0,Math.min(360,wet))));}
    public static boolean resetRequired(boolean fault,int level,boolean latched,int gainCap){return fault||level>0||latched||gainCap<100;}
    public static int id(String value){for(int i=0;i<KEYS.length;i++)if(KEYS[i].equals(value))return i;return 0;}
    public static boolean nativeMatches(int desired,int gain,boolean muted,int profile,int wet,int effectiveGain,int effectiveMuted,int bypass,long command,long applied){
        return desired>0&&desired<KEYS.length&&profile==desired&&wet==WET_PERMILLE[desired]&&effectiveGain==gain*10
            &&effectiveMuted==(muted?1:0)&&bypass==(muted?1:0)&&command>0&&command==applied;
    }
    public static final class DryLease {
        public final long pid,lease;public final int input,output;
        public DryLease(long pid,long lease,int input,int output){this.pid=pid;this.lease=lease;this.input=input;this.output=output;}
        public boolean valid(){return pid>0&&lease>0&&input>0&&output>0;}
        public boolean same(DryLease other){return other!=null&&pid==other.pid&&lease==other.lease&&input==other.input&&output==other.output;}
    }
    private DryLease live,authorizedLease;
    private boolean allowed,muted=true,authorized,readyOnce,fault,explicitPending,monitor;
    private int gain,profile;
    /** Returns true only when an existing authorization has just been revoked. */
    public boolean environment(DryLease live,boolean allowed,boolean muted,int gain){
        this.live=live;this.allowed=allowed;this.muted=muted;this.gain=Math.max(0,Math.min(100,gain));
        if(authorized&&(!allowed||live==null||!authorizedLease.same(live))){revoke();return true;}return false;
    }
    public boolean audition(int profile){
        if(profile<=0||profile>=KEYS.length||!canAudition())return false;
        boolean same=authorized&&!fault&&authorizedLease.same(live);
        authorizedLease=live;authorized=true;fault=false;explicitPending=true;this.profile=profile;if(!same)readyOnce=false;return true;
    }
    public boolean protect(){
        if(!allowed||live==null||!live.valid()||muted||gain==0||fault)return false;
        authorizedLease=live;authorized=true;readyOnce=false;explicitPending=true;monitor=true;return true;
    }
    public void resetFault(){revoke();fault=false;}
    public boolean monitoring(){return monitor;}
    public boolean canAudition(){return allowed&&live!=null&&live.valid()&&!muted&&gain>0;}
    public void select(int profile){if(profile>=0&&profile<KEYS.length)this.profile=profile;}
    public boolean shouldStart(boolean opening,boolean active,boolean closing){return authorized&&!fault&&allowed&&!muted&&gain>0&&(profile>0||monitor)&&!opening&&!active&&!closing&&(explicitPending||readyOnce);}
    public void startIssued(){explicitPending=false;}
    public void observeReady(int actualProfile,boolean matchingNative){if(authorized&&!fault&&matchingNative&&actualProfile==profile&&(profile>0||monitor))readyOnce=true;}
    public void fail(){fault=true;authorized=false;readyOnce=false;explicitPending=false;}
    public void revoke(){authorized=false;readyOnce=false;explicitPending=false;}
    public boolean authorized(){return authorized&&!fault;}
    public boolean available(){return authorized()&&readyOnce;}
    public boolean fault(){return fault;}
    public boolean muted(){return muted||gain==0;}
    public int gain(){return gain;}
    public int profile(){return profile;}
    public DryLease lease(){return authorizedLease;}
}
