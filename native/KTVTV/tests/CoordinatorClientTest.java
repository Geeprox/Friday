package local.friday.ktv;

import java.io.*;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** No sockets, Android audio, account data, device or service is created. */
public final class CoordinatorClientTest {
    private static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    private interface Checked {void run()throws Exception;}
    private static void refused(Checked action)throws Exception{try{action.run();throw new AssertionError("expected refusal");}catch(IOException expected){}}
    public static void main(String[] args)throws Exception{
        KtvEndpoint nas=KtvEndpoint.parse("https://nas.example.invalid/ktv/");
        check(nas.base.equals("https://nas.example.invalid/ktv"),"HTTPS endpoint prefix");
        check(nas.api("/v1/party").equals("https://nas.example.invalid/ktv/v1/party"),"prefix retained");
        check(KtvEndpoint.parse("http://127.0.0.1:9876/ktv").base.endsWith("/ktv"),"private LAN prefix");
        for(String value:new String[]{"http://nas.example.invalid:9876","http://198.51.100.2:9876","http://127.0.0.1:80","http://127.00.0.1:9876","https://user@example.invalid/ktv","https://nas.example.invalid/../admin","https://nas.example.invalid/ktv?q=1","https://nas.example.invalid/%2fadmin","https://nas.example.invalid/ktv#secret"})refused(()->KtvEndpoint.parse(value));
        for(String value:new String[]{"//other.example.invalid/v1/party","/v1/../source","/v1/jobs/%2e%2e","/v1/jobs/a#x","/v1/jobs/a\\b"})refused(()->nas.api(value));
        check(nas.api("/v1/party/events?after=1&wait=20").contains("?after=1"),"bounded event path is representable");
        String path="/v1/jobs/00000000-0000-0000-0000-000000000001/media/original",ticket="1000000900."+String.join("",Collections.nCopies(43,"a"));
        MediaRoute cache=MediaRoute.local(nas,path,"original"),worker=MediaRoute.worker("http://10.77.0.2:9876",path,"X-KTV-Media",ticket,1000000900,1000000000,"original");
        check(cache.coordinator&&cache.credential.isEmpty(),"cache carries only configured authority");
        check(!worker.coordinator&&worker.header.equals("X-KTV-Media")&&worker.credential.equals(ticket),"worker media scope");
        Map<String,String> directHeaders=worker.headers("synthetic-control-secret","instance",true,true);check(directHeaders.size()==1&&ticket.equals(directHeaders.get("X-KTV-Media")),"production authorization policy never forwards control secret or TV identity to a worker");
        Map<String,String> nasHeaders=cache.headers("synthetic-tv-secret","instance",true,true);check(nasHeaders.size()==2&&!nasHeaders.containsKey("X-KTV-Key")&&nasHeaders.containsKey("X-KTV-TV-Key"),"NAS and legacy control authority stay separate");
        refused(()->MediaRoute.worker("https://other.example.invalid",path,"X-KTV-Media",ticket,1000000900,1000000000,"original"));
        refused(()->MediaRoute.worker("http://127.0.0.1:9876",path,"X-KTV-Media",ticket,1000000900,1000000000,"original"));
        refused(()->MediaRoute.worker("http://10.77.0.2:9876","/v1/source/settings","X-KTV-Media",ticket,1000000900,1000000000,"original"));
        refused(()->MediaRoute.worker("http://10.77.0.2:9876",path,"X-KTV-Key",ticket,1000000900,1000000000,"original"));
        refused(()->MediaRoute.worker("http://10.77.0.2:9876",path,"X-KTV-Media",ticket,1000000000,1000000000,"original"));
        refused(()->MediaRoute.worker("http://10.77.0.2:9876",path,"X-KTV-Media",ticket,1000001000,1000000000,"original"));
        refused(()->MediaRoute.local(nas,path,"instrumental"));

        CoordinatorSession session=new CoordinatorSession();check(session.claimOpen(true,true,0).isEmpty(),"poll cannot authorize an unopened session");session.requestOpen();
        check(session.claimOpen(false,true,0).isEmpty(),"background cannot open");String request=session.claimOpen(true,true,0);check(!request.isEmpty(),"explicit app start");
        check(session.claimOpen(true,true,10).isEmpty(),"backoff");check(session.claimOpen(true,true,1000).equals(request),"retry same idempotency key");check(session.claimOpen(true,true,4000).equals(request),"third attempt same key");check(session.claimOpen(true,true,50000).isEmpty(),"finite retry bound");
        session.acknowledged(request);check(!session.pending(),"ACK consumes authorization");check(session.claimOpen(true,true,60000).isEmpty(),"poll cannot reopen after ACK");session.requestOpen();session.closed();check(session.claimOpen(true,true,70000).isEmpty(),"closed session cannot reopen");

        Map<String,Boolean> gates=new HashMap<>();for(String gate:CoordinatorStatus.GATES)gates.put(gate,true);CoordinatorStatus status=new CoordinatorStatus();
        status.observe(true,true,gates);check(status.complete(),"all complete gates");check(!status.expire(6000,true)&&status.complete(),"fresh status");check(status.expire(6001,true)&&!status.complete(),"an unanswered poll cannot leave complete readiness stale");gates.put("worker_model",false);status.observe(true,true,gates);check(!status.complete()&&status.text().contains("伴奏处理"),"HTTP ready cannot override failed model dependency");gates.put("worker_model",true);gates.remove("qr");status.observe(true,true,gates);check(!status.complete(),"missing gate fails closed");status.disconnected(true);check(!status.complete()&&status.text().contains("本地播放继续"),"disconnect is visibly degraded");

        PlaybackTimeline timeline=new PlaybackTimeline("","","");check(timeline.start("song","edition",true),"first performance");String epoch=timeline.epoch();check(!timeline.start("song","edition",false)&&epoch.equals(timeline.epoch()),"pause/resume and route recovery retain timeline");check(timeline.start("song","edition",true)&&!epoch.equals(timeline.epoch()),"same-song replay even at time zero changes epoch");epoch=timeline.epoch();check(timeline.start("song","new-edition",false)&&!epoch.equals(timeline.epoch()),"changed edition invalidates calibration");PlaybackTimeline restored=new PlaybackTimeline(timeline.epoch(),timeline.song(),timeline.media());check(!restored.start("song","new-edition",false)&&restored.epoch().equals(timeline.epoch()),"persistent timeline proof");
        check(MediaTimeline.valid(48000,1000,192044,192044,1,1),"exact PCM timeline");check(MediaTimeline.valid(48000,1000,192044,192048,1,48001/48000.0),"bounded equivalent tail");check(!MediaTimeline.valid(48000,1000,192044,250000,1,1),"mismatched stem timeline");check(!MediaTimeline.valid(48000,1000,192044,192044,Double.NaN,1),"nonfinite duration");
        Set<String> seen=new HashSet<>();String privateKey=String.join("",Collections.nCopies(43,"x")),profile=ProvisionedConnection.fingerprint("nas",nas.base,privateKey);
        check(profile.equals("2f31dbf920e06f5d55687db99eab9be275809e7ce33c52b34b7bb11d5cd39b84"),"Python/Android provisioning fingerprint fixture");
        check(ProvisionedConnection.shouldApply("nas",profile,nas.base,privateKey,seen),"new explicit NAS profile may migrate");seen.add(profile);
        check(!ProvisionedConnection.shouldApply("nas",profile,nas.base,privateKey,seen),"same private APK cannot undo a later manual setting");
        check(!ProvisionedConnection.shouldApply("portable","","","",seen),"portable update preserves household prefs");
        check(!ProvisionedConnection.shouldApply("mac",profile,nas.base,privateKey,new HashSet<>()),"legacy defaults cannot silently migrate an existing setting");
        String legacyOrigin="http://192.168.1.2:8769",legacyProfile=ProvisionedConnection.fingerprint("mac",legacyOrigin,privateKey);
        check(ProvisionedConnection.shouldSeedLegacy("mac",legacyProfile,legacyOrigin,privateKey,false,false),"persist legacy compiled defaults before a later portable update");
        check(!ProvisionedConnection.shouldSeedLegacy("mac",legacyProfile,legacyOrigin,privateKey,true,true),"preserve a complete manual or migrated connection");
        check(!ProvisionedConnection.shouldSeedLegacy("mac",legacyProfile,legacyOrigin,privateKey,true,false),"do not fill a partially configured origin with an unrelated key");
        check(!ProvisionedConnection.shouldSeedLegacy("mac",legacyProfile,legacyOrigin,privateKey,false,true),"do not replace a separately configured key");
        check(!ProvisionedConnection.shouldSeedLegacy("portable",legacyProfile,legacyOrigin,privateKey,false,false),"portable builds never seed household values");
        check(!ProvisionedConnection.shouldSeedLegacy("nas",legacyProfile,legacyOrigin,privateKey,false,false),"NAS migration retains its independent explicit-once policy");
        check(!ProvisionedConnection.shouldSeedLegacy("mac",legacyProfile,legacyOrigin+"/other",privateKey,false,false),"legacy seed binds the exact canonical connection");
        check(!ProvisionedConnection.shouldSeedLegacy("mac",legacyProfile,legacyOrigin,privateKey+"x",false,false),"legacy seed binds the existing local key");
        check(!ProvisionedConnection.shouldApply("nas",profile,nas.base+"/other",privateKey,new HashSet<>()),"fingerprint binds origin and secret");
        String newer=ProvisionedConnection.fingerprint("nas",nas.base+"/new",privateKey);check(ProvisionedConnection.shouldApply("nas",newer,nas.base+"/new",privateKey,seen),"new explicit profile is independent");seen.add(newer);
        check(!ProvisionedConnection.shouldApply("nas",profile,nas.base,privateKey,seen),"rolling back an already-seen older profile preserves manual choices");

        Path temporary=Files.createTempDirectory("ktv-coordinator-client-");
        try{
            File root=temporary.toFile();MediaCache mediaCache=new MediaCache(root,1024*1024);byte[] data=new byte[80];for(int i=0;i<data.length;i++)data[i]=(byte)i;String sha=MediaTransfer.hex(MessageDigest.getInstance("SHA-256").digest(data));File target=new File(root,sha+".wav");
            List<MediaRoute> opened=new ArrayList<>();int[] verified={0};
            File ready=MediaTransfer.download(target,data.length,sha,new MediaRoute[]{cache,worker},route->{opened.add(route);if(route.coordinator)throw new IOException("cache_pending");return new ByteArrayInputStream(data);},file->{verified[0]++;check(file.length()==data.length,"whole bytes before publish");},new AtomicBoolean(),mediaCache);
            check(ready.equals(target)&&target.isFile()&&opened.equals(Arrays.asList(cache,worker)),"NAS cache failure falls back directly without archive wait");check(verified[0]==1,"format verified");check(!new File(root,sha+".part").exists(),"partial cleaned");
            opened.clear();MediaTransfer.download(target,data.length,sha,new MediaRoute[]{worker},route->{opened.add(route);throw new AssertionError("cache should be verified locally");},file->{verified[0]++;},new AtomicBoolean(),mediaCache);check(opened.isEmpty()&&verified[0]==2,"existing cache gets hash and format verification");
            check(MediaTransfer.existing(target,data.length,sha,file->{},new AtomicBoolean(),mediaCache).equals(target),"offline cache validation needs no current network ticket or route");
            check(target.delete(),"remove fixture");opened.clear();byte[] broken=data.clone();broken[8]++;
            MediaTransfer.download(target,data.length,sha,new MediaRoute[]{cache,worker},route->{opened.add(route);return new ByteArrayInputStream(route.coordinator?broken:data);},file->{},new AtomicBoolean(),mediaCache);check(opened.size()==2&&MediaTransfer.hash(target).equals(sha),"bad first SHA never poisons published cache");
            check(target.delete(),"remove fixture");AtomicBoolean cancelled=new AtomicBoolean();opened.clear();
            refused(()->MediaTransfer.download(target,data.length,sha,new MediaRoute[]{cache,worker},route->{opened.add(route);cancelled.set(true);return new ByteArrayInputStream(data);},file->{},cancelled,mediaCache));check(opened.size()==1&&!target.exists()&&!new File(root,sha+".part").exists(),"cancel prevents fallback and publication");
            refused(()->MediaTransfer.download(target,data.length,sha,new MediaRoute[]{worker},route->new ByteArrayInputStream(data),file->{throw new IOException("wrong_format");},new AtomicBoolean(),mediaCache));check(!target.exists(),"matching hash alone cannot bypass media format validation");
            MediaTransfer.download(target,data.length,sha,new MediaRoute[]{worker},route->{
                MediaCache reconnected=new MediaCache(root,1024*1024);
                check(Arrays.stream(root.listFiles()).anyMatch(file->file.getName().endsWith(".part")),"reconnect must not erase another active client's partial");
                MediaTransfer.download(target,data.length,sha,new MediaRoute[]{worker},second->new ByteArrayInputStream(data),file->{},new AtomicBoolean(),reconnected);
                return new ByteArrayInputStream(data);
            },file->{},new AtomicBoolean(),mediaCache);
            check(target.isFile()&&MediaTransfer.hash(target).equals(sha),"overlapping clients atomically publish the same verified edition");
            check(Arrays.stream(root.listFiles()).noneMatch(file->file.getName().endsWith(".part")),"every owned partial is cleaned");
            File orphan=new File(root,sha+"-00000000000000000000000000000001.part");Files.write(orphan.toPath(),new byte[]{1,2});new MediaCache(root,1024*1024);check(!orphan.exists(),"unowned partials from an interrupted process are reclaimed");
        }finally{try(java.util.stream.Stream<Path> files=Files.walk(temporary)){files.sorted(Comparator.reverseOrder()).forEach(p->{try{Files.deleteIfExists(p);}catch(IOException failure){throw new RuntimeException(failure);}});}}
        System.out.println("CoordinatorClientTest passed: endpoint/TLS boundaries, media-only routes, verified cache/direct fallback, cancellation, explicit start, readiness and playback epochs; no sockets/audio");
    }
}
