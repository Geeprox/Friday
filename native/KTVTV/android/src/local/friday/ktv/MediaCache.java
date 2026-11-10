package local.friday.ktv;
import java.io.*;
import java.util.*;
/** Rebuildable TV media only; LRU eviction never removes current/next media. */
public final class MediaCache {
 public static final long DEFAULT_LIMIT=1024L*1024*1024;
 private static final Set<String> activePartials=new HashSet<>();
 private final File root;private final long limit;
 private Set<String> protectedHashes=new HashSet<>();
 public MediaCache(File root)throws IOException{this(root,DEFAULT_LIMIT);}
 public MediaCache(File root,long limit)throws IOException{
  this.root=root;this.limit=limit;if(limit<=0||(!root.isDirectory()&&!root.mkdirs()))throw new IOException("cache_unavailable");
  for(File f:files())if(f.getName().endsWith(".part"))synchronized(activePartials){if(!activePartials.contains(f.getAbsolutePath()))f.delete();}
 }
 private File[] files(){File[] all=root.listFiles((dir,name)->name.matches("[a-f0-9]{64}(?:\\.(?:wav|mp4|part)|-[a-f0-9]{32}\\.part)"));return all==null?new File[0]:all;}
 public File beginPartial(String sha)throws IOException{
  if(!sha.matches("[a-f0-9]{64}"))throw new IOException("invalid_media");
  synchronized(activePartials){for(int attempt=0;attempt<3;attempt++){File file=new File(root,sha+"-"+UUID.randomUUID().toString().replace("-","")+".part");if(file.createNewFile()){activePartials.add(file.getAbsolutePath());return file;}}}
  throw new IOException("cache_unavailable");
 }
 public static void endPartial(File file){synchronized(activePartials){activePartials.remove(file.getAbsolutePath());}}
 private static boolean active(File file){synchronized(activePartials){return activePartials.contains(file.getAbsolutePath());}}
 public synchronized void protect(Set<String> hashes){protectedHashes=new HashSet<>(hashes);}
 public synchronized void used(File file){if(file.getParentFile().equals(root))file.setLastModified(System.currentTimeMillis());}
 public synchronized long bytes(){long n=0;for(File f:files())n+=f.length();return n;}
 public long limit(){return limit;}
 public synchronized void reserve(File target,long size)throws IOException{
  if(size<0||size>limit)throw new IOException("cache_full");
  File[] all=files();Arrays.sort(all,Comparator.comparingLong(File::lastModified));long used=bytes();
  for(File f:all){if(used+size<=limit)break;String hash=f.getName().substring(0,64);
   if(!f.equals(target)&&!protectedHashes.contains(hash)&&!active(f)){long n=f.length();if(f.delete())used-=n;}}
  if(used+size>limit||root.getUsableSpace()<size+16L*1024*1024)throw new IOException("cache_full");
 }
}
