package local.friday.ktv;

/** Pure DSP, one render thread. No platform APIs, files, network or microphone ownership. */
public final class PcmMixer {
    private final float[] hpLastX = new float[2], hpLastY = new float[2], envelope = new float[2];
    /** Legacy wet slider maps to pop until setVocalPreset is first used. */
    public volatile float musicGain = .7f, gainA = .5f, gainB = .5f, wet = 0;
    public volatile boolean micMuted=true;
    public final VoiceSafety safety=new VoiceSafety();
    public final FeedbackNotch feedbackA=new FeedbackNotch(),feedbackB=new FeedbackNotch();
    private final CompatVocalEffects effects=new CompatVocalEffects();
    private final java.util.concurrent.atomic.AtomicLong safetyReset=new java.util.concurrent.atomic.AtomicLong();
    private long appliedSafetyReset;
    private float limiterGain=1,voiceGate;
    private boolean voiceInput;
    private long processedFrames;
    private volatile long voiceGateWord;
    public volatile long limitedSamples;
    public long setVocalPreset(String key){long revision=effects.configure(key);wet=effects.requestedWet()/1000f;return revision;}
    public long setVocalPreset(String key,int wetPermille){long revision=effects.configure(key,wetPermille);wet=effects.requestedWet()/1000f;return revision;}
    public CompatVocalEffects.Status compatEffectsStatus(){return effects.status();}
    /** Render-only proof, published after a complete mix block: high bits are
     * monotonically processed frames; bit 0 means dry gate zero, bit 1 observed
     * safety latch, bit 2 observed mic mute. No device/playhead/SPL claim. */
    public long voiceGateState(){return voiceGateWord;}
    /** The UI only requests a reset. The render owner applies it at a block
     * boundary; polling and ordinary stream clears never release protection. */
    public void resetVoiceSafety(){safetyReset.incrementAndGet();}
    public void clearVoice() {
        feedbackA.clearState();feedbackB.clearState();safety.interruptInput();voiceInput=false;
        java.util.Arrays.fill(hpLastX,0);java.util.Arrays.fill(hpLastY,0);java.util.Arrays.fill(envelope,0);
        effects.clear();
    }
    public void reset(){clearVoice();limitedSamples=0;limiterGain=1;voiceGate=0;}
    private static float clamp(float v, float lo, float hi) { return Float.isFinite(v)?Math.max(lo, Math.min(hi, v)):lo; }

    private float voice(float sample, int channel) {
        // 90 Hz high-pass, smooth downward expander and 3:1 compressor above -9 dBFS.
        float hp = .9883f * (hpLastY[channel] + sample - hpLastX[channel]);
        hpLastX[channel] = sample; hpLastY[channel] = hp;
        float magnitude = Math.abs(hp);
        float coefficient = magnitude > envelope[channel] ? .00416f : .000208f;
        envelope[channel] += coefficient * (magnitude - envelope[channel]);
        float level = envelope[channel];
        if (level < .0015f) hp *= level / .0015f;
        if (level > .35f) hp *= (.35f + (level - .35f) / 3f) / level;
        return hp;
    }

    public void mix(short[] music, short[] mic, int micChannels, int frames, short[] output) {
        mix(music,mic,micChannels,frames,frames,output);
    }
    /** 只处理歌曲音轨，沿用同一音乐增益和限幅。 */
    public void mixBacking(short[] music,int frames,short[] output){
        if(frames<0||frames>music.length/2||frames>output.length/2)throw new IllegalArgumentException("invalid_pcm_block");
        float gain=clamp(musicGain,0,1);
        for(int sample=0;sample<frames*2;sample+=2){
            limit(music[sample]/32768f*gain,music[sample+1]/32768f*gain,output,sample);
        }
    }
    public void mix(short[] music, short[] mic, int micChannels, int frames, int micFrames, short[] output) {
        if (frames < 0 || frames>music.length/2 || frames>output.length/2
                || (micChannels != 1 && micChannels != 2) || micFrames<0 || micFrames>frames || micFrames>mic.length/micChannels)
            throw new IllegalArgumentException("invalid_pcm_block");
        float mg = clamp(musicGain, 0, 1), a = clamp(gainA, 0, 1), b = clamp(gainB, 0, 1);
        long reset=safetyReset.get();if(frames>0&&reset!=appliedSafetyReset){feedbackA.reset();feedbackB.reset();safety.reset();appliedSafetyReset=reset;}
        effects.configureLegacy(Math.round(clamp(wet,0,.36f)*1000));
        float gainReference=micChannels==2?Math.max(a,b):a;
        boolean observedMute=false,observedLatch=false;
        for (int frame = 0; frame < frames; frame++) {
            float voiceA=0,voiceB=0;
            if(frame<micFrames){
                voiceInput=true;
                int sampleA=mic[frame*micChannels],sampleB=micChannels==2?mic[frame*2+1]:0;
                float rawA=sampleA/32768f,rawB=sampleB/32768f;
                voiceA=voice(feedbackA.process(rawA),0)*a;
                voiceB=micChannels==2?voice(feedbackB.process(rawB),1)*b:voiceA;
                safety.accept(rawA,rawB,Math.abs(sampleA)>=32760,Math.abs(sampleB)>=32760,feedbackA,micChannels==2?feedbackB:null);
            }else if(voiceInput){
                // 补零不参与防啸叫。恢复输入后用新的连续窗口恢复人声输出。
                clearVoice();voiceGate=0;
            }
            float vocal=(voiceA+voiceB)*.5f;
            observedMute=micMuted;observedLatch=safety.latched;
            float target=observedMute||observedLatch||!safety.ready?0:1;voiceGate+=clamp(target-voiceGate,-1f/96,1f/2400);
            // Only the wet state is delayed. Mute/guard/zero gain clear it on
            // the current sample; the existing dry gate keeps its short fade.
            effects.process(voiceA*voiceGate,voiceB*voiceGate,target>0&&gainReference>0);
            float protection=safety.gainCap/100f,budget=gainReference*voiceGate*protection,dryCeiling=budget/(1+effects.activeWet()),wetCeiling=budget-dryCeiling;
            float dry=CompatVocalEffects.softLimit(vocal*voiceGate*protection/(1+effects.activeWet()),dryCeiling);
            float left=music[frame*2]/32768f*mg+dry+CompatVocalEffects.softLimit(effects.left*protection,wetCeiling);
            float right=music[frame*2+1]/32768f*mg+dry+CompatVocalEffects.softLimit(effects.right*protection,wetCeiling);
            limit(left,right,output,frame*2);
        }
        if(frames>0){processedFrames+=frames;voiceGateWord=(processedFrames<<3)|(observedMute?4:0)|(observedLatch?2:0)|(voiceGate==0?1:0);}
    }
    /** 复用按立体声峰值联动的限幅和释放，不增加缓冲。 */
    private void limit(float left,float right,short[] output,int sample){
        float peak=Math.max(Math.abs(left),Math.abs(right));
        float desired=peak>.8f?.8f/peak:1;
        limiterGain=desired<limiterGain?desired:Math.min(desired,limiterGain+.0002f);
        if(desired<1)limitedSamples+=2;
        output[sample]=(short)Math.round(clamp(left*limiterGain,-.8f,.8f)*32767);
        output[sample+1]=(short)Math.round(clamp(right*limiterGain,-.8f,.8f)*32767);
    }
}
