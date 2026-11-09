package local.friday.ktv.audio.protocol;

/** Pure lease gate. Caller holds the same policy monitor for every method. */
public final class CarrierGate {
    private long lease;
    private boolean occupied,ready,cancelled,sequenceFinished,closing;
    public boolean attach(long value){if(occupied)return false;lease=value;occupied=true;ready=cancelled=sequenceFinished=closing=false;return true;}
    public boolean occupied(){return occupied;}
    public void cancel(){if(occupied)cancelled=true;}
    public boolean ready(long value){if(!occupied||lease!=value||cancelled||closing)return false;ready=true;return true;}
    public boolean allowVendorStart(long value){return occupied&&lease==value&&ready&&!cancelled&&!closing;}
    public void sequenceFinished(long value){if(occupied&&lease==value)sequenceFinished=true;}
    public boolean beginClose(long value,boolean stopIssuedAfterSequence){if(!occupied||lease!=value||!sequenceFinished||!stopIssuedAfterSequence||closing)return false;closing=true;return true;}
    public void finishClose(long value,boolean released){if(occupied&&lease==value&&closing&&released)occupied=false;}
}
