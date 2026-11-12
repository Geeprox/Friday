package local.friday.ktv;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;

/** A coordinator-provided route has media authority only, never control authority. */
public final class MediaRoute {
    private static final String UUID="[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}";
    public final String url,header,credential;
    public final boolean coordinator;
    private MediaRoute(String url,String header,String credential,boolean coordinator){this.url=url;this.header=header;this.credential=credential;this.coordinator=coordinator;}
    public static String kind(String path)throws IOException{
        if(path==null||!path.matches("/v1/jobs/"+UUID+"/media/(original|instrumental|video|background)"))throw new IOException("invalid_media_route");
        return path.substring(path.lastIndexOf('/')+1);
    }
    public static MediaRoute local(KtvEndpoint endpoint,String path,String kind)throws IOException{
        if(!kind(path).equals(kind))throw new IOException("invalid_media_route");
        return new MediaRoute(endpoint.api(path),"","",true);
    }
    public static MediaRoute worker(String origin,String path,String header,String credential,long expires,long now,String kind)throws IOException{
        KtvEndpoint endpoint=KtvEndpoint.parse(origin);
        if(!origin.equals(endpoint.base)||!origin.matches("http://[0-9.]+:[0-9]+")||origin.startsWith("http://127.")||!kind(path).equals(kind)
            ||!"X-KTV-Media".equals(header)||credential==null||credential.length()>128
            ||!credential.matches("[A-Za-z0-9_.-]{32,128}")||expires<=now||expires>now+930)throw new IOException("invalid_media_route");
        return new MediaRoute(endpoint.api(path),header,credential,false);
    }
    public static Map<String,String> apiHeaders(String key,String instance,boolean negotiated,boolean coordinator){
        Map<String,String> headers=new LinkedHashMap<>();
        if(!negotiated||!coordinator)headers.put("X-KTV-Key",key);
        if(!negotiated||coordinator){headers.put("X-KTV-TV-Key",key);if(!instance.isEmpty())headers.put("X-KTV-TV-Instance",instance);}
        return headers;
    }
    public Map<String,String> headers(String key,String instance,boolean negotiated,boolean coordinator){
        if(this.coordinator)return apiHeaders(key,instance,negotiated,coordinator);
        Map<String,String> headers=new LinkedHashMap<>();headers.put(header,credential);return headers;
    }
}
