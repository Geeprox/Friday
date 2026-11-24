package local.friday.ktv;

import java.util.function.LongSupplier;

/** 用当前服务的响应时间校验票据和指令，之后按电视的单调时钟推进。 */
final class KtvServerClock {
    private static final class Sample {
        final long epochMillis, requestedAt, receivedAt;

        Sample(long epochMillis, long requestedAt, long receivedAt) {
            this.epochMillis=epochMillis;
            this.requestedAt=requestedAt;
            this.receivedAt=receivedAt;
        }
    }

    private final LongSupplier elapsedMillis, wallMillis;
    private volatile Sample sample;

    KtvServerClock(LongSupplier elapsedMillis, LongSupplier wallMillis) {
        this.elapsedMillis=elapsedMillis;
        this.wallMillis=wallMillis;
    }

    long elapsedMillis() {
        return elapsedMillis.getAsLong();
    }

    synchronized void observe(long epochMillis, long requestedAt, long receivedAt) {
        if(epochMillis<=0||receivedAt<requestedAt)return;
        // 较早发出的慢请求可能在新快照之后返回。
        if(sample!=null&&(requestedAt<sample.requestedAt||receivedAt<sample.receivedAt))return;
        sample=new Sample(epochMillis,requestedAt,receivedAt);
    }

    long epochSeconds() {
        Sample current=sample;
        if(current==null)return wallMillis.getAsLong()/1000;
        return (current.epochMillis+Math.max(0,elapsedMillis()-current.receivedAt))/1000;
    }
}
