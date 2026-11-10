package local.friday.ktv;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;

/** Automatic migration and intent decisions only; no engine, JNI or audio. */
public final class AudioBackendSelectionTest {
    private static int checks;
    private static void yes(boolean value,String reason){checks++;if(!value)throw new AssertionError(reason);}
    private static void held(AudioBackendSelection p,String reason){yes(!p.playbackAllowed()&&!p.microphoneAllowed()&&p.microphoneHeld(),reason);}
    private static void migrations(){
        for(String saved:new String[]{null,"","auto","compat","system","unknown"}){
            AudioBackendSelection tcl=new AudioBackendSelection(saved,true,true,false);
            yes(tcl.system()&&tcl.selected(),"all old TCL preferences prefer the actually safe system path");held(tcl,"safe migration never auto-plays or restores a microphone");
            yes(!tcl.localMicrophone(false),"unmute before fresh play cannot pre-arm capture");
            yes(tcl.userPlay()&&tcl.playbackAllowed()&&!tcl.microphoneAllowed(),"fresh play releases only backing");
            yes(tcl.localMicrophone(false)&&tcl.microphoneAllowed(),"a separate fresh unmute may open voice");
            AudioBackendSelection missing=new AudioBackendSelection(saved,true,false,false);
            yes(missing.unavailable()&&!missing.system()&&AudioBackendSelection.UNSELECTED.equals(missing.mode()),"missing safety evidence never sends legacy TCL to compat");held(missing,"unsafe TCL starts silent");
            yes(missing.userPlay()&&missing.playbackAllowed()&&!missing.microphoneAllowed(),"QR/backing remains usable after fresh play without unsafe voice");
            yes(!missing.localMicrophone(false)&&!missing.microphoneAllowed(),"unavailable voice cannot be enabled");
            for(boolean safe:new boolean[]{false,true}){
                AudioBackendSelection generic=new AudioBackendSelection(saved,false,safe,false);
                yes(generic.selected()&&!generic.system()&&AudioBackendSelection.COMPAT.equals(generic.mode()),"standard devices automatically use their compatible implementation");held(generic,"generic migration also needs fresh play and unmute");
            }
        }
    }
    private static void freshRemoteIntent(){
        AudioBackendSelection p=new AudioBackendSelection("compat",true,true,false);
        yes(!p.remoteCommandsFresh(),"first command snapshot is history, even if an old PLAY is recent");held(p,"history does not release playback");
        yes(p.remoteCommandsFresh(),"subsequent commands can represent fresh user actions");
        yes(!p.remoteMicrophone(false,7,true),"first restored unmute cannot become a gesture merely from its revision");
        yes(p.userPlay()&&!p.microphoneAllowed(),"fresh playback does not reuse pre-play unmute");
        yes(!p.remoteMicrophone(false,7,true)&&!p.remoteMicrophone(false,6,true),"same and older revisions cannot unmute");
        yes(p.remoteMicrophone(false,8,true)&&p.microphoneAllowed(),"new post-play user mic action may unmute");
        yes(p.remoteMicrophone(true,9,false)&&!p.microphoneAllowed(),"mute always wins");
        p.roomChanged();held(p,"new room cannot inherit playback or microphone intent");
        yes(!p.remoteCommandsFresh()&&!p.remoteMicrophone(false,100,true),"new room establishes fresh baselines instead of replaying old controls");
        yes(!p.remoteMicrophone(false,101,true)&&p.userPlay()&&!p.microphoneAllowed(),"mic request before playback remains held after play");
        yes(!p.remoteMicrophone(false,101,true)&&p.remoteMicrophone(false,102,true),"requires an additional new unmute after playback");
    }
    private static void releaseAndCapabilityChanges(){
        AudioBackendSelection p=new AudioBackendSelection("system",true,true,true);
        yes(p.cleanupBlocked()&&!p.selected()&&!p.userPlay(),"unfinished prior ownership blocks all playback and capture");
        long first=p.beginAutomatic(true);yes(p.switching()&&p.currentUpdate(first),"internal recovery begins without a user choice");
        yes(!p.completeAutomatic(first,false,true)&&!p.completeAutomatic(first,true,false),"both render and other owners need real release evidence");
        yes(p.completeAutomatic(first,true,true)&&p.system(),"verified cleanup makes supported path available");held(p,"cleanup completion still never starts audio");
        yes(p.userPlay()&&!p.microphoneAllowed(),"play after cleanup remains mic-muted");
        yes(p.remoteMicrophone(true,3,false)&&p.remoteMicrophone(false,4,true),"fresh voice operation after baseline");
        yes(p.updateNeeded(false),"lost safety contract requires an internal update");
        long loss=p.beginAutomatic(false);held(p,"capability loss immediately holds output and voice");
        yes(p.completeAutomatic(loss,true,true)&&p.unavailable()&&!p.system(),"lost contract cannot select software on a legacy system device");
        yes(p.userPlay()&&!p.microphoneAllowed(),"only backing may resume by user action");
        long restore=p.beginAutomatic(true),newer=p.beginAutomatic(true);
        yes(!p.currentUpdate(restore)&&p.currentUpdate(newer)&&!p.completeAutomatic(restore,true,true),"late cleanup callback cannot publish a new owner");
        yes(p.completeAutomatic(newer,true,true)&&p.system(),"restored capability automatically chooses the supported system path");held(p,"safe recovery never auto-resumes or auto-unmutes");
        yes(!p.remoteCommandsFresh()&&!p.remoteMicrophone(false,99,true),"recovery starts new history and mic baselines");
        long failure=p.beginAutomatic(true);p.failAutomatic(failure);
        yes(p.cleanupBlocked()&&p.unavailable()&&!p.userPlay()&&!p.localMicrophone(false),"unverified cleanup blocks every new owner");
    }
    private static void noChoiceUi()throws Exception{
        String source=new String(Files.readAllBytes(Paths.get("native/KTVTV/android/src/local/friday/ktv/MainActivity.java")),StandardCharsets.UTF_8);
        for(String removed:new String[]{"audioBackendDialog", "backendPrompted", "选择演唱通道", "系统低延迟", "兼容软件", "使用所选通道", "演唱通道检查"})
            yes(!source.contains(removed),"normal TV UI has no backend chooser: "+removed);
        yes(!source.contains("putString(\"audio-backend\""),"automatic decisions are not persisted as explicit user choices");
        yes(source.contains("prior.released()")&&source.contains("vendorReleased()")&&source.contains("vocalEffects.released()"),"automatic recovery retains all actual ownership fences");
        yes(source.contains("if(!backendSelection.remoteCommandsFresh())"),"real command consumer skips the first history snapshot");
        yes(source.contains("engine=createEngine(!AudioBackendSelection.COMPAT.equals(backendSelection.mode()))"),"unavailable legacy voice uses output-only engine, not compat capture");
    }
    public static void main(String[] args)throws Exception{
        migrations();freshRemoteIntent();releaseAndCapabilityChanges();noChoiceUi();
        System.out.println("AudioBackendSelectionTest: "+checks+" checks passed; automatic migration, no chooser, actual cleanup fence, fresh play/unmute and no legacy fallback");
    }
}
