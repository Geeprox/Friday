package local.friday.ktv.audio.protocol;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Pure state machine. Times are elapsed realtime milliseconds, never wall clock. */
public final class ProbePolicy {
    public static final int IDLE=0, VERIFYING=1, READY=2, STARTING=3, ZERO=4, LOW=5, STOPPING=6, STOPPED=7, RECOVERY=8;
    public static final int USER=1, DEADLINE=2, CLIENT_DIED=3, DEVICE_CHANGED=4, COMMAND_ERROR=5, SERVICE_END=6;
    private static final Pattern ADDRESS=Pattern.compile("card=(0|[1-9][0-9]?);device=(0|[1-9][0-9]?)");
    private static final Pattern CARD=Pattern.compile("^[ \\t]*(0|[1-9][0-9]?)[ \\t]+\\[[^\\]\\r\\n]{1,128}\\][ \\t]*:[ \\t]*(\\S[^\\r\\n]{0,512})[ \\t]*$");
    public static int[] address(String address){
        Matcher matcher=ADDRESS.matcher(address==null?"":address);
        if(!matcher.matches())return new int[]{-1,-1};
        int card=Integer.parseInt(matcher.group(1)),device=Integer.parseInt(matcher.group(2));
        return card<=31&&device<=31?new int[]{card,device}:new int[]{-1,-1};
    }
    public static final class Target {
        public final int inputId,card,device,usbCount,usbMatches,vendor,product,usbClass;
        public final boolean stereo,rate48,pcm16,audioInterface;
        public final boolean explicitSelection,descriptorBound;public final String usbIdentity;
        public Target(int inputId,String address,int usbCount,int usbMatches,int vendor,int product,int usbClass,
                      boolean stereo,boolean rate48,boolean pcm16,boolean audioInterface){
            this(inputId,address,usbCount,usbMatches,vendor,product,usbClass,stereo,rate48,pcm16,audioInterface,false,false,"");
        }
        Target(int inputId,String address,int usbCount,int usbMatches,int vendor,int product,int usbClass,
               boolean stereo,boolean rate48,boolean pcm16,boolean audioInterface,boolean explicitSelection,boolean descriptorBound,String usbIdentity){
            this.inputId=inputId;this.usbCount=usbCount;this.usbMatches=usbMatches;this.vendor=vendor;this.product=product;this.usbClass=usbClass;
            this.stereo=stereo;this.rate48=rate48;this.pcm16=pcm16;this.audioInterface=audioInterface;
            this.explicitSelection=explicitSelection;this.descriptorBound=descriptorBound;this.usbIdentity=usbIdentity;
            int[] route=address(address);card=route[0];device=route[1];
        }
        public boolean valid(){return inputId>0&&card>=0&&card<=31&&device>=0&&device<=31&&usbCount>0&&usbCount<=64&&usbMatches>0&&usbMatches<=64
            &&(usbCount==1||explicitSelection)&&(usbMatches==1||descriptorBound)
            &&usbIdentity!=null&&usbIdentity.length()<=256&&(!descriptorBound||!usbIdentity.isEmpty())
            &&vendor>0&&vendor<=65535&&product>0&&product<=65535&&usbClass>=0&&usbClass<=255
            &&stereo&&rate48&&pcm16&&audioInterface;}
        public boolean same(Target other){return other!=null&&valid()&&other.valid()&&inputId==other.inputId&&card==other.card&&device==other.device
            &&vendor==other.vendor&&product==other.product&&usbClass==other.usbClass&&usbIdentity.equals(other.usbIdentity);}
    }
    public int state=IDLE, stopReason, halStatus=Integer.MIN_VALUE, requestedGain=0;
    public long deadlineMs, verifiedAtMs, startedMs, stoppedMs, controllerDeathMs;
    public boolean owns, startReturned, startCallSucceeded, zeroCallsReturned, lowCallsReturned, stopReturned,stopAfterGainWrites;
    public boolean flagEnableReturned, flagDisableReturned;
    public boolean watchdogVerified, controllerDead, foregroundReady, independentUid, stopAfterStart;
    public int stopCalls, startCalls, gainCalls;
    public long cancellationEpoch,leaseId;
    public Target target;
    public String reject="", version="", model="";
    public static boolean cardMatches(Target target,String cards){
        if(target==null||!target.valid()||cards==null||cards.length()>32768||cards.indexOf('\0')>=0)return false;
        boolean[] seen=new boolean[32];boolean matched=false;String[] lines=cards.split("\\r?\\n",-1);if(lines.length>1024)return false;
        for(String line:lines){
            Matcher m=CARD.matcher(line);if(!m.matches())continue;int card=Integer.parseInt(m.group(1));
            if(card>31||seen[card])return false;seen[card]=true;String driver=m.group(2).trim();
            if(card==target.card)matched=driver.equals("USB-Audio")||driver.startsWith("USB-Audio - ");
        }return matched;
    }
    public boolean idleEvidence(Target t,String version,String model,int status,String cards,boolean exactPlatform){
        if(owns)return refuse("busy");
        this.version=version;this.model=model;halStatus=status;
        if(!exactPlatform||!"1.1.3".equals(version)||!"single".equals(model))return refuse("protocol_or_platform");
        if(status!=-1&&status!=2)return refuse("not_idle");
        if(t==null||!t.valid()||!cardMatches(t,cards))return refuse("target_unknown");
        target=t;reject="";state=READY;return true;
    }
    public void clientDied(long now){controllerDead=true;controllerDeathMs=now;if(owns)requestStop(CLIENT_DIED);}
    public void cancelPending(){cancellationEpoch++;}
    public boolean pendingAllowed(long capturedEpoch){return capturedEpoch==cancellationEpoch;}
    public boolean beginStart(long now,long actionMs,boolean localAction,boolean permission){
        if(owns||state!=READY||target==null||!target.valid()||(halStatus!=-1&&halStatus!=2))return refuse("not_ready");
        if(!foregroundReady||!independentUid)return refuse("independent_guard_unavailable");
        if(!localAction||!permission||actionMs<0||now<actionMs)return refuse("local_action_required");
        leaseId++;startedMs=now;deadlineMs=0;owns=true;startReturned=false;startCallSucceeded=false;
        zeroCallsReturned=lowCallsReturned=stopReturned=stopAfterStart=stopAfterGainWrites=flagEnableReturned=flagDisableReturned=false;
        controllerDead=false;stopReason=0;stoppedMs=0;requestedGain=0;
        startCalls++;state=STARTING;reject="";return true;
    }
    public boolean startFinished(long now,boolean returned,int status,boolean zeroReturned,boolean enableReturned){
        startReturned=true;startCallSucceeded=returned;zeroCallsReturned=zeroReturned;flagEnableReturned=enableReturned;halStatus=status;
        if(!returned||!zeroReturned||!enableReturned||status!=0)requestStop(COMMAND_ERROR);
        if(!owns||state==STOPPING||state==RECOVERY)return false;
        state=ZERO;return true;
    }
    public boolean allowGain(long lease,long now,long actionMs,boolean localAction,int gain){
        if((state!=ZERO&&state!=LOW)||!owns||lease!=leaseId||!zeroCallsReturned||!flagEnableReturned||controllerDead)return refuse("active_lease_required");
        if(!localAction||actionMs<0||now<actionMs||!Protocol.gainAllowed(gain))return refuse("local_gain_required");
        return true;
    }
    public void gainFinished(long lease,int gain,boolean returned,int status){
        if(!owns||lease!=leaseId||(state!=ZERO&&state!=LOW))return;
        halStatus=status;lowCallsReturned=returned;
        if(!returned||status!=0)requestStop(COMMAND_ERROR);
        if(state==ZERO||state==LOW){requestedGain=gain;state=LOW;}
    }
    public void requestStop(int reason){if(!owns)return;if(stopReason==0)stopReason=reason;state=STOPPING;}
    public void stopFinished(long now,boolean returned,int status,boolean issuedAfterStartReturned,boolean disableReturned,boolean issuedAfterGainWrites){
        stopCalls++;halStatus=status;stopReturned=returned;flagDisableReturned=disableReturned;
        stopAfterStart=issuedAfterStartReturned&&startReturned;
        stopAfterGainWrites=issuedAfterGainWrites;
        if(returned&&disableReturned&&status==2&&stopAfterStart&&stopAfterGainWrites){owns=false;stoppedMs=now;state=STOPPED;}
        else state=RECOVERY;
    }
    public void recoverOwnedMarker(){owns=true;startReturned=true;state=RECOVERY;reject="previous_guard_exit_cleanup_unknown";}
    private boolean refuse(String reason){reject=reason;return false;}
}
