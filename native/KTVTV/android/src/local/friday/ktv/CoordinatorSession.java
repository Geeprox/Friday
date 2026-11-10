package local.friday.ktv;

import java.util.UUID;

/** Only explicit app/room opening arms a bounded, idempotent coordinator request. */
public final class CoordinatorSession {
    private String request="";private int attempts;private long next;
    public synchronized void requestOpen(){request=UUID.randomUUID().toString();attempts=0;next=0;}
    public synchronized String claimOpen(boolean foreground,boolean enabled,long now){
        if(!foreground||!enabled||request.isEmpty()||attempts>=3||now<next)return "";
        attempts++;next=now+(attempts==1?1000:attempts==2?3000:10000);return request;
    }
    public synchronized void acknowledged(String request){if(this.request.equals(request))this.request="";}
    public synchronized void closed(){request="";attempts=0;}
    public synchronized boolean pending(){return !request.isEmpty();}
}
