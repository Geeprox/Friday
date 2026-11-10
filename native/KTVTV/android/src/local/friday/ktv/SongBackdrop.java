package local.friday.ktv;

/** 仅持有可选背景的歌曲代次，迟到图片不能覆盖下一首。 */
public final class SongBackdrop {
    private String song="",pending="",applied="";
    private int attempts=0;
    private long epoch=0;
    public boolean select(String value){
        if(song.equals(value))return false;
        song=value;pending="";applied="";attempts=0;epoch++;return true;
    }
    public long begin(String value,String hash){
        if(!song.equals(value)||!hash.matches("[a-f0-9]{64}")||!pending.isEmpty()||applied.equals(hash)||attempts>=3)return 0;
        pending=hash;attempts++;return ++epoch;
    }
    public boolean accept(String value,String hash,long request){
        if(!song.equals(value)||epoch!=request||!pending.equals(hash))return false;
        pending="";applied=hash;return true;
    }
    public void fail(long request){if(epoch==request)pending="";}
}
