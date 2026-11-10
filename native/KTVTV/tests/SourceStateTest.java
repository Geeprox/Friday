package local.friday.ktv;
import java.io.*;
import java.nio.*;
import java.nio.file.*;
import java.util.*;
public final class SourceStateTest {
 static void check(boolean v,String reason){if(!v)throw new AssertionError(reason);}
 static File wav(File root,String name,int level)throws Exception{
  byte[] bytes=new byte[44+48000*4];ByteBuffer b=ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
  b.put("RIFF".getBytes("US-ASCII")).putInt(bytes.length-8).put("WAVEfmt ".getBytes("US-ASCII")).putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(192000).putShort((short)4).putShort((short)16).put("data".getBytes("US-ASCII")).putInt(bytes.length-44);
  while(b.hasRemaining())b.putShort((short)level);File f=new File(root,name);Files.write(f.toPath(),bytes);return f;
 }
 public static void main(String[] args)throws Exception{
  LyricCue cue=new LyricCue();long[] starts={10000,14000,30000};cue.update(5000,starts);check(cue.current==-1&&cue.next==0&&cue.countdown==5,"five second lead-in");cue.update(9001,starts);check(cue.countdown==1,"last lead-in dot");cue.update(10000,starts);check(cue.current==0&&cue.next==1&&cue.countdown==0,"current plus next lyric");cue.update(26000,starts);check(cue.current==1&&cue.next==2&&cue.countdown==4,"interlude countdown");
  File root=new File(args[0]);root.mkdirs();short[] clean=new short[480],bad=new short[480];Arrays.fill(bad,(short)32767);
  InputMonitor input=new InputMonitor();for(int i=0;i<4;i++)input.add(bad,480,2);
  check(!input.ready&&input.clippedA==960&&input.clippedB==960,"startup noise quarantined");
  for(int i=0;i<40;i++)input.add(clean,480,2);check(input.ready,"healthy input admitted after settling");
  InputMonitor clipped=new InputMonitor();for(int i=0;i<200;i++)clipped.add(bad,480,2);check(!clipped.ready&&clipped.frames==48000,"bad input never admitted");
  check(clipped.rmsA()==32767&&clipped.dcA()==32767,"aggregate health preserves cause");
  File one=wav(root,"one.wav",1000),two=wav(root,"two.wav",2000);short[] out=new short[480];
  try(SwitchingWavSource src=new SwitchingWavSource(one)){src.seek(4800);src.request(two);check(src.read(out,240)==240&&src.position==5040,"hot switch stays on timeline");check(out[0]>1000&&out[0]<1010&&out[478]==2000&&src.switches==1,"bounded crossfade");src.request(new File(root,"missing.wav"));check(src.read(out,240)==240&&src.position==5280&&out[0]==2000&&src.failures==1,"failed switch preserves source and timeline");}
  File cache=new File(root,"cache");cache.mkdir();String a=String.join("",Collections.nCopies(64,"a")),b=String.join("",Collections.nCopies(64,"b")),c=String.join("",Collections.nCopies(64,"c"));
  File fa=new File(cache,a+".wav"),fb=new File(cache,b+".mp4"),fc=new File(cache,c+".wav");Files.write(fa.toPath(),new byte[40]);Files.write(fb.toPath(),new byte[40]);fa.setLastModified(1);fb.setLastModified(2);
  MediaCache media=new MediaCache(cache,100);media.protect(new HashSet<>(Arrays.asList(a)));media.reserve(fc,40);check(fa.exists()&&!fb.exists(),"pinned current survives LRU eviction");Files.write(fc.toPath(),new byte[40]);media.protect(new HashSet<>(Arrays.asList(a,c)));boolean full=false;try{media.reserve(fb,40);}catch(IOException e){full=true;}check(full&&media.bytes()==80,"pinned media never evicted to overfill");media.protect(new HashSet<>());media.used(fa);media.reserve(fb,40);check(fa.exists()&&!fc.exists(),"recently used song survives");
  System.out.println("Input quarantine, hot switch and protected TV LRU passed; no device started");
 }
}
