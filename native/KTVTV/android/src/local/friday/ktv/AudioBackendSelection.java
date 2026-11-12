package local.friday.ktv;

/** Internal capability policy. Saved user-facing mode choices are deliberately
 * ignored during migration. A legacy system device never silently falls back
 * to software; backing playback remains possible without a microphone. */
public final class AudioBackendSelection {
    public static final String COMPAT="compat",SYSTEM="system",UNSELECTED="unselected";
    private volatile String mode,requested;
    private final boolean legacySystemCandidate;
    private volatile boolean transitioning,playbackHold=true,micHold=true,cleanupBlocked;
    private boolean remoteCommandsSeen;
    private long serial,remoteMicRevision=-1;
    public AudioBackendSelection(String previousMode,boolean legacySystemCandidate,boolean systemSafe,boolean cleanupPending){
        this.legacySystemCandidate=legacySystemCandidate;mode=requested=automaticMode(systemSafe);cleanupBlocked=cleanupPending;
    }
    private String automaticMode(boolean safe){return legacySystemCandidate?(safe?SYSTEM:UNSELECTED):COMPAT;}
    public boolean updateNeeded(boolean systemSafe){return !transitioning&&(cleanupBlocked||!automaticMode(systemSafe).equals(mode));}
    public long beginAutomatic(boolean systemSafe){
        requested=automaticMode(systemSafe);transitioning=cleanupBlocked=true;playbackHold=micHold=true;remoteMicRevision=-1;remoteCommandsSeen=false;return ++serial;
    }
    public boolean completeAutomatic(long ticket,boolean engineReleased,boolean otherOwnersReleased){
        if(ticket!=serial||!transitioning||!engineReleased||!otherOwnersReleased)return false;
        mode=requested;transitioning=cleanupBlocked=false;playbackHold=micHold=true;return true;
    }
    public void failAutomatic(long ticket){if(ticket==serial){mode=requested=UNSELECTED;transitioning=false;cleanupBlocked=true;playbackHold=micHold=true;}}
    public boolean userPlay(){if(transitioning||cleanupBlocked)return false;playbackHold=false;return true;}
    public void holdPlayback(){playbackHold=true;}
    public void holdMicrophone(){micHold=true;}
    public void roomChanged(){playbackHold=micHold=true;remoteMicRevision=-1;remoteCommandsSeen=false;}
    /** Consume the first command snapshot as history, including recent old PLAY. */
    public boolean remoteCommandsFresh(){boolean fresh=remoteCommandsSeen;remoteCommandsSeen=true;return fresh;}
    public boolean localMicrophone(boolean muted){if(muted){micHold=true;return true;}if(!selected()||!playbackAllowed())return false;micHold=false;return true;}
    public boolean remoteMicrophone(boolean muted,long revision,boolean freshGesture){
        if(revision<0||revision<remoteMicRevision)return false;
        boolean initialized=remoteMicRevision>=0,newer=revision>remoteMicRevision;remoteMicRevision=revision;
        if(muted){micHold=true;return true;}
        if(initialized&&newer&&freshGesture&&selected()&&playbackAllowed()){micHold=false;return true;}return false;
    }
    public boolean selected(){return !transitioning&&!cleanupBlocked&&!UNSELECTED.equals(mode);}
    public boolean playbackAllowed(){return !transitioning&&!cleanupBlocked&&!playbackHold;}
    public boolean microphoneAllowed(){return selected()&&playbackAllowed()&&!micHold;}
    public boolean microphoneHeld(){return micHold;}
    public boolean switching(){return transitioning;}
    public boolean currentUpdate(long ticket){return transitioning&&serial==ticket;}
    public boolean unavailable(){return !selected();}
    public boolean cleanupBlocked(){return cleanupBlocked;}
    public boolean system(){return SYSTEM.equals(mode);}
    public String mode(){return mode;}
}
