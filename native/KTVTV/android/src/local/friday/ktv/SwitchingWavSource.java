package local.friday.ktv;
import java.io.*;
import java.util.concurrent.atomic.AtomicReference;
/** Sample-aligned 5 ms crossfade. Preparing a replacement never runs on the render thread. */
public final class SwitchingWavSource implements AutoCloseable {
 private WavSource current;
 private final AtomicReference<WavSource> pending=new AtomicReference<>();
 private final short[] alternate=new short[480];
 private final long frames;
 private boolean closed;
 public long position;public volatile int failures,switches;
 public SwitchingWavSource(File path)throws IOException{current=new WavSource(path);frames=current.frames;}
 public synchronized void request(File path){
  if(closed)return;
  WavSource next=null;
  try{
   next=new WavSource(path);
   if(Math.abs(next.frames-frames)>4800)throw new IOException("source_duration_mismatch");
   WavSource prior=pending.getAndSet(next);next=null;if(prior!=null)prior.close();
  }catch(IOException error){failures++;if(next!=null)try{next.close();}catch(IOException ignored){}}
 }
 public void seek(long frame)throws IOException{current.seek(frame);position=current.position;}
 public int read(short[] out,int count)throws IOException{
  WavSource next=pending.getAndSet(null);
  if(next!=null){try{
   next.seek(current.position);int n=current.read(out,count),m=next.read(alternate,count);
   if(n!=m)throw new IOException("source_duration_mismatch");
   for(int i=0;i<n;i++){float blend=(i+1f)/n;for(int c=0;c<2;c++)out[i*2+c]=(short)Math.round(out[i*2+c]*(1-blend)+alternate[i*2+c]*blend);}
   current.close();current=next;next=null;switches++;position=current.position;return n;
  }catch(IOException error){failures++;if(next!=null)next.close();current.seek(position);}}
  int n=current.read(out,count);position=current.position;return n;
 }
 public synchronized void close()throws IOException{
  closed=true;WavSource next=pending.getAndSet(null);
  try{current.close();}finally{if(next!=null)next.close();java.util.Arrays.fill(alternate,(short)0);}
 }
}
