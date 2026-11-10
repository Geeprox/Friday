package local.friday.ktv;

/** Render-side gate evidence plus a presented-frame fence. This is a software
 * acknowledgement, not an acoustic/device-latency measurement. */
public final class CompatMuteReadback {
    private long requestedAfter,outputFence=-1;
    private int candidate=-1,confirmed=-1;
    private boolean requested=true;
    public void session(long gateWord){requestedAfter=gateWord>>>3;outputFence=-1;candidate=confirmed=-1;}
    public void request(boolean muted,long gateWord){if(requested!=muted){requested=muted;session(gateWord);}}
    public void observe(long gateWord,long rendered,long presented,boolean running,boolean released){
        if(!running){confirmed=released?1:-1;candidate=-1;outputFence=-1;return;}
        if((gateWord>>>3)<=requestedAfter||rendered<0||presented<0){confirmed=-1;return;}
        boolean zero=(gateWord&1)!=0,latched=(gateWord&2)!=0,muted=(gateWord&4)!=0;
        int actual=zero&&(muted||latched)?1:!zero&&!muted&&!latched?0:-1;
        if(actual<0){confirmed=-1;candidate=-1;outputFence=-1;return;}
        if(actual!=candidate){candidate=actual;confirmed=-1;outputFence=rendered+240;}
        if(presented>=outputFence)confirmed=actual;
    }
    public boolean known(){return confirmed>=0;}
    public boolean muted(){return confirmed==1;}
}
