package local.friday.ktv;

/** A local remote key takes effect now; stale room snapshots cannot undo it. */
public final class LocalControlIntent {
    private long token, revision=-1, deadline;
    private int value;
    private boolean pending,reconcile;
    public synchronized long request(int value,long now){this.value=value;pending=true;revision=-1;deadline=now+10000;return ++token;}
    public synchronized void accepted(long request,long revision){if(pending&&request==token)this.revision=revision;}
    public synchronized void failed(long request){if(request==token){reconcile=pending;pending=false;}}
    public synchronized int resolve(int server,long serverRevision,long now){
        if(pending&&(now>=deadline||revision>=0&&serverRevision>=revision)){pending=false;reconcile=true;}
        return pending?value:server;
    }
    public synchronized boolean isCurrent(long request,long now){return pending&&request==token&&now<deadline;}
    public synchronized boolean pending(){return pending;}
    public synchronized boolean takeReconcile(){boolean value=reconcile;reconcile=false;return value;}
    public synchronized void clear(){++token;pending=false;reconcile=false;revision=-1;}
}
