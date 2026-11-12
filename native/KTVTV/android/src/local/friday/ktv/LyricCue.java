package local.friday.ktv;
/** Shared presentation clock for current/next lyrics and a five-second lead-in. */
public final class LyricCue {
 public int current=-1,next=-1,countdown,entrance=-1;
 public void update(long ms,long[] starts){
  update(ms,starts,null);
 }
 public void update(long ms,long[] starts,long[] entries){
  current=-1;for(int i=0;i<starts.length;i++){if(starts[i]<=ms)current=i;else break;}
  next=current+1<starts.length?current+1:-1;countdown=0;entrance=-1;
  if(entries!=null){for(long entry:entries){long until=entry-ms;if(until>0){if(until<=5000){long nearest=1501;for(int i=0;i<starts.length;i++){long distance=Math.abs(starts[i]-entry);if(distance<nearest){nearest=distance;entrance=i;}}if(entrance>=0)countdown=(int)((until+999)/1000);}break;}}return;}
  if(next>=0){long until=starts[next]-ms;boolean gap=current<0||starts[next]-starts[current]>=10000;
   if(gap&&until>0&&until<=5000){countdown=(int)((until+999)/1000);entrance=next;}}
 }
}
