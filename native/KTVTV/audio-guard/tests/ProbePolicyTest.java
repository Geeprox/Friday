import local.friday.ktv.audio.protocol.*;

public final class ProbePolicyTest {
    static int checks;
    static void check(boolean value,String reason){checks++;if(!value)throw new AssertionError(reason);}
    static ProbePolicy.Target target(String address,int sourceCount,int matches,int vendor,int product){
        return new ProbePolicy.Target(95,address,sourceCount,matches,vendor,product,0,true,true,true,true);
    }
    static ProbePolicy.Target target(){return target("card=4;device=0",1,1,4660,22136);}
    static String cards(){return " 0 [mtk]: card\n 4 [SYNTHMIC       ]: USB-Audio - Synthetic USB input\n  vendor text\n";}
    static ProbePolicy ready(){
        ProbePolicy p=new ProbePolicy();p.foregroundReady=true;p.independentUid=true;
        check(p.idleEvidence(target(),"1.1.3","single",-1,cards(),true),"idle");
        return p;
    }
    static ProbePolicy started(){ProbePolicy p=ready();check(p.beginStart(5000,5000,true,true),"explicit start");return p;}
    public static void main(String[] args){
        ProbePolicy empty=new ProbePolicy();check(!empty.owns&&empty.startCalls==0,"default no start");
        check(!empty.beginStart(10,10,true,true),"no target evidence no start");
        check(target().valid(),"current capability-qualified target");
        for(String address:new String[]{"","4,0","card=-1;device=0","card=4;device=0;61696=0","card=4;device=0\n","card=32;device=0","card=4;device=32","card=04;device=0"})
            check(!target(address,1,1,4660,22136).valid(),"reject address "+address);
        check(!target("card=4;device=0",2,1,4660,22136).valid(),"ambiguous audio target");
        check(!target("card=4;device=0",1,2,4660,22136).valid(),"ambiguous USB target");
        check(target("card=4;device=0",1,1,1,22136).valid(),"vendor brand is not a capability whitelist");
        check(!target("card=4;device=0",1,1,0,22136).valid(),"unknown USB vendor descriptor");
        check(!target("card=4;device=0",1,1,1,0).valid(),"unknown USB product descriptor");
        check(ProbePolicy.cardMatches(target(),cards()+"5 [x]: USB-Audio - Another output\n"),"other card cannot redirect the selected ALSA address");
        check(!ProbePolicy.cardMatches(target(),cards()+"4 [duplicate]: USB-Audio - Generic receiver\n"),"duplicate selected card is ambiguous");
        check(ProbePolicy.cardMatches(target(),"0 [internal]: PCH\n4 [generic]: USB-Audio - Any capable input\n"),"generic actual USB card marker");
        check(!ProbePolicy.cardMatches(target("card=5;device=0",1,1,4660,22136),cards()),"address not inferred from cards");
        check(Protocol.start(target()).equals("61696=4,0,2,0"),"closed start args");
        check(Protocol.STOP_COMMAND.equals("61697=null")&&Protocol.MIC_ZERO.equals("61698=0")&&Protocol.SYSTEM_ZERO.equals("61713=0"),"fixed rollback and zero");
        check(Protocol.MIC_LOW.equals("61698=5")&&Protocol.SYSTEM_LOW.equals("61713=100"),"fixed five percent product");
        for(int gain:new int[]{0,1,5,6,10,20,40,50,60,70,99,100})check(Protocol.gainAllowed(gain)&&Protocol.micGain(gain).equals("61698="+gain),"fixed gain "+gain);
        for(int gain:new int[]{-1,101,Integer.MIN_VALUE,Integer.MAX_VALUE}){check(!Protocol.gainAllowed(gain),"reject other gain");boolean rejected=false;try{Protocol.micGain(gain);}catch(IllegalArgumentException expected){rejected=true;}check(rejected,"no arbitrary gain command");}
        check(Protocol.status("61959=0")==0&&Protocol.status("61959=2")==2&&Protocol.status("61959=-1")==-1,"status encoding");
        check(Protocol.status("")==Integer.MIN_VALUE&&Protocol.status("61959=2;fake=0")==Integer.MIN_VALUE,"empty and multi-value unknown");
        ProbePolicy p=ready();check(!p.idleEvidence(target(),"1.1.3","single",0,cards(),true),"do not take over existing HAL session");
        p=ready();p.foregroundReady=false;check(!p.beginStart(5000,5000,true,true),"FGS unavailable");
        p=ready();p.independentUid=false;check(!p.beginStart(5000,5000,true,true),"independent guard unavailable");
        p=ready();check(!p.beginStart(5000,5000,false,true),"explicit local gesture needed");
        p=ready();check(!p.beginStart(5000,5000,true,false),"explicit capture permission");
        p=ready();check(!p.beginStart(5000,5001,true,true),"future action is not evidence");
        p=ready();check(p.beginStart(125000,3000,true,true),"no action expiry or selftest gate");check(!p.watchdogVerified&&p.deadlineMs==0,"no invented watchdog verification or automatic deadline");
        p=started();long lease=p.leaseId;
        check(p.startFinished(5100,true,0,true,true),"returned start, zero and enable calls");
        check(!p.allowGain(lease,5200,5200,false,5),"gain must use local action");
        check(!p.allowGain(lease+1,5200,5200,true,5),"gain cannot use another lease");
        for(int gain:new int[]{0,1,5,6,10,20,40,50,60,70,99,100}){check(p.allowGain(lease,5300,5300,true,gain),"allowed local gain");p.gainFinished(lease,gain,true,0);check(p.state==ProbePolicy.LOW&&p.requestedGain==gain,"gain return recorded");}
        check(!p.allowGain(lease,5400,5400,true,101),"gain outside closed set");
        p.requestStop(ProbePolicy.USER);p.stopFinished(20010,true,2,true,true,true);check(!p.owns&&p.state==ProbePolicy.STOPPED,"STOP plus disable return and status2 after start");
        p=started();p.clientDied(5100);check(p.state==ProbePolicy.STOPPING,"client death requests stop");
        p.stopFinished(5200,true,2,false,true,true);check(p.owns&&p.state==ProbePolicy.RECOVERY,"early stop cannot declare late start closed");
        check(!p.startFinished(5300,true,0,true,true),"late start must lead to final stop");
        p.requestStop(ProbePolicy.CLIENT_DIED);p.stopFinished(5400,true,2,true,true,true);check(!p.owns,"final stop after late start");
        p=started();p.startFinished(5100,true,0,true,true);p.requestStop(ProbePolicy.USER);p.stopFinished(5200,false,2,true,true,true);
        check(p.owns&&p.state==ProbePolicy.RECOVERY,"state2 alone not cleanup proof");
        p=started();p.startFinished(5100,true,0,true,true);p.requestStop(ProbePolicy.USER);p.stopFinished(5200,true,0,true,true,true);
        check(p.owns,"stop return alone not enough");
        p=started();p.startFinished(5100,true,0,true,true);p.requestStop(ProbePolicy.USER);p.stopFinished(5200,true,2,true,false,true);
        check(p.owns&&p.state==ProbePolicy.RECOVERY,"disable return required for owned cleanup");
        p=started();check(!p.startFinished(5100,true,0,true,false)&&p.state==ProbePolicy.STOPPING,"failed enable leads to stop");
        p=new ProbePolicy();p.recoverOwnedMarker();check(p.owns&&p.state==ProbePolicy.RECOVERY&&p.startCalls==0,"restart never starts, retained unknown cleanup");
        System.out.println("ProbePolicyTest: "+checks+" checks passed");
    }
}
