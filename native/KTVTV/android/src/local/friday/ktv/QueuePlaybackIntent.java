package local.friday.ktv;
/** Queue waiting survives an empty list; an explicit pause or failure needs a resume. */
public final class QueuePlaybackIntent {
    private boolean automatic=true;
    public void awaitNext(){automatic=true;}
    public void pause(){automatic=false;}
    public boolean shouldStart(boolean foreground,boolean loading,boolean hasCurrent){
        return automatic&&foreground&&!loading&&!hasCurrent;
    }
}
