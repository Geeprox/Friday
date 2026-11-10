package local.friday.ktv;

import java.util.Arrays;
import java.util.concurrent.atomic.AtomicLong;
import local.friday.audio.WetProfiles;

/** Pure Java wet return for the software mixer. No platform/native APIs, dry
 * output, lookahead, input queue, allocation, locks or I/O while processing.
 * The seven IDs, room tunings and short echo match the native wet contract. */
public final class CompatVocalEffects {
    public static final int RATE=48000,PROFILE_COUNT=7,FADE_FRAMES=240;
    private static final int LINES=8,DELAY_MAX=4096,EARLY_MAX=8192;
    private static final long EXPLICIT=1L<<44,REVISION_MASK=0xffffffffL;
    private static final int[] BASE={1493,1601,1747,1867,1999,2131,2273,2399};
    private static final float[] LEFT={1,1,-1,1,-1,1,-1,-1},RIGHT={1,-1,1,1,1,-1,-1,1},INJECT={1,1,1,-1,-1,1,-1,-1};
    private static final float[] PRE={0,10,18,28,35,3,0},DECAY={0,.72f,1.25f,1.65f,2.60f,.38f,.42f};
    private static final float[] DAMP={0,6500,5600,5100,7200,7200,4200},HP={0,120,110,145,100,150,160};
    private static final float[] ROOM={1,.72f,1,1.12f,1.44f,.50f,1},EARLY={0,.40f,.48f,.30f,.42f,.30f,.70f},LATE={0,.48f,.64f,.60f,.74f,.28f,0};
    private final Prepared[] prepared=new Prepared[PROFILE_COUNT];
    private final float[][] lines=new float[LINES][DELAY_MAX],early=new float[2][EARLY_MAX];
    private final int[] indices=new int[LINES];
    private final float[] damping=new float[LINES],scatter=new float[LINES],hpInput=new float[2],hpState=new float[2],inputTone=new float[2],echoDamping=new float[2];
    private final AtomicLong requested=new AtomicLong(1L<<12);
    private long observed,applied=0;
    private volatile long statusWord,renderedFrames;
    private int profile,earlyAt,transition,ramp,pendingProfile;
    private float vocalEnvelope,wetGain,targetGain=-1,gainStep,fadeStep;
    private boolean cleared=true;
    /** Latest pure wet pair; valid only on the single render thread. */
    public float left,right;

