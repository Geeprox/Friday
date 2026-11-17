package local.friday.ktv;
import java.io.*;
import java.util.*;
/** 电视媒体可以重新下载。优先清理播放次数少且较早使用的文件。 */
public final class MediaCache {
 public static final long DEFAULT_LIMIT=1024L*1024*1024;
 private static final long MIN_FREE=16L*1024*1024;
 private static final Set<String> activePartials=new HashSet<>();
 private final File root;private final long limit;
 private Set<String> protectedHashes=new HashSet<>();
 private final Map<String,Long> reserved=new HashMap<>();
 private final Map<String,Integer> pinned=new HashMap<>();
 private final Map<String,Integer> plays=new LinkedHashMap<>();
 public MediaCache(File root)throws IOException{this(root,DEFAULT_LIMIT);}
 public MediaCache(File root,long limit)throws IOException{
  this.root=root;this.limit=limit;if(limit<=0||(!root.isDirectory()&&!root.mkdirs()))throw new IOException("cache_unavailable");
  for(File f:files())if(f.getName().endsWith(".part"))synchronized(activePartials){if(!activePartials.contains(f.getAbsolutePath()))f.delete();}
 }
 private File[] files(){File[] all=root.listFiles((dir,name)->name.matches("[a-f0-9]{64}(?:\\.(?:wav|m4a|pcm|mp4|png|jpg|part)|-[a-f0-9]{32}\\.part)"));return all==null?new File[0]:all;}
 public File beginPartial(String sha)throws IOException{
  if(!sha.matches("[a-f0-9]{64}"))throw new IOException("invalid_media");
  synchronized(activePartials){for(int attempt=0;attempt<3;attempt++){File file=new File(root,sha+"-"+UUID.randomUUID().toString().replace("-","")+".part");if(file.createNewFile()){activePartials.add(file.getAbsolutePath());return file;}}}
  throw new IOException("cache_unavailable");
 }
 public static void endPartial(File file){synchronized(activePartials){activePartials.remove(file.getAbsolutePath());}}
 private static boolean active(File file){synchronized(activePartials){return activePartials.contains(file.getAbsolutePath());}}
 public synchronized void protect(Set<String> hashes){protectedHashes=new HashSet<>(hashes);}
 public synchronized void used(File file){if(file.getParentFile().equals(root))file.setLastModified(System.currentTimeMillis());}
 public synchronized void played(File file){
  if(!root.equals(file.getParentFile()))return;
  String name=file.getName();
  plays.put(name,Math.min(100000,plays.getOrDefault(name,0)+1));
  if(plays.size()>1024)plays.remove(plays.keySet().iterator().next());
  used(file);
 }
 public synchronized long bytes(){long n=0;for(File f:files())n+=f.length();return n;}
 public long limit(){return limit;}
 public synchronized void reserve(File target,long size)throws IOException{
  if(size<0||size>limit)throw new IOException("cache_full");
  long held=0;
  for(long transfer:reserved.values())held+=transfer;
  File[] all=files();
  Arrays.sort(all,Comparator.comparingInt((File file)->plays.getOrDefault(file.getName(),0))
      .thenComparingLong(File::lastModified));
  long used=held;
  for(File file:all)if(!active(file)&&!reserved.containsKey(file.getName()))used+=file.length();
  long requiredFree=held+size+MIN_FREE;
  for(File file:all){
   if(used+size<=limit&&root.getUsableSpace()>=requiredFree)break;
   String name=file.getName();
   if(file.equals(target)||protectedHashes.contains(name.substring(0,64))||active(file)||reserved.containsKey(name)||pinned.containsKey(name))continue;
   if(!file.isFile()||!file.getCanonicalFile().equals(new File(root.getCanonicalFile(),name)))continue;
   long bytes=file.length();
   if(file.delete()){used-=bytes;plays.remove(name);}
  }
  if(used+size>limit||root.getUsableSpace()<requiredFree)throw new IOException("cache_full");
 }
 public synchronized Reservation reserveTransfer(File target,long size)throws IOException{
  String name=target.getName();
  if(reserved.containsKey(name))throw new IOException("cache_unavailable");
  reserve(target,size);
  reserved.put(name,size);
  return new Reservation(name);
 }
 public synchronized AutoCloseable pin(File file)throws IOException{
  if(!root.equals(file.getParentFile())||!file.isFile())throw new IOException("cache_unavailable");
  String name=file.getName();pinned.put(name,pinned.getOrDefault(name,0)+1);
  return ()->{synchronized(MediaCache.this){int count=pinned.getOrDefault(name,1)-1;if(count==0)pinned.remove(name);else pinned.put(name,count);}};
 }
 public final class Reservation implements AutoCloseable {
  private final String name;
  private boolean closed;
  private Reservation(String name){this.name=name;}
  @Override public void close(){
   synchronized(MediaCache.this){
    if(!closed){closed=true;reserved.remove(name);}
   }
  }
 }
}
