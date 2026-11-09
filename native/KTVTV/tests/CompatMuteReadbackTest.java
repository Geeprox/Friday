package local.friday.ktv;

public final class CompatMuteReadbackTest {
    private static void yes(boolean value,String reason){if(!value)throw new AssertionError(reason);}
    private static long word(long frames,boolean zero,boolean latched,boolean muted){return frames<<3|(zero?1:0)|(latched?2:0)|(muted?4:0);}
    public static void main(String[] args){
        CompatMuteReadback state=new CompatMuteReadback();state.session(0);
        state.observe(0,0,0,false,true);yes(state.known()&&state.muted(),"a never-opened/released engine is silent");
        state.session(word(1000,false,false,false));state.request(true,word(1000,false,false,false));
        state.observe(word(1000,true,false,true),0,0,true,false);yes(!state.known(),"reset or intent without frame advance cannot acknowledge");
        state.observe(word(1240,false,false,true),240,240,true,false);yes(!state.known(),"unfinished dry fade cannot acknowledge mute");
        state.observe(word(1480,true,false,true),480,400,true,false);yes(!state.known(),"real zero gate still waits for presentation");
        state.observe(word(1720,true,false,true),720,720,true,false);yes(state.known()&&state.muted(),"software playhead crosses the zero output fence");
        state.request(false,word(1720,true,false,true));state.observe(word(1960,false,false,false),960,960,true,false);yes(!state.known(),"unmute also waits for output fence");
        state.observe(word(2200,false,false,false),1200,1200,true,false);yes(state.known()&&!state.muted(),"actual positive gate presented");
        state.observe(word(2440,true,true,false),1440,1440,true,false);yes(!state.known(),"new protection latch requires its own fence");
        state.observe(word(2680,true,true,false),1680,1680,true,false);yes(state.known()&&state.muted(),"latched zero gate can acknowledge protection mute");
        state.session(word(2680,true,true,false));state.observe(word(2680,true,false,true),0,0,true,false);yes(!state.known(),"new session cannot reuse an old gate word");
        state.observe(word(2920,true,false,true),240,240,true,false);state.observe(word(3160,true,false,true),480,480,true,false);yes(state.muted(),"mixer lifetime counter and session-relative output counter are separate");
        state.observe(0,0,0,false,false);yes(!state.known(),"an unverified stop is unknown");
        System.out.println("CompatMuteReadbackTest passed: real frame advance, dry fade, presentation fence, latch and session clocks; no audio devices");
    }
}
