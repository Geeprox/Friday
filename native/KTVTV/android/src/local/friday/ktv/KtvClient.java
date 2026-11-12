package local.friday.ktv;

import org.json.JSONObject;
import org.json.JSONArray;
import java.io.*;
import java.net.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Native LAN client. Authentication never appears in URLs, logs or exported diagnostics. */
public final class KtvClient {
    public final String origin;
    private final KtvEndpoint endpoint;
    private final String key;
    private volatile boolean coordinator,negotiated;
    private String instance="";
    private final File cache;
    public final MediaCache mediaCache;
    private static final long LIMIT=345600044L;
    public KtvClient(String origin, String key, File cache) throws Exception {
        endpoint=KtvEndpoint.parse(origin);
        if(key==null||!key.matches("[A-Za-z0-9_-]{32,128}"))throw new IOException("invalid_connection");
        this.origin=endpoint.base;this.key=key;this.cache=cache;
        if (!cache.exists() && !cache.mkdirs()) throw new IOException("cache_unavailable");
        mediaCache=new MediaCache(cache);
    }
    public boolean coordinator(){return coordinator;}
    public void instance(String value)throws IOException{if(value==null||!value.matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"))throw new IOException("invalid_instance");instance=value;}
    public void observeStatus(JSONObject value){coordinator=value.optBoolean("coordinator");negotiated=true;}
    private void authenticate(HttpURLConnection c){
        for(Map.Entry<String,String> header:MediaRoute.apiHeaders(key,instance,negotiated,coordinator).entrySet())c.setRequestProperty(header.getKey(),header.getValue());
    }
    private HttpURLConnection connection(String path, String method) throws Exception {
        HttpURLConnection c=(HttpURLConnection)new URL(endpoint.api(path)).openConnection();
        c.setInstanceFollowRedirects(false); c.setConnectTimeout(5000);c.setReadTimeout(15000);
        c.setRequestMethod(method);authenticate(c);c.setUseCaches(false);
        return c;
    }
    /** A bounded actual cache read/write probe; no audio device is opened. */
    public boolean storageReady(){
        File probe=null,published=null;
        try{
            if(!cache.isDirectory()||cache.getUsableSpace()<16L*1024*1024)return false;
            probe=File.createTempFile(".ready-",".tmp",cache);byte[] value=new byte[16];new java.security.SecureRandom().nextBytes(value);
            try(FileOutputStream out=new FileOutputStream(probe)){out.write(value);out.getFD().sync();}
            published=new File(cache,probe.getName()+".published");if(!probe.renameTo(published))return false;
            byte[] found=new byte[16];try(FileInputStream in=new FileInputStream(published)){if(in.read(found)!=16||in.read()!=-1)return false;}
            return MessageDigest.isEqual(value,found)&&published.delete();
        }catch(Exception failure){return false;}finally{if(probe!=null)probe.delete();if(published!=null)published.delete();}
    }
    public JSONObject json(String path, JSONObject body) throws Exception {
        return json(path,body,false);
    }
    public JSONObject controlJson(String path,JSONObject body)throws Exception{
        return json(path,body,true);
    }
    private JSONObject json(String path, JSONObject body,boolean immediate) throws Exception {
        HttpURLConnection c=connection(path,body==null?"GET":"POST");
        if(immediate){c.setConnectTimeout(2000);c.setReadTimeout(2500);}
        try {
            if (body!=null) {
                byte[] bytes=body.toString().getBytes("UTF-8");
                int limit=path.equals("/v1/tv/diagnostics")?24576:4096;
                if (bytes.length>limit) throw new IOException("request_too_large");
                c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json");
                c.setFixedLengthStreamingMode(bytes.length);
                try(OutputStream out=c.getOutputStream()){out.write(bytes);}
            }
            int status=c.getResponseCode();
            if (status<200 || status>=300) throw new IOException(status==401?"unauthorized":"request_failed");
            try(InputStream in=c.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream()) {
                byte[] b=new byte[8192];int count;
                while((count=in.read(b))!=-1) {if(out.size()+count>1048576)throw new IOException("response_too_large");out.write(b,0,count);}
                return new JSONObject(out.toString("UTF-8"));
            }
        } finally {c.disconnect();}
    }
    public byte[] image(String path) throws Exception {
        HttpURLConnection c=connection(path,"GET");
        try {
            if(c.getResponseCode()!=200 || !"image/png".equals(c.getContentType()))throw new IOException("qr_unavailable");
            try(InputStream in=c.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream()){
                byte[] bytes=new byte[8192];int n;
                while((n=in.read(bytes))!=-1){if(out.size()+n>262144)throw new IOException("qr_budget_exceeded");out.write(bytes,0,n);}
                return out.toByteArray();
            }
        } finally {c.disconnect();}
    }
    public File download(JSONObject media, AtomicBoolean cancel) throws Exception {
        return downloadMedia(media,cancel,false);
    }
    public File downloadVideo(JSONObject media, AtomicBoolean cancel) throws Exception {
        return downloadMedia(media,cancel,true);
    }
    public boolean manifestValid(JSONObject job){
        if(!coordinator||!job.optString("state").equals("ready"))return true;
        try{
            if(job.optBoolean("simulated")||!job.getString("version").matches("[a-f0-9]{64}"))return false;
            JSONObject t=job.getJSONObject("timeline"),m=job.getJSONObject("media"),a=m.getJSONObject("original"),b=m.getJSONObject("instrumental");
            return t.getInt("sample_rate")==48000&&a.getInt("sample_rate")==48000&&b.getInt("sample_rate")==48000&&a.getInt("channels")==2&&b.getInt("channels")==2
                &&a.getString("sha256").matches("[a-f0-9]{64}")&&b.getString("sha256").matches("[a-f0-9]{64}")
                &&MediaTimeline.valid(t.getLong("frames"),t.getLong("duration_ms"),a.getLong("bytes"),b.getLong("bytes"),a.getDouble("duration_seconds"),b.getDouble("duration_seconds"));
        }catch(Exception invalid){return false;}
    }
    public boolean cached(JSONObject media,boolean video){try{String sha=media.getString("sha256");if(!sha.matches("[a-f0-9]{64}"))return false;File f=new File(cache,sha+(video?".mp4":".wav"));return f.isFile()&&f.length()==media.getLong("bytes");}catch(Exception e){return false;}}
    private synchronized File downloadMedia(JSONObject media, AtomicBoolean cancel,boolean video) throws Exception {
        String sha=media.getString("sha256"),path=media.getString("url");long size=media.getLong("bytes");
        if (!sha.matches("[a-f0-9]{64}") || size<48 || size>(video?134217728L:LIMIT))throw new IOException("invalid_media");
        String kind=MediaRoute.kind(path);
        if(video){if(!kind.equals("video") || !media.getString("encoding").matches("h264_mp4_(video_only|silent_clock)"))throw new IOException("invalid_media");}
        else if(!Arrays.asList("original","instrumental").contains(kind)
                || media.getInt("sample_rate")!=48000 || media.getInt("channels")!=2
                || !media.getString("encoding").equals("pcm_s16le_wav"))throw new IOException("invalid_media");
        File target=new File(cache,sha+(video?".mp4":".wav"));
        MediaTransfer.Verifier verify=partial->{if(!video)try(WavSource source=new WavSource(partial)){} };
        // Cached songs remain usable when their old network tickets expire.
        // Validate local bytes before parsing any route or opening a socket.
        File existing=MediaTransfer.existing(target,size,sha,verify,cancel,mediaCache);if(existing!=null)return existing;
        MediaRoute[] routes=routes(media,path,kind);
        return MediaTransfer.download(target,size,sha,routes,route->openMedia(route,size),verify,cancel,mediaCache);
    }
    private MediaRoute[] routes(JSONObject media,String path,String kind)throws Exception{
        JSONArray sources=media.optJSONArray("sources");
        if(sources==null)return new MediaRoute[]{MediaRoute.local(endpoint,path,kind)};
        if(!coordinator||sources.length()<1||sources.length()>3)throw new IOException("invalid_media_sources");
        MediaRoute[] routes=new MediaRoute[sources.length()];long now=System.currentTimeMillis()/1000;
        for(int i=0;i<routes.length;i++){
            JSONObject source=sources.optJSONObject(i);if(source==null)throw new IOException("invalid_media_sources");
            Iterator<String> keys=source.keys();while(keys.hasNext())if(!Arrays.asList("origin","url","header","credential","expires").contains(keys.next()))throw new IOException("invalid_media_sources");
            if(source.optString("origin").isEmpty()){
                if(source.has("header")||source.has("credential")||source.has("expires"))throw new IOException("invalid_media_sources");
                routes[i]=MediaRoute.local(endpoint,source.getString("url"),kind);
            }else routes[i]=MediaRoute.worker(source.getString("origin"),source.getString("url"),source.getString("header"),source.getString("credential"),source.getLong("expires"),now,kind);
        }return routes;
    }
    private InputStream openMedia(MediaRoute route,long size)throws Exception{
        final HttpURLConnection c=(HttpURLConnection)new URL(route.url).openConnection();
        c.setInstanceFollowRedirects(false);c.setConnectTimeout(3000);c.setReadTimeout(5000);c.setRequestMethod("GET");c.setUseCaches(false);
        // The Mac receives only the scoped media ticket. A coordinator key can
        // never be forwarded to an origin named by a media description.
        for(Map.Entry<String,String> header:route.headers(key,instance,negotiated,coordinator).entrySet())c.setRequestProperty(header.getKey(),header.getValue());
        try{
            if(c.getResponseCode()!=200||(c.getContentLengthLong()>=0&&c.getContentLengthLong()!=size))throw new IOException("media_download_failed");
            return new FilterInputStream(c.getInputStream()){@Override public void close()throws IOException{try{super.close();}finally{c.disconnect();}}};
        }catch(Exception failure){c.disconnect();throw failure;}
    }
}
