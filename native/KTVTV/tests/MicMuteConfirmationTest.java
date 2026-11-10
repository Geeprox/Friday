package local.friday.ktv;

public final class MicMuteConfirmationTest {
    public static void main(String[] args){
        // Neither a local mute choice nor a revoked readiness appears in this API.
        boolean starting=MicMuteConfirmation.ownedReadback(3,true,false,0,false,false,0);
        if(MicMuteConfirmation.known(100,30,4,true,starting,false))throw new AssertionError("STARTING raw false is unknown, not an unmute ACK");
        if(MicMuteConfirmation.ownedReadback(8,true,false,0,true,true,20))throw new AssertionError("RECOVERY is unknown");
        if(MicMuteConfirmation.ownedReadback(6,true,false,0,true,true,20))throw new AssertionError("STOPPING is unknown");
        if(!MicMuteConfirmation.ownedReadback(4,true,true,0,true,false,0))throw new AssertionError("ZERO finished");
        if(!MicMuteConfirmation.ownedReadback(5,true,false,0,true,true,20))throw new AssertionError("LOW positive finished");
        if(MicMuteConfirmation.ownedReadback(5,true,false,0,true,true,20,true))throw new AssertionError("old positive ACK cannot acknowledge a pending fresh restore");
        if(!MicMuteConfirmation.ownedReadback(5,true,true,0,true,true,0,true))throw new AssertionError("completed zero remains a valid mute ACK during restore");
        if(!MicMuteConfirmation.ownedReadback(5,true,true,0,true,true,0))throw new AssertionError("LOW zero finished");
        if(MicMuteConfirmation.ownedReadback(5,true,false,0,true,false,20))throw new AssertionError("LOW setter pending");
        if(MicMuteConfirmation.ownedReadback(5,true,false,2,true,true,20))throw new AssertionError("wrong HAL status");
        if(MicMuteConfirmation.ownedReadback(5,true,true,0,true,true,20))throw new AssertionError("inconsistent raw mute");
        if(!MicMuteConfirmation.known(100,30,4,true,true,false))throw new AssertionError();
        if(MicMuteConfirmation.muted(100,30,4,true,true,false,false))throw new AssertionError("MIC_ZERO pending cannot acknowledge mute");
        if(!MicMuteConfirmation.muted(100,30,4,true,true,true,false))throw new AssertionError("fresh zero-call receipt");
        if(MicMuteConfirmation.muted(751,30,4,true,true,true,false))throw new AssertionError("stale receipt");
        if(MicMuteConfirmation.known(100,30,0,true,true,false))throw new AssertionError("no owned lease identity");
        if(MicMuteConfirmation.known(100,30,4,true,false,false))throw new AssertionError("missing boolean");
        if(MicMuteConfirmation.known(100,0,4,true,true,false))throw new AssertionError("missing process identity");
        if(!MicMuteConfirmation.muted(100,30,4,false,false,false,true))throw new AssertionError("verified full release");
        if(MicMuteConfirmation.muted(100,30,4,false,false,false,false))throw new AssertionError("unobserved release");
        System.out.println("MicMuteConfirmationTest passed: STARTING/RECOVERY unknown, completed ZERO/LOW, stale receipt and verified release");
    }
}
