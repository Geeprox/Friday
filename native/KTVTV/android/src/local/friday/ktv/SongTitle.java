package local.friday.ktv;

/** Only a recognizable media filename permits removing an artist/file prefix. */
public final class SongTitle {
    public static String display(String raw){
        if(raw==null)return "";
        String title=raw.trim();
        boolean filename=title.matches("(?i).*\\.(flac|mp3|m4a|wav|aac|ogg|ape|alac)$");
        if(!filename)return title;
        title=title.replaceFirst("(?i)\\.(flac|mp3|m4a|wav|aac|ogg|ape|alac)$","");
        title=title.replaceFirst("^\\d{1,3}[.、 _-]+","");
        int separator=title.indexOf(" - ");
        if(separator>0&&separator+3<title.length())title=title.substring(separator+3);
        return title.trim();
    }
}
