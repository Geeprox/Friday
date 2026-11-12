package local.friday.audio;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/** Real cache-hook lifecycle with fake platform/native resources. No Android,
 * microphone, device, native library or audio output is used. */
public final class WetCacheHookTest {
    private static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
    private static final class Platform implements WetCacheHook.Writer {
        final List<String> events=new ArrayList<>();boolean permission=true,failEnable,failDisable;
        public boolean permitted(){return permission;}
        public void write(String value){
            events.add(value);
            if(value.equals("IsTianlaiEnabled=enable")&&failEnable)throw new IllegalStateException();
            if(value.equals("IsTianlaiEnabled=disable")&&failDisable)throw new IllegalStateException();
            yes(value.equals("IsTianlaiEnabled=enable")||value.equals("IsTianlaiEnabled=disable"),"fixed hook pair only");
        }
    }
    private static final class Native implements WetCacheHook.Resource {
        final Platform platform;boolean closed=true,failBypass,failFinish,failReadback,failDispose;
        Native(Platform p){platform=p;}
        public void bypass(){platform.events.add("bypass");if(failBypass)throw new IllegalStateException();}
        public void finish(){platform.events.add("finish");if(failFinish)throw new IllegalStateException();}
        public boolean released(){platform.events.add("closed_readback");if(failReadback)throw new IllegalStateException();return closed;}
        public void dispose(){platform.events.add("dispose");if(failDispose)throw new IllegalStateException();}
    }
    private static void order(Platform p,String... events){yes(p.events.equals(Arrays.asList(events)),"ordering: "+p.events);}
    public static void main(String[] args){
        Platform p=new Platform();WetCacheHook h=new WetCacheHook(p);
        yes(h.close(null)&&h.enableCalls()==0&&h.disableCalls()==0,"bind/read/idle close has no setter");
        p.permission=false;yes(!h.enable()&&h.error()==WetCacheHook.PERMISSION_MISSING&&!h.requested(),"permission before enable");
        yes(h.close(null)&&p.events.isEmpty(),"missing permission never creates reset work");
        p.permission=true;yes(h.enable(),"explicit enable returns");h.beforeNativeOpen();p.events.add("open");
        yes(h.close(new Native(p)),"normal close/reset");
        order(p,"IsTianlaiEnabled=enable","open","bypass","finish","closed_readback","dispose","IsTianlaiEnabled=disable");
        yes(!h.requested()&&h.error()==0&&h.enableCalls()==1&&h.disableCalls()==1,"call facts, no pending reset");
        h.close(null);yes(h.disableCalls()==1,"onUnbind/destroy cannot repeat completed reset");

        p=new Platform();h=new WetCacheHook(p);p.failEnable=true;
        yes(!h.enable()&&h.requested()&&h.error()==WetCacheHook.ENABLE_FAILED,"ambiguous enable still owns reset");
        yes(h.close(null)&&!h.requested()&&h.error()==WetCacheHook.ENABLE_FAILED,"pre-open failure reset retains cause");
        order(p,"IsTianlaiEnabled=enable","IsTianlaiEnabled=disable");

        // An open that returns an invalid native handle still has a releasable
        // object: finish/readback/dispose must precede reset exactly as normal.
        p=new Platform();h=new WetCacheHook(p);yes(h.enable(),"enable");h.beforeNativeOpen();
        yes(h.close(new Native(p))&&h.disableCalls()==1,"failed native open cleanup");

        // A native crash does not run Java cleanup. We model the last observable
        // state: no resource or close readback is available. Never claim reset.
        p=new Platform();h=new WetCacheHook(p);yes(h.enable(),"enable");h.beforeNativeOpen();
        yes(!h.close(null)&&h.requested()&&h.disableCalls()==0&&h.error()==WetCacheHook.RELEASE_UNCONFIRMED,"unobserved native release keeps flag responsibility");
        yes(!h.enable()&&h.enableCalls()==1,"unresolved prior resources cannot open again");

        for(int failure=0;failure<4;failure++){
            p=new Platform();h=new WetCacheHook(p);yes(h.enable(),"enable");h.beforeNativeOpen();Native n=new Native(p);
            if(failure==0)n.closed=false;if(failure==1)n.failFinish=true;if(failure==2)n.failReadback=true;if(failure==3)n.failDispose=true;
            yes(!h.close(n)&&h.requested()&&h.disableCalls()==0&&h.error()==WetCacheHook.RELEASE_UNCONFIRMED,"never disable before confirmed finish+dispose, case "+failure);
            yes(p.events.contains("dispose"),"dispose attempted despite earlier release failure");
            h.close(null);yes(h.disableCalls()==0,"later lifecycle callback cannot invent close evidence");
        }
        p=new Platform();h=new WetCacheHook(p);yes(h.enable(),"enable");h.beforeNativeOpen();Native n=new Native(p);n.failBypass=true;
        yes(h.close(n)&&h.disableCalls()==1,"finish and dispose still run after bypass error");

        p=new Platform();h=new WetCacheHook(p);yes(h.enable(),"enable");h.beforeNativeOpen();p.failDisable=true;
        yes(!h.close(new Native(p))&&h.requested()&&h.error()==WetCacheHook.DISABLE_FAILED,"failed reset remains unknown");
        h.close(null);yes(h.disableCalls()==1,"no automatic reset retry");
        p=new Platform();h=new WetCacheHook(p);yes(h.enable(),"enable");h.beforeNativeOpen();p.permission=false;
        yes(!h.close(new Native(p))&&h.requested()&&h.error()==WetCacheHook.PERMISSION_MISSING&&h.disableCalls()==0,"permission rechecked before reset");
        p.permission=true;h.close(null);yes(h.disableCalls()==0,"permission change is not an automatic retry");

        p=new Platform();h=new WetCacheHook(p);
        for(int i=0;i<3;i++){yes(h.enable(),"new explicit cycle");h.beforeNativeOpen();yes(h.close(new Native(p)),"cycle close");}
        yes(h.enableCalls()==3&&h.disableCalls()==3&&!h.requested(),"explicit subsequent sessions counted independently");
        System.out.println("WetCacheHookTest passed: fixed pair, permission, release-before-reset, open/close failures, ambiguous setter, no retry and crash-unknown boundary");
    }
}
