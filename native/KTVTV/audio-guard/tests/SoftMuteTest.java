import local.friday.ktv.audio.protocol.*;

public final class SoftMuteTest {
    static int checks;
    static void yes(boolean value){checks++;if(!value)throw new AssertionError("check "+checks);}
    public static void main(String[] args){
        ProbePolicy p=ProbePolicyTest.started();long lease=p.leaseId;yes(p.startFinished(5100,true,0,true,true));yes(p.allowGain(lease,5200,5200,true,5));p.gainFinished(lease,5,true,0);
        int starts=p.startCalls;yes(p.allowGain(lease,5300,5300,true,0));p.gainFinished(lease,0,true,0);yes(p.owns&&p.state==ProbePolicy.LOW&&p.requestedGain==0&&p.startCalls==starts&&p.stopCalls==0);
        yes(p.allowGain(lease,5400,5400,true,20));p.gainFinished(lease,20,true,0);yes(p.owns&&p.requestedGain==20&&p.startCalls==starts&&p.stopCalls==0);
        GainRequestGate gains=new GainRequestGate();long loud=gains.request(),mute=gains.request();yes(!gains.current(loud)&&gains.current(mute));long resumed=gains.request();yes(!gains.current(mute)&&gains.current(resumed));gains.cancel();yes(!gains.current(resumed));
        p.requestStop(ProbePolicy.USER);yes(!p.allowGain(lease,5500,5500,true,0));p.stopFinished(5600,true,2,true,true,true);p.gainFinished(lease,20,true,0);yes(!p.owns&&p.state==ProbePolicy.STOPPED);
        System.out.println("SoftMuteTest: "+checks+" checks passed; zero retains lease, resume avoids START, latest request and STOP reject stale gain");
    }
}
