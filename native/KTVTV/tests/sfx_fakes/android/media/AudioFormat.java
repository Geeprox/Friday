package android.media;
public final class AudioFormat {
    public static final int ENCODING_PCM_16BIT=2,CHANNEL_OUT_STEREO=12;
    public static final class Builder {
        public Builder setSampleRate(int value){if(value!=48000)throw new AssertionError("rate");return this;}
        public Builder setEncoding(int value){if(value!=ENCODING_PCM_16BIT)throw new AssertionError("encoding");return this;}
        public Builder setChannelMask(int value){if(value!=CHANNEL_OUT_STEREO)throw new AssertionError("channels");return this;}
        public AudioFormat build(){return new AudioFormat();}
    }
}
