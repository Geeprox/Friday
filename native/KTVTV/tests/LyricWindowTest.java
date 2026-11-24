package local.friday.ktv;

import java.util.Locale;
import java.util.Random;

/** Exercises the production display policy; no Activity or device is started. */
public final class LyricWindowTest {
    private static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
    private static void visible(int previousTop,int currentTop,int currentBottom,int nextBottom,int height){
        int scroll=MainActivity.LyricWindow.scrollTarget(previousTop,currentTop,currentBottom,nextBottom,height);
        yes(scroll>=0&&scroll<=previousTop,"previous lyric remains entirely visible");
        yes(scroll+height>=nextBottom,"current and next lyric remain entirely visible");
    }
    private static void largeTimelineKeepsTheCueContract(){
        final int length=1000,lookups=20000;
        long[] starts=new long[length];
        long[] entries=new long[length/5];
        // 重复时间戳、等距的前后歌词、跳转和靠近曲末的随机取样。
        for(int i=0;i<length;i++)starts[i]=(i%11==10?i-1:i)*1350L;
        for(int i=0;i<entries.length;i++)entries[i]=starts[i*5]+(i%2==0?675:1351);
        Random random=new Random(504094L);
        long[] positions=new long[lookups];
        for(int i=0;i<lookups;i++)positions[i]=random.nextInt(1400000)-3000;
        LyricCue actual=new LyricCue();
        for(long position:positions){
            actual.update(position,starts,entries);
            int current=-1,following=-1,entrance=-1,countdown=0;
            for(int i=0;i<length;i++){
                if(starts[i]>position){following=i;break;}
                current=i;
            }
            for(long entry:entries){
                long until=entry-position;
                if(until<=0)continue;
                if(until<=5000){
                    long nearest=1501;
                    for(int i=0;i<length;i++){
                        long distance=Math.abs(starts[i]-entry);
                        if(distance<nearest){nearest=distance;entrance=i;}
                    }
                    if(entrance>=0)countdown=(int)((until+999)/1000);
                }
                break;
            }
            yes(actual.current==current&&actual.next==following&&actual.entrance==entrance&&actual.countdown==countdown,
                "binary search preserves the original visible cue, including duplicate timestamps");
        }
        int checksum=0;
        long start=System.nanoTime();
        for(long position:positions){actual.update(position,starts,entries);checksum+=actual.current;}
        System.out.printf(Locale.ROOT,"LyricWindowTest: %d lookups over %d lines in %.2f ms (checksum %d; JVM only)%n",
            lookups,length,(System.nanoTime()-start)/1_000_000d,checksum);
    }
    public static void main(String[] args){
        largeTimelineKeepsTheCueContract();
        LyricCue cue=new LyricCue();long[] starts={1000,2500,5000};
        cue.update(0,starts);yes(MainActivity.LyricWindow.previous(cue.current,3)==-1&&cue.next==0,"intro has no fabricated previous line");
        cue.update(1000,starts);yes(MainActivity.LyricWindow.previous(cue.current,3)==-1&&cue.next==1,"first lyric keeps next");
        cue.update(2500,starts);yes(cue.current==1&&MainActivity.LyricWindow.previous(cue.current,3)==0&&cue.next==2,"early timestamp retains just-sung lyric");
        cue.update(7000,starts);yes(MainActivity.LyricWindow.previous(cue.current,3)==1&&cue.next==-1,"last lyric retains previous");
        cue.update(1000,starts);yes(MainActivity.LyricWindow.previous(cue.current,3)==-1,"replay cannot retain old end-of-song lyric");
        yes(MainActivity.LyricWindow.previous(0,0)==-1&&MainActivity.LyricWindow.previous(3,3)==-1,"invalid/empty indices stay empty");
        visible(90,230,300,390,300); // Center-only scrolling would clip the previous line.
        visible(100,180,250,310,300);
        visible(0,0,65,130,300);
        visible(220,290,360,360,300); // Last line: no invented next content.
        int longNext=MainActivity.LyricWindow.scrollTarget(100,180,250,600,300);
        yes(longNext<=100&&longNext+300>=250,"oversized next lyric does not push previous/current away");
        yes(MainActivity.LyricWindow.scrollTarget(100,350,500,800,300)==100,"oversized full group retains previous start");
        yes(MainActivity.LyricWindow.scrollTarget(0,0,65,130,0)==0,"unmeasured viewport stays at origin");
        System.out.println("LyricWindowTest passed: previous/current/next at transitions, replay, boundaries and wrapped-line scroll; no device actions");
    }
}
