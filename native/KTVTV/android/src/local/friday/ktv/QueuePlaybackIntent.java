package local.friday.ktv;

import java.util.Set;
import java.util.function.Predicate;

/** 保留等待本场歌曲的意图，直到用户暂停或新曲播放。 */
public final class QueuePlaybackIntent {
    static final int MIN_LYRIC_LINES=3;
    private boolean automatic=true;
    private boolean playbackEnabled,commandSnapshotSeen;
    public void awaitNext(){automatic=true;}
    public void pause(){automatic=false;}
    public void enablePlayback(){playbackEnabled=true;}
    public boolean playbackEnabled(){return playbackEnabled;}
    public void holdPlayback(){playbackEnabled=false;pause();}
    public void roomChanged(){playbackEnabled=false;commandSnapshotSeen=false;awaitNext();}
    /** 房间首个指令快照只记录历史，防止恢复连接时重放旧指令。 */
    public boolean remoteCommandsFresh(){
        boolean fresh=commandSnapshotSeen;
        commandSnapshotSeen=true;
        return fresh;
    }
    public boolean shouldStart(boolean foreground,boolean loading,boolean hasCurrent){
        return automatic&&foreground&&!loading&&!hasCurrent;
    }

    public boolean waitingForRequest(boolean foreground,boolean loading,boolean hasCurrent,String next){
        return !next.isEmpty()&&shouldStart(foreground,loading,hasCurrent);
    }

    static String firstPending(Iterable<String> ordered,Set<String> played,String current){
        return firstPending(ordered,played,current,id->false);
    }

    static String firstPending(Iterable<String> ordered,Set<String> played,String current,Predicate<String> exclude){
        for(String id:ordered){
            if(!id.isEmpty()&&!id.equals(current)&&!played.contains(id)&&!exclude.test(id))return id;
        }
        return "";
    }

    static boolean lyricsUnavailable(int state,boolean synced,String version,String exhaustedVersion){
        return state==-1||(state!=1&&!synced&&version.equals(exhaustedVersion));
    }

    static String reportedCurrent(boolean hasCurrent,boolean warmup,String current){
        return hasCurrent&&!warmup?current:"";
    }

    static boolean canPlay(boolean audioReady,int verifiedLyricLines){
        return audioReady&&verifiedLyricLines>=MIN_LYRIC_LINES;
    }
}
