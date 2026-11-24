package local.friday.ktv;

import java.io.*;
import java.lang.reflect.*;
import java.nio.*;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import android.content.Context;
import android.widget.ProgressBar;
import android.widget.TextView;
import org.json.JSONArray;
import org.json.JSONObject;

/** Exercises real switch/install methods before publication, without Context,
 * AudioTrack, microphone, focus, a render thread, or an AudioEngine constructor. */
public final class BackingStartupIntentTest {
    private static final String TRACK_PATH="/v1/jobs/00000000-0000-0000-0000-000000000001/media/original";
    private static void yes(boolean value,String note){if(!value)throw new AssertionError(note);}
    private static <T> T empty(Class<T> target)throws Exception {
        Class<?> type=Class.forName("sun.misc.Unsafe");Field access=type.getDeclaredField("theUnsafe");access.setAccessible(true);
        return target.cast(type.getMethod("allocateInstance",Class.class).invoke(access.get(null),target));
    }
    private static Field activityField(String name)throws Exception {
        Field field=MainActivity.class.getDeclaredField(name);
        field.setAccessible(true);
        return field;
    }
    private static Method activityMethod(String name,Class<?>... types)throws Exception {
        Method method=MainActivity.class.getDeclaredMethod(name,types);
        method.setAccessible(true);
        return method;
    }
    private static void initial(AudioEngine engine,File initial)throws Exception {
        // The known window after start has selected a file and before its
        // render worker publishes musicSource. No Android entry point runs.
        Field desired=AudioEngine.class.getDeclaredField("desiredBacking");desired.setAccessible(true);desired.set(engine,initial);
        Field source=AudioEngine.class.getDeclaredField("musicSource");source.setAccessible(true);source.set(engine,null);
    }
    private static void install(AudioEngine engine,SwitchingWavSource source,File initial)throws Exception {
        Method method=AudioEngine.class.getDeclaredMethod("installMusicSource",SwitchingWavSource.class,File.class);
        method.setAccessible(true);method.invoke(engine,source,initial);
    }
    private static File wave(Path root,String name,int value)throws Exception {
        byte[] pcm=new byte[44+48000*4];ByteBuffer data=ByteBuffer.wrap(pcm).order(ByteOrder.LITTLE_ENDIAN);
        data.put("RIFF".getBytes("US-ASCII")).putInt(pcm.length-8).put("WAVEfmt ".getBytes("US-ASCII"));
        data.putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(192000).putShort((short)4).putShort((short)16);
        data.put("data".getBytes("US-ASCII")).putInt(pcm.length-44);
        for(int i=0;i<48000;i++){data.putShort((short)value);data.putShort((short)value);}
        Path path=root.resolve(name);Files.write(path,pcm);return path.toFile();
    }

    // 使用真实的缓存校验入口，受控地暂停第一条音轨的清单校验。
    private static final class CachedTrack extends JSONObject {
        String hash;
        long size;
        JSONArray sources;
        CountDownLatch validating, release;
        @Override public String getString(String name) {
            switch (name) {
                case "sha256": return hash;
                case "encoding": return "pcm_s16le_wav";
                case "url": return TRACK_PATH;
                default: throw new AssertionError(name);
            }
        }
        @Override public String optString(String name) { return getString(name); }
        @Override public JSONArray optJSONArray(String name) {
            yes(name.equals("sources"),name);
            return sources;
        }
        @Override public long getLong(String name) {
            yes(name.equals("bytes"),name);
            return size;
        }
        @Override public int getInt(String name) {
            if (name.equals("channels")) return 2;
            yes(name.equals("sample_rate"),name);
            if (validating != null) validating.countDown();
            if (release != null) {
                try { yes(release.await(7,TimeUnit.SECONDS),"stalled track was not released"); }
                catch (InterruptedException interrupted) { throw new AssertionError(interrupted); }
            }
            return 48000;
        }
    }

    private static CachedTrack cachedTrack(Path directory, File wave)throws Exception {
        CachedTrack track=trackSpec(wave,null);
        Path target=directory.resolve(track.hash+".wav");
        if (!Files.exists(target)) Files.copy(wave.toPath(),target);
        return track;
    }
    private static CachedTrack trackSpec(File wave,JSONArray sources)throws Exception {
        CachedTrack track=empty(CachedTrack.class);
        track.hash=MediaTransfer.hash(wave);
        track.size=wave.length();
        track.sources=sources;
        return track;
    }

