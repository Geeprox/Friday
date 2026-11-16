package local.friday.ktv;

/** 仅持有可选背景的歌曲代次，迟到图片不能覆盖下一首。 */
public final class SongBackdrop {
    private String song="",pending="",applied="",attempted="";
    private boolean pendingUhd,appliedUhd,attemptedUhd;
    private int attempts=0;
    private long epoch=0,retryAt=0;
    public boolean select(String value){
        if(song.equals(value))return false;
        song=value;clear();return true;
    }
    public void clear(){
        pending="";applied="";attempted="";attempts=0;retryAt=0;epoch++;
    }
    public long begin(String value,String hash,boolean uhd,long now){
        if(!song.equals(value)||!hash.matches("[a-f0-9]{64}")||!pending.isEmpty()||applied.equals(hash)&&appliedUhd==uhd)return 0;
        if(!attempted.equals(hash)||attemptedUhd!=uhd){attempted=hash;attemptedUhd=uhd;attempts=0;retryAt=0;}
        if(now<retryAt)return 0;
        pending=hash;pendingUhd=uhd;return ++epoch;
    }
    public boolean accept(String value,String hash,long request){
        if(!song.equals(value)||epoch!=request||!pending.equals(hash))return false;
        pending="";applied=hash;appliedUhd=pendingUhd;attempts=0;retryAt=0;return true;
    }
    public void fail(long request,long now){
        if(epoch!=request||pending.isEmpty())return;
        pending="";retryAt=now+Math.min(30000L,1000L<<Math.min(5,attempts++));
    }
}
