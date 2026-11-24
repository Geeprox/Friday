package local.friday.ktv;
/** 以歌曲时间轴定位当前歌词和入唱倒计时。 */
public final class LyricCue {
    public int current=-1,next=-1,countdown,entrance=-1;

    public void update(long ms,long[] starts){update(ms,starts,null);}

    public void update(long ms,long[] starts,long[] entries){
        int following=search(starts,ms,true);
        current=following-1;
        next=following<starts.length?following:-1;
        countdown=0;
        entrance=-1;
        if(entries!=null){
            int at=search(entries,ms,true);
            if(at==entries.length||entries[at]-ms>5000)return;
            long entry=entries[at];
            int after=search(starts,entry,false);
            long later=after<starts.length?starts[after]-entry:Long.MAX_VALUE;
            long earlier=after>0?entry-starts[after-1]:Long.MAX_VALUE;
            if(Math.min(earlier,later)>1500)return;
            entrance=earlier<=later?search(starts,starts[after-1],false):after;
            countdown=(int)((entry-ms+999)/1000);
            return;
        }
        if(next>=0){
            long until=starts[next]-ms;
            boolean gap=current<0||starts[next]-starts[current]>=10000;
            if(gap&&until<=5000){countdown=(int)((until+999)/1000);entrance=next;}
        }
    }

    private static int search(long[] values,long instant,boolean after){
        int left=0,right=values.length;
        while(left<right){
            int mid=left+(right-left)/2;
            if(values[mid]<instant||after&&values[mid]==instant)left=mid+1;
            else right=mid;
        }
        return left;
    }
}
