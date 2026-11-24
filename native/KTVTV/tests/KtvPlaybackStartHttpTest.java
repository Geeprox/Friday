package local.friday.ktv;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayDeque;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONObject;

/** 使用本机 HTTP 回归电视的新请求、回执与拒绝，不连接实际 NAS。 */
public final class KtvPlaybackStartHttpTest {
    private static final String ROOM="00000000-0000-0000-0000-000000000001";
    private static final String SONG="00000000-0000-0000-0000-000000000002";
    private static final String TOKEN="00000000-0000-0000-0000-000000000003";

    private static void check(boolean value,String description){if(!value)throw new AssertionError(description);}
    private static JSONObject read(InputStream input)throws Exception {
        try(InputStream in=input;ByteArrayOutputStream output=new ByteArrayOutputStream()){
            byte[] buffer=new byte[1024];int count;
            while((count=in.read(buffer))!=-1)output.write(buffer,0,count);
            return new JSONObject(output.toString("UTF-8"));
        }
    }
    private static void respond(HttpExchange request,int status,JSONObject value)throws IOException {
        byte[] bytes=value.toString().getBytes(StandardCharsets.UTF_8);
        request.sendResponseHeaders(status,bytes.length);
        request.getResponseBody().write(bytes);
    }
    private static final class ReleaseWorker extends ThreadPoolExecutor {
        final ArrayDeque<Runnable> jobs=new ArrayDeque<>();
        ReleaseWorker(){super(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<>(1));}
        @Override public void execute(Runnable task){jobs.add(task);}
    }
    private static final class ManualClock {
        long now=1000,due;
        Runnable wake;
        void later(Runnable task,long delay){check(wake==null,"one combined release wake");wake=task;due=now+delay;}
        void remove(Runnable task){if(wake==task)wake=null;}
        void advanceToWake(){Runnable task=wake;wake=null;now=due;task.run();}
    }
    private static void retryReleaseAcrossConnections(HttpServer nas,KtvClient tv,KtvClient reconnected,Path cache)throws Exception {
        KtvClient otherEndpoint=new KtvClient(tv.origin+"/other","offline-test-000000000000000000000000000",cache.toFile());
        otherEndpoint.instance(ROOM);
        PlaybackStartClaim first=new PlaybackStartClaim(tv,ROOM,SONG,1,1);
        PlaybackStartClaim second=new PlaybackStartClaim(tv,ROOM,SONG,1,2);
        JSONObject busy=new JSONObject().put("code","recovering");
        JSONObject roomChanged=new JSONObject().put("code","room_changed");
        JSONObject televisionChanged=new JSONObject().put("code","tv_instance_changed");
        AtomicInteger retries=new AtomicInteger();
        nas.removeContext("/v1/tv/playback-start");
        nas.createContext("/v1/tv/playback-start",request->{
            try {
                check(ROOM.equals(request.getRequestHeaders().getFirst("X-KTV-TV-Instance")),"release reaches the original TV instance");
                JSONObject body=read(request.getRequestBody());
                int n=retries.getAndIncrement();
                check(ROOM.equals(body.optString("room_id"))&&SONG.equals(body.optString("song"))
                    &&Boolean.TRUE.equals(body.opt("release")),"cleanup keeps the original room and song");
                check((n<2?first:second).request.equals(body.optString("request_id")),"cleanup retries use the same request id");
                if(n%2==0)respond(request,503,busy);
                else respond(request,409,n==1?roomChanged:televisionChanged);
            }catch(Exception error){throw new IOException(error);}
            finally {request.close();}
        });
        nas.createContext(KtvClient.PLAYBACK_REPORT,request->{
            try {respond(request,409,televisionChanged);}
            finally {request.close();}
        });
        try {
            tv.controlJson(KtvClient.PLAYBACK_REPORT,new JSONObject());
            throw new AssertionError("superseded television cannot continue reporting playback");
        }catch(KtvClient.HttpFailure refusal){check(refusal.televisionChanged(),"an explicit takeover is distinguishable from a failure");}

        ReleaseWorker worker=new ReleaseWorker();
        ManualClock time=new ManualClock();
        PlaybackReleaseQueue queue=new PlaybackReleaseQueue(worker,()->time.now,Runnable::run,time::later,time::remove);
        try {
            queue.connectionChanged(tv);
            first.markSending();queue.enqueue(first,null);queue.enqueue(first,null);
            check(worker.jobs.size()==1,"the same claim has one release work item");
            queue.connectionChanged(null);worker.jobs.remove().run();
            check(time.wake==null&&worker.jobs.isEmpty(),"pending release waits through the gap between connections");
            time.now+=800;queue.connectionChanged(otherEndpoint);
            check(time.wake==null&&worker.jobs.isEmpty(),"another NAS cannot inherit release work");
            queue.connectionChanged(reconnected);
            check(worker.jobs.size()==1,"the original NAS picks up the pending release after reconnect");
            worker.jobs.remove().run();
            check(time.wake==null&&worker.jobs.isEmpty(),"a changed room settles the pending cleanup");

            second.markSending();queue.enqueue(second,null);worker.jobs.remove().run();
            check(time.wake!=null&&time.due==time.now+500,"one monotonic retry is scheduled after a transport failure");
            queue.connectionChanged(reconnected);queue.connectionChanged(reconnected);
            check(time.wake!=null&&time.due==time.now+500,"reconnect notifications do not multiply retry wakes");
            time.advanceToWake();worker.jobs.remove().run();
            check(time.wake==null&&worker.jobs.isEmpty()&&retries.get()==4,"a changed TV instance settles the second cleanup");
        }finally {queue.close();worker.shutdownNow();}
    }
    public static void main(String[] args)throws Exception {
        Path cache=Files.createTempDirectory(Paths.get(args[0]),"playback-start-");
        HttpServer nas=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        try {
            JSONObject invalid=new JSONObject().put("accepted",true).put("lease_ms",31000);
            JSONObject grant=new JSONObject().put("accepted",true).put("lease_ms",29000);
            JSONObject release=new JSONObject().put("accepted",true);
            JSONObject removed=new JSONObject().put("code","song_unavailable");
            AtomicInteger step=new AtomicInteger();
            nas.createContext("/v1/tv/playback-start",request->{
                try {
                    check("POST".equals(request.getRequestMethod()),"starts require POST");
                    check(ROOM.equals(request.getRequestHeaders().getFirst("X-KTV-TV-Instance")),"request uses the same TV identity");
                    JSONObject body=read(request.getRequestBody());
                    int n=step.getAndIncrement();
                    check(ROOM.equals(body.optString("room_id"))&&SONG.equals(body.optString("song"))
                        &&TOKEN.equals(body.optString("request_id")),"room, song and retry key preserved");
                    check(n==2?Boolean.TRUE.equals(body.opt("release")):body.opt("release")==null,"only release carries the flag");
                    JSONObject response=n==0?invalid:n==1?grant:n==2?release:removed;
                    respond(request,n==3?409:200,response);
                }catch(Exception error){throw new IOException(error);}
                finally {request.close();}
            });
            nas.start();
            String endpoint="http://127.0.0.1:"+nas.getAddress().getPort();
            KtvClient tv=new KtvClient(endpoint,
                "offline-test-000000000000000000000000000",cache.toFile());
            KtvClient reconnected=new KtvClient(endpoint,"offline-reconnect-00000000000000000000000",cache.toFile());
            tv.instance(ROOM);
            reconnected.instance(SONG);
            check(!tv.sameTelevisionEndpoint(reconnected),"a different television instance cannot inherit the release");
            reconnected.instance(ROOM);
            check(tv.sameTelevisionEndpoint(reconnected),"reconnect to the same television endpoint can use its current credentials");
            try {tv.claimPlaybackStart(ROOM,SONG,TOKEN);throw new AssertionError("an invalid lease is not a grant");}
            catch(IOException error){check("invalid_playback_start_receipt".equals(error.getMessage()),"malformed grant refused");}
            check(tv.claimPlaybackStart(ROOM,SONG,TOKEN)==29000,"retry reads a bounded lease");
            reconnected.releasePlaybackStart(ROOM,SONG,TOKEN);
            try {tv.claimPlaybackStart(ROOM,SONG,TOKEN);throw new AssertionError("withdrawn song must not get a grant");}
            catch(KtvClient.HttpFailure error){check(error.status==409&&"song_unavailable".equals(error.getMessage()),"withdrawal has a distinct refusal");}
            check(step.get()==4,"all request shapes exercised");
            retryReleaseAcrossConnections(nas,tv,reconnected,cache);
            System.out.println("KtvPlaybackStartHttpTest passed: claim and withdraw, same-NAS release recovery, coalesced retries and takeover refusal");
        }finally {nas.stop(0);Files.delete(cache);}
    }
}
