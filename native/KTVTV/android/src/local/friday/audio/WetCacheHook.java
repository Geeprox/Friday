package local.friday.audio;

/** Owns only the OEM USB cache-hook request, never the GlobalMic dry lease.
 * Calls/returns are not hardware readback. Disable is the OEM deinit reset,
 * not restoration of an observed previous value. A native process crash can
 * leave this process-global HAL flag enabled; this class cannot guarantee cleanup.
 * All methods run on the wet service's single control worker.
 */
public final class WetCacheHook {
    public static final int NONE=0, PERMISSION_MISSING=1, ENABLE_FAILED=2,
            DISABLE_FAILED=3, RELEASE_UNCONFIRMED=4, PREVIOUS_REQUEST_PENDING=5;
    private static final String ENABLE="IsTianlaiEnabled=enable";
    private static final String DISABLE="IsTianlaiEnabled=disable";
    public interface Writer {
        boolean permitted();
        void write(String fixedValue)throws Exception;
    }
    /** finish must precede released(), and dispose must return before reset. */
    public interface Resource {
        void bypass()throws Exception;
        void finish()throws Exception;
        boolean released()throws Exception;
        void dispose()throws Exception;
    }
    private final Writer writer;
    private long enableCalls,disableCalls;
    private int error;
    private boolean requested,nativeMayExist,disableAttempted;
    public WetCacheHook(Writer writer){this.writer=writer;}
    private boolean permitted(){
        try{return writer.permitted();}catch(RuntimeException|LinkageError denied){return false;}
    }
    public boolean enable(){
        if(requested||nativeMayExist){error=PREVIOUS_REQUEST_PENDING;return false;}
        error=NONE;disableAttempted=false;
        if(!permitted()){error=PERMISSION_MISSING;return false;}
        // A thrown setter may have taken effect before it threw. Retain reset
        // responsibility even when its return could not be observed.
        requested=true;enableCalls++;
        try{writer.write(ENABLE);return true;}
        catch(Exception|LinkageError failure){error=ENABLE_FAILED;return false;}
    }
    public void beforeNativeOpen(){
        if(!requested||error!=NONE)throw new IllegalStateException("cache_hook_not_enabled");
        nativeMayExist=true;
    }
    /** Release wet resources, then reset once. Never retry a failed reset from
     * a later poll/onUnbind/onDestroy, and never reset before input release.
     * Null is safe only if native opening was never attempted. */
    public boolean close(Resource resource){
        if(resource!=null){
            boolean released=false,disposed=false;
            try{resource.bypass();}catch(Exception|LinkageError failure){error=RELEASE_UNCONFIRMED;}
            try{resource.finish();released=resource.released();}
            catch(Exception|LinkageError failure){error=RELEASE_UNCONFIRMED;}
            finally{
                try{resource.dispose();disposed=true;}
                catch(Exception|LinkageError failure){error=RELEASE_UNCONFIRMED;}
            }
            nativeMayExist=!(released&&disposed);
        }
        if(nativeMayExist){error=RELEASE_UNCONFIRMED;return false;}
        if(!requested)return true;
        if(disableAttempted)return false;
        disableAttempted=true;
        if(!permitted()){error=PERMISSION_MISSING;return false;}
        disableCalls++;
        try{writer.write(DISABLE);requested=false;return true;}
        catch(Exception|LinkageError failure){error=DISABLE_FAILED;return false;}
    }
    public long enableCalls(){return enableCalls;}
    public long disableCalls(){return disableCalls;}
    public int error(){return error;}
    /** Our unresolved request, not the actual current HAL flag value. */
    public boolean requested(){return requested;}
}
