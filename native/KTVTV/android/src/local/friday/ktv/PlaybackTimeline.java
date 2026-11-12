package local.friday.ktv;

import java.util.UUID;

/** A real replay/seek/new edition advances the timeline; pause/resume does not. */
public final class PlaybackTimeline {
    private String epoch,song,media;
    public PlaybackTimeline(String epoch,String song,String media){this.epoch=valid(epoch)?epoch:UUID.randomUUID().toString();this.song=song==null?"":song;this.media=media==null?"":media;}
    private static boolean valid(String value){return value!=null&&value.matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}");}
    public boolean start(String song,String media,boolean discontinuity){
        if(song==null||media==null)throw new IllegalArgumentException();
        if(discontinuity||!this.song.equals(song)||!this.media.equals(media)){this.song=song;this.media=media;epoch=UUID.randomUUID().toString();return true;}return false;
    }
    public String epoch(){return epoch;}public String song(){return song;}public String media(){return media;}
}
