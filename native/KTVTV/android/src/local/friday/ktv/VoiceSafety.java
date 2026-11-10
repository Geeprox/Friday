package local.friday.ktv;

/** Stereo-aware software risk aggregation and latched overload protection.
 * Per-channel notches analyze their actual residual; no channel average, extra
 * FFT, dry queue or lookahead exists here. A tonal risk is not proof of feedback. */
public final class VoiceSafety {
    private static final int HOP=512,OVERLOAD_FRAMES=4800;
    private final FeedbackNotch standalone=new FeedbackNotch(0);
    private int at,clipped,hotFrames;
    private double energyA,energyB,blockPeak;
    private long lastASequence,lastBSequence;
    public volatile boolean ready,attenuated,latched;
    public volatile long frames,sequence,clippedSamples;
    public volatile int severity,gainCap=100,trips,reason;
    public volatile float peakHz,peak;

    /** Explicit render-owner reset. Telemetry counters and frame progress remain
     * monotonic; normal clear/stream transitions never call this operation. */
    public void reset(){
        at=clipped=hotFrames=0;energyA=energyB=blockPeak=0;ready=attenuated=latched=false;
        severity=reason=0;gainCap=100;peakHz=peak=0;standalone.reset();
    }
    /** Legacy mono observer for callers without the streaming notch stage. Its
     * zero notch budget escalates conservative sustained risk to gain then mute. */
    public boolean accept(float sample,boolean rawClipped){standalone.process(sample);return accept(sample,0,rawClipped,false,standalone,null);}
    public boolean accept(float rawA,float rawB,boolean clippedA,boolean clippedB,FeedbackNotch a,FeedbackNotch b){
        ready=a.ready&&(b==null||b.ready);
        frames++;clipped+=(clippedA?1:0)+(clippedB?1:0);clippedSamples+=(clippedA?1:0)+(clippedB?1:0);
        double left=clean(rawA),right=b==null?0:clean(rawB);energyA+=left*left;energyB+=right*right;blockPeak=Math.max(blockPeak,Math.max(Math.abs(left),Math.abs(right)));
        if(++at==HOP){
            peak=(float)blockPeak;int channels=b==null?1:2;
            boolean overload=clipped>=HOP*channels*.02&&Math.sqrt(Math.max(energyA,energyB)/HOP)>=.2;
            hotFrames=ready&&overload?Math.min(OVERLOAD_FRAMES,hotFrames+HOP):0;
            if(hotFrames>=OVERLOAD_FRAMES&&!latched)latch(1,0);
            at=clipped=0;energyA=energyB=blockPeak=0;
        }
        boolean changedA=a.sequence!=lastASequence,changedB=b!=null&&b.sequence!=lastBSequence;
        if(changedA||changedB){
            lastASequence=a.sequence;if(b!=null)lastBSequence=b.sequence;sequence++;
            FeedbackNotch source=changedB&&(!changedA||b.severity>a.severity)?b:a;
            if(source.peakHz>0&&!(latched&&reason==1))peakHz=source.peakHz;
        }
        int risk=Math.max(a.severity,b==null?0:b.severity);
        if(risk>severity)severity=risk;
        if(risk>=2){attenuated=true;gainCap=Math.min(gainCap,50);}
        if(risk>=3&&!latched)latch(2,peakHz);
        if(latched){severity=3;gainCap=0;}
        return !latched;
    }
    private void latch(int why,float frequency){latched=true;attenuated=true;severity=3;gainCap=0;reason=why;peakHz=frequency;trips++;sequence++;}
    private static double clean(float value){return Float.isFinite(value)?Math.max(-4,Math.min(4,value)):0;}
}
