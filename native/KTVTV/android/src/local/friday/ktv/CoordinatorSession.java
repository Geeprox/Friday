package local.friday.ktv;

import java.util.UUID;

/** 暂时失败按有上限的退避间隔重试，所有重试使用同一请求 ID。 */
public final class CoordinatorSession {
    private static final long[] RETRY_DELAYS_MS={1000,3000,5000,8000,10000};
    private String request="";
    private int attempts;
    private int refusal;
    private long next;

    public synchronized void requestOpen(){
        request=UUID.randomUUID().toString();
        retry();
    }
    public synchronized void retry(){attempts=0;refusal=0;next=0;}
    public synchronized String claimOpen(boolean foreground,boolean enabled,long now){
        if(!foreground||!enabled||request.isEmpty()||refusal!=0||now<next)return "";
        int index=Math.min(attempts,RETRY_DELAYS_MS.length-1);
        next=now+RETRY_DELAYS_MS[index];
        attempts=index+1;
        return request;
    }
    public synchronized void refused(String request,int status){
        if(request.isEmpty()||!this.request.equals(request))return;
        if(status==400||status==401||status==403||status==409||status==410)refusal=status;
    }
    public synchronized boolean acknowledged(String request){
        if(!this.request.equals(request)||request.isEmpty())return false;
        this.request="";
        return true;
    }
    public synchronized void closed(){request="";retry();}
    public synchronized boolean pending(){return !request.isEmpty();}
    public synchronized boolean slow(){return pending()&&attempts>=RETRY_DELAYS_MS.length;}
    public synchronized int refusal(){return refusal;}
}