    private static final class LoadingNotice extends TextView {
        String text;
        LoadingNotice(Context context){super(context);}
        @Override public CharSequence getText(){return text;}
    }
    private static final class LoadingProgress extends ProgressBar {
        LoadingProgress(Context context){super(context);}
        @Override public void setVisibility(int visibility){}
    }
    private static final class PendingLoads extends ThreadPoolExecutor {
        final List<Runnable> jobs=new ArrayList<>();
        PendingLoads(){super(2,2,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<>(2));}
        @Override public void execute(Runnable job){jobs.add(job);}
    }

    private static MainActivity loadingActivity(PendingLoads tasks,JSONObject job)throws Exception {
        MainActivity activity=empty(MainActivity.class);
        activityField("notice").set(activity,empty(LoadingNotice.class));
        activityField("roomProgress").set(activity,empty(LoadingProgress.class));
        activityField("current").set(activity,job);
        activityField("engine").set(activity,empty(AudioEngine.class));
        activityField("backingNetwork").set(activity,tasks);
        activityField("backingLoads").set(activity,new HashSet<Boolean>());
        activityField("backingStartFailures").set(activity,new int[2]);
        activityField("generation").set(activity,new AtomicInteger());
        activityField("loading").setBoolean(activity,true);
        activityField("foreground").setBoolean(activity,true);
        return activity;
    }

    private static void chooseWhileLoading(File previous)throws Exception {
        PendingLoads tasks=new PendingLoads();
        try {
            MainActivity activity=loadingActivity(tasks,manifest("test-song",null,null));
            LoadingNotice notice=(LoadingNotice)activityField("notice").get(activity);
            Set<?> pending=(Set<?>)activityField("backingLoads").get(activity);
            Method select=activityMethod("switchMusic");
            for(boolean original:new boolean[]{false,true,false,true}){
                activityField("original").setBoolean(activity,original);
                notice.text=original?"正在加载原唱…":"正在加载伴奏…";
                select.invoke(activity);
            }
            yes(tasks.jobs.size()==2&&pending.contains(false)&&pending.contains(true),
                "selection starts both distinct requests without waiting or duplicating work");
            Method accept=activityMethod("finishBackingLoad",boolean.class,JSONObject.class,File.class);
            accept.invoke(activity,false,null,previous);
            yes(activityField("loading").getBoolean(activity)&&pending.contains(true)&&!pending.contains(false),
                "completion of the previous choice leaves the new start pending");
            yes(previous.equals(activityField("currentInstrumental").get(activity)),"previous track remains available");
        } finally {tasks.shutdownNow();}
    }

    private static final class JsonFixture extends JSONObject {
        Map<String,Object> values;
        @Override public JSONObject optJSONObject(String name){return (JSONObject)values.get(name);}
        @Override public JSONArray optJSONArray(String name){return (JSONArray)values.get(name);}
        @Override public String optString(String name){return String.valueOf(values.getOrDefault(name,""));}
        @Override public long optLong(String name){return optLong(name,0);}
        @Override public long optLong(String name,long fallback){
            return values.get(name) instanceof Number?((Number)values.get(name)).longValue():fallback;
        }
        @Override public String getString(String name){return (String)values.get(name);}
        @Override public long getLong(String name){return ((Number)values.get(name)).longValue();}
        @Override public boolean has(String name){return values.containsKey(name);}
        @Override public Iterator<String> keys(){return values.keySet().iterator();}
    }
    private static JsonFixture record()throws Exception {
        JsonFixture json=empty(JsonFixture.class);
        json.values=new LinkedHashMap<>();
        return json;
    }
    private static JSONObject manifest(String id,JSONObject original,JSONObject instrumental)throws Exception {
        JsonFixture job=record(), media=record();
        job.values.put("id",id);
        job.values.put("media",media);
        media.values.put("original",original);
        media.values.put("instrumental",instrumental);
        return job;
    }
    private static final class SourceList extends JSONArray {
        JSONObject source;
        @Override public int length(){return 1;}
        @Override public JSONObject getJSONObject(int index){return source;}
    }
    private static JSONArray delivery(Long expires)throws Exception {
        JsonFixture source=record();
        source.values.put("url",TRACK_PATH);
        if(expires!=null){
            source.values.put("origin","http://10.77.0.2:9876");
            source.values.put("header","X-KTV-Media");
            source.values.put("credential","synthetic-only-00000000000000000000000");
            source.values.put("expires",expires);
        }
        SourceList routes=empty(SourceList.class);
        routes.source=source;
        return routes;
    }

