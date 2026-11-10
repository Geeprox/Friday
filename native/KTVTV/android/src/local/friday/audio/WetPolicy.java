package local.friday.audio;

/** Pure lease/freshness policy. Its only cleanup action is closeWet, never dry STOP. */
public final class WetPolicy {
    public static final long HEARTBEAT_MS=1500;
    private long lease,lastHeartbeat,openedMs,armNonce;private int input,output;private boolean active,armed;
    private String reason="idle";
    public boolean start(long lease,int input,int output,long now){
        if(active||lease<=0||input<=0||output<=0||now<0){reason="invalid_wet_start";return false;}
        this.lease=lease;this.input=input;this.output=output;lastHeartbeat=now;active=true;armed=false;openedMs=armNonce=0;reason="";return true;
    }
    public boolean heartbeat(long lease,int input,int output,boolean dryActive,long now){
        if(!active)return false;
        if(!dryActive||this.lease!=lease||this.input!=input||this.output!=output){stop("dry_lease_or_route_changed");return false;}
        if(now<lastHeartbeat||now-lastHeartbeat>HEARTBEAT_MS){stop("dry_owner_heartbeat_stale");return false;}
        lastHeartbeat=now;return true;
    }
    public boolean healthy(long now){if(active&&(now<lastHeartbeat||now-lastHeartbeat>HEARTBEAT_MS))stop("dry_owner_heartbeat_stale");return active;}
    public void opened(long nonce,long now){if(active){armNonce=nonce;openedMs=now;lastHeartbeat=now;armed=false;}}
    public boolean confirm(long nonce,long sentMs,long now){if(active&&armNonce>0&&nonce==armNonce&&sentMs>openedMs&&sentMs<=now)armed=true;return armed;}
    public boolean armed(){return armed;}
    public long nonce(){return armNonce;}
    public long openedMs(){return openedMs;}
    public void stop(String reason){active=false;armed=false;armNonce=0;this.reason=reason;}
    public boolean active(){return active;}
    public long lease(){return lease;}
    public int input(){return input;}
    public int output(){return output;}
    public String reason(){return reason;}
}
