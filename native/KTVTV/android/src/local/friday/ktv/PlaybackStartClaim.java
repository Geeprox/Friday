package local.friday.ktv;

import java.util.UUID;

/** 一次电视启播意图及其 NAS 预约。超时重试沿用同一个请求号。 */
final class PlaybackStartClaim {
    static final int MAX_LEASE_MS=30000;
    private static final int[] RETRY_MS={350,1200,3000};
    final KtvClient owner;
    final String room,song,request=UUID.randomUUID().toString();
    final long connection;
    final int generation;
    private boolean attempted,cancelled;
    private boolean inFlight;
    private int failures;
    private long retryAt,expiresAt;

    PlaybackStartClaim(KtvClient owner,String room,String song,long connection,int generation){
        this.owner=owner;this.room=room;this.song=song;this.connection=connection;this.generation=generation;
    }
    boolean canStart(long now){return expiresAt>now;}
    boolean expired(long now){return expiresAt!=0&&now>=expiresAt;}
    boolean canRequest(long now){return !inFlight&&now>=retryAt;}
    void begin(){inFlight=true;}
    void accepted(long requestedAt,int leaseMillis){
        inFlight=false;
        // 从发送时间算保守有效期，响应在路上的时间不会延长 NAS 预约。
        expiresAt=requestedAt+leaseMillis;
    }
    void retry(long now){
        inFlight=false;
        retryAt=now+RETRY_MS[failures];
        failures=Math.min(failures+1,RETRY_MS.length-1);
    }
    synchronized boolean markSending(){
        if(cancelled)return false;
        attempted=true;
        return true;
    }
    synchronized boolean cancel(){
        if(cancelled)return false;
        cancelled=true;
        return attempted;
    }
}