    private static void refreshTrackDelivery(Path root,File original,File instrumental)throws Exception {
        Path cache=Files.createDirectory(root.resolve("refresh-via-nas"));
        PendingLoads tasks=new PendingLoads();
        try {
            KtvClient client=new KtvClient("http://127.0.0.1:19999",
                "offline-test-000000000000000000000000000",cache.toFile());
            Field coordinator=KtvClient.class.getDeclaredField("coordinator");
            coordinator.setAccessible(true);
            coordinator.setBoolean(client,true);
            CachedTrack expired=trackSpec(original,delivery(System.currentTimeMillis()/1000-10));
            CachedTrack recovered=trackSpec(original,delivery(null));
            CachedTrack backing=trackSpec(instrumental,null);
            JSONObject performance=manifest("test-song",expired,backing);
            JSONObject refreshed=manifest("test-song",recovered,backing);
            MainActivity activity=loadingActivity(tasks,performance);
            activityField("client").set(activity,client);
            activityField("original").setBoolean(activity,true);
            ((LoadingNotice)activityField("notice").get(activity)).text="正在加载原唱…";
            Method ready=activityMethod("audioReady",JSONObject.class);
            yes(!(Boolean)ready.invoke(activity,performance),"an expired worker ticket alone does not make the track playable");
            yes((Boolean)ready.invoke(activity,refreshed),"new verified NAS source is usable");
            activityMethod("switchMusic").invoke(activity);
            yes(tasks.jobs.size()==1,"initial request still pending");
            Method update=activityMethod("updateCurrentManifest",JSONObject.class);
            yes(!(Boolean)update.invoke(activity,manifest("another-song",recovered,backing)),"wrong request ID cannot replace delivery");
            yes(!(Boolean)update.invoke(activity,manifest("test-song",recovered,recovered)),"different stem pair cannot replace delivery");
            yes((Boolean)update.invoke(activity,refreshed),"accept refreshed route for the same pair");
            Method finish=activityMethod("finishBackingLoad",boolean.class,JSONObject.class,File.class);
            finish.invoke(activity,true,expired,null);
            yes(tasks.jobs.size()==2&&activityField("loading").getBoolean(activity),
                "retry with the NAS source before reporting a playback failure");
            yes(activityField("current").get(activity)==performance,"same performance remains in control");
            yes(!client.hasNewMediaRoute(recovered,trackSpec(original,delivery(null))),
                "repeated manifest with unchanged routes does not trigger a retry");
        } finally {
            tasks.shutdownNow();
            Files.delete(cache);
        }
    }

