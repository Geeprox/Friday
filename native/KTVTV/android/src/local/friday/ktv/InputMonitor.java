package local.friday.ktv;
/** Aggregate input health only. No sample buffer retained. */
public final class InputMonitor {
 public volatile long frames;public long clippedA,clippedB,sumA,sumB,squaresA,squaresB;
 public int peakA,peakB,cleanFrames;public boolean ready;
 public void add(short[] block,int count,int channels){
  int clipped=0;for(int i=0;i<count;i++){int v=block[i],channel=i%channels;long square=(long)v*v;
   if(channel==0){sumA+=v;squaresA+=square;peakA=Math.max(peakA,Math.abs(v));if(Math.abs(v)>=32760){clippedA++;clipped++;}}
   else{sumB+=v;squaresB+=square;peakB=Math.max(peakB,Math.abs(v));if(Math.abs(v)>=32760){clippedB++;clipped++;}}
  }
  int n=count/channels;frames+=n;cleanFrames=clipped==0?cleanFrames+n:0;
  if(!ready&&frames>=9600&&cleanFrames>=2400)ready=true;
 }
 public long rmsA(){return frames>0?Math.round(Math.sqrt(squaresA/(double)frames)):0;}
 public long rmsB(){return frames>0?Math.round(Math.sqrt(squaresB/(double)frames)):0;}
 public long dcA(){return frames>0?sumA/frames:0;}
 public long dcB(){return frames>0?sumB/frames:0;}
}
