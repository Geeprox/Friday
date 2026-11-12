package android.media;
public final class AudioAttributes {
    public static final int USAGE_MEDIA=1,CONTENT_TYPE_MUSIC=2;
    public static final class Builder {
        public Builder setUsage(int value){if(value!=USAGE_MEDIA)throw new AssertionError("usage");return this;}
        public Builder setContentType(int value){if(value!=CONTENT_TYPE_MUSIC)throw new AssertionError("content");return this;}
        public AudioAttributes build(){return new AudioAttributes();}
    }
}