    private static void retryTheSameTrackAndStopAtTheBudget(Path root,File original,File instrumental)throws Exception {
        Path cache=Files.createDirectory(root.resolve("retry-selected"));
        PendingLoads tasks=new PendingLoads();
        try{
            KtvClient client=new KtvClient("http://127.0.0.1:19999",
                "offline-test-000000000000000000000000000",cache.toFile());
            Field coordinator=KtvClient.class.getDeclaredField("coordinator");
            coordinator.setAccessible(true);
            coordinator.setBoolean(client,true);
            CachedTrack same=trackSpec(original,delivery(null));
            CachedTrack other=trackSpec(instrumental,null);
            JSONObject song=manifest("test-song",same,other);
            MainActivity activity=loadingActivity(tasks,song);
            activityField("client").set(activity,client);
            activityField("original").setBoolean(activity,true);
            JSONArray verifiedLyrics=empty(QueueIds.class);
            activityField("startingLyrics").set(activity,verifiedLyrics);
            ((LoadingNotice)activityField("notice").get(activity)).text="正在加载原唱…";
            activityMethod("switchMusic").invoke(activity);
            Method complete=activityMethod("finishBackingLoad",boolean.class,JSONObject.class,File.class);
            Method advance=activityMethod("advanceBackingRetry",long.class);
            Field retryAt=activityField("backingRetryAt");

            complete.invoke(activity,true,same,null);
            long deadline=retryAt.getLong(activity);
            yes(deadline!=0&&tasks.jobs.size()==1,"same-source failure backs off instead of pausing the room");
            advance.invoke(activity,deadline-1);
            yes(tasks.jobs.size()==1,"no duplicate transfer before the retry window");
            advance.invoke(activity,deadline);
            yes(tasks.jobs.size()==2&&activityField("current").get(activity)==song
                &&activityField("startingLyrics").get(activity)==verifiedLyrics&&activityField("original").getBoolean(activity),
                "retry starts the same choice and leaves the selected song and verified lyrics in place");

            Method retry=activityMethod("retryBackingStart",boolean.class,JSONObject.class);
            CachedTrack stale=trackSpec(original,delivery(System.currentTimeMillis()/1000-10));
            yes(client.hasNewMediaRoute(stale,same),"fresh route changes the retry timing");
            yes((Boolean)retry.invoke(activity,true,stale)&&(Boolean)retry.invoke(activity,true,stale)
                &&!(Boolean)retry.invoke(activity,true,stale),"repeated route renewals cannot keep restart attempts alive");

            MainActivity cancelled=loadingActivity(tasks,song);
            activityField("client").set(cancelled,client);
            yes((Boolean)retry.invoke(cancelled,false,other),"new performance gets its own retry window");
            long cancelledAt=retryAt.getLong(cancelled);
            int before=tasks.jobs.size();
            activityField("foreground").setBoolean(cancelled,false);
            advance.invoke(cancelled,cancelledAt);
            yes(retryAt.getLong(cancelled)==0&&tasks.jobs.size()==before,"background playback cannot revive a delayed transfer");
        }finally{
            tasks.shutdownNow();
            Files.delete(cache);
        }
    }

    private static void independentTracks(Path root,File first,File second)throws Exception {
        Path cache=Files.createDirectory(root.resolve("tracks"));
        CachedTrack stalled=cachedTrack(cache,first), alternate=cachedTrack(cache,second);
        CachedTrack sameDigest=cachedTrack(cache,first);
        stalled.validating=new CountDownLatch(1);
        stalled.release=new CountDownLatch(1);
        sameDigest.validating=new CountDownLatch(1);
        ExecutorService tasks=Executors.newFixedThreadPool(3);
        try {
            KtvClient client=new KtvClient("http://127.0.0.1:19999",
                "offline-test-000000000000000000000000000",cache.toFile());
            Future<File> waiting=tasks.submit(()->client.download(stalled,new AtomicBoolean()));
            yes(stalled.validating.await(2,TimeUnit.SECONDS),"first track entered verification");
            Future<File> selected=tasks.submit(()->client.download(alternate,new AtomicBoolean()));
            yes(selected.get(2,TimeUnit.SECONDS).getName().equals(alternate.hash+".wav")&&!waiting.isDone(),
                "ready selected track must finish while another digest is stalled");

            AtomicBoolean cancelled=new AtomicBoolean();
            CountDownLatch attempted=new CountDownLatch(1);
            Future<File> duplicate=tasks.submit(()->{
                attempted.countDown();
                return client.download(sameDigest,cancelled);
            });
            yes(attempted.await(2,TimeUnit.SECONDS),"same digest requested");
            yes(!sameDigest.validating.await(100,TimeUnit.MILLISECONDS),"same digest has one cache owner");
            cancelled.set(true);
            try {
                duplicate.get(2,TimeUnit.SECONDS);
                throw new AssertionError("cancelled duplicate completed");
            } catch (ExecutionException e) {
                yes(e.getCause() instanceof IOException&&"cancelled".equals(e.getCause().getMessage()),
                    "waiting same-digest request exits on cancellation");
            }
            stalled.release.countDown();
            yes(waiting.get(2,TimeUnit.SECONDS).getName().equals(stalled.hash+".wav"),"first content preserved");
            yes(client.download(sameDigest,new AtomicBoolean()).equals(cache.resolve(stalled.hash+".wav").toFile()),
                "same digest remains reusable");
        } finally {
            stalled.release.countDown();
            tasks.shutdownNow();
            yes(tasks.awaitTermination(3,TimeUnit.SECONDS),"all fixtures released");
            Files.delete(cache.resolve(stalled.hash+".wav"));
            Files.delete(cache.resolve(alternate.hash+".wav"));
            Files.delete(cache);
        }
    }

