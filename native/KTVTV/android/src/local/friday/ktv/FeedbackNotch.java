package local.friday.ktv;

import java.util.Arrays;

/** Causal IIR notches with a bounded tonal-risk observer, not proof of acoustic
 * feedback. Analysis uses only past filtered samples and never delays output.
 * Harmonic voices, moving pitches and short transients are rejected; an isolated
 * loud sustained tone remains ambiguous and receives conservative protection. */
public final class FeedbackNotch {
    private static final int N=2048,HOP=512,MAX=4;
    private static final int GROWTH=5760,STEADY=8640,CUT=8640,LATCH=12000;
    private final double[] real=new double[N],imag=new double[N],window=new double[N];
    private final float[] samples=new float[N];
    private final Notch[] filters=new Notch[MAX];
    private final int notchBudget;
    private int at,filled,since,stable,growth;
    private double candidate,lowFrequency,highFrequency,initialRms,lastRms,maximumRms,toneFrequency,toneRms;
    private long actionFrame,lastNotchFrame;
    public volatile int count,events,severity,gainCap=100;
    public volatile boolean ready,attenuated,latched;
    public volatile float peakHz,peak;
    public volatile long frames,sequence;

    public FeedbackNotch(){this(MAX);}
    /** Zero budget is used by the legacy standalone safety observer. */
    FeedbackNotch(int budget){
        notchBudget=Math.max(0,Math.min(MAX,budget));
        for(int i=0;i<N;i++)window[i]=.5-.5*Math.cos(2*Math.PI*i/(N-1));
        for(int i=0;i<MAX;i++)filters[i]=new Notch();
    }
    /** Clears sample/filter history at stream boundaries, retaining protection,
     * installed notches and their finite budget. It never releases a latch. */
    public void clearState(){
        at=filled=since=0;ready=false;clearCandidate();Arrays.fill(samples,0);
        for(Notch filter:filters)filter.clear();
    }
    /** Explicit operator reset, called only by the render owner. Counters remain
     * monotonic; a new action budget is never granted by a song/stream change. */
    public void reset(){
        clearState();count=severity=0;gainCap=100;attenuated=latched=false;peakHz=peak=0;actionFrame=lastNotchFrame=0;
    }
    public float process(float sample){
        float value=Float.isFinite(sample)?Math.max(-4,Math.min(4,sample)):0;
        for(int i=0;i<count;i++)value=filters[i].process(value);
        value=Float.isFinite(value)?Math.max(-4,Math.min(4,value)):0;
        // Observe the actual residual: successful narrow filtering must not
        // trigger an independent pre-filter detector that then mutes the singer.
        samples[at]=value;at=(at+1)&(N-1);filled=Math.min(N,filled+1);frames++;
        if(++since==HOP){since=0;if(filled==N)inspect();}
        return value;
    }
    private void clearCandidate(){stable=growth=0;candidate=lowFrequency=highFrequency=initialRms=lastRms=maximumRms=0;}
    private void startCandidate(double frequency,double rms){candidate=lowFrequency=highFrequency=frequency;initialRms=lastRms=maximumRms=rms;stable=HOP;growth=0;}
    private double power(int i){return real[i]*real[i]+imag[i]*imag[i];}
    private double band(int bin,int radius){double sum=0;for(int i=Math.max(1,bin-radius);i<=Math.min(N/2-1,bin+radius);i++)sum+=real[i];return sum;}
    private void fft(){
        for(int i=1,j=0;i<N;i++){int bit=N>>1;for(;(j&bit)!=0;bit>>=1)j^=bit;j^=bit;if(i<j){double t=real[i];real[i]=real[j];real[j]=t;}}
        for(int length=2;length<=N;length<<=1){
            double angle=-2*Math.PI/length,cr=Math.cos(angle),ci=Math.sin(angle);
            for(int start=0;start<N;start+=length){double wr=1,wi=0;for(int k=0;k<length/2;k++){
                int a=start+k,b=a+length/2;double br=real[b]*wr-imag[b]*wi,bi=real[b]*wi+imag[b]*wr;
                real[b]=real[a]-br;imag[b]=imag[a]-bi;real[a]+=br;imag[a]+=bi;double t=wr*cr-wi*ci;wi=wr*ci+wi*cr;wr=t;
            }}
        }
    }
    private boolean spectrum(double energy){
        toneRms=Math.sqrt(energy/N);if(toneRms<.035)return false;
        for(int i=0;i<N;i++){real[i]=samples[(at+i)&(N-1)]*window[i];imag[i]=0;}
        fft();double total=0,largest=0;int best=0;
        for(int i=1;i<N/2;i++){real[i]=power(i);total+=real[i];if(i>=8&&i<=277&&real[i]>largest){largest=real[i];best=i;}}
        if(best==0||total<=1e-15)return false;
        double narrow=band(best,2),harmonic=0;for(int n=2;n<=6&&best*n<N/2-2;n++)harmonic+=band(best*n,2);
        if(narrow/total<.72||harmonic/total>.10)return false;
        // A dominant vowel formant can hide its weaker fundamental below it.
        for(int divisor=2;divisor<=5;divisor++){int sub=(int)Math.round(best/(double)divisor);if(sub>=3&&band(sub,1)/total>.04)return false;}
        double l=Math.log(Math.max(real[best-1],1e-30)),c=Math.log(Math.max(real[best],1e-30)),r=Math.log(Math.max(real[best+1],1e-30));
        double denominator=l-2*c+r,delta=Math.abs(denominator)>1e-12?.5*(l-r)/denominator:0;
        toneFrequency=(best+Math.max(-.5,Math.min(.5,delta)))*48000/N;return true;
    }
    private void action(int level,double frequency){
        severity=level;peakHz=(float)frequency;sequence++;actionFrame=frames;
        if(level==1){filters[count++].set(frequency);events++;lastNotchFrame=frames;}
        if(level>=2){attenuated=true;gainCap=level==3?0:50;}
        if(level==3)latched=true;
    }
    private boolean installed(double frequency){for(int i=0;i<count;i++)if(Math.abs(filters[i].hz-frequency)<=Math.max(18,filters[i].hz*.015))return true;return false;}
    private void inspect(){
        ready=true;double energy=0,maximum=0;
        for(float sample:samples){energy+=sample*sample;maximum=Math.max(maximum,Math.abs(sample));}peak=(float)maximum;
        if(latched)return;
        if(!spectrum(energy)){clearCandidate();return;}
        double frequency=toneFrequency,rms=toneRms;
        if(candidate==0||Math.abs(frequency-candidate)>10){startCandidate(frequency,rms);return;}
        lowFrequency=Math.min(lowFrequency,frequency);highFrequency=Math.max(highFrequency,frequency);
        if(highFrequency-lowFrequency>10){startCandidate(frequency,rms);return;}
        boolean rising=rms>lastRms*1.003;
        stable=Math.min(96000,stable+HOP);growth=Math.min(1000,growth+(rms>lastRms*1.01?1:0));lastRms=rms;maximumRms=Math.max(maximumRms,rms);
        boolean growing=stable>=GROWTH&&growth>=3&&rising&&rms>=initialRms*1.3&&rms>=maximumRms*.95;
        boolean sustained=stable>=STEADY&&rms>=.10&&(severity>0||rms>=maximumRms*.85);
        if(!growing&&!sustained)return;
        if(severity>=2){if(frames-actionFrame>=LATCH&&rms>=.10)action(3,frequency);return;}
        if(!installed(frequency)){
            if(count>=notchBudget){action(2,frequency);return;}
            if(severity==0||frames-lastNotchFrame>=12000)action(1,frequency);
            return;
        }
        if(severity==1&&frames-actionFrame>=CUT&&rms>=.10)action(2,frequency);
    }
    private static final class Notch {
        double hz,b0,b1,b2,a1,a2,z1,z2,mix;
        void set(double frequency){hz=frequency;double w=2*Math.PI*frequency/48000,alpha=Math.sin(w)/(2*45),a0=1+alpha;b0=1/a0;b1=-2*Math.cos(w)/a0;b2=b0;a1=b1;a2=(1-alpha)/a0;clear();}
        void clear(){z1=z2=mix=0;}
        float process(float value){double out=b0*value+z1;z1=b1*value-a1*out+z2;z2=b2*value-a2*out;mix=Math.min(.94,mix+.004);double result=value+mix*(out-value);if(!Double.isFinite(result)){clear();return 0;}return (float)result;}
    }
}
