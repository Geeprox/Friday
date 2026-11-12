package local.friday.ktv;

/** A visible local audition owns only the snapshot it started from. A newer
 * phone revision, or the acknowledged revision, can replace it. */
public final class VocalEffectsIntent {
    private long token,baseline=-1,accepted=-1;
    private int value;
    private boolean pending,failed;
    public synchronized long request(int value,long baseline){this.value=value;this.baseline=baseline;accepted=-1;pending=true;failed=false;return ++token;}
    public synchronized void accepted(long request,long revision){if(current(request)){accepted=revision;failed=false;}}
    public synchronized void failed(long request){if(current(request))failed=true;}
    public synchronized int resolve(int server,long revision){
        if(pending&&(accepted>=0?revision>=accepted:revision>baseline))pending=false;
        return pending?value:server;
    }
    public synchronized boolean current(long request){return pending&&request==token;}
    public synchronized boolean pending(){return pending;}
    public synchronized boolean failed(){return pending&&failed;}
    public synchronized void clear(){++token;pending=false;failed=false;accepted=-1;}
}
