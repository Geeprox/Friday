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
        feedbackA.clearState();feedbackB.clearState();
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
        if (frames < 0 || music.length < frames * 2 || output.length < frames * 2
                || (micChannels != 1 && micChannels != 2) || mic.length < frames * micChannels)
            throw new IllegalArgumentException("invalid_pcm_block");
        float mg = clamp(musicGain, 0, 1), a = clamp(gainA, 0, 1), b = clamp(gainB, 0, 1);
        long reset=safetyReset.get();if(frames>0&&reset!=appliedSafetyReset){feedbackA.reset();feedbackB.reset();safety.reset();appliedSafetyReset=reset;}
        effects.configureLegacy(Math.round(clamp(wet,0,.36f)*1000));
        float gainReference=micChannels==2?Math.max(a,b):a;
        boolean observedMute=false,observedLatch=false;
        for (int frame = 0; frame < frames; frame++) {
            float raw=mic[frame*micChannels]/32768f;
            boolean clippedA=Math.abs((int)mic[frame*micChannels])>=32760,clippedB=false;
            float filteredA=feedbackA.process(raw);
            float voiceA=voice(filteredA,0)*a,voiceB=voiceA;
            float vocal=voiceA;
            float rawB=0;
            if(micChannels==2){rawB=mic[frame*2+1]/32768f;float filteredB=feedbackB.process(rawB);clippedB=Math.abs((int)mic[frame*2+1])>=32760;voiceB=voice(filteredB,1)*b;vocal=(voiceA+voiceB)*.5f;}
            safety.accept(raw,rawB,clippedA,clippedB,feedbackA,micChannels==2?feedbackB:null);
            observedMute=micMuted;observedLatch=safety.latched;
            float target=observedMute||observedLatch?0:1;voiceGate+=clamp(target-voiceGate,-1f/96,1f/2400);
            // Only the wet state is delayed. Mute/guard/zero gain clear it on
            // the current sample; the existing dry gate keeps its short fade.
            effects.process(voiceA*voiceGate,voiceB*voiceGate,target>0&&gainReference>0);
            float protection=safety.gainCap/100f,budget=gainReference*voiceGate*protection,dryCeiling=budget/(1+effects.activeWet()),wetCeiling=budget-dryCeiling;
            float dry=CompatVocalEffects.softLimit(vocal*voiceGate*protection/(1+effects.activeWet()),dryCeiling);
            float left=music[frame*2]/32768f*mg+dry+CompatVocalEffects.softLimit(effects.left*protection,wetCeiling);
            float right=music[frame*2+1]/32768f*mg+dry+CompatVocalEffects.softLimit(effects.right*protection,wetCeiling);
            // Causal linked peak limiting, with smooth release. No dry FIFO or
            // lookahead is added by the effects or the mixer.
            float peak=Math.max(Math.abs(left),Math.abs(right));
            float desired=peak>.8f?.8f/peak:1;
            limiterGain=desired<limiterGain?desired:Math.min(desired,limiterGain+.0002f);
            if(desired<1)limitedSamples+=2;
            output[frame*2]=(short)Math.round(clamp(left*limiterGain,-.8f,.8f)*32767);
            output[frame*2+1]=(short)Math.round(clamp(right*limiterGain,-.8f,.8f)*32767);
        }
        if(frames>0){processedFrames+=frames;voiceGateWord=(processedFrames<<3)|(observedMute?4:0)|(observedLatch?2:0)|(voiceGate==0?1:0);}
    }
}
