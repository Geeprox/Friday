package local.tv.camera;
import android.app.Activity;
import android.os.*;
import android.view.*;
import android.widget.FrameLayout;
import android.graphics.*;
import android.media.*;
import org.json.*;
import java.net.*;
import java.io.*;
import java.nio.ByteBuffer;
import java.util.*;
import java.util.concurrent.*;

/** TV owns decoding, layout and overlays; the household owner supplies observations. */
public final class MainActivity extends Activity {
 private final String base="__FRIDAY_VIEW_ORIGIN__",key="__FRIDAY_VIEW_KEY__";
 private final Set<HttpURLConnection> connections=Collections.newSetFromMap(new ConcurrentHashMap<HttpURLConnection,Boolean>());
 private final LinkedHashMap<String,Tile> tiles=new LinkedHashMap<>();
 private static final Semaphore networkSlots=new Semaphore(32);
 private static final ConcurrentHashMap<HttpURLConnection,Runnable> cancellations=new ConcurrentHashMap<>();
 private static final ThreadPoolExecutor cleanup=new ThreadPoolExecutor(2,2,5,TimeUnit.SECONDS,new ArrayBlockingQueue<Runnable>(32),new ThreadPoolExecutor.AbortPolicy());
 static {cleanup.allowCoreThreadTimeOut(true);}
 private static final java.util.concurrent.atomic.AtomicBoolean nativePermit=new java.util.concurrent.atomic.AtomicBoolean();
 private volatile int generation;
 private volatile long serverOffset;
 private WallView screen;
 private Thread statusThread,reportThread;
 private final java.util.concurrent.atomic.AtomicBoolean statusPending=new java.util.concurrent.atomic.AtomicBoolean();
 private int selected;
 private volatile String focused;
 private volatile Tile[] tileSnapshot=new Tile[0];
 private boolean debug;
 private volatile long statusAt;
 private static final int MAX_PACKET=2097152;
 private static final String[][] BONES={{"leftShoulder","rightShoulder"},{"leftShoulder","leftElbow"},{"leftElbow","leftWrist"},{"rightShoulder","rightElbow"},{"rightElbow","rightWrist"},{"leftShoulder","leftHip"},{"rightShoulder","rightHip"},{"leftHip","rightHip"},{"leftHip","leftKnee"},{"leftKnee","leftAnkle"},{"rightHip","rightKnee"},{"rightKnee","rightAnkle"}};
 private final class Tile implements TextureView.SurfaceTextureListener {
  final String id; final TextureView texture;
  volatile String name="",room="",codecName="";
  volatile JSONObject overlay,toast;
  volatile boolean active=true;
  volatile boolean fallback,picturePending;
  volatile Bitmap picture;
  volatile String codecIssue="",lastFailure="";
  volatile long nativeStarted,nativeRetryAt,decoderEpoch;
  volatile int retries,reconnects;
  volatile boolean decoderAllocated;
  volatile boolean closed;
  volatile long renderedAt,renderedLocal,frames;
  volatile int width=16,height=9;
  volatile String error="connecting";
  volatile HttpURLConnection connection,stillConnection;
  volatile Surface surface;
  Thread thread,previewThread;
  RectF area=new RectF(),video=new RectF();
  Tile(String id){this.id=id;texture=new TextureView(MainActivity.this);texture.setSurfaceTextureListener(this);texture.setOnClickListener(v->{selected=new ArrayList<>(tiles.keySet()).indexOf(id);focus(focused==null?id:null);});screen.addView(texture);}
  public void onSurfaceTextureAvailable(SurfaceTexture s,int w,int h){surface=new Surface(s);if(active)startVideo(this,generation);}
  public void onSurfaceTextureSizeChanged(SurfaceTexture s,int w,int h){}
  public boolean onSurfaceTextureDestroyed(SurfaceTexture s){stop();return true;}
  public void onSurfaceTextureUpdated(SurfaceTexture s){}
  void clearPicture(){Bitmap old=picture;picture=null;if(old!=null)old.recycle();}
  synchronized void stop(){closed=true;clearPicture();cancel(connection);cancel(stillConnection);if(thread!=null)thread.interrupt();if(previewThread!=null)previewThread.interrupt();if(thread==null||!thread.isAlive()){Surface s=surface;surface=null;if(s!=null)s.release();}}
 }
 @Override public void onCreate(Bundle b){super.onCreate(b);getWindow().getDecorView().setSystemUiVisibility(5894);getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);debug=getPreferences(0).getBoolean("debug",false);screen=new WallView();setContentView(screen);}
 @Override protected void onResume(){super.onResume();start();}
 @Override protected void onPause(){generation++;for(Tile t:tiles.values())t.stop();for(HttpURLConnection c:connections)cancel(c);if(statusThread!=null)statusThread.interrupt();if(reportThread!=null)reportThread.interrupt();tiles.clear();tileSnapshot=new Tile[0];screen.removeAllViews();super.onPause();}
 private void cancel(HttpURLConnection c){if(c==null)return;synchronized(connections){
  if(!connections.contains(c)||cancellations.containsKey(c))return;
  Runnable task=()->{try{c.disconnect();}catch(Exception ignored){}finally{cancellations.remove(c);}};
  cancellations.put(c,task);cleanup.execute(task);
 }}
 private void forget(HttpURLConnection c){synchronized(connections){Runnable task=cancellations.remove(c);if(task!=null)cleanup.remove(task);if(connections.remove(c))networkSlots.release();}}
 private HttpURLConnection connect(String path)throws IOException {
  if(Thread.currentThread().isInterrupted())throw new IOException("cancelled");
  if(!networkSlots.tryAcquire())throw new IOException("connection_budget");
  try{HttpURLConnection c=(HttpURLConnection)new URL(base+path+"?k="+key).openConnection();c.setConnectTimeout(2000);c.setReadTimeout(2000);c.setUseCaches(false);connections.add(c);return c;}catch(IOException|RuntimeException e){networkSlots.release();throw e;}
 }
 private byte[] bytes(InputStream in,int max)throws IOException {ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] buf=new byte[8192];int n;long until=SystemClock.elapsedRealtime()+2000;while((n=in.read(buf))!=-1){if(out.size()+n>max||SystemClock.elapsedRealtime()>until)throw new IOException("budget");out.write(buf,0,n);}return out.toByteArray();}
 private JSONObject status()throws Exception {HttpURLConnection c=connect("/api/wall");try{if(c.getResponseCode()!=200)throw new IOException("status");try(InputStream in=c.getInputStream()){return new JSONObject(new String(bytes(in,512000),"UTF-8"));}}finally{c.disconnect();forget(c);}}
 private void reconcile(JSONObject value,int mine)throws Exception {
  JSONArray rows=value.getJSONArray("cameras");if(value.optInt("version")!=1||rows.length()>8)throw new IOException("status");
  HashSet<String> present=new HashSet<>();
  for(int i=0;i<rows.length();i++){JSONObject r=rows.getJSONObject(i);String id=r.getString("id");if(!id.matches("cam_[a-f0-9]{10}"))throw new IOException("camera");present.add(id);Tile t=tiles.get(id);if(t==null){t=new Tile(id);tiles.put(id,t);}t.name=r.optString("name");t.room=r.optString("room");t.overlay=r.optJSONObject("overlay");t.toast=r.optJSONObject("toast");}
  Iterator<Map.Entry<String,Tile>> it=tiles.entrySet().iterator();while(it.hasNext()){Tile t=it.next().getValue();if(!present.contains(t.id)){t.stop();screen.removeView(t.texture);it.remove();}}
  tileSnapshot=tiles.values().toArray(new Tile[0]);statusAt=SystemClock.elapsedRealtime();screen.arrange();screen.invalidate();
 }
 private void start(){final int mine=++generation;
  statusThread=new Thread(()->{
   while(generation==mine){long began=SystemClock.elapsedRealtime();try{
    JSONObject v=status();long end=SystemClock.elapsedRealtime();if(serverOffset==0||end-began<500)serverOffset=v.getLong("server_time_ms")-(began+end)/2;
    if(statusPending.compareAndSet(false,true))runOnUiThread(()->{try{if(generation==mine)reconcile(v,mine);}catch(Exception ignored){}finally{statusPending.set(false);}});
   }catch(Exception ignored){}try{Thread.sleep(Math.max(30,200-(SystemClock.elapsedRealtime()-began)));}catch(InterruptedException e){break;}}
  },"friday-tv-status");statusThread.start();
  reportThread=new Thread(()->{long previous=0,reportAt=SystemClock.elapsedRealtime();
   while(generation==mine){try{Thread.sleep(4000);if(generation!=mine)break;long now=SystemClock.elapsedRealtime(),total=0;JSONArray cameras=new JSONArray();
    for(Tile t:tileSnapshot){total+=t.frames;JSONObject c=new JSONObject();c.put("camera",t.id);c.put("frames",t.frames);c.put("width",t.width);c.put("height",t.height);c.put("decoder",t.codecName);c.put("state",t.error);c.put("active",t.active);c.put("render_mode",t.fallback?"jpeg_preview":"native_video");c.put("codec_issue",t.codecIssue);c.put("last_failure",t.lastFailure);c.put("reconnects",t.reconnects);c.put("frame_age_ms",serverNow()-t.renderedAt);cameras.put(c);}
    report(total,Math.max(0,total-previous)*1000.0/Math.max(1,now-reportAt),cameras);previous=total;reportAt=now;
   }catch(InterruptedException e){break;}catch(Exception ignored){}}
  },"friday-tv-report");reportThread.start();
 }
 private long serverNow(){return SystemClock.elapsedRealtime()+serverOffset;}
 private void report(long frames,double fps,JSONArray cameras)throws Exception {JSONObject v=new JSONObject();v.put("version","3.2");v.put("mode","native_video_wall");v.put("detail",SystemClock.elapsedRealtime()-statusAt>5000?"ui_stale":"active");v.put("frames",frames);v.put("fps",fps);v.put("cameras",cameras);byte[] data=v.toString().getBytes("UTF-8");HttpURLConnection c=connect("/api/viewer");try{c.setRequestMethod("POST");c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json");try(OutputStream out=c.getOutputStream()){out.write(data);}try(InputStream in=c.getInputStream()){bytes(in,1000);}}finally{c.disconnect();forget(c);}}
 private static ArrayList<byte[]> nals(byte[] data){ArrayList<byte[]> out=new ArrayList<>();int start=-1;for(int i=0;i+3<data.length;i++){int n=data[i]==0&&data[i+1]==0&&data[i+2]==1?3:data[i]==0&&data[i+1]==0&&data[i+2]==0&&data[i+3]==1?4:0;if(n>0){if(start>=0&&i>start)out.add(Arrays.copyOfRange(data,start,i));start=i+n;i+=n-1;}}if(start>=0&&start<data.length)out.add(Arrays.copyOfRange(data,start,data.length));return out;}
 private static byte[] annex(byte[] data){byte[] out=new byte[data.length+4];out[3]=1;System.arraycopy(data,0,out,4,data.length);return out;}
 private static void parameters(MediaFormat format,byte[] data,int type)throws Exception {HashMap<Integer,byte[]> found=new HashMap<>();for(byte[] nal:nals(data)){int k=type==1?nal[0]&31:(nal[0]>>1)&63;found.put(k,annex(nal));}if(type==1){if(!found.containsKey(7)||!found.containsKey(8))throw new IOException("headers");format.setByteBuffer("csd-0",ByteBuffer.wrap(found.get(7)));format.setByteBuffer("csd-1",ByteBuffer.wrap(found.get(8)));}else{ByteArrayOutputStream all=new ByteArrayOutputStream();for(int k:new int[]{32,33,34}){if(!found.containsKey(k))throw new IOException("headers");all.write(found.get(k));}format.setByteBuffer("csd-0",ByteBuffer.wrap(all.toByteArray()));}}
 private void drain(Tile t,MediaCodec codec,MediaCodec.BufferInfo info){for(int n=0;n<12;n++){int i=codec.dequeueOutputBuffer(info,0);if(i==MediaCodec.INFO_OUTPUT_FORMAT_CHANGED){MediaFormat f=codec.getOutputFormat();t.width=f.getInteger(MediaFormat.KEY_WIDTH);t.height=f.getInteger(MediaFormat.KEY_HEIGHT);runOnUiThread(()->screen.arrange());continue;}if(i<0)break;long at=info.presentationTimeUs/1000,age=serverNow()-at;boolean fresh=age>=-100&&age<1800&&!t.closed&&t.active;
  codec.releaseOutputBuffer(i,fresh);
 }}
 private void focus(String id){
  focused=id;screen.arrange();
  if(id!=null){Tile t=tiles.get(id);if(t!=null){t.retries=0;if(t.fallback)t.nativeRetryAt=SystemClock.elapsedRealtime()+1500;}}
 }
 private boolean retryReady(Tile t,long now){
  if(t.nativeRetryAt==0||now<t.nativeRetryAt||t.retries>=2||nativePermit.get())return false;
  if(focused!=null){if(!focused.equals(t.id))return false;for(Tile other:tileSnapshot)if(other!=t&&other.decoderAllocated)return false;}
  return true;
 }
 private void usePreview(Tile t,String reason){synchronized(t){
  if(t.fallback)return;
  t.fallback=true;t.codecIssue=reason;t.decoderEpoch++;
  t.nativeRetryAt=reason.equals("decoder_budget")||reason.equals("resource_limit")?0:SystemClock.elapsedRealtime()+30000;cancel(t.connection);
 }}
 private static String failure(Exception e){
  if(e instanceof MediaCodec.CodecException)return "decoder_failed";
  if(e instanceof java.net.SocketTimeoutException)return "stream_timeout";
  if(e instanceof EOFException)return "stream_ended";
  String value=e.getMessage();
  for(String fixed:new String[]{"stream","packet","headers","changed","busy","capacity","stale","still","dimensions","image","budget","connection_budget"})if(fixed.equals(value))return fixed;
  return "transport_failed";
 }
 private void still(Tile t,int mine)throws Exception{
  if(t.picturePending)return;
  HttpURLConnection c=connect("/api/wall/"+t.id+"/still");t.stillConnection=c;
  try{
   if(c.getResponseCode()!=200)throw new IOException("still");
   long at=Long.parseLong(c.getHeaderField("X-Friday-Frame-Unix-Ms")),age=serverNow()-at;
   if(age< -100||age>=1800||at<=t.renderedAt)return;
   byte[] data;try(InputStream in=c.getInputStream()){data=bytes(in,MAX_PACKET);}
   BitmapFactory.Options bounds=new BitmapFactory.Options();bounds.inJustDecodeBounds=true;BitmapFactory.decodeByteArray(data,0,data.length,bounds);
   if(bounds.outWidth<16||bounds.outWidth>1920||bounds.outHeight<16||bounds.outHeight>1080)throw new IOException("dimensions");
   final Bitmap bitmap=BitmapFactory.decodeByteArray(data,0,data.length);if(bitmap==null)throw new IOException("image");
   t.picturePending=true;
   runOnUiThread(()->{t.picturePending=false;long currentAge=serverNow()-at;
    if(generation==mine&&!t.closed&&t.active&&t.fallback&&currentAge>=-100&&currentAge<1800&&at>t.renderedAt){
     t.clearPicture();t.picture=bitmap;t.width=bitmap.getWidth();t.height=bitmap.getHeight();t.renderedAt=at;t.renderedLocal=SystemClock.elapsedRealtime();t.frames++;t.error="playing";screen.arrange();
    }else bitmap.recycle();
   });
  }finally{c.disconnect();forget(c);t.stillConnection=null;}
 }
 private boolean releaseCodec(MediaCodec codec){try{codec.stop();}catch(Exception ignored){}try{codec.release();return true;}catch(Exception ignored){return false;}}
 private void startPreview(Tile t,int mine){
  if(t.previewThread==null||!t.previewThread.isAlive()){
   t.previewThread=new Thread(()->{while(!t.closed&&t.active&&generation==mine){try{
    if(!t.fallback&&SystemClock.elapsedRealtime()-Math.max(t.nativeStarted,t.renderedLocal)>=6000)usePreview(t,"video_timeout");
    if(t.fallback)still(t,mine);Thread.sleep(333);
   }catch(InterruptedException e){break;}catch(Exception e){t.lastFailure=failure(e);try{Thread.sleep(500);}catch(InterruptedException ended){break;}}}},"friday-preview-"+t.id);t.previewThread.start();
  }
 }
 private void startVideo(Tile t,int mine){
  t.nativeStarted=SystemClock.elapsedRealtime();startPreview(t,mine);
  t.thread=new Thread(()->{
   MediaCodec decoder=null;boolean ownsPermit=false;int oldType=0,oldW=0,oldH=0;
   try{
    while(!t.closed&&t.active&&generation==mine){
     HttpURLConnection c=null;boolean initializing=false,release=false;
     try{
      if(t.fallback){
       if(retryReady(t,SystemClock.elapsedRealtime())){t.retries++;t.fallback=false;t.nativeRetryAt=0;t.nativeStarted=SystemClock.elapsedRealtime();}
       else{Thread.sleep(200);continue;}
      }
      long last=Math.max(t.nativeStarted,t.renderedLocal);
      if(SystemClock.elapsedRealtime()-last>=6000){usePreview(t,"video_timeout");release=true;continue;}
      c=connect("/api/wall/"+t.id+"/video");t.connection=c;
      if(c.getResponseCode()!=200||!"annex-b-v1".equals(c.getHeaderField("X-Friday-Video")))throw new IOException("stream");
      DataInputStream in=new DataInputStream(new BufferedInputStream(c.getInputStream(),65536));MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();long sequence=-1;
      while(!t.closed&&t.active&&!t.fallback&&generation==mine){
       if(SystemClock.elapsedRealtime()-Math.max(t.nativeStarted,t.renderedLocal)>=6000){usePreview(t,"video_timeout");release=true;break;}
       int length=in.readInt(),type=in.readUnsignedByte(),w=in.readUnsignedShort(),h=in.readUnsignedShort();long at=in.readLong(),seq=in.readLong();
       if(length<1||length>MAX_PACKET||type<1||type>2||w<16||w>8192||h<16||h>4320||seq<=sequence||(sequence>=0&&seq!=sequence+1))throw new IOException("packet");
       sequence=seq;byte[] packet=new byte[length];in.readFully(packet);
       if(serverNow()-at>1800||serverNow()-at< -100)throw new IOException("stale");
       if(decoder!=null&&(type!=oldType||w!=oldW||h!=oldH)){release=true;throw new IOException("changed");}
       if(decoder==null){
        String mime=type==1?"video/avc":"video/hevc";MediaFormat f=MediaFormat.createVideoFormat(mime,w,h);
        f.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE,MAX_PACKET);f.setInteger("low-latency",1);parameters(f,packet,type);
        if(!nativePermit.compareAndSet(false,true)){usePreview(t,"decoder_budget");break;}
        ownsPermit=true;initializing=true;t.decoderAllocated=true;decoder=MediaCodec.createDecoderByType(mime);t.codecName=decoder.getName();decoder.configure(f,t.surface,null,0);decoder.start();initializing=false;
        final long epoch=++t.decoderEpoch;
        decoder.setOnFrameRenderedListener((owner,pts,nano)->{long frameAt=pts/1000,age=serverNow()-frameAt;if(epoch==t.decoderEpoch&&!t.closed&&t.active&&!t.fallback&&generation==mine&&age>=-100&&age<1800&&frameAt>t.renderedAt){t.renderedAt=frameAt;t.renderedLocal=SystemClock.elapsedRealtime();t.frames++;t.error="playing";t.codecIssue="";t.clearPicture();}},new Handler(Looper.getMainLooper()));
        oldType=type;oldW=w;oldH=h;t.width=w;t.height=h;runOnUiThread(()->{if(generation==mine&&!t.closed)screen.arrange();});
       }
       drain(t,decoder,info);int index=-1;long until=SystemClock.elapsedRealtime()+120;
       while(index<0&&SystemClock.elapsedRealtime()<until&&!t.closed&&t.active){index=decoder.dequeueInputBuffer(5000);if(index<0)drain(t,decoder,info);}
       if(index<0){release=true;throw new IOException("busy");}ByteBuffer buffer=decoder.getInputBuffer(index);
       if(buffer==null||buffer.capacity()<length){release=true;throw new IOException("capacity");}buffer.clear();buffer.put(packet);decoder.queueInputBuffer(index,0,length,at*1000,0);drain(t,decoder,info);
      }
     }catch(Exception e){
      t.error="reconnecting";t.lastFailure=failure(e);t.reconnects++;
      if(initializing||e instanceof MediaCodec.CodecException){release=true;if(!t.closed&&t.active)usePreview(t,e instanceof MediaCodec.CodecException&&((MediaCodec.CodecException)e).getErrorCode()==MediaCodec.CodecException.ERROR_INSUFFICIENT_RESOURCE?"resource_limit":"decoder_unavailable");}
      else if(!t.fallback&&SystemClock.elapsedRealtime()-Math.max(t.nativeStarted,t.renderedLocal)>=6000){usePreview(t,"video_timeout");release=true;}
     }finally{
      if(c!=null){c.disconnect();forget(c);}t.connection=null;
      // Transport reconnects start from a complete IDR and keep the hardware slot.
      if(decoder!=null&&(release||t.fallback||t.closed||!t.active||generation!=mine)){
       t.decoderEpoch++;boolean released=releaseCodec(decoder);decoder=null;ownsPermit=false;t.decoderAllocated=!released;
       if(released)nativePermit.set(false);else t.codecIssue="decoder_release_failed";
      }
      if(decoder==null&&ownsPermit){ownsPermit=false;t.decoderAllocated=false;nativePermit.set(false);}
     }
     if(!t.closed&&t.active)try{Thread.sleep(250);}catch(InterruptedException e){break;}
    }
   }finally{
    t.decoderEpoch++;if(decoder!=null){boolean released=releaseCodec(decoder);ownsPermit=false;t.decoderAllocated=!released;if(released)nativePermit.set(false);else t.codecIssue="decoder_release_failed";}
    if(ownsPermit){t.decoderAllocated=false;nativePermit.set(false);}
    synchronized(t){if(t.closed){Surface surface=t.surface;t.surface=null;if(surface!=null)surface.release();}}
   }
  },"friday-video-"+t.id);t.thread.start();
 }
 private void selectStep(int step){selected=Math.max(0,Math.min(Math.max(0,tiles.size()-1),selected+step));screen.describeSelection();screen.invalidate();}
 private void toggleSelected(){if(!tiles.isEmpty()){selected=Math.min(selected,tiles.size()-1);focus(focused==null?new ArrayList<>(tiles.keySet()).get(selected):null);screen.describeSelection();}}
 @Override public boolean onKeyDown(int code,KeyEvent e){if(code==KeyEvent.KEYCODE_MENU){debug=!debug;getPreferences(0).edit().putBoolean("debug",debug).apply();screen.invalidate();return true;}if(code==KeyEvent.KEYCODE_DPAD_LEFT||code==KeyEvent.KEYCODE_DPAD_UP){selectStep(-1);return true;}if(code==KeyEvent.KEYCODE_DPAD_RIGHT||code==KeyEvent.KEYCODE_DPAD_DOWN){selectStep(1);return true;}if(code==KeyEvent.KEYCODE_DPAD_CENTER||code==KeyEvent.KEYCODE_ENTER){toggleSelected();return true;}if(code==KeyEvent.KEYCODE_BACK&&focused!=null){focus(null);screen.describeSelection();return true;}return super.onKeyDown(code,e);}

 private final class WallView extends FrameLayout {
  final Paint p=new Paint(3);final Bitmap icon;
  WallView(){super(MainActivity.this);setWillNotDraw(false);setFocusable(true);setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);setOnClickListener(v->toggleSelected());describeSelection();setBackgroundColor(0xfff2f3ef);icon=BitmapFactory.decodeResource(getResources(),getResources().getIdentifier("friday_icon","drawable",getPackageName()));}
  void describeSelection(){ArrayList<Tile> list=new ArrayList<>(tiles.values());String label="暂无镜头，请检查家庭连接。";if(!list.isEmpty()){Tile t=list.get(Math.min(selected,list.size()-1));label=(t.room.isEmpty()?t.name:t.room)+"，第 "+(selected+1)+" 个，共 "+list.size()+" 个镜头。";}String description="Friday 全屋视野。"+label+(focused==null?"确认键放大，方向键选择。":"确认键或返回键返回全屋。");if(!description.contentEquals(getContentDescription()==null?"":getContentDescription()))setContentDescription(description);setClickable(!list.isEmpty());}
  @Override public void onInitializeAccessibilityNodeInfo(android.view.accessibility.AccessibilityNodeInfo info){super.onInitializeAccessibilityNodeInfo(info);if(!tiles.isEmpty())info.addAction(new android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_CLICK,focused==null?"放大所选镜头":"返回全屋"));if(focused==null&&tiles.size()>1){info.setScrollable(true);info.addAction(new android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_SCROLL_FORWARD,"下一个镜头"));info.addAction(new android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD,"上一个镜头"));}}
  @Override public boolean performAccessibilityAction(int action,android.os.Bundle args){if(focused==null&&action==android.view.accessibility.AccessibilityNodeInfo.ACTION_SCROLL_FORWARD){selectStep(1);return true;}if(focused==null&&action==android.view.accessibility.AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD){selectStep(-1);return true;}return super.performAccessibilityAction(action,args);}
  void waiting(Canvas c,Tile t){String issue=t.error==null?"":t.error;String title="等待实时画面",detail="请检查镜头连接";if(issue.contains("shutter")||issue.contains("privacy")){title="镜头已关闭";detail="在米家打开镜头后再查看";}else if(issue.contains("paused")){title="画面已暂停";detail="在 Friday 恢复摄像头感知";}else if(issue.equals("connecting")||issue.equals("stopped")){title="正在连接画面";detail="";}p.setTextSize(25);text(c,title,t.video.centerX()-p.measureText(title)/2,t.video.centerY(),25,0xff596356);if(!detail.isEmpty()){p.setTextSize(21);text(c,detail,t.video.centerX()-p.measureText(detail)/2,t.video.centerY()+34,21,0xff596356);}}
  void text(Canvas c,String s,float x,float y,float size,int color){p.setStyle(Paint.Style.FILL);p.setColor(color);p.setTextSize(size);p.setTypeface(Typeface.create("sans-serif",Typeface.NORMAL));c.drawText(s,x,y,p);}
  @Override protected void onSizeChanged(int w,int h,int ow,int oh){arrange();}
  void layoutTexture(Tile t,int w,int h,int x,int y){FrameLayout.LayoutParams prior=(FrameLayout.LayoutParams)t.texture.getLayoutParams();if(prior!=null&&prior.width==w&&prior.height==h&&prior.leftMargin==x&&prior.topMargin==y)return;FrameLayout.LayoutParams lp=new FrameLayout.LayoutParams(w,h);lp.leftMargin=x;lp.topMargin=y;t.texture.setLayoutParams(lp);}
  void arrange(){describeSelection();ArrayList<Tile> show=new ArrayList<>();for(Tile t:tiles.values()){boolean visible=focused==null||focused.equals(t.id),wasActive=t.active;t.active=visible;if(wasActive!=visible)t.texture.setAlpha(visible?1f:0f);if(!visible){t.clearPicture();t.error="paused";if(wasActive){cancel(t.connection);cancel(t.stillConnection);}layoutTexture(t,1,1,0,0);}else{show.add(t);if(t.surface!=null&&!t.closed)startPreview(t,generation);if(t.surface!=null&&(t.thread==null||!t.thread.isAlive())&&!t.closed)startVideo(t,generation);}}if(show.isEmpty()){focused=null;invalidate();return;}int n=show.size(),cols=n==1?1:n<=4?2:3,rows=(n+cols-1)/cols;float sx=getWidth()/1920f,sy=getHeight()/1080f,w=(1824-(cols-1)*22f)/cols,h=(856-(rows-1)*22f)/rows;
   for(int i=0;i<n;i++){Tile t=show.get(i);float x=48+i%cols*(w+22),y=132+i/cols*(h+22);t.area.set(x,y,x+w,y+h);float scale=Math.min(w/t.width,(h-62)/t.height),vw=t.width*scale,vh=t.height*scale;t.video.set(x+(w-vw)/2,y+(h-62-vh)/2,x+(w+vw)/2,y+(h-62+vh)/2);layoutTexture(t,Math.max(1,Math.round(vw*sx)),Math.max(1,Math.round(vh*sy)),Math.round(t.video.left*sx),Math.round(t.video.top*sy));}invalidate();}
  @Override protected void onDraw(Canvas c){super.onDraw(c);c.save();c.scale(getWidth()/1920f,getHeight()/1080f);if(icon!=null)c.drawBitmap(icon,null,new RectF(42,28,110,96),p);text(c,"Friday",132,73,40,0xff222721);text(c,"全屋视野",295,72,26,0xff767d72);text(c,tiles.size()+" 个镜头",1720,70,22,0xff687060);for(Tile t:tiles.values())if(t.active){p.setColor(0xffe4e6e1);c.drawRoundRect(t.area,20,20,p);}c.restore();}
  void skeleton(Canvas c,Tile t){JSONObject obs=t.overlay;if(obs==null)return;long delta=t.renderedAt-obs.optLong("at_ms",0);if(delta<0||delta>750||serverNow()-obs.optLong("at_ms",0)>1500)return;JSONArray people=obs.optJSONArray("people");if(people==null)return;RectF r=t.video;c.save();c.clipRect(r);for(int i=0;i<Math.min(16,people.length());i++){JSONObject person=people.optJSONObject(i);if(person==null)continue;JSONObject box=person.optJSONObject("box"),j=person.optJSONObject("joints");int color=0xffb5f2bf;p.setColor(color);p.setStrokeWidth(2.5f);p.setStyle(Paint.Style.STROKE);if(box!=null){float x=r.left+(float)box.optDouble("x")*r.width(),y=r.top+(float)box.optDouble("y")*r.height(),w=(float)box.optDouble("w")*r.width(),h=(float)box.optDouble("h")*r.height();c.drawRoundRect(new RectF(x,y,x+w,y+h),7,7,p);String label=person.optString("name",person.optString("label","人物"));text(c,label,x+4,Math.max(r.top+22,y-8),21,Color.WHITE);p.setColor(color);p.setStyle(Paint.Style.STROKE);}
   if(j!=null){for(String[] bone:BONES){JSONObject a=j.optJSONObject(bone[0]),b=j.optJSONObject(bone[1]);if(a!=null&&b!=null)c.drawLine(r.left+(float)a.optDouble("x")*r.width(),r.top+(float)a.optDouble("y")*r.height(),r.left+(float)b.optDouble("x")*r.width(),r.top+(float)b.optDouble("y")*r.height(),p);}p.setStyle(Paint.Style.FILL);Iterator<String> names=j.keys();while(names.hasNext()){JSONObject a=j.optJSONObject(names.next());if(a!=null)c.drawCircle(r.left+(float)a.optDouble("x")*r.width(),r.top+(float)a.optDouble("y")*r.height(),3,p);}}
  }c.restore();p.setStyle(Paint.Style.FILL);}
  void toast(Canvas c,Tile t){JSONObject notice=t.toast;if(notice==null||serverNow()-notice.optLong("at_ms",0)>3000)return;String value=notice.optString("text");if(value.isEmpty())return;p.setTextSize(22);float width=Math.min(t.video.width()-32,p.measureText(value)+40);RectF r=new RectF(t.video.centerX()-width/2,t.video.bottom-58,t.video.centerX()+width/2,t.video.bottom-12);p.setColor(0xddf4f6f0);p.setStyle(Paint.Style.FILL);c.drawRoundRect(r,16,16,p);text(c,value,r.left+20,r.top+30,22,0xff263629);}
  @Override protected void dispatchDraw(Canvas c){super.dispatchDraw(c);c.save();c.scale(getWidth()/1920f,getHeight()/1080f);int index=0;for(Tile t:tiles.values()){if(!t.active){index++;continue;}long sourceAge=serverNow()-t.renderedAt;boolean fresh=sourceAge>=-100&&sourceAge<2000&&SystemClock.elapsedRealtime()-t.renderedLocal<2000&&t.renderedLocal>0&&SystemClock.elapsedRealtime()-statusAt<5000;if(fresh&&t.fallback&&t.picture!=null)c.drawBitmap(t.picture,null,t.video,p);if(!fresh){p.setColor(0xffe4e6e1);c.drawRect(t.video,p);waiting(c,t);}else if(debug){skeleton(c,t);toast(c,t);}p.setColor(0xfffdfdfb);c.drawRect(t.area.left,t.area.bottom-62,t.area.right,t.area.bottom,p);text(c,t.room.isEmpty()?t.name:t.room,t.area.left+24,t.area.bottom-22,25,0xff252824);if(focused!=null||index==selected){p.setStyle(Paint.Style.STROKE);p.setStrokeWidth(3);p.setColor(0xff687466);c.drawRoundRect(t.area,20,20,p);p.setStyle(Paint.Style.FILL);}index++;}text(c,focused==null?"方向键选择 · 确认键放大 · 菜单键调试":"确认键或返回键 · 返回全屋",48,1040,21,0xff596356);text(c,debug?"调试已开启":"本地实时",1740,1040,21,0xff596356);c.restore();postInvalidateDelayed(33);}
 }
}
