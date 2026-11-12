import local.friday.ktv.audio.protocol.*;

/** Crosses the actual START/ZERO/protection state machines without platform audio. */
public final class ProtectionStartupTest {
    public static void main(String[] args){
        ProbePolicy p=ProbePolicyTest.started();FeedbackProtection protection=new FeedbackProtection();protection.begin(p.leaseId);
        if(p.requestedGain!=0||protection.watchdog(5100,p.requestedGain>0)||protection.latched())throw new AssertionError("START/carrier wait must not appear as applied positive gain");
        if(!p.startFinished(5200,true,0,true,true))throw new AssertionError();
        if(protection.watchdog(5300,p.requestedGain>0)||protection.effectiveGain(70,5300)!=0)throw new AssertionError("ZERO awaits ready observer");
        protection.heartbeat(p.leaseId,1001,4096,true,0,5400,5400);
        if(!p.allowGain(p.leaseId,5400,5400,true,5))throw new AssertionError();
        p.gainFinished(p.leaseId,protection.effectiveGain(5,5400),true,0);
        if(p.requestedGain!=5||protection.watchdog(5500,p.requestedGain>0))throw new AssertionError();
        // Explicit reset must keep old protection while zero is pending; begin
        // the replacement only after the native zero completion is recorded.
        p.gainFinished(p.leaseId,0,true,0);protection.begin(p.leaseId);
        if(protection.watchdog(5600,p.requestedGain>0)||protection.latched())throw new AssertionError("reset zero ordering");
        protection.heartbeat(p.leaseId,2001,4096,true,0,5700,5700);
        if(protection.effectiveGain(20,5700)!=20)throw new AssertionError("fresh monitor can enable after reset");
        System.out.println("ProtectionStartupTest passed: START wait, ZERO bootstrap and zero-before-reset across actual policies");
    }
}