    private static void serviceClockKeepsRemotePlaybackAvailable(Path root,File original)throws Exception {
        Path cache=Files.createDirectory(root.resolve("service-clock"));
        long serviceTime=1_790_000_000L;
        AtomicLong elapsed=new AtomicLong(10000);
        AtomicLong localWall=new AtomicLong((serviceTime-3600)*1000);
        KtvServerClock clock=new KtvServerClock(elapsed::get,localWall::get);
        KtvClient client=new KtvClient("http://127.0.0.1:19999",
            "offline-test-000000000000000000000000000",cache.toFile(),clock);
        Field coordinator=KtvClient.class.getDeclaredField("coordinator");
        coordinator.setAccessible(true);
        coordinator.setBoolean(client,true);
        CachedTrack ready=trackSpec(original,delivery(serviceTime+30));
        CachedTrack expired=trackSpec(original,delivery(serviceTime));
        try {
            yes(!client.hasMediaRoute(ready)&&!client.playbackCommandFresh(serviceTime-2),
                "without a server sample a slow device clock rejects a valid ticket and command");
            clock.observe(serviceTime*1000,9900,elapsed.get());
            for(long offset:new long[]{-3600,3600}){
                localWall.set((serviceTime+offset)*1000);
                yes(client.hasMediaRoute(ready)&&!client.hasMediaRoute(expired),
                    "the same valid track is available on a slow or fast television clock");
                yes(client.playbackCommandFresh(serviceTime-2)&&!client.playbackCommandFresh(serviceTime+1),
                    "accept a fresh command but reject a future one on the service clock");
            }
            elapsed.addAndGet(10000);
            clock.observe((serviceTime-90)*1000,9800,elapsed.get());
            clock.observe(0,elapsed.get(),elapsed.get());
            yes(client.serverSeconds()==serviceTime+10,"late or missing date cannot roll back an established clock");
            elapsed.addAndGet(10000);
            yes(!client.playbackCommandFresh(serviceTime),"a deferred play intent expires at twenty seconds");
            yes(client.hasMediaRoute(ready),"expiring a control does not revoke a still valid track");
            elapsed.addAndGet(10000);
            yes(!client.hasMediaRoute(ready),"the worker route expires on monotonic time");
        } finally {
            Files.delete(cache);
        }
    }

    private static final class QueueIds extends JSONArray {
        String[] ids;
        @Override public int length(){return ids.length;}
        @Override public String optString(int index){return ids[index];}
    }
    private static QueueIds queueIds(String... ids)throws Exception {
        QueueIds result=empty(QueueIds.class);
        result.ids=ids;
        return result;
    }
    private static void withdrawnPlayRequestIsNotReplayed(Path root)throws Exception {
        Path cache=Files.createDirectory(root.resolve("withdrawn-play"));
        try {
            MainActivity activity=empty(MainActivity.class);
            activityField("client").set(activity,new KtvClient("http://127.0.0.1:19999",
                "offline-test-000000000000000000000000000",cache.toFile()));
            activityField("foreground").setBoolean(activity,true);
            activityField("autoRoom").setBoolean(activity,true);
            activityField("queueRevision").setLong(activity,1);
            activityField("roomOrder").set(activity,queueIds("current","requested","history"));
            activityField("played").set(activity,new HashSet<>(Collections.singleton("history")));
            JSONObject current=manifest("current",null,null);
            activityField("current").set(activity,current);
            activityField("currentId").set(activity,"current");
            Field pending=activityField("pendingPlay");
            JsonFixture snapshot=record(),command=record();
            snapshot.values.put("queue_revision",1L);
            command.values.put("song","requested");
            command.values.put("created",System.currentTimeMillis()/1000);
            Method apply=activityMethod("applyQueue",JSONObject.class);
            snapshot.values.put("playable",queueIds("requested"));
            pending.set(activity,command);
            apply.invoke(activity,snapshot);
            yes(pending.get(activity)==command,"a verified request can remain pending for local media");
            snapshot.values.put("playable",queueIds());
            apply.invoke(activity,snapshot);
            yes(pending.get(activity)==null,"losing its route cancels even an already pending request");
            pending.set(activity,command);
            activityMethod("runPendingPlay").invoke(activity);
            yes(pending.get(activity)==null&&activityField("current").get(activity)==current,
                "a newly delivered stale request cannot interrupt the current song");
            Method allowed=activityMethod("remoteTargetEligible",String.class);
            yes((Boolean)allowed.invoke(activity,"current")&&(Boolean)allowed.invoke(activity,"history"),
                "current and explicit history replay retain their media validation path");
            snapshot.values.put("playable",queueIds("requested"));
            apply.invoke(activity,snapshot);
            yes(pending.get(activity)==null,"recovery does not revive the cancelled request");
        } finally {Files.delete(cache);}
    }

