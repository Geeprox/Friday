package local.friday.ktv;
public final class QueuePlaybackIntentTest {
    private static void check(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args){
        QueuePlaybackIntent intent=new QueuePlaybackIntent();
        check(intent.shouldStart(true,false,false)); // new room, then first song
        intent.awaitNext();check(intent.shouldStart(true,false,false)); // queue ran empty
        intent.pause();check(!intent.shouldStart(true,false,false)); // user pause/failure
        intent.awaitNext();check(intent.shouldStart(true,false,false)); // resume/new room
        check(!intent.shouldStart(false,false,false));
        check(!intent.shouldStart(true,true,false));
        check(!intent.shouldStart(true,false,true));
    }
}
