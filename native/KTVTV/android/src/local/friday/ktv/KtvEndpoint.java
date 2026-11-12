package local.friday.ktv;

import java.io.IOException;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/** Pure endpoint validation. Parsing never resolves DNS or changes TLS checks. */
public final class KtvEndpoint {
    public final String base;
    private KtvEndpoint(String base){this.base=base;}
    public static boolean privateIpv4(String host){
        if(host==null||!host.matches("(?:0|[1-9][0-9]{0,2})(?:\\.(?:0|[1-9][0-9]{0,2})){3}"))return false;
        String[] parts=host.split("\\.");int[] n=new int[4];
        for(int i=0;i<4;i++){n[i]=Integer.parseInt(parts[i]);if(n[i]>255)return false;}
        return n[0]==10||n[0]==127||(n[0]==172&&n[1]>=16&&n[1]<=31)||(n[0]==192&&n[1]==168);
    }
    public static KtvEndpoint parse(String value)throws IOException{
        try{
            if(value==null||value.length()>512)throw new IOException("invalid_connection");
            URI u=new URI(value);String scheme=u.getScheme(),host=u.getHost(),path=u.getRawPath();int port=u.getPort();
            if(host==null||u.getRawUserInfo()!=null||u.getRawQuery()!=null||u.getRawFragment()!=null
                ||host.length()>253||path==null||path.length()>128||!path.matches("(?:/[A-Za-z0-9_-]+)*/?"))throw new IOException("invalid_connection");
            boolean tls="https".equals(scheme),lan="http".equals(scheme)&&privateIpv4(host);
            if(!tls&&!lan)throw new IOException("private_http_or_https_required");
            if(lan&&(port<1024||port>65535))throw new IOException("private_port_required");
            if(tls&&(port==0||port>65535||port< -1||!host.matches("[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")))throw new IOException("invalid_connection");
            while(path.endsWith("/"))path=path.substring(0,path.length()-1);
            return new KtvEndpoint(scheme+"://"+host.toLowerCase(Locale.ROOT)+(port<0?"":":"+port)+path);
        }catch(URISyntaxException|IllegalArgumentException e){throw new IOException("invalid_connection");}
    }
    public String api(String path)throws IOException{
        if(path==null||path.length()>512||!path.startsWith("/v1/")||path.contains("..")||path.contains("#")||path.contains("\\")||path.contains("%")
            ||!path.matches("/v1/[A-Za-z0-9_/?&=.-]+"))throw new IOException("invalid_endpoint");
        return base+path;
    }
}
