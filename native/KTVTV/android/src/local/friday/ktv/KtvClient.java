package local.friday.ktv;

import org.json.JSONObject;
import org.json.JSONArray;
import java.io.*;
import java.net.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;

/** Native LAN client. Authentication never appears in URLs, logs or exported diagnostics. */
public final class KtvClient {
    public final String origin;
    private final KtvEndpoint endpoint;
    private final String key;
    private volatile boolean coordinator,negotiated;
    private volatile boolean compactNegotiated;
    private String instance="";
    private final File cache;
    public final MediaCache mediaCache;
    private static final long LIMIT=345600044L;
    static final class HttpFailure extends IOException {
        final int status;
        HttpFailure(int status){super(status==401?"unauthorized":"request_failed");this.status=status;}
    }
    public KtvClient(String origin, String key, File cache) throws Exception {
        endpoint=KtvEndpoint.parse(origin);
        if(key==null||!key.matches("[A-Za-z0-9_-]{32,128}"))throw new IOException("invalid_connection");
        this.origin=endpoint.base;this.key=key;this.cache=cache;
        if (!cache.exists() && !cache.mkdirs()) throw new IOException("cache_unavailable");
        mediaCache=new MediaCache(cache);
    }
    public boolean coordinator(){return coordinator;}
    public boolean negotiated(){return negotiated;}
    public void instance(String value)throws IOException{if(value==null||!value.matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"))throw new IOException("invalid_instance");instance=value;}
    public void observeStatus(JSONObject value){
        coordinator=value.optBoolean("coordinator");negotiated=true;
        JSONArray formats=value.optJSONArray("audio_formats");
        compactNegotiated=false;
        if(formats!=null)for(int i=0;i<formats.length();i++)if("aac_lc_m4a".equals(formats.optString(i)))compactNegotiated=true;
    }
    public boolean compactNegotiated(){return compactNegotiated;}
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
    TvUpdateSpec televisionUpdate() throws Exception {
        if (!negotiated || !coordinator) return null;
        HttpURLConnection request = connection("/v1/tv/update", "GET");
        request.setConnectTimeout(2000);
        request.setReadTimeout(2500);
        long deadline = System.nanoTime() + 4_000_000_000L;
        try {
            int status = request.getResponseCode();
            if (status == 404) return null;
            if (status != 200) throw new HttpFailure(status);
            try (InputStream input = request.getInputStream(); ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[1024];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    if (System.nanoTime() > deadline || bytes.size() + count > 4096)
                        throw new IOException("invalid_update_manifest");
                    bytes.write(buffer, 0, count);
                }
                JSONObject result = new JSONObject(bytes.toString("UTF-8"));
                return TvUpdateSpec.checked(result.get("version_code"), result.get("version_name"),
                    result.get("bytes"), result.get("sha256"), result.get("url"));
            }
        } finally {
            request.disconnect();
        }
    }
    void downloadTelevisionUpdate(TvUpdateSpec release, File target, BooleanSupplier cancelled) throws Exception {
        if (!negotiated || !coordinator || cancelled.getAsBoolean()) throw new IOException("update_cancelled");
        HttpURLConnection request = connection(TvUpdateSpec.APK_PATH, "GET");
        request.setConnectTimeout(3000);
        request.setReadTimeout(5000);
        request.setRequestProperty("Accept-Encoding", "identity");
        request.setRequestProperty("If-Match", "\"" + release.sha256 + "\"");
        long deadline = System.nanoTime() + 90_000_000_000L;
        try {
            int status = request.getResponseCode();
            if (status != 200) throw new HttpFailure(status);
            long length = request.getContentLengthLong();
            if (length >= 0 && length != release.bytes) throw new IOException("invalid_update_bytes");
            try (InputStream input = request.getInputStream(); FileOutputStream output = new FileOutputStream(target)) {
                release.copyVerified(input, output, () -> cancelled.getAsBoolean() || System.nanoTime() > deadline);
                output.getFD().sync();
            }
        } finally {
            request.disconnect();
        }
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
            if (status<200 || status>=300) throw new HttpFailure(status);
            try(InputStream in=c.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream()) {
                byte[] b=new byte[8192];int count;
                while((count=in.read(b))!=-1) {if(out.size()+count>1048576)throw new IOException("response_too_large");out.write(b,0,count);}
                return new JSONObject(out.toString("UTF-8"));
            }
        } finally {c.disconnect();}
    }
    public byte[] image(String path) throws Exception {
        HttpURLConnection c=connection(path,"GET");
        c.setConnectTimeout(2000);c.setReadTimeout(2500);
        try {
            if(c.getResponseCode()!=200 || !"image/png".equals(c.getContentType()))throw new IOException("qr_unavailable");
            try(InputStream in=c.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream()){
                byte[] bytes=new byte[8192];int n;
                while((n=in.read(bytes))!=-1){if(out.size()+n>262144)throw new IOException("qr_budget_exceeded");out.write(bytes,0,n);}
                return out.toByteArray();
            }
        } finally {c.disconnect();}
    }
    private final Object wavTransfers=new Object(),videoTransfers=new Object(),backgroundTransfers=new Object();
    public File download(JSONObject media, AtomicBoolean cancel) throws Exception {
        synchronized(wavTransfers){return downloadMedia(media,cancel,false);}
    }
    public File downloadVideo(JSONObject media, AtomicBoolean cancel) throws Exception {
        synchronized(videoTransfers){return downloadMedia(media,cancel,true);}
    }
    public File downloadBackground(JSONObject media,AtomicBoolean cancel)throws Exception{
        synchronized(backgroundTransfers){return downloadBackgroundMedia(media,cancel);}
    }
    private File downloadBackgroundMedia(JSONObject media,AtomicBoolean cancel)throws Exception{
        String hash=media.getString("sha256"),path=media.getString("url"),encoding=media.getString("encoding");
        long size=media.getLong("bytes");
        if(!hash.matches("[a-f0-9]{64}")||size<33||size>12L*1024*1024
                ||!("jpeg".equals(encoding)||"png".equals(encoding))||!MediaRoute.kind(path).equals("background"))
            throw new IOException("invalid_background");
        File target=new File(cache,hash+("jpeg".equals(encoding)?".jpg":".png"));
        MediaTransfer.Verifier verify=file->BackdropImage.verify(file,encoding);
        File found=MediaTransfer.existing(target,size,hash,verify,cancel,mediaCache);if(found!=null)return found;
        return MediaTransfer.download(target,size,hash,routes(media,path,"background"),route->openMedia(route,size),verify,cancel,mediaCache);
    }
    public boolean manifestValid(JSONObject job){
        if(!coordinator||!job.optString("state").equals("ready"))return true;
        try{
            if(job.optBoolean("simulated")||!job.getString("version").matches("[a-f0-9]{64}"))return false;
            JSONObject t=job.getJSONObject("timeline"),m=job.getJSONObject("media"),a=m.getJSONObject("original"),b=m.getJSONObject("instrumental");
            if(a.optString("encoding").equals("aac_lc_m4a")||b.optString("encoding").equals("aac_lc_m4a")){
                return t.getInt("sample_rate")==48000&&compactValid(a,t.getLong("frames"),t.getLong("duration_ms"))
                    &&compactValid(b,t.getLong("frames"),t.getLong("duration_ms"))&&a.getLong("bitrate")==b.getLong("bitrate");
            }
            return t.getInt("sample_rate")==48000&&a.getInt("sample_rate")==48000&&b.getInt("sample_rate")==48000&&a.getInt("channels")==2&&b.getInt("channels")==2
                &&a.getString("sha256").matches("[a-f0-9]{64}")&&b.getString("sha256").matches("[a-f0-9]{64}")
                &&MediaTimeline.valid(t.getLong("frames"),t.getLong("duration_ms"),a.getLong("bytes"),b.getLong("bytes"),a.getDouble("duration_seconds"),b.getDouble("duration_seconds"));
        }catch(Exception invalid){return false;}
    }
    private static boolean compactValid(JSONObject spec,long frames,long duration)throws org.json.JSONException{
        return spec.getString("encoding").equals("aac_lc_m4a")&&spec.getInt("sample_rate")==48000&&spec.getInt("channels")==2
            &&spec.getString("sha256").matches("[a-f0-9]{64}")&&spec.getLong("bytes")>=48&&spec.getLong("bytes")<=134217728
            &&MediaTimeline.compact(frames,duration,spec.getLong("frames"),spec.getLong("encoder_delay"),spec.getLong("encoder_padding"),
                spec.getLong("bitrate"),spec.getDouble("duration_seconds"));
    }
    public boolean cached(JSONObject media,boolean video){
        try{
            String sha=media.getString("sha256");if(!sha.matches("[a-f0-9]{64}"))return false;
            File f=new File(cache,sha+(video?".mp4":".wav"));
            if(!video&&media.optString("encoding").equals("aac_lc_m4a"))
                return CompactAudioDecoder.cached(f,new File(cache,sha+".pcm"),media.getLong("frames"),media.getLong("encoder_delay"),media.getLong("encoder_padding"));
            return f.isFile()&&f.length()==media.getLong("bytes");
        }catch(Exception e){return false;}
    }
    public boolean cachedBackground(JSONObject media){
        String sha=media.optString("sha256"),encoding=media.optString("encoding");
        if(!sha.matches("[a-f0-9]{64}")||!("jpeg".equals(encoding)||"png".equals(encoding)))return false;
        File image=new File(cache,sha+("jpeg".equals(encoding)?".jpg":".png"));
        return image.isFile()&&image.length()==media.optLong("bytes");
    }
    private File downloadMedia(JSONObject media, AtomicBoolean cancel,boolean video) throws Exception {
        String sha=media.getString("sha256"),path=media.getString("url");long size=media.getLong("bytes");
        if (!sha.matches("[a-f0-9]{64}") || size<48 || size>(video?134217728L:LIMIT))throw new IOException("invalid_media");
        String kind=MediaRoute.kind(path);
        boolean compact=!video&&media.optString("encoding").equals("aac_lc_m4a");
        if(video){if(!kind.equals("video") || !media.getString("encoding").matches("h264_mp4_(video_only|silent_clock)"))throw new IOException("invalid_media");}
        else if(!Arrays.asList("original","instrumental").contains(kind)
                || media.getInt("sample_rate")!=48000 || media.getInt("channels")!=2
                || !(compact?compactValid(media,media.getLong("frames"),Math.round(media.getDouble("duration_seconds")*1000)):
                    media.getString("encoding").equals("pcm_s16le_wav")))throw new IOException("invalid_media");
        if(compact){
            File decoded=CompactAudioDecoder.existing(cache,sha,media.getLong("frames"),media.getLong("encoder_delay"),media.getLong("encoder_padding"),cancel,mediaCache);
            if(decoded!=null)return decoded;
        }
        File target=new File(cache,sha+(video?".mp4":compact?".m4a":".wav"));
        MediaTransfer.Verifier verify=partial->{if(!video)try(WavSource source=new WavSource(partial)){} };
        if(compact)verify=partial->{};
        // Cached songs remain usable when their old network tickets expire.
        // Validate local bytes before parsing any route or opening a socket.
        File existing=MediaTransfer.existing(target,size,sha,verify,cancel,mediaCache);
        if(existing==null){
            MediaRoute[] routes=routes(media,path,kind);
            existing=MediaTransfer.download(target,size,sha,routes,route->openMedia(route,size),verify,cancel,mediaCache);
        }
        return compact?CompactAudioDecoder.decode(existing,sha,media.getLong("frames"),media.getLong("encoder_delay"),media.getLong("encoder_padding"),cancel,mediaCache):existing;
    }
    private MediaRoute[] routes(JSONObject media,String path,String kind)throws Exception{
        JSONArray sources=media.optJSONArray("sources");
        if(sources==null)return new MediaRoute[]{MediaRoute.local(endpoint,path,kind)};
        if(!coordinator||sources.length()<1||sources.length()>3)throw new IOException("invalid_media_sources");
        List<MediaRoute> routes=new ArrayList<>();
        long now=System.currentTimeMillis()/1000;
        for(int i=0;i<sources.length();i++){
            try{routes.add(mediaRoute(sources.getJSONObject(i),now,kind));}
            catch(IOException|org.json.JSONException ignored){}
        }
        if(routes.isEmpty())throw new IOException("invalid_media_sources");
        return routes.toArray(new MediaRoute[0]);
    }
    private MediaRoute mediaRoute(JSONObject source,long now,String kind)throws IOException,org.json.JSONException{
        Iterator<String> keys=source.keys();
        while(keys.hasNext())if(!Arrays.asList("origin","url","header","credential","expires").contains(keys.next()))throw new IOException("invalid_media_sources");
        if(source.optString("origin").isEmpty()){
            if(source.has("header")||source.has("credential")||source.has("expires"))throw new IOException("invalid_media_sources");
            return MediaRoute.local(endpoint,source.getString("url"),kind);
        }
        return MediaRoute.worker(source.getString("origin"),source.getString("url"),source.getString("header"),source.getString("credential"),source.getLong("expires"),now,kind);
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
