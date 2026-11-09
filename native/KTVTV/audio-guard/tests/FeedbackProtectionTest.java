import local.friday.ktv.audio.protocol.FeedbackProtection;

public final class FeedbackProtectionTest {
    static void yes(boolean v){if(!v)throw new AssertionError();}
    public static void main(String[] args){
        FeedbackProtection p=new FeedbackProtection();p.begin(7);
        yes(p.effectiveGain(70,10)==0); // no unprotected bootstrap or slider bypass
        yes(p.heartbeat(7,42,4096,true,320,100,100));yes(p.effectiveGain(70,100)==53);
        yes(p.referenceGain(70)==70&&FeedbackProtection.dryBudget(100,360)==73);
        yes(!p.heartbeat(8,42,4096,true,320,100,100));
        yes(p.observe(7,42,1,1,2000,70));yes(p.eqCommand().contains("28:1838,29:6,30:-6"));
        yes(!p.observe(7,42,1,3,2000,70));yes(!p.latched());
        yes(p.observe(7,42,2,2,2000,70));yes(p.gainCap()==35&&p.effectiveGain(100,100)==26);
        yes(p.heartbeat(7,42,8192,true,320,200,200));yes(p.gainCap()==35); // poll cannot restore
        yes(p.observe(7,42,3,3,2000,35));yes(p.latched()&&p.effectiveGain(100,201)==0);
        yes(p.heartbeat(7,42,12288,true,0,300,300));yes(p.latched()&&!p.healthy(300));
        yes(!p.emergencyStopDue(300,true));yes(p.emergencyStopDue(800,true));yes(!p.emergencyStopDue(900,true));
        p.begin(8);yes(!p.observe(7,42,99,3,0,20));
        yes(p.heartbeat(8,43,4096,true,0,400,400));yes(p.effectiveGain(20,400)==20);
        yes(!p.watchdog(1000,true));yes(p.watchdog(1200,true)&&p.reason()==4);
        p.begin(9);yes(p.heartbeat(9,44,4096,true,0,1300,1300));
        yes(!p.heartbeat(9,45,8192,true,0,1400,1400)&&p.latched());
        p.begin(10);yes(p.heartbeat(10,46,4096,true,0,2000,2000));
        yes(!p.heartbeat(10,46,4095,true,0,2010,2010)&&p.latched());
        for(int user=0;user<=100;user++)for(int wet:new int[]{0,250,280,320,360}){
            int dry=FeedbackProtection.dryBudget(user,wet),wetCeiling=user-dry;
            yes(dry>=0&&dry<=user&&wetCeiling>=0&&dry+wetCeiling==user);
        }
        p.begin(11);yes(p.heartbeat(11,47,4096,true,0,3000,3000));
        yes(!p.observe(11,47,1,1,0,20));yes(!p.observe(11,47,1,1,12001,20));
        yes(p.observe(11,47,1,3,0,20)&&p.reason()==5);
        System.out.println("FeedbackProtectionTest passed: lease/PID/replay, watchdog, latched attenuation, closed EQ and dry/wet budget");
    }
}