    private static void authorizeAndRetirePlaybackStart(Path root)throws Exception {
        PendingLoads starts=new PendingLoads(),releases=new PendingLoads(),loads=new PendingLoads();
        CountDownLatch oldRendererCanExit=new CountDownLatch(1);
        Thread oldRenderer=new Thread(()->{
            try{oldRendererCanExit.await();}catch(InterruptedException interrupted){Thread.currentThread().interrupt();}
        });
        try {
            MainActivity activity=loadingActivity(loads,manifest("song",null,null));
            activityField("playbackStarts").set(activity,starts);
            activityField("playbackReleases").set(activity,new PlaybackReleaseQueue(releases,()->10000L,
                ignored->{},(ignored,delay)->{},ignored->{}));
            KtvClient owner=new KtvClient("http://127.0.0.1:19999","offline-test-000000000000000000000000000",root.toFile());
            activityField("client").set(activity,owner);
            activityField("protocolReady").setBoolean(activity,true);
            activityField("autoRoom").setBoolean(activity,true);
            activityField("queueClaims").setBoolean(activity,true);
            activityField("roomId").set(activity,"room");
            activityField("currentId").set(activity,"song");
            ((LoadingNotice)activityField("notice").get(activity)).text="正在确认点唱…";
            Method allow=activityMethod("authorizePlaybackStart",long.class);
            Field pending=activityField("playbackStart");
            yes(!(Boolean)allow.invoke(activity,10000L),"no output before a NAS grant");
            PlaybackStartClaim claim=(PlaybackStartClaim)pending.get(activity);
            yes(starts.jobs.size()==1&&claim.markSending(),"one network request for the selected job");
            yes(!(Boolean)allow.invoke(activity,10001L)&&starts.jobs.size()==1,"a pending claim does not duplicate requests");
            claim.retry(10100L);
            yes(!(Boolean)allow.invoke(activity,10449L)&&starts.jobs.size()==1,"transport loss waits before retrying");
            yes(!(Boolean)allow.invoke(activity,10450L)&&pending.get(activity)==claim&&starts.jobs.size()==2,
                "an ambiguous receipt retries with the original request id");
            claim.accepted(10450L,30000);
            yes((Boolean)allow.invoke(activity,10490L)&&!claim.canStart(40450L),"only the still valid grant can start audio");
            activityMethod("cancelPlaybackStart").invoke(activity);
            yes(pending.get(activity)==null&&starts.jobs.size()==2&&releases.jobs.size()==1&&!claim.markSending(),
                "cancellation schedules the old grant in a dedicated release lane");
            activityMethod("finishPlaybackStart",PlaybackStartClaim.class,long.class,int.class,Exception.class)
                .invoke(activity,claim,10450L,30000,null);
            yes(pending.get(activity)==null&&loads.jobs.isEmpty(),"a late successful receipt cannot start cancelled audio");

            Field sounding=activityField("activePlaybackClaim");
            PlaybackStartClaim active=new PlaybackStartClaim(owner,"room","song",0,0);
            active.markSending();sounding.set(activity,active);
            AudioEngine engine=(AudioEngine)activityField("engine").get(activity);
            Method retire=activityMethod("releaseStoppedPlayback");
            engine.running=true;retire.invoke(activity);
            yes(sounding.get(activity)==active&&releases.jobs.size()==1,"a running renderer keeps its NAS reservation");

            Field render=AudioEngine.class.getDeclaredField("render");render.setAccessible(true);render.set(engine,oldRenderer);
            oldRenderer.start();engine.running=false;
            ((LoadingNotice)activityField("notice").get(activity)).text="正在切换歌曲…";
            activityMethod("startLoadedBacking",File.class).invoke(activity,(File)null);
            yes(activityField("loading").getBoolean(activity)&&activityField("waitingForRenderer").getBoolean(activity)
                &&sounding.get(activity)==active&&starts.jobs.size()==2,"verified audio waits while the old renderer is still exiting");
            oldRendererCanExit.countDown();oldRenderer.join(1000);
            yes((Boolean)activityMethod("rendererReadyForStart").invoke(activity)&&sounding.get(activity)==null
                &&!active.markSending(),"a released renderer retires its own reservation before a new start");
            ((LoadingNotice)activityField("notice").get(activity)).text="正在确认点唱…";
            yes(!(Boolean)allow.invoke(activity,11000L)&&starts.jobs.size()==3&&releases.jobs.size()==1,
                "an unresolved release cannot delay the next authorization request");
            activityField("controlEpoch").setLong(activity,3);
            activityMethod("televisionOwnershipLost",KtvClient.class,long.class).invoke(activity,owner,2L);
            yes(activityField("protocolReady").getBoolean(activity)&&pending.get(activity)!=null,
                "an old control epoch cannot apply a takeover refusal to the new connection");
        } finally {
            oldRendererCanExit.countDown();oldRenderer.join(1000);
            starts.shutdownNow();releases.shutdownNow();loads.shutdownNow();
        }
    }

