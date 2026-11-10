import local.friday.ktv.DirectMonitorGate;

public final class DirectMonitorGateTest {
    private static void check(boolean value,String reason){if(!value)throw new AssertionError(reason);}
    public static void main(String[] args){
        DirectMonitorGate gate=new DirectMonitorGate();
        check(!gate.ready(),"new screen must require a quiet duplex check");
        gate.completed(gate.revision(),0,true,true,true);
        check(!gate.ready(),"output-only success cannot authorize a microphone audition");
        gate.completed(gate.revision(),0,true,false,true);
        check(gate.ready(),"quiet duplex check authorizes the checked mode");
        long standard=gate.revision();gate.selectMode(1);
        check(!gate.ready(),"changing backend revokes the old check");
        gate.completed(standard,0,true,false,true);
        check(!gate.ready(),"late result from previous backend cannot authorize the new backend");
        gate.completed(gate.revision(),1,true,false,true);
        check(gate.ready(),"compatibility backend can qualify independently");
        gate.completed(gate.revision(),1,true,true,true);
        check(!gate.ready(),"subsequent output-only check does not preserve duplex approval");
        gate.completed(gate.revision(),1,true,false,true);
        gate.completed(gate.revision(),1,false,false,true);
        check(gate.ready(),"normal audition completion preserves its preceding check");
        gate.completed(gate.revision(),1,false,false,false);
        check(!gate.ready(),"audition failure requires another quiet check");
        long beforeRouteChange=gate.revision();gate.invalidate();
        gate.completed(beforeRouteChange,1,true,false,true);
        check(!gate.ready(),"late result after a device or foreground change cannot rearm");
        gate.completed(gate.revision(),1,true,false,true);
        gate.selectMode(2);
        check(!gate.ready(),"exclusive candidate must qualify independently");
        gate.completed(gate.revision(),2,true,true,true);
        check(!gate.ready(),"exclusive output-only check still cannot authorize microphone output");
        gate.completed(gate.revision(),2,true,false,true);
        check(gate.ready(),"successful exclusive duplex check authorizes only that mode");
        gate.invalidate();check(!gate.ready(),"background invalidates a completed check");
        try{gate.selectMode(3);throw new AssertionError("unknown mode accepted");}catch(IllegalArgumentException expected){}
        System.out.println("Diagnostic route and mode gate contracts passed; no audio device started");
    }
}
