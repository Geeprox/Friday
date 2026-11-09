package local.friday.ktv;

/** Exercises the production display policy; no Activity or device is started. */
public final class LyricWindowTest {
    private static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
    private static void visible(int previousTop,int currentTop,int currentBottom,int nextBottom,int height){
        int scroll=MainActivity.LyricWindow.scrollTarget(previousTop,currentTop,currentBottom,nextBottom,height);
        yes(scroll>=0&&scroll<=previousTop,"previous lyric remains entirely visible");
        yes(scroll+height>=nextBottom,"current and next lyric remain entirely visible");
    }
    public static void main(String[] args){
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