    public static void main(String[] args)throws Exception {
        Path root=Files.createTempDirectory("ktv-backing-startup-");File original=null,alternate=null;
        try {
            original=wave(root,"original.wav",1000);alternate=wave(root,"alternate.wav",5000);
            AudioEngine engine=empty(AudioEngine.class);short[] output=new short[480];
            initial(engine,original);
            engine.switchBacking(alternate);
            try(SwitchingWavSource source=new SwitchingWavSource(original)){
                source.seek(480);install(engine,source,original);yes(source.read(output,240)==240,"full initial block");
                yes(source.switches==1&&source.position==720,"pre-publication intent retained without rewind");
                yes(output[0]>1000&&output[0]<5000&&output[478]==5000,"queued stem reaches the render stream");
            }
            initial(engine,original);engine.switchBacking(alternate);engine.switchBacking(original);
            try(SwitchingWavSource source=new SwitchingWavSource(original)){
                install(engine,source,original);source.read(output,240);
                yes(source.switches==0&&output[478]==1000,"latest pre-publication choice wins");
            }
            for(String name:new String[]{"track","record","render","capture"}){
                Field field=AudioEngine.class.getDeclaredField(name);field.setAccessible(true);yes(field.get(engine)==null,"no audio resource: "+name);
            }
            yes(!engine.running&&engine.capturedFrames==0,"no microphone or rendering started");
            Field start=AudioEngine.class.getDeclaredField("backingStartFrame");start.setAccessible(true);start.setLong(engine,48000);
            engine.running=true;engine.playheadFrames=48000;
            yes(!engine.backingPresented(),"startup at a resume offset does not present the backing");
            engine.playheadFrames=48001;
            yes(engine.backingPresented(),"playback head moved beyond the resume offset");
            engine.running=false;
            yes(!engine.backingPresented(),"a stopped backing does not report playback");
            chooseWhileLoading(original);
            independentTracks(root,original,alternate);
            refreshTrackDelivery(root,original,alternate);
            retryTheSameTrackAndStopAtTheBudget(root,original,alternate);
            serviceClockKeepsRemotePlaybackAvailable(root,original);
            withdrawnPlayRequestIsNotReplayed(root);
            authorizeAndRetirePlaybackStart(root);
            System.out.println("BackingStartupIntentTest passed: startup, track retries, withdrawn requests, NAS grant, late response and renderer-held reservation; no Android audio calls");
        } finally {
            if(original!=null)Files.deleteIfExists(original.toPath());if(alternate!=null)Files.deleteIfExists(alternate.toPath());Files.delete(root);
        }
    }
}
