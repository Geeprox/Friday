package local.friday.ktv;
import java.util.*;
public final class PartyPlaybackTest {
    private static void require(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args){
        WarmupShuffle shuffle=new WarmupShuffle(new Random(25));
        List<String> catalog=Arrays.asList("a","b","c","a");String last="";
        for(int round=0;round<20;round++){
            Set<String> heard=new HashSet<>();
            for(int i=0;i<3;i++){String next=shuffle.next(catalog);require(!next.equals(last));require(heard.add(next));last=next;}
            require(heard.size()==3);
        }
        for(int i=0;i<5;i++)require(shuffle.next(Collections.singleton("only")).equals("only"));
        require(shuffle.next(Collections.emptyList())==null);
        require(!shuffle.next(Arrays.asList("new","other")).equals("only"));
        RemoteShortcuts remote=new RemoteShortcuts();
        require(remote.press(22,true,0,true)==RemoteShortcuts.Action.NEXT);
        require(remote.press(22,true,0,true)==RemoteShortcuts.Action.NONE);
        require(remote.press(22,true,1,true)==RemoteShortcuts.Action.NONE);
        require(remote.press(22,false,0,true)==RemoteShortcuts.Action.NONE);
        require(remote.press(22,true,0,true)==RemoteShortcuts.Action.NEXT);
        require(remote.press(20,true,0,false)==RemoteShortcuts.Action.NONE);
        require(remote.press(20,true,0,true)==RemoteShortcuts.Action.MUTE);
        require(remote.press(19,true,0,true)==RemoteShortcuts.Action.ORIGINAL);
        require(remote.press(21,true,0,true)==RemoteShortcuts.Action.REPLAY);
        require(SongTitle.display("01. 周杰伦 (Feat. 潘儿) - 夜的第七章.flac").equals("夜的第七章"));
        require(SongTitle.display("01. 歌手 - 歌名 (Live).flac").equals("歌名 (Live)"));
        require(SongTitle.display("七里香").equals("七里香"));
        require(SongTitle.display("1973").equals("1973"));
        require(SongTitle.display("Love - Part 2").equals("Love - Part 2"));
        LyricCue cue=new LyricCue();long[] words={10000,14000,30000};
        cue.update(5000,words);require(cue.countdown==5);
        cue.update(26000,words);require(cue.countdown==4);
        cue.update(10000,words);require(cue.countdown==0);
        cue.update(25000,words,new long[]{10100,30100});require(cue.countdown==0);
        cue.update(26100,words,new long[]{10100,30100});require(cue.countdown==4);
        cue.update(30100,words,new long[]{10100,30100});require(cue.countdown==0);
        cue.update(10050,words,new long[]{10100,30100});require(cue.countdown==1&&cue.entrance==0&&cue.current==0);
        cue.update(26000,words,new long[0]);require(cue.countdown==0);
        System.out.println("PartyPlaybackTest passed");
    }
}
