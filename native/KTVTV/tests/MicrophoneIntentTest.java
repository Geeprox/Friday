package local.friday.ktv;

/** Uses MainActivity's production intent policy without constructing an
 * Activity or calling a microphone, permission dialog, or output API. */
public final class MicrophoneIntentTest {
    private static void yes(boolean value,String note){if(!value)throw new AssertionError(note);}
    private static boolean enabled(MainActivity.MicrophoneIntent intent,boolean foreground,boolean paused){
        return intent.mayRun(foreground,true,paused,true,true,20);
    }
    public static void main(String[] args){
        MainActivity.MicrophoneIntent intent=new MainActivity.MicrophoneIntent();
        yes(!intent.muted,"first entry defaults to user-unmuted intent");
        yes(!enabled(intent,true,false),"await saved room controls before any capture");
        intent.controlsReady=true;intent.choose(false);
        yes(enabled(intent,true,false),"new-room default and warmup permit foreground capture");
        yes(!enabled(intent,false,false)&&!enabled(intent,true,true),"background and pause stop hardware");
        intent.leaveForeground();yes(!intent.muted&&!enabled(intent,true,false),"return waits for fresh room controls");
        intent.controlsReady=true;
        yes(!intent.muted&&enabled(intent,true,false),"same server revision restores the prior unmuted choice");

        intent.choose(true);
        for(int song=0;song<3;song++){
            yes(!enabled(intent,true,false),"mute survives song change/replay/warmup");
            intent.leaveForeground();yes(!enabled(intent,false,true)&&!enabled(intent,true,false),"mute survives background/return");
            intent.controlsReady=true;
        }
        yes(intent.muted,"lifecycle never becomes a different user choice");
        intent.awaitControls();yes(intent.muted&&!enabled(intent,true,false),"reconnect waits before applying server state");
        intent.controlsReady=true;intent.choose(true);yes(!enabled(intent,true,false),"saved room mute restores on first snapshot");

        intent.choose(false);
        yes(!intent.mayRun(true,true,false,false,true,20),"missing permission never starts capture");
        yes(!intent.mayRun(true,true,false,true,false,20),"missing input never starts capture");
        yes(!intent.mayRun(true,true,false,true,true,0),"zero gain never starts capture");
        yes(!intent.mayRun(true,false,false,true,true,20),"closed room never starts capture");
        intent.permissionRequested(true);
        intent.leaveForeground();
        yes(!enabled(intent,false,true),"permission activity remains a hardware stop");
        yes(intent.takePermissionResume()&&!intent.takePermissionResume(),"only explicit permission return can resume once");
        yes(!enabled(intent,true,false),"permission return cannot use an old room snapshot");intent.controlsReady=true;
        yes(!intent.muted&&enabled(intent,true,false),"granted permission preserves prior unmuted intent");
        intent.choose(true);intent.permissionRequested(true);yes(intent.takePermissionResume(),"permission can return to music");
        yes(intent.muted&&!enabled(intent,true,false),"permission grant cannot override manual mute");
        intent.permissionRequested(false);yes(!intent.takePermissionResume(),"a previously paused song stays paused");
        intent.permissionRequested(true);intent.awaitControls();yes(!intent.takePermissionResume(),"new room invalidates old permission return");

        intent.controlsReady=true;intent.choose(false);intent.block();
        yes(!intent.muted&&!enabled(intent,true,false),"runtime fault is a separate hold, not a user mute");
        intent.leaveForeground();intent.controlsReady=true;
        yes(!enabled(intent,false,true)&&!enabled(intent,true,false),"fresh controls after background do not clear a real fault hold");
        intent.awaitControls();intent.restore(false);intent.controlsReady=true;
        yes(!enabled(intent,true,false),"first restored controls after reconnect do not clear the fault hold");
        intent.choose(false);yes(enabled(intent,true,false),"explicit retry clears the hold");
        System.out.println("MicrophoneIntentTest passed: default/warmup, manual choice, pause/background, permission return and fault hold; no device actions");
    }
}
