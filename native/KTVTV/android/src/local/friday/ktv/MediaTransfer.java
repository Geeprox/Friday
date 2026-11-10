package local.friday.ktv;

import java.io.*;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

/** Synchronous, bounded transfer with verified fallback and atomic cache publication. */
public final class MediaTransfer {
    public interface Opener { InputStream open(MediaRoute route)throws Exception; }
    public interface Verifier { void verify(File partial)throws Exception; }
    private MediaTransfer(){}
    static String hex(byte[] value){StringBuilder b=new StringBuilder();for(byte v:value)b.append(String.format(Locale.ROOT,"%02x",v&255));return b.toString();}
    public static String hash(File file)throws Exception{
        MessageDigest digest=MessageDigest.getInstance("SHA-256");try(InputStream in=new FileInputStream(file)){byte[] bytes=new byte[65536];int n;while((n=in.read(bytes))!=-1)digest.update(bytes,0,n);}return hex(digest.digest());
    }
    private static void check(AtomicBoolean cancel)throws IOException{if(cancel.get()||Thread.currentThread().isInterrupted())throw new IOException("cancelled");}
    public static File existing(File target,long size,String sha,Verifier verify,AtomicBoolean cancel,MediaCache cache)throws Exception{
        check(cancel);
        if(size>=48&&sha.matches("[a-f0-9]{64}")&&target.isFile()&&target.length()==size&&hash(target).equals(sha)){
            try{verify.verify(target);check(cancel);cache.used(target);return target;}catch(Exception invalidFormat){check(cancel);}
        }return null;
    }
    public static File download(File target,long size,String sha,MediaRoute[] routes,Opener opener,Verifier verify,AtomicBoolean cancel,MediaCache cache)throws Exception{
        if(size<48||!sha.matches("[a-f0-9]{64}")||routes==null||routes.length<1||routes.length>3)throw new IOException("invalid_media");
        if(existing(target,size,sha,verify,cancel,cache)!=null)return target;
        if(target.exists()&&!target.delete())throw new IOException("cache_unavailable");
        cache.reserve(target,size);
        long deadline=System.nanoTime()+180000000000L;
        for(MediaRoute route:routes){
            check(cancel);long total=0;MessageDigest h=MessageDigest.getInstance("SHA-256");File partial=cache.beginPartial(sha);
            try{
                try(InputStream in=opener.open(route);OutputStream out=new FileOutputStream(partial)){
                    byte[] bytes=new byte[65536];int n;
                    while((n=in.read(bytes))!=-1){check(cancel);if(System.nanoTime()>deadline||total+n>size)throw new IOException("media_budget_exceeded");out.write(bytes,0,n);h.update(bytes,0,n);total+=n;}
                }
                check(cancel);if(total!=size||!hex(h.digest()).equals(sha))throw new IOException("media_integrity_failed");
                verify.verify(partial);check(cancel);
                if(!partial.renameTo(target)){if(existing(target,size,sha,verify,cancel,cache)!=null)return target;throw new IOException("cache_publish_failed");}cache.used(target);return target;
            }catch(Exception failure){
                check(cancel);if(System.nanoTime()>deadline)throw new IOException("media_budget_exceeded");
                // A cache miss, stale ticket or damaged source never publishes bytes.
                // The next already-authorized source is independently checked.
            }finally{try{if(partial.exists()&&!partial.delete())throw new IOException("cache_unavailable");}finally{MediaCache.endPartial(partial);}}
        }
        throw new IOException("media_sources_unavailable");
    }
}