    public CompatVocalEffects(){
        for(int p=1;p<PROFILE_COUNT;p++){
            Prepared c=new Prepared();prepared[p]=c;c.pre=Math.round(PRE[p]*48);
            c.hp=(float)Math.exp(-2*Math.PI*HP[p]/RATE);c.damp=1-(float)Math.exp(-2*Math.PI*DAMP[p]/RATE);
            c.inputDamp=1-(float)Math.exp(-2*Math.PI*Math.min(10000,DAMP[p]*1.5f)/RATE);
            for(int i=0;i<LINES;i++){c.length[i]=Math.round(BASE[i]*ROOM[p])|1;c.feedback[i]=(float)Math.pow(.001,c.length[i]/(RATE*DECAY[p]));}
        }
    }
    /** Thread-safe intent. A revision is applied only after render-side fades. */
    public long configure(String key){int p=WetProfiles.id(key);if(p<0)throw new IllegalArgumentException("unknown_wet_profile");return configure(p,Math.round(WetProfiles.DEFAULT_WET[p]*1000));}
    public long configure(String key,int wetPermille){int p=WetProfiles.id(key);if(p<0)throw new IllegalArgumentException("unknown_wet_profile");return configure(p,wetPermille);}
    public long configure(int id,int wetPermille){return configure(id,wetPermille,true);}
    /** A legacy slider cannot overwrite a newer explicit preset, even when a
     * render block races a UI request. Both intents share one CAS control word. */
    public void configureLegacy(int wetPermille){configure(wetPermille>0?1:0,wetPermille,false);}
    private long configure(int id,int wetPermille,boolean explicit){
        if(id<0||id>=PROFILE_COUNT||wetPermille<0||wetPermille>360)throw new IllegalArgumentException("invalid_wet_configuration");
        if(id==0)wetPermille=0;
        final long settings=id|((long)wetPermille<<3);
        for(;;){long old=requested.get(),revision=(old>>>12)&REVISION_MASK;
            if(!explicit&&(old&EXPLICIT)!=0)return revision;
            if((old&4095)==settings&&(!explicit||(old&EXPLICIT)!=0))return revision;
            revision=(revision+1)&REVISION_MASK;if(revision==0)revision=1;
            if(requested.compareAndSet(old,(revision<<12)|settings|(explicit?EXPLICIT:0)))return revision;}
    }
    public int requestedProfile(){return (int)(requested.get()&7);}
    public int requestedWet(){return (int)((requested.get()>>>3)&511);}
    /** Actual current wet amount, including a profile or gain fade. The mixer
     * divides its current dry/wet budget using this value; no separate dry FIFO. */
    public float activeWet(){return wetGain;}
    public void clear(){
        clearDsp();left=right=wetGain=0;targetGain=-1;transition=ramp=0;cleared=true;observed=0;applied=0;
        publish(true);
    }
    public void process(float inputLeft,float inputRight,boolean enabled){
        long command=requested.get();int wantedProfile=(int)(command&7),wet=(int)((command>>>3)&511);long revision=(command>>>12)&REVISION_MASK;
        boolean gated=!enabled||wantedProfile==0||wet==0;
        if(gated){
            if(!cleared||profile!=wantedProfile)clearDsp();profile=wantedProfile;left=right=wetGain=0;targetGain=-1;
            transition=ramp=0;cleared=true;observed=command;applied=revision;renderedFrames++;publish(true);return;
        }
        if(command!=observed){
            observed=command;
            if(wantedProfile!=profile){
                pendingProfile=wantedProfile;
                if(!cleared&&profile>0&&wetGain>0){if(transition==0){transition=FADE_FRAMES;fadeStep=wetGain/FADE_FRAMES;}}
                else{clearDsp();profile=wantedProfile;wetGain=0;targetGain=-1;ramp=transition=0;}
            }else if(transition>0){transition=0;targetGain=-1;}
        }
        cleared=false;
        if(transition>0){
            dsp(0,0);wetGain=Math.max(0,wetGain-fadeStep);left*=wetGain;right*=wetGain;
            if(--transition==0){clearDsp();profile=pendingProfile;wetGain=0;targetGain=-1;ramp=0;}
        }else{
            dsp(inputLeft,inputRight);float wanted=wet/1000f;
            if(targetGain!=wanted){targetGain=wanted;ramp=FADE_FRAMES;gainStep=(wanted-wetGain)/FADE_FRAMES;}
            if(ramp>0){wetGain+=gainStep;if(--ramp==0)wetGain=targetGain;}
            left*=wetGain;right*=wetGain;
        }
        if(transition==0&&ramp==0&&profile==wantedProfile&&wetGain==wet/1000f)applied=revision;
        renderedFrames++;publish(false);
    }
    /** Allocation occurs only on the caller's diagnostics thread. This proves
     * software application, not an open device, physical output or latency. */
    public Status status(){long command=requested.get(),word=statusWord;return new Status(command,word,renderedFrames);}
    public static final class Status {
        public final boolean capable=true,pureWet=true,acousticallyVerified=false,gated;
        public final String requestedProfile,profile;
        public final int requestedWetPermille,wetPermille,transitionFrames,gainRampFrames;
        public final long requestedRevision,appliedRevision,renderedFrames;
        private Status(long command,long word,long frames){
            requestedProfile=WetProfiles.KEYS[(int)(command&7)];profile=WetProfiles.KEYS[(int)(word&7)];
            requestedWetPermille=(int)((command>>>3)&511);wetPermille=(int)((word>>>3)&511);transitionFrames=(int)((word>>>12)&255);gainRampFrames=(int)((word>>>20)&255);
            gated=((word>>>28)&1)!=0;requestedRevision=(command>>>12)&REVISION_MASK;appliedRevision=word>>>32;renderedFrames=frames;
        }
        public boolean applied(){return requestedRevision==appliedRevision&&transitionFrames==0&&gainRampFrames==0;}
    }
    private void publish(boolean gated){
        statusWord=(applied<<32)|((gated?1L:0)<<28)|((long)ramp<<20)|((long)transition<<12)|((long)Math.round(wetGain*1000)<<3)|profile;
    }
    private void clearDsp(){
        for(float[] line:lines)Arrays.fill(line,0);for(float[] line:early)Arrays.fill(line,0);
        Arrays.fill(indices,0);Arrays.fill(damping,0);Arrays.fill(hpInput,0);Arrays.fill(hpState,0);Arrays.fill(inputTone,0);Arrays.fill(echoDamping,0);
        vocalEnvelope=0;earlyAt=0;
    }
    private void dsp(float inputLeft,float inputRight){
        left=right=0;if(profile==0)return;Prepared c=prepared[profile];
        float a=shape(inputLeft,0,c),b=shape(inputRight,1,c),level=Math.max(Math.abs(a),Math.abs(b));
        vocalEnvelope=zap(vocalEnvelope+(level-vocalEnvelope)*(level>vocalEnvelope?1f/192:1f/5760));
        float duck=Math.max(.45f,1/(1+2*Math.max(0,vocalEnvelope-.08f)));
        if(profile==6){
            float dl=read(0,3744),dr=read(1,4992);
            echoDamping[0]=zap(echoDamping[0]+c.damp*(dl-echoDamping[0]));echoDamping[1]=zap(echoDamping[1]+c.damp*(dr-echoDamping[1]));
            early[0][earlyAt]=zap(a+.23f*echoDamping[1]);early[1][earlyAt]=zap(b+.23f*echoDamping[0]);
            left=softLimit(.70f*dl*duck,.9f);right=softLimit(.70f*dr*duck,.9f);
        }else{
            early[0][earlyAt]=a;early[1][earlyAt]=b;float preL=read(0,c.pre),preR=read(1,c.pre);
            left=EARLY[profile]*(.45f*read(0,c.pre+383)+.27f*read(1,c.pre+811)-.18f*read(0,c.pre+1297)+.10f*read(1,c.pre+1871));
            right=EARLY[profile]*(.45f*read(1,c.pre+431)+.27f*read(0,c.pre+877)-.18f*read(1,c.pre+1361)+.10f*read(0,c.pre+1949));
            for(int i=0;i<LINES;i++){
                float sample=lines[i][indices[i]];damping[i]=zap(damping[i]+c.damp*(sample-damping[i]));scatter[i]=damping[i];
                left+=LATE[profile]*.353553391f*LEFT[i]*sample;right+=LATE[profile]*.353553391f*RIGHT[i]*sample;
            }
            for(int width=1;width<LINES;width*=2)for(int begin=0;begin<LINES;begin+=width*2)for(int j=0;j<width;j++){
                float x=scatter[begin+j],y=scatter[begin+j+width];scatter[begin+j]=x+y;scatter[begin+j+width]=x-y;
            }
            for(int i=0;i<LINES;i++){
                float injection=((i&1)!=0?preR:preL)*INJECT[i]*.176776695f;
                lines[i][indices[i]]=zap(injection+c.feedback[i]*scatter[i]*.353553391f);if(++indices[i]>=c.length[i])indices[i]=0;
            }
            left=softLimit(left*duck,.9f);right=softLimit(right*duck,.9f);
        }
        earlyAt=(earlyAt+1)%EARLY_MAX;
    }
    private float shape(float value,int channel,Prepared c){
        float input=Float.isFinite(value)?Math.max(-4,Math.min(4,value)):0;
        float hp=c.hp*(hpState[channel]+input-hpInput[channel]);hpInput[channel]=input;hpState[channel]=zap(hp);
        inputTone[channel]=zap(inputTone[channel]+c.inputDamp*(hpState[channel]-inputTone[channel]));return softLimit(inputTone[channel],1);
    }
    private float read(int channel,int delay){return early[channel][(earlyAt-delay+EARLY_MAX)%EARLY_MAX];}
    private static float zap(float value){return !Float.isFinite(value)||Math.abs(value)<1e-20f?0:value;}
    public static float softLimit(float value,float ceiling){
        if(!Float.isFinite(value)||!Float.isFinite(ceiling)||ceiling<=0)return 0;
        float magnitude=Math.abs(value),knee=ceiling*.8f;if(magnitude<=knee)return value;
        float excess=magnitude-knee,space=ceiling-knee;return Math.copySign(knee+space*(excess/(space+excess)),value);
    }
    private static final class Prepared {int pre;float hp,damp,inputDamp;final int[] length=new int[LINES];final float[] feedback=new float[LINES];}
}
