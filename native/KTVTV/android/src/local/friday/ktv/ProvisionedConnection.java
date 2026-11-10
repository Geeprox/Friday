package local.friday.ktv;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Set;

/** An explicit private NAS profile may migrate settings once. Legacy Mac
 * defaults are saved only into an entirely unconfigured preference store.
 * Portable builds cannot undo a user's later manual choice. */
public final class ProvisionedConnection {
    private ProvisionedConnection(){}
    public static String fingerprint(String role,String origin,String key){
        try{return MediaTransfer.hex(MessageDigest.getInstance("SHA-256").digest(("friday-ktv-profile-v1\n"+role+"\n"+origin+"\n"+key+"\n").getBytes(StandardCharsets.UTF_8)));}
        catch(java.security.NoSuchAlgorithmException failure){throw new IllegalStateException("profile_digest_unavailable");}
    }
    public static boolean shouldApply(String role,String profile,String origin,String key,Set<String> seen){
        if(!"nas".equals(role)||profile==null||!profile.matches("[a-f0-9]{64}")||seen==null||seen.contains(profile)||seen.size()>=128
            ||key==null||!key.matches("[A-Za-z0-9_-]{43,100}"))return false;
        try{return KtvEndpoint.parse(origin).base.equals(origin)&&profile.equals(fingerprint(role,origin,key));}
        catch(java.io.IOException invalid){return false;}
    }
    public static boolean shouldSeedLegacy(String role,String profile,String origin,String key,boolean hasOrigin,boolean hasKey){
        if(!"mac".equals(role)||hasOrigin||hasKey||profile==null||!profile.matches("[a-f0-9]{64}")
            ||key==null||!key.matches("[A-Za-z0-9_-]{32,128}"))return false;
        try{return KtvEndpoint.parse(origin).base.equals(origin)&&profile.equals(fingerprint(role,origin,key));}
        catch(java.io.IOException invalid){return false;}
    }
}
