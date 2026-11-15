package local.friday.ktv;

/** 控制状态立即上报，播放进度每秒更新，空闲时只续心跳。由控制线程独占。 */
final class PlaybackReportThrottle {
    private static final long HEARTBEAT_MS=3000;
    private long lastEpoch=-1,lastAcceptedAt;
    private String lastPayload="";

    boolean shouldSend(long epoch,String payload,boolean playing,long now){
        return epoch!=lastEpoch||!payload.equals(lastPayload)||now-lastAcceptedAt>=(playing?1000:HEARTBEAT_MS);
    }
    void accepted(long epoch,String payload,long now){
        lastEpoch=epoch;lastPayload=payload;lastAcceptedAt=now;
    }
}
