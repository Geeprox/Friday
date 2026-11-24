package local.friday.ktv;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.function.BiConsumer;
import java.util.function.Consumer;
import java.util.function.LongSupplier;

/** 主线程持有释放待办，独立网络线程负责执行，网络退避使用单调时钟。 */
final class PlaybackReleaseQueue {
    private static final class Release {
        final PlaybackStartClaim claim;
        final Thread stopping;
        int failures;
        long retryAt;

        Release(PlaybackStartClaim claim,Thread stopping){this.claim=claim;this.stopping=stopping;}
        KtvClient route(KtvClient current){
            if(failures==0)return claim.owner;
            return claim.owner.sameTelevisionEndpoint(current)?current:null;
        }
    }

    private final ExecutorService network;
    private final LongSupplier clock;
    private final Consumer<Runnable> post,remove;
    private final BiConsumer<Runnable,Long> later;
    private final Map<String,Release> pending=new LinkedHashMap<>();
    private final Runnable wake=this::pump;
    private KtvClient current;
    private Release active;
    private boolean closing;

    PlaybackReleaseQueue(ExecutorService network,LongSupplier clock,Consumer<Runnable> post,
                         BiConsumer<Runnable,Long> later,Consumer<Runnable> remove){
        this.network=network;this.clock=clock;this.post=post;this.later=later;this.remove=remove;
    }

    void connectionChanged(KtvClient route){current=route;pump();}
    void enqueue(PlaybackStartClaim claim,Thread stopping){
        if(closing||claim==null||!claim.cancel())return;
        pending.put(claim.request,new Release(claim,stopping));
        pump();
    }
    void close(){closing=true;pump();}

    private void pump(){
        remove.accept(wake);
        if(active!=null)return;
        long now=clock.getAsLong(),next=Long.MAX_VALUE;
        for(Release release:pending.values()){
            KtvClient route=release.route(current);
            if(route==null)continue;
            long delay=release.retryAt-now;
            if(release.failures==0||delay<=0){
                active=release;
                network.execute(()->send(release,route));
                return;
            }
            next=Math.min(next,delay);
        }
        if(closing){pending.clear();network.shutdown();}
        else if(next!=Long.MAX_VALUE)later.accept(wake,next);
    }

    private void send(Release release,KtvClient route){
        boolean complete=false;
        try{
            AudioEngine.awaitPlaybackWorker(release.stopping);
            PlaybackStartClaim claim=release.claim;
            route.releasePlaybackStart(claim.room,claim.song,claim.request);
            complete=true;
        }catch(KtvClient.HttpFailure refusal){
            complete=refusal.televisionChanged()||"room_changed".equals(refusal.getMessage());
        }catch(InterruptedException interrupted){Thread.currentThread().interrupt();}
        catch(Exception ignored){/* 同一连接恢复后再释放原预约。 */}
        final boolean done=complete;
        post.accept(()->finished(release,done));
    }

    private void finished(Release release,boolean complete){
        active=null;
        if(complete||closing)pending.remove(release.claim.request);
        else{
            release.retryAt=clock.getAsLong()+(500L<<release.failures);
            release.failures=Math.min(release.failures+1,3);
        }
        pump();
    }
}
