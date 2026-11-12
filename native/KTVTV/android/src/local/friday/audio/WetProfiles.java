package local.friday.audio;

/** Closed product contract; these are local DSP tunings, not vendor preset indices. */
public final class WetProfiles {
    public static final String[] KEYS={"dry","pop","ktv","power","opera","studio","echo"};
    public static final String[] LABELS={"关闭混响","流行","KTV","唱将","歌剧","清晰小房间","短回声"};
    public static final float[] DEFAULT_WET={0f,.25f,.32f,.28f,.36f,.18f,.16f};
    private WetProfiles(){}
    public static int id(String key){for(int i=0;i<KEYS.length;i++)if(KEYS[i].equals(key))return i;return -1;}
    public static boolean canArm(int profile,boolean muted,boolean bypassed,int gain){return valid(profile)&&!muted&&!bypassed&&gain>0;}
    public static boolean valid(int id){return id>=0&&id<KEYS.length;}
    public static int permille(float value){if(Float.isNaN(value)||Float.isInfinite(value)||value<0||value>1)throw new IllegalArgumentException("unit_interval_required");return Math.round(value*1000);}
}
