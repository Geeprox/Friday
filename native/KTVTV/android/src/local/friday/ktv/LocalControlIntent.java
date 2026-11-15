package local.friday.ktv;

/** A local remote key takes effect now; stale room snapshots cannot undo it. */
public final class LocalControlIntent {
    private long token, revision=-1, baseline=-1;
    private int value;
    private boolean pending,reconcile;
    public synchronized long request(int value,long fieldRevision){
        this.value=value;baseline=fieldRevision;pending=true;reconcile=false;revision=-1;return ++token;
    }
    public synchronized void accepted(long request,long revision){if(pending&&request==token)this.revision=revision;}
    public synchronized int resolve(int server,long serverRevision,long fieldRevision){
        if(pending&&(fieldRevision>baseline||revision>=0&&serverRevision>=revision)){pending=false;reconcile=true;}
        return pending?value:server;
    }
    public synchronized boolean isCurrent(long request){return pending&&request==token;}
    public synchronized boolean pending(){return pending;}
    public synchronized boolean takeReconcile(){boolean value=reconcile;reconcile=false;return value;}
    public synchronized void clear(){++token;pending=false;reconcile=false;revision=baseline=-1;}
}
