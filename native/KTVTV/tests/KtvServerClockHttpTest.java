package local.friday.ktv;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.concurrent.atomic.AtomicLong;
import org.json.JSONObject;

/** 通过真实的本机 HTTP 响应验证电视读取 Date。JSON 使用既有的 JVM 测试信封。 */
public final class KtvServerClockHttpTest {
    public static void main(String[] args)throws Exception {
        Path cache=Files.createTempDirectory(Paths.get(args[0]),"server-clock-");
        HttpServer nas=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        try {
            byte[] room=new JSONObject().put("active",true).toString().getBytes(StandardCharsets.UTF_8);
            nas.createContext("/v1/party",request->{
                try {
                    if(!"GET".equals(request.getRequestMethod())){
                        request.sendResponseHeaders(405,-1);
                        return;
                    }
                    request.getResponseHeaders().set("Content-Type","application/json");
                    request.sendResponseHeaders(200,room.length);
                    request.getResponseBody().write(room);
                } finally {request.close();}
            });
            nas.start();
            AtomicLong elapsed=new AtomicLong(10000);
            KtvServerClock clock=new KtvServerClock(elapsed::get,()->System.currentTimeMillis()+3600000);
            KtvClient tv=new KtvClient("http://127.0.0.1:"+nas.getAddress().getPort(),
                "offline-test-000000000000000000000000000",cache.toFile(),clock);
            long before=System.currentTimeMillis()/1000;
            if(!tv.controlJson("/v1/party",null).optBoolean("active"))throw new AssertionError("room reply unavailable");
            long observed=tv.serverSeconds(), after=System.currentTimeMillis()/1000;
            if(observed<before||observed>after)throw new AssertionError("television did not use response Date");
            if(!tv.playbackCommandFresh(observed))throw new AssertionError("fresh remote command was rejected");
            elapsed.addAndGet(20000);
            if(tv.playbackCommandFresh(observed))throw new AssertionError("command survived its deadline");
            System.out.println("KtvServerClockHttpTest passed: actual HTTP Date and monotonic expiry");
        } finally {
            nas.stop(0);
            Files.delete(cache);
        }
    }
}
