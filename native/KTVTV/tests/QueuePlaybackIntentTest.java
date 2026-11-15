package local.friday.ktv;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Set;
public final class QueuePlaybackIntentTest {
    private static void check(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args){
        QueuePlaybackIntent intent=new QueuePlaybackIntent();
        check(!intent.playbackEnabled()&&!intent.remoteCommandsFresh()&&intent.remoteCommandsFresh());
        intent.enablePlayback();check(intent.playbackEnabled());
        intent.holdPlayback();check(!intent.playbackEnabled());
        intent.roomChanged();check(!intent.playbackEnabled()&&!intent.remoteCommandsFresh()&&intent.remoteCommandsFresh());
        check(intent.shouldStart(true,false,false)); // new room, then first song
        intent.awaitNext();check(intent.shouldStart(true,false,false)); // queue ran empty
        intent.pause();check(!intent.shouldStart(true,false,false)); // user pause/failure
        intent.awaitNext();check(intent.shouldStart(true,false,false)); // resume/new room
        check(!intent.shouldStart(false,false,false));
        check(!intent.shouldStart(true,true,false));
        check(!intent.shouldStart(true,false,true));
        String warmup=QueuePlaybackIntent.reportedCurrent(true,true,"cached-warmup");
        check(warmup.isEmpty());
        String upcoming=QueuePlaybackIntent.firstPending(Arrays.asList("first","second"),Collections.emptySet(),warmup);
        check(upcoming.equals("first"));
        check(QueuePlaybackIntent.firstPending(Collections.singletonList("second"),Collections.emptySet(),warmup).equals("second"));
        check(!QueuePlaybackIntent.canPlay(true,0));
        check(!QueuePlaybackIntent.canPlay(true,2));
        check(!QueuePlaybackIntent.canPlay(false,12));
        check(QueuePlaybackIntent.canPlay(true,3));
        check(intent.waitingForRequest(true,false,false,upcoming));
        check(!intent.waitingForRequest(true,false,true,upcoming));
        check(QueuePlaybackIntent.firstPending(Arrays.asList("first","second"),Collections.singleton("first"),"").equals("second"));
        check(QueuePlaybackIntent.reportedCurrent(true,false,"first").equals("first"));
        check(QueuePlaybackIntent.lyricsUnavailable(-1,false,"v1",null));
        check(!QueuePlaybackIntent.lyricsUnavailable(0,false,"v1",null));
        check(QueuePlaybackIntent.lyricsUnavailable(0,false,"v1","v1"));
        check(!QueuePlaybackIntent.lyricsUnavailable(0,false,"v2","v1"));
        check(!QueuePlaybackIntent.lyricsUnavailable(0,true,"v1","v1"));
        check(!QueuePlaybackIntent.lyricsUnavailable(1,false,"v1","v1"));

        Set<String> terminal=Collections.singleton("exhausted");
        List<String> queued=Arrays.asList("current","exhausted","repairing","ready");
        check(QueuePlaybackIntent.firstPending(queued,Collections.emptySet(),"current",terminal::contains).equals("repairing"));
        check(QueuePlaybackIntent.firstPending(Arrays.asList("exhausted","ready"),Collections.emptySet(),"current",terminal::contains).equals("ready"));
        check(QueuePlaybackIntent.firstPending(queued,Collections.emptySet(),"current").equals("exhausted"));
        List<String> terminalOnly=Collections.singletonList("exhausted");
        String selection=QueuePlaybackIntent.firstPending(terminalOnly,Collections.emptySet(),"",terminal::contains);
        check(selection.isEmpty()&&!intent.waitingForRequest(true,false,false,selection));
        check(!QueuePlaybackIntent.firstPending(terminalOnly,Collections.emptySet(),"").isEmpty());
        intent.pause();
        check(!intent.waitingForRequest(true,false,false,upcoming));
    }
}
