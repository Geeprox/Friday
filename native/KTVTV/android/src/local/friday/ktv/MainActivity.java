package local.friday.ktv;

import android.Manifest;
import android.animation.ValueAnimator;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.*;
import android.graphics.drawable.*;
import android.media.*;
import android.hardware.usb.*;
import android.os.*;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.*;
import android.widget.*;
import org.json.*;
import local.friday.audio.WetProfiles;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** Audience display: room QR, then synchronized lyrics and optional silent MV. */
public final class MainActivity extends Activity implements TextureView.SurfaceTextureListener {
    /** User choice survives pauses/background stops. Runtime permission, route
     * and fault gates never masquerade as a new user mute command. */
    static final class MicrophoneIntent {
        boolean muted,blocked,controlsReady;
        private boolean permissionReturn,resumeAfterPermission;
        void awaitControls(){controlsReady=false;permissionReturn=false;resumeAfterPermission=false;}
        void leaveForeground(){controlsReady=false;}
        void choose(boolean value){muted=value;blocked=false;}
        void restore(boolean value){muted=value;}
        void block(){blocked=true;}
        boolean requested(){return controlsReady&&!muted&&!blocked;}
        boolean mayRun(boolean foreground,boolean roomActive,boolean paused,boolean permission,boolean connected,int gain){
            return requested()&&foreground&&roomActive&&!paused&&permission&&connected&&gain>0;
        }
        void permissionRequested(boolean resume){permissionReturn=true;resumeAfterPermission=resume;}
        boolean takePermissionResume(){boolean resume=permissionReturn&&resumeAfterPermission;permissionReturn=false;resumeAfterPermission=false;return resume;}
    }
    private final Handler ui=new Handler(Looper.getMainLooper());
    private final ExecutorService network=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(8));
    private final ExecutorService mediaNetwork=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(4));
    private long roomEpoch;
    private volatile long controlEpoch;
    private JSONObject pendingPlay;
    private String currentFilesJob="";
    private final ExecutorService controlNetwork=Executors.newSingleThreadExecutor();
    private final ExecutorService backdropNetwork=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(1));
    private final SongBackdrop songBackdrop=new SongBackdrop();
    private final ExecutorService controlActions=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(16));
    private final LocalControlIntent localOriginal=new LocalControlIntent(),localMute=new LocalControlIntent();
    private final VocalEffectsIntent localVocal=new VocalEffectsIntent();
    private VocalEffectsController vocalEffects;
    private AlertDialog vocalDialog;private TextView vocalStatus;
    private long vocalRequest,vocalSyncScheduled;private String vocalSyncMessage="";
    private boolean controlBusy,protocolReady;
    private File currentOriginal,currentInstrumental;
    private final AtomicInteger generation=new AtomicInteger();
    private final ExecutorService preloader=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(1));
    private AtomicBoolean prefetchCancel=new AtomicBoolean();
    private boolean prefetchBusy;private String prefetchId="",prefetchedId="";private long prefetchAttempt;
    private AtomicBoolean transferCancel=new AtomicBoolean();
    private final ArrayList<JSONObject> jobs=new ArrayList<>();
    private final ArrayList<TextView> lyricViews=new ArrayList<>();
    private final Set<String> played=new LinkedHashSet<>();
    private SharedPreferences preferences;
    private final String tvInstance=UUID.randomUUID().toString();
    private final CoordinatorSession coordinatorSession=new CoordinatorSession();
    private final CoordinatorStatus coordinatorStatus=new CoordinatorStatus();
    private PlaybackTimeline playbackTimeline;
    private volatile long readinessAt;
    private long coordinatorReceivedAt;
    private boolean coordinatorVisible;
    private volatile KtvClient client;
    private AudioEngine engine;
    private AudioBackendSelection backendSelection;
    private NativeVendorContract.Result systemContract;
    private final ExecutorService audioLifecycle=Executors.newSingleThreadExecutor();
    private final CompatMuteReadback compatMute=new CompatMuteReadback();
    private long engineEpoch,compatSessionSeek;
    private boolean engineReleased=true,backendRestartShown;
    private VendorMicClient vendorMic;
    private SoundEffectPlayer soundEffects;
    private PartyEffectsView partyEffects;
    private long shownSfx;
    private boolean vendorMode;
    private int vendorGain=20;
    private String vendorProblem="";
    private AudioManager audio;
    private FrameLayout page;
    private ImageView background,smallQr,bigQr,requesterAvatar;
    private TextureView picture;
    private LinearLayout idle,lyrics,byline;
    private ScrollView lyricScroll;
    private LinearLayout mvLyrics;
    private TextView mvPrevious,mvCurrent,mvNext;
    private int countdownLine=-1,countdownValue;
    private final LyricCue lyricCue=new LyricCue();
    private long[] lyricStarts=new long[0];
    private long[] entryStarts;
    private TextView headline,notice,trackTitle,requester,nextSong,clock,connectionBadge;
    private ProgressBar progress;
    private MediaPlayer video;
    private Surface videoSurface;
    private File videoFile;
    private Bitmap atlas,qrBitmap;
    private JSONArray lines=new JSONArray();
    private JSONObject current,pendingPermission;
    private String theme="content",currentId="";
    private volatile boolean foreground;
    private boolean pollBusy,loading,paused=true,autoRoom=true,videoReady,videoSeeking,original;
    private final MicrophoneIntent micIntent=new MicrophoneIntent();
    private Button warmupButton;private final QueuePlaybackIntent queueIntent=new QueuePlaybackIntent();
    private boolean warmup,remoteBusy;
    private final Map<String,JSONObject> warmupJobs=new LinkedHashMap<>();
    private final Map<String,Long> warmupFailed=new HashMap<>(),lyricsWait=new HashMap<>();
    private final Map<String,JSONArray> preparedLyrics=new HashMap<>();
    private final Map<String,long[]> preparedEntries=new HashMap<>();
    private boolean queueLyricsBusy;
    private final WarmupShuffle warmupShuffle=new WarmupShuffle();
    private final RemoteShortcuts remoteShortcuts=new RemoteShortcuts();
    private String roomId="";
    private long lastCommand,appliedRevision=-1,lastMicRevision=-1;
    private boolean controlsInitialized;
    private long nextConnectAttempt;private int connectionFailures;
    private long benchmarkMicros=-1;private int installedVersion;
    private long resumeFrame,videoOffsetMs,videoDurationMs;
    private final VideoSync videoClock=new VideoSync();
    private long videoSeekCount,videoBufferEvents,videoSurfaceFrames,videoPositionMs,videoTargetMs,seekResultMs,videoPrepareCount,routeRestartCount;
    private float videoSpeed=1;private boolean videoSpeedSupported=true;
    private int activeLine=-1,ticks,accent=0xfffa6670;
    private volatile boolean lyricsBusy;
    private long lyricFetchAt;private int lyricRetries;
    private static final int WHITE=0xfff8f7f2,MUTED=0xffd2d7d2;

    @Override public void onCreate(Bundle state){
        super.onCreate(state);
        try{installedVersion=getPackageManager().getPackageInfo(getPackageName(),0).versionCode;}catch(PackageManager.NameNotFoundException ignored){}
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN|View.SYSTEM_UI_FLAG_HIDE_NAVIGATION|View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        preferences=getSharedPreferences("ktv-private",MODE_PRIVATE);
        applyProvisionedConnection();
        playbackTimeline=new PlaybackTimeline(preferences.getString("playback-epoch",""),preferences.getString("playback-song",""),preferences.getString("playback-media",""));
        played.addAll(preferences.getStringSet("played",new HashSet<String>()));
        audio=(AudioManager)getSystemService(AUDIO_SERVICE);
        systemContract=NativeVendorContract.inspect(this);
        boolean legacyCandidate="mtktv".equals(Build.HARDWARE)&&Build.VERSION.SDK_INT>=34;
        boolean cleanupPending=preferences.getBoolean("audio-backend-owner-cleanup",false);
        backendSelection=new AudioBackendSelection(preferences.getString("audio-backend",""),legacyCandidate,systemAvailable(),cleanupPending);
        preferences.edit().remove("audio-backend").remove("audio-backend-start-pending").apply();
        vendorMode=backendSelection.system();
        if(vendorMode||(cleanupPending&&VendorMicClient.available(this)))vendorMic=new VendorMicClient(this,this::vendorMicChanged);
        engine=createEngine(!AudioBackendSelection.COMPAT.equals(backendSelection.mode()));
        micIntent.choose(true);if(!backendSelection.playbackAllowed())queueIntent.pause();
        soundEffects=new SoundEffectPlayer(this);
        if(vendorMode)vocalEffects=new VocalEffectsController(this,this::vocalEffectsChanged,true);
        build();audio.registerAudioDeviceCallback(devices,ui);
        try(InputStream in=getAssets().open("avatars.png")){atlas=BitmapFactory.decodeStream(in);}catch(Exception ignored){}
        connect();
    }
    private AudioEngine createEngine(boolean outputOnly){
        final long epoch=++engineEpoch;
        AudioEngine created=new AudioEngine(this,(code,natural)->ui.post(()->{if(epoch==engineEpoch){engineReleased=!"input_stop_timeout".equals(code)&&!"audio_stopping".equals(code);ended(code,natural);}}),outputOnly);
        String preset=preferences.getString("vocal-preset","dry");created.mixer.setVocalPreset(WetProfiles.id(preset)<0?"dry":preset);created.mixer.micMuted=true;
        engineReleased=true;compatMute.session(0);return created;
    }
    private boolean systemAvailable(){return VendorMicClient.available(this)&&systemContract!=null&&systemContract.ready()&&systemContract.silentStart()&&systemContract.cacheOnly();}
    private boolean compatSelected(){return backendSelection!=null&&backendSelection.selected()&&!vendorMode&&AudioBackendSelection.COMPAT.equals(backendSelection.mode());}
    private boolean requestedMic(){return backendSelection!=null&&backendSelection.microphoneAllowed()&&micIntent.requested();}
    private boolean userPlayback(){
        if(!refreshAutomaticBackend()||!backendSelection.userPlay()){notice.setText("正在确认麦克风已关闭，请稍后再按播放。");return false;}
        return true;
    }
    private String voiceUnavailableMessage(){return backendSelection.switching()?"麦克风正在关闭，请稍后重试。":"麦克风暂不可用，仍可扫码点歌和播放伴奏。";}
    private boolean refreshAutomaticBackend(){
        if(backendSelection==null||backendRestartShown||backendSelection.switching())return false;
        systemContract=NativeVendorContract.inspect(this);boolean safe=systemAvailable();
        if(backendSelection.updateNeeded(safe)){switchAudioBackend(safe);return false;}
        return true;
    }
    private boolean vendorReleased(){
        if(vendorMic==null)return true;JSONObject raw=vendorMic.telemetry(),state=vendorMic.state();int hal=raw.optInt("hal_status",-999);
        return vendorMic.readbackAgeMs()<=750&&raw.optLong("guard_pid")>0&&state.optBoolean("stop_verified")&&!state.optBoolean("stopping")
            &&!raw.optBoolean("owns_unresolved_lease")&&!raw.optBoolean("carrier_resources_pending")&&(hal==-1||hal==2);
    }
    private void switchAudioBackend(boolean systemSafe){
        final long ticket=backendSelection.beginAutomatic(systemSafe);
        final AudioEngine prior=engine;final String preset=requestedVocalKey();final float musicGain=prior.mixer.musicGain,a=prior.mixer.gainA,b=prior.mixer.gainB;
        preferences.edit().putBoolean("audio-backend-owner-cleanup",true).apply();
        controlEpoch++;pendingPlay=null;localMute.clear();queueIntent.pause();backendSelection.holdMicrophone();micIntent.choose(true);prior.mixer.micMuted=true;
        stopAudio(true);if(vocalEffects!=null)vocalEffects.suspend(6);if(vendorMic!=null){vendorMic.setForeground(true);vendorMic.stop();}
        notice.setText("正在暂停播放，麦克风保持静音…");
        try{audioLifecycle.execute(()->{boolean released=false;try{prior.awaitStopped();released=prior.released();}catch(Exception ignored){}final boolean stopped=released;ui.post(()->finishAutomaticBackend(ticket,prior,preset,musicGain,a,b,stopped,SystemClock.elapsedRealtime()+5000));});}
        catch(RejectedExecutionException ignored){backendSelection.failAutomatic(ticket);backendRestartRequired();}
    }
    private void finishAutomaticBackend(long ticket,AudioEngine prior,String preset,float music,float a,float b,boolean stopped,long deadline){
        if(isFinishing()||prior!=engine||!backendSelection.currentUpdate(ticket))return;
        boolean owners=vendorReleased()&&(vocalEffects==null||vocalEffects.released());
        if(!stopped||!engineReleased||!prior.released()||!owners){if(stopped&&SystemClock.elapsedRealtime()<deadline){ui.postDelayed(()->finishAutomaticBackend(ticket,prior,preset,music,a,b,true,deadline),100);return;}backendSelection.failAutomatic(ticket);backendRestartRequired();return;}
        if(!backendSelection.completeAutomatic(ticket,true,true))return;
        if(vocalEffects!=null){vocalEffects.close();vocalEffects=null;}if(vendorMic!=null){vendorMic.close();vendorMic=null;}
        vendorMode=backendSelection.system();engine=createEngine(!AudioBackendSelection.COMPAT.equals(backendSelection.mode()));engine.mixer.musicGain=music;engine.mixer.gainA=a;engine.mixer.gainB=b;engine.mixer.setVocalPreset(preset);
        if(vendorMode){vendorMic=new VendorMicClient(this,this::vendorMicChanged);vocalEffects=new VocalEffectsController(this,this::vocalEffectsChanged,true);vocalEffects.select(preset);if(foreground)vendorMic.setForeground(true);}
        preferences.edit().putBoolean("audio-backend-owner-cleanup",false).apply();readinessAt=0;paused=true;showIdle();warmupButton.setVisibility(View.VISIBLE);warmupButton.setText(current!=null?"继续播放":"播放暖场");warmupButton.requestFocus();notice.setText(backendSelection.selected()?"已准备好。按播放继续，麦克风保持静音。":voiceUnavailableMessage());
    }
    private void backendRestartRequired(){
        preferences.edit().putBoolean("audio-backend-owner-cleanup",true).apply();
        if(!isFinishing()&&!backendRestartShown){backendRestartShown=true;new AlertDialog.Builder(this).setTitle("麦克风正在关闭").setMessage("播放已暂停，麦克风保持关闭。请退出并重新打开应用后重试。").setPositiveButton("退出应用",(d,w)->finishAndRemoveTask()).setCancelable(false).show();}
    }
    private void applyProvisionedConnection(){
        Set<String> seen=new HashSet<>(preferences.getStringSet("provisioned-profiles",Collections.emptySet()));
        boolean migrate=ProvisionedConnection.shouldApply(SetupDefaults.ROLE,SetupDefaults.PROFILE,SetupDefaults.ORIGIN,SetupDefaults.KEY,seen);
        boolean seed=ProvisionedConnection.shouldSeedLegacy(SetupDefaults.ROLE,SetupDefaults.PROFILE,SetupDefaults.ORIGIN,SetupDefaults.KEY,preferences.contains("origin"),preferences.contains("key"));
        if(!migrate&&!seed)return;
        if(migrate)seen.add(SetupDefaults.PROFILE);
        preferences.edit().putString("origin",SetupDefaults.ORIGIN).putString("key",SetupDefaults.KEY).putStringSet("provisioned-profiles",seen).apply();
    }
    private int dp(float value){return Math.round(value*getResources().getDisplayMetrics().density);}
    private TextView text(String value,int size,int color){TextView v=new TextView(this);v.setText(value);v.setTextSize(size);v.setTextColor(color);v.setIncludeFontPadding(false);v.setLineSpacing(0,1.25f);return v;}
    private FrameLayout.LayoutParams box(int width,int height,int gravity,int x,int y){FrameLayout.LayoutParams p=new FrameLayout.LayoutParams(width<0?width:dp(width),height<0?height:dp(height),gravity);p.setMargins(dp(x),dp(y),dp(x),dp(y));return p;}
    private GradientDrawable surface(int color,int radius){GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(radius));return d;}
    private void build(){
        page=new FrameLayout(this);page.setBackgroundColor(0xff0c1110);setContentView(page);
        background=new ImageView(this);background.setScaleType(ImageView.ScaleType.CENTER_CROP);page.addView(background,new FrameLayout.LayoutParams(-1,-1));
        picture=new TextureView(this);picture.setSurfaceTextureListener(this);picture.setAlpha(0);page.addView(picture,new FrameLayout.LayoutParams(-1,-1));
        page.addView(new View(this){private final Paint paint=new Paint();@Override protected void onDraw(Canvas c){paint.setShader(new LinearGradient(0,0,getWidth(),0,new int[]{0xb8000000,0xb8000000,0x08000000},new float[]{0,.68f,1},Shader.TileMode.CLAMP));c.drawRect(0,0,getWidth(),getHeight(),paint);}},new FrameLayout.LayoutParams(-1,-1));
        TextView brand=text("Friday KTV",20,WHITE);brand.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));brand.setContentDescription("Friday KTV，长按打开房间维护");brand.setOnLongClickListener(v->{maintenance();return true;});page.addView(brand,box(-2,-2,Gravity.TOP|Gravity.START,32,27));
        connectionBadge=text("",13,MUTED);connectionBadge.setMaxLines(2);connectionBadge.setGravity(Gravity.END);connectionBadge.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);FrameLayout.LayoutParams connection=box(-1,-2,Gravity.TOP|Gravity.END,150,30);connection.leftMargin=dp(230);page.addView(connectionBadge,connection);
        smallQr=new ImageView(this);smallQr.setPadding(dp(6),dp(6),dp(6),dp(6));smallQr.setBackground(surface(0xffffffff,12));smallQr.setContentDescription("扫码点歌，按确定放大二维码");smallQr.setFocusable(true);smallQr.setClickable(true);smallQr.setOnClickListener(v->enlargeQr());smallQr.setOnFocusChangeListener((v,focus)->{GradientDrawable d=surface(0xffffffff,12);if(focus)d.setStroke(dp(4),accent);smallQr.setBackground(d);});smallQr.setVisibility(View.INVISIBLE);page.addView(smallQr,box(100,100,Gravity.TOP|Gravity.END,28,25));
        idle=new LinearLayout(this);idle.setOrientation(LinearLayout.VERTICAL);idle.setGravity(Gravity.CENTER);idle.setPadding(dp(20),dp(4),dp(20),dp(4));
        headline=text("",36,WHITE);headline.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));headline.setGravity(Gravity.CENTER);idle.addView(headline);
        bigQr=new ImageView(this);bigQr.setPadding(dp(12),dp(12),dp(12),dp(12));bigQr.setBackground(surface(0xffffffff,18));bigQr.setContentDescription("本场点歌二维码");bigQr.setVisibility(View.GONE);
        int heightDp=(int)(getResources().getDisplayMetrics().heightPixels/getResources().getDisplayMetrics().density);int qrSize=Math.min(226,Math.max(150,heightDp-285));LinearLayout.LayoutParams qr=new LinearLayout.LayoutParams(dp(qrSize),dp(qrSize));qr.topMargin=dp(22);qr.bottomMargin=dp(16);idle.addView(bigQr,qr);
        warmupButton=new Button(this);warmupButton.setText("播放暖场");warmupButton.setTextSize(17);warmupButton.setAllCaps(false);warmupButton.setMinWidth(dp(160));warmupButton.setPadding(dp(24),0,dp(24),0);warmupButton.setFocusable(true);warmupButton.setStateListAnimator(null);warmupButton.setContentDescription("随机播放已准备好的暖场歌曲");warmupButton.setTextColor(WHITE);warmupButton.setBackground(surface(0xcc1c2421,24));warmupButton.setOnFocusChangeListener((v,focus)->{warmupButton.setTextColor(focus?0xff101612:WHITE);GradientDrawable d=surface(focus?WHITE:0xcc1c2421,24);d.setStroke(dp(2),focus?WHITE:0x77ffffff);warmupButton.setBackground(d);});warmupButton.setOnClickListener(v->toggleWarmup());page.addView(warmupButton,box(-2,48,Gravity.BOTTOM|Gravity.CENTER_HORIZONTAL,32,70));
        notice=text("正在连接房间…",16,MUTED);notice.setGravity(Gravity.CENTER);notice.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);idle.addView(notice);page.addView(idle,box(-1,-2,Gravity.CENTER,140,60));
        lyricScroll=new ScrollView(this);lyricScroll.setFillViewport(false);lyricScroll.setVerticalScrollBarEnabled(false);lyricScroll.setOverScrollMode(View.OVER_SCROLL_NEVER);lyricScroll.setClipToPadding(false);lyricScroll.setVisibility(View.GONE);
        lyrics=new LinearLayout(this);lyrics.setOrientation(LinearLayout.VERTICAL);lyricScroll.addView(lyrics,new ScrollView.LayoutParams(-1,-2));FrameLayout.LayoutParams lyricArea=box(-1,-1,Gravity.TOP|Gravity.START,64,94);lyricArea.rightMargin=dp(190);lyricArea.bottomMargin=dp(104);page.addView(lyricScroll,lyricArea);
        mvLyrics=new LinearLayout(this);mvLyrics.setOrientation(LinearLayout.VERTICAL);mvLyrics.setPadding(dp(64),dp(32),dp(64),dp(20));mvLyrics.setBackground(new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,new int[]{0x00000000,0x99000000}));mvLyrics.setVisibility(View.GONE);
        mvPrevious=text("",26,0xffe2e2e2);mvPrevious.setPadding(0,0,0,dp(8));mvPrevious.setShadowLayer(dp(3),0,dp(1),0xff000000);mvLyrics.addView(mvPrevious);
        mvCurrent=text("",34,WHITE);mvCurrent.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));mvCurrent.setShadowLayer(dp(3),0,dp(1),0xff000000);mvLyrics.addView(mvCurrent);
        mvNext=text("",22,0xffdddddd);mvNext.setPadding(0,dp(9),0,0);mvNext.setShadowLayer(dp(3),0,dp(1),0xff000000);mvLyrics.addView(mvNext);FrameLayout.LayoutParams cues=new FrameLayout.LayoutParams(-1,-2,Gravity.BOTTOM);cues.bottomMargin=dp(98);page.addView(mvLyrics,cues);
        trackTitle=text("",19,WHITE);trackTitle.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));FrameLayout.LayoutParams title=box(-1,-2,Gravity.TOP|Gravity.START,32,62);title.rightMargin=dp(170);page.addView(trackTitle,title);
        LinearLayout footer=new LinearLayout(this);footer.setOrientation(LinearLayout.VERTICAL);footer.setPadding(dp(32),dp(12),dp(32),dp(20));footer.setBackground(new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,new int[]{0x00000000,0xd9000000}));
        byline=new LinearLayout(this);byline.setGravity(Gravity.CENTER_VERTICAL);requesterAvatar=new ImageView(this);byline.addView(requesterAvatar,new LinearLayout.LayoutParams(dp(30),dp(30)));requester=text("",14,MUTED);LinearLayout.LayoutParams name=new LinearLayout.LayoutParams(0,-2,1);name.setMarginStart(dp(10));byline.addView(requester,name);nextSong=text("",13,MUTED);nextSong.setGravity(Gravity.END);nextSong.setMaxLines(2);byline.addView(nextSong,new LinearLayout.LayoutParams(0,-2,1));footer.addView(byline);
        progress=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);progress.setMax(10000);progress.setProgressTintList(android.content.res.ColorStateList.valueOf(WHITE));progress.setProgressBackgroundTintList(android.content.res.ColorStateList.valueOf(0x55ffffff));LinearLayout.LayoutParams bar=new LinearLayout.LayoutParams(-1,dp(3));bar.topMargin=dp(12);footer.addView(progress,bar);
        clock=text("",11,MUTED);clock.setFontFeatureSettings("tnum");LinearLayout.LayoutParams time=new LinearLayout.LayoutParams(-1,-2);time.topMargin=dp(7);footer.addView(clock,time);page.addView(footer,new FrameLayout.LayoutParams(-1,-2,Gravity.BOTTOM));byline.setVisibility(View.GONE);progress.setVisibility(View.INVISIBLE);setTheme("content");
        partyEffects=new PartyEffectsView(this);page.addView(partyEffects,new FrameLayout.LayoutParams(-1,-1));
    }
    private void setTheme(String value){
        if(!Arrays.asList("content","festival","live").contains(value))return;
        if(background.getDrawable()!=null && theme.equals(value))return;theme=value;accent=theme.equals("festival")?0xffeee353:0xfffa6670;
        try(InputStream in=getAssets().open(theme+".png")){background.setImageBitmap(BitmapFactory.decodeStream(in));}catch(Exception ignored){}
        if(activeLine>=0 && activeLine<lyricViews.size())lyricViews.get(activeLine).setTextColor(theme.equals("festival")?accent:WHITE);if(smallQr.hasFocus()){GradientDrawable frame=surface(0xffffffff,12);frame.setStroke(dp(4),accent);smallQr.setBackground(frame);}
    }
    private void songBackground(JSONObject job){
        final String song=job==null?"":job.optString("id","");
        if(songBackdrop.select(song)){String previous=theme;theme="";setTheme(previous);}
        if(job==null||client==null)return;
        final JSONObject descriptor=job.optJSONObject("background");
        if(descriptor==null||!descriptor.optString("status").equals("ready"))return;
        final String hash=descriptor.optString("sha256");final long request=songBackdrop.begin(song,hash);if(request==0)return;
        final KtvClient owner=client;final int version=generation.get();final AtomicBoolean cancelled=transferCancel;
        try{backdropNetwork.execute(()->{try{
            File image=owner.downloadBackground(descriptor,cancelled);
            BitmapFactory.Options options=new BitmapFactory.Options();options.inJustDecodeBounds=true;BitmapFactory.decodeFile(image.getAbsolutePath(),options);
            options.inSampleSize=1;while(options.outWidth/options.inSampleSize>1920||options.outHeight/options.inSampleSize>1080)options.inSampleSize*=2;
            options.inJustDecodeBounds=false;final Bitmap bitmap=BitmapFactory.decodeFile(image.getAbsolutePath(),options);if(bitmap==null)throw new IOException("background_unavailable");
            ui.post(()->{if(foreground&&version==generation.get()&&current!=null&&currentId.equals(song)&&songBackdrop.accept(song,hash,request))background.setImageBitmap(bitmap);else{songBackdrop.fail(request);bitmap.recycle();}});
        }catch(Exception|OutOfMemoryError ignored){ui.post(()->songBackdrop.fail(request));}});}catch(RejectedExecutionException ignored){songBackdrop.fail(request);}
    }
    private void connect(){
        prefetchCancel.set(true);
        coordinatorSession.requestOpen();readinessAt=0;coordinatorVisible=false;coordinatorStatus.disconnected(false);if(connectionBadge!=null)connectionBadge.setText("");
        controlEpoch++;roomEpoch++;pendingPlay=null;stopAudio(true);micIntent.awaitControls();engine.mixer.micMuted=true;protocolReady=false;roomId="";currentFilesJob="";currentOriginal=currentInstrumental=null;client=null;current=null;currentId="";jobs.clear();qrBitmap=null;showIdle();
        String origin=preferences.getString("origin",SetupDefaults.ORIGIN),key=preferences.getString("key",SetupDefaults.KEY);
        try{client=new KtvClient(origin,key,new File(getCacheDir(),"audio"));client.instance(tvInstance);notice.setText("正在连接房间…");poll();}
        catch(Exception e){headline.setText("连接你的房间");notice.setText("按遥控器菜单键设置家庭服务。");}
    }
    private void poll(){
        if(client==null || pollBusy || !foreground || SystemClock.elapsedRealtime()<nextConnectAttempt)return;pollBusy=true;final KtvClient c=client;final long epoch=roomEpoch;
        if(!background(()->{try{
            JSONObject state=c.json("/v1/status",null);int protocol=state.optInt("protocol");if(protocol!=1&&!(protocol==2&&state.optBoolean("coordinator")))throw new IOException();c.observeStatus(state);
            if(state.optBoolean("simulated")){ui.post(()->{if(c==client)notice.setText("模拟房间 · 测试音源");});return;}
            JSONObject party=c.json("/v1/party",null);
            String startRequest=coordinatorSession.claimOpen(foreground,autoRoom,SystemClock.elapsedRealtime());
            if(!startRequest.isEmpty()){
                if(c.coordinator()||!party.optBoolean("active"))party=c.json("/v1/party",c.coordinator()?new JSONObject().put("instance",tvInstance).put("request_id",startRequest):new JSONObject());
                if(party.optBoolean("active"))coordinatorSession.acknowledged(startRequest);
            }
            // A new instance must claim the explicit opening before it may
            // renew readiness for a room previously owned by another instance.
            if(c.coordinator()&&!coordinatorSession.pending()&&SystemClock.elapsedRealtime()-readinessAt>=3000){JSONObject ready=c.json("/v1/tv/readiness",readiness(c));state.put("readiness",ready);readinessAt=SystemClock.elapsedRealtime();}
            JSONObject room=party;byte[] qr=party.optBoolean("active") && (qrBitmap==null || !roomId.equals(party.optString("room_id")) || ticks%300<20)?roomQr(c):null;
            JSONObject response=c.json("/v1/jobs",null);if(response.optJSONArray("jobs")==null)throw new IOException("invalid_jobs");connectionFailures=0;nextConnectAttempt=0;try{c.json("/v1/tv/diagnostics",telemetry());}catch(Exception ignored){}ui.post(()->{if(!foreground || c!=client||epoch!=roomEpoch||(!roomId.isEmpty()&&!roomId.equals(room.optString("room_id"))))return;
                protocolReady=true;coordinatorVisible=c.coordinator();
                if(coordinatorVisible){JSONObject readiness=room.optJSONObject("readiness");if(readiness==null)readiness=state.optJSONObject("readiness");observeReadiness(room.optBoolean("active"),readiness);}
                setTheme(room.optString("theme","content"));if(qr!=null){qrBitmap=BitmapFactory.decodeByteArray(qr,0,qr.length);smallQr.setImageBitmap(qrBitmap);bigQr.setImageBitmap(qrBitmap);smallQr.setVisibility(View.VISIBLE);}
                if(!room.optBoolean("active")){smallQr.setVisibility(View.INVISIBLE);bigQr.setVisibility(View.GONE);}
                JSONArray order=room.optJSONArray("order");Set<String> allowed=new HashSet<>();if(order!=null)for(int i=0;i<order.length();i++)allowed.add(order.optString(i));
                jobs.clear();JSONArray incoming=response.optJSONArray("jobs"),values=new JSONArray();if(incoming!=null)for(int i=0;i<incoming.length();i++){JSONObject job=incoming.optJSONObject(i);if(job!=null&&c.manifestValid(job))values.put(job);}refreshWarmup(values);for(int i=0;i<values.length();i++){JSONObject job=values.optJSONObject(i);rememberJob(job);if(order!=null&&allowed.contains(job.optString("id")))jobs.add(job);}
                if(current!=null)for(JSONObject job:jobs)if(job.optString("id").equals(currentId)){songBackground(job);break;}
                if(order!=null){final JSONArray sorted=order;jobs.sort((a,b)->Integer.compare(orderIndex(sorted,a.optString("id")),orderIndex(sorted,b.optString("id"))));}runPendingPlay();prepareFirstLyrics();prepareWarmupLyrics();
                refreshQueuePlayback();protectCache();updateNext();prefetchNext();
            });
        }catch(Exception e){connectionFailures=Math.min(4,connectionFailures+1);nextConnectAttempt=SystemClock.elapsedRealtime()+(1L<<connectionFailures)*500;ui.post(()->{if(!foreground || c!=client)return;if(coordinatorVisible){coordinatorStatus.disconnected(engine.running);connectionBadge.setText(coordinatorStatus.text());}if(current==null)notice.setText("正在重新连接房间…");});}finally{pollBusy=false;}}))pollBusy=false;
    }
    private byte[] roomQr(KtvClient c){try{return c.image("/v1/party/qr");}catch(Exception notReady){return null;}}
    /** Read-only capability checks and an isolated cache probe; no AudioRecord,
     * AudioTrack instance, model, microphone or output is started here. */
    private JSONObject readiness(KtvClient c)throws JSONException{
        boolean output=false;int speakers=0;
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(d.isSink()&&d.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)speakers++;
        boolean playback=false;try{playback=AudioTrack.getMinBufferSize(48000,AudioFormat.CHANNEL_OUT_STEREO,AudioFormat.ENCODING_PCM_16BIT)>0
            &&new MediaCodecList(MediaCodecList.ALL_CODECS).findDecoderForFormat(MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC,1280,720))!=null;}catch(RuntimeException ignored){}
        output=speakers==1;
        JSONObject guard=vendorMic==null?new JSONObject():vendorMic.telemetry();
        boolean preflight=vendorMic!=null&&vendorMic.readbackAgeMs()<=1500&&guard.optBoolean("foreground_ready")&&guard.optBoolean("independent_uid")
            &&guard.optBoolean("strict_bridge_available")&&guard.optBoolean("observed_target_valid")&&"1.1.3".equals(guard.optString("version"))&&"single".equals(guard.optString("play_model"));
        AudioDeviceInfo mic=microphone();boolean softwareFormat=false;if(mic!=null){boolean channels=false,rate=false,pcm=false;for(int value:mic.getChannelCounts())if(value==1||value==2)channels=true;for(int value:mic.getSampleRates())if(value==48000)rate=true;for(int value:mic.getEncodings())if(value==AudioFormat.ENCODING_PCM_16BIT)pcm=true;softwareFormat=channels&&rate&&pcm;}
        boolean protectedAudio=vendorMode?systemAvailable()&&vendorMic!=null&&vendorMic.permissionReady()&&vocalPermission()&&mic!=null&&output&&preflight
            :compatSelected()&&vocalPermission()&&softwareFormat&&output;
        return new JSONObject().put("instance",tvInstance).put("protocol",1).put("playback",playback&&output).put("audio",protectedAudio).put("media",c.storageReady());
    }
    private void observeReadiness(boolean active,JSONObject ready){
        coordinatorReceivedAt=SystemClock.elapsedRealtime();
        Map<String,Boolean> gates=new HashMap<>();JSONObject values=ready==null?null:ready.optJSONObject("gates");
        if(values!=null)for(String key:CoordinatorStatus.GATES)gates.put(key,Boolean.TRUE.equals(values.opt(key)));
        coordinatorStatus.observe(active,!coordinatorSession.pending()&&ready!=null&&Boolean.TRUE.equals(ready.opt("ready")),gates);connectionBadge.setText(coordinatorStatus.text());
    }
    private void refreshCoordinatorStatus(){if(coordinatorVisible&&coordinatorStatus.expire(SystemClock.elapsedRealtime()-coordinatorReceivedAt,engine.running))connectionBadge.setText(coordinatorStatus.text());}
    private int orderIndex(JSONArray values,String id){for(int i=0;i<values.length();i++)if(id.equals(values.optString(i)))return i;return Integer.MAX_VALUE;}
    private final Runnable controlTick=new Runnable(){public void run(){
        if(!foreground)return;
        if(client!=null&&autoRoom&&protocolReady&&!controlBusy){
            controlBusy=true;final KtvClient c=client;final long epoch=controlEpoch;final JSONObject report=playbackReport();
            try{controlNetwork.execute(()->{try{
                JSONObject room=c.controlJson("/v1/party",null);
                ui.post(()->{if(foreground&&autoRoom&&epoch==controlEpoch&&c==client&&room.optBoolean("active"))applyRoom(room);});
                if(!report.optString("room_id").isEmpty()){
                    if(report.optString("current").equals(currentId))report.put("position_ms",Math.min(1800000,Math.max(0,engine.playheadFrames/48)));
                    c.controlJson("/v1/tv/playback",report);
                }
            }catch(Exception ignored){}finally{ui.post(()->controlBusy=false);}});}catch(RejectedExecutionException ignored){controlBusy=false;}
        }
        ui.postDelayed(this,150);
    }};
    private void switchMusic(){
        if(current==null||!engine.running)return;
        File file=original?currentOriginal:currentInstrumental;
        if(file!=null)engine.switchBacking(file);
    }

    private void setMicMuted(boolean value){setMicMuted(value,false,true);}
    private void setMicMuted(boolean value,boolean initialize,boolean userCommand){
        setMicMuted(value,initialize,userCommand,-1);
    }
    private void setMicMuted(boolean value,boolean initialize,boolean userCommand,long remoteRevision){
        if(userCommand&&!value&&(!refreshAutomaticBackend()||!backendSelection.selected()||!backendSelection.playbackAllowed())){
            if(remoteRevision>=0)backendSelection.remoteMicrophone(false,remoteRevision,true);
            backendSelection.holdMicrophone();micIntent.choose(true);engine.mixer.micMuted=true;reconcileMicHardware(false);
            notice.setText(backendSelection.selected()?"请先按播放，再开启麦克风。":voiceUnavailableMessage());return;
        }
        boolean previouslyRequested=requestedMic();
        if(remoteRevision>=0)backendSelection.remoteMicrophone(value,remoteRevision,userCommand);
        else if(userCommand)backendSelection.localMicrophone(value);
        compatMute.request(value||backendSelection.microphoneHeld(),engine.mixer.voiceGateState());
        if(userCommand&&!value&&backendSelection.microphoneAllowed()&&!vendorMode)engine.mixer.resetVoiceSafety();
        boolean changed=micIntent.muted!=value||(userCommand&&micIntent.blocked);
        if(userCommand){
            if(!value&&backendSelection.microphoneAllowed()&&vendorMode&&vendorMic!=null){JSONObject guard=vendorMic.telemetry();if(VocalEffectsPolicy.resetRequired(vocalEffects!=null&&vocalEffects.fault(),guard.optInt("protection_level"),guard.optBoolean("protection_latched"),guard.optInt("protection_gain_cap",100))){if(vocalEffects!=null)vocalEffects.resetProtection();vendorMic.resetProtection();}}
            micIntent.choose(value);
        }else micIntent.restore(value);
        reconcileMicHardware(changed||initialize||previouslyRequested!=requestedMic());
    }
    private void reconcileMicHardware(boolean restartSoftware){
        engine.mixer.micMuted=vendorMode||!requestedMic();
        if(vendorMode){syncVendorMic();return;}
        // A soft mute keeps an already-owned capture. Adding the first capture
        // requires rebuilding the existing software session at its current position.
        if(restartSoftware&&backendSelection.playbackAllowed()&&requestedMic()&&(engine.inputDevice<0||loading)&&(engine.running||loading)&&current!=null){JSONObject job=current;long frame=loading?resumeFrame:engine.playheadFrames;stopAudio(false);play(job,frame);}
    }
    private static int normalizeMicGain(int value){return VendorMicClient.normalizeGain(value);}
    private void updateMicGain(int value){
        engine.mixer.gainA=engine.mixer.gainB=Math.max(0,Math.min(100,value))/100f;
        vendorGain=normalizeMicGain(value);
    }
    private void setMicGain(int value){updateMicGain(value);syncVendorMic();}
    private void syncVendorMic(){
        if(vendorMic==null)return;
        engine.mixer.micMuted=true;
        refreshVocalEffects();
        vendorMic.setEnabled(backendSelection.microphoneAllowed()&&micIntent.mayRun(foreground,autoRoom,paused,vendorMic.permissionReady(),microphone()!=null,vendorGain),vendorGain);
    }
    private boolean compatMuteKnown(){compatMute.observe(engine.mixer.voiceGateState(),engine.renderedFrames,Math.max(0,engine.playheadFrames-compatSessionSeek),engine.running,engineReleased&&engine.released());return compatSelected()&&compatMute.known();}
    private boolean effectiveMicMuted(){return vendorMode?(vendorMic!=null&&vendorMic.confirmedMuted()):(compatMuteKnown()&&compatMute.muted());}
    private String microphoneLabel(){
        if(backendSelection.unavailable())return "   麦克风暂不可用";
        if(!vendorMode){if(!requestedMic()||engine.mixer.safety.latched)return compatMuteKnown()&&compatMute.muted()?"   麦克风已静音":"   麦克风静音待确认";if(!vocalPermission())return "   按 ↓ 开启麦克风权限";return "";}
        if(vendorMic.telemetry().optBoolean("protection_latched")||micIntent.blocked)return vendorMic.confirmedMuted()?"   人声保护已静音，按 ↓ 手动恢复":"   人声保护正在静音…";
        if(micIntent.muted||vendorGain==0)return vendorMic.confirmedMuted()?"   麦克风已静音":"   麦克风正在静音…";
        if(!vendorMic.permissionReady())return "   按 ↓ 开启麦克风权限";
        if(!vocalPermission())return "   按 ↓ 开启语音保护权限";
        if(vendorMic.telemetry().optInt("protection_level")>0)return "   正在抑制啸叫风险";
        if(!vendorProblem.isEmpty())return "   麦克风暂不可用";
        return vendorMic.state().optBoolean("enabled")?"":"   麦克风准备中";
    }
    private void vendorMicChanged(JSONObject state){
        String problem=state.optString("last_error","");
        if(foreground&&!problem.isEmpty()&&!problem.equals(vendorProblem)&&notice!=null)notice.setText("麦克风暂不可用，请静音后重新开启。");
        vendorProblem=problem;
        refreshVocalEffects();
    }
    private void requestVendorPermission(){
        if(vendorMic==null||!foreground)return;
        if(!vocalPermission()){requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},73);return;}
        micIntent.permissionRequested(autoRoom&&!paused&&(engine.running||loading));
        vendorMic.openPermission();
        if("permission_entry_unavailable".equals(vendorMic.state().optString("last_error")))micIntent.takePermissionResume();
    }
    private void applyRoom(JSONObject room){
        String id=room.optString("room_id","");if(!id.equals(roomId)){roomEpoch++;pendingPlay=null;jobs.clear();currentOriginal=currentInstrumental=null;currentFilesJob="";prefetchCancel.set(true);prefetchedId="";warmup=false;stopAudio(true);current=null;currentId="";resumeFrame=0;if(!id.equals(preferences.getString("played-room",""))){played.clear();preferences.edit().remove("played").putString("played-room",id).apply();}roomId=id;shownSfx=0;soundEffects.resetRoom();localOriginal.clear();localMute.clear();lastCommand=preferences.getLong("command:"+id,0);controlsInitialized=false;appliedRevision=-1;lastMicRevision=-1;micIntent.awaitControls();backendSelection.roomChanged();engine.mixer.micMuted=true;paused=!backendSelection.playbackAllowed();queueIntent.awaitNext();}
        JSONObject controls=room.optJSONObject("controls");if(controls!=null){
            long revision=controls.optLong("control_revision",0);long now=SystemClock.elapsedRealtime();
            if(revision>=appliedRevision){
                boolean nextOriginal=localOriginal.resolve(controls.optInt("original",0),revision,now)!=0;
                if(nextOriginal!=original){original=nextOriginal;switchMusic();}
                // Stage gain before enabling the selected intent; no transient
                // start using a stale gain or the previous room's mute choice.
                if(revision!=appliedRevision){soundEffects.gain(controls.optInt("sfx_gain",60));engine.mixer.musicGain=Math.max(0,Math.min(100,controls.optInt("music_gain",70)))/100f;updateMicGain(controls.optInt("mic_gain",vendorMode?20:50));}
                boolean mute=localMute.resolve(controls.optInt("mic_muted",0),revision,now)!=0;
                long micRevision=controls.optLong("mic_revision",0);boolean reconcileMute=localMute.takeReconcile();
                boolean firstControls=!controlsInitialized,returningControls=!micIntent.controlsReady;micIntent.controlsReady=true;
                if(firstControls||micRevision!=lastMicRevision||reconcileMute){setMicMuted(mute,firstControls||returningControls,!firstControls,micRevision);}
                else if(returningControls)reconcileMicHardware(requestedMic());
                else if(revision!=appliedRevision)syncVendorMic();
                if(!localMute.pending())lastMicRevision=micRevision;controlsInitialized=true;appliedRevision=revision;
                {boolean localPending=localVocal.pending();int preset=localVocal.resolve(VocalEffectsPolicy.id(controls.optString("vocal_preset","dry")),revision);if(localPending&&!localVocal.pending())vocalSyncMessage=preset==requestedVocalProfile()?"已同步手机":"";selectVocal(WetProfiles.KEYS[preset]);refreshVocalEffects();}
            }
        }

        JSONArray commands=room.optJSONArray("commands");if(commands==null)return;
        if(!backendSelection.remoteCommandsFresh()){
            for(int i=0;i<commands.length();i++){JSONObject command=commands.optJSONObject(i);if(command!=null)lastCommand=Math.max(lastCommand,command.optLong("seq",0));}
            preferences.edit().putLong("command:"+roomId,lastCommand).apply();return;
        }
        for(int i=0;i<commands.length();i++){JSONObject command=commands.optJSONObject(i);if(command==null)continue;long seq=command.optLong("seq",0);if(seq<=lastCommand)continue;String action=command.optString("action"),target=command.optString("song");long age=System.currentTimeMillis()/1000-command.optLong("created");
            if(Arrays.asList("sfx_applause","sfx_cheer","sfx_stop").contains(action)){
                if(!backendSelection.playbackAllowed()){lastCommand=seq;preferences.edit().putLong("command:"+roomId,lastCommand).apply();continue;}
                soundEffects.command(seq,action,command.optLong("created"),command.optLong("expires"));
                lastCommand=seq;preferences.edit().putLong("command:"+roomId,lastCommand).apply();continue;
            }
            if(age<0||age>20){lastCommand=seq;continue;}
            if(action.equals("play")&&!userPlayback()){lastCommand=seq;preferences.edit().putLong("command:"+roomId,lastCommand).apply();continue;}
            JSONObject chosen=null;if(action.equals("play")){for(JSONObject job:jobs)if(job.optString("id").equals(target)&&job.optString("state").equals("ready")){chosen=job;break;}}
            lastCommand=seq;preferences.edit().putLong("command:"+roomId,lastCommand).apply();
            if(action.equals("play")&&chosen==null){pendingPlay=command;continue;}
            if(action.equals("play")){pendingPlay=null;queueIntent.awaitNext();if(current!=null&&!warmup&&!currentId.equals(target))played.add(currentId);warmup=false;played.remove(target);stopAudio(false);current=null;play(chosen,0,true);}
            else if(action.equals("next")&&target.equals(currentId))next();
            else if(action.equals("pause")){pendingPlay=null;queueIntent.pause();if(engine.running||loading){stopAudio(true);clock.setText("已暂停");}}
            else if(action.equals("resume")){queueIntent.awaitNext();if(!engine.running&&!loading)togglePause();}
        }
    }
    private void runPendingPlay(){
        if(pendingPlay==null||!foreground||!autoRoom||!backendSelection.playbackAllowed())return;
        long age=System.currentTimeMillis()/1000-pendingPlay.optLong("created");
        if(age<0||age>20){pendingPlay=null;return;}
        String target=pendingPlay.optString("song");
        for(JSONObject job:jobs)if(job.optString("id").equals(target)&&job.optString("state").equals("ready")){
            pendingPlay=null;queueIntent.awaitNext();if(current!=null&&!warmup&&!currentId.equals(target))played.add(currentId);
            warmup=false;played.remove(target);stopAudio(false);current=null;play(job,0,true);return;
        }
    }
    private JSONObject playbackReport(){JSONObject data=new JSONObject();try{boolean muteKnown=vendorMode?(vendorMic!=null&&vendorMic.muteConfirmed()):compatMuteKnown();boolean micMuted=vendorMode?(vendorMic!=null&&vendorMic.confirmedMuted()):muteKnown&&compatMute.muted();JSONArray done=new JSONArray();for(JSONObject job:jobs)if(played.contains(job.optString("id"))&&done.length()<80)done.put(job.optString("id"));JSONObject waiting=firstPending();data.put("audio_capabilities",new JSONObject().put("vocal_presets",vocalCapabilities()).put("sfx",soundEffects.ready&&backendSelection.playbackAllowed()).put("voice_protection",voiceProtectionReport(muteKnown))).put("vocal_preset",requestedVocalKey()).put("vocal_preset_state",vocalReportState()).put("sfx_seq",soundEffects.sequence).put("sfx_action",soundEffects.action).put("sfx_state",soundEffects.state);data.put("lyrics_waiting",waiting!=null&&waiting.optString("state").equals("ready")&&!readyForQueue(waiting)?waiting.optString("id"):"");data.put("playback_epoch",playbackTimeline.epoch()).put("room_id",roomId).put("current",current==null||warmup?"":currentId).put("played",done).put("state",loading?"preparing":engine.running?"playing":paused?"paused":"idle").put("mic_muted",micMuted).put("safety_latched",vendorMode?(vendorMic.telemetry().optBoolean("protection_latched")||(vocalEffects!=null&&vocalEffects.fault())):engine.mixer.safety.latched).put("position_ms",Math.min(1800000,Math.max(0,engine.playheadFrames/48))).put("last_command",lastCommand).put("applied_revision",Math.max(0,appliedRevision));}catch(Exception ignored){}return data;}
    private void showIdle(){
        if(current==null)songBackground(null);
        warmupButton.setVisibility(!readyForQueue(firstPending())?View.VISIBLE:View.GONE);idle.setVisibility(View.VISIBLE);lyricScroll.setVisibility(View.GONE);byline.setVisibility(View.GONE);progress.setVisibility(View.INVISIBLE);clock.setText("");trackTitle.setText("");picture.setAlpha(0);
        boolean joining=qrBitmap!=null && autoRoom;
        headline.setVisibility(joining?View.GONE:View.VISIBLE);headline.setText("");notice.setText(joining?"":"正在连接房间…");mvLyrics.setVisibility(View.GONE);
        bigQr.setVisibility(joining?View.VISIBLE:View.GONE);smallQr.setVisibility(View.INVISIBLE);
        for(JSONObject job:jobs)if(!played.contains(job.optString("id")) && !Arrays.asList("failed","cancelled").contains(job.optString("state"))){bigQr.setVisibility(View.GONE);smallQr.setVisibility(joining?View.VISIBLE:View.INVISIBLE);headline.setVisibility(View.VISIBLE);headline.setText(SongTitle.display(job.optString("title","下一首")));notice.setText(job.optString("state").equals("ready")&&paused?"歌曲已就绪":"正在准备…");break;}
        warmupButton.setText(current!=null?"继续播放":hasPending()?"开始播放":"播放暖场");
        if(!backendSelection.playbackAllowed())warmupButton.setVisibility(View.VISIBLE);
        if(backendSelection.unavailable())notice.setText(voiceUnavailableMessage());
    }
    private JSONObject firstPending(){for(JSONObject job:jobs)if(!played.contains(job.optString("id"))&&!Arrays.asList("failed","cancelled").contains(job.optString("state")))return job;return null;}
    private boolean readyForQueue(JSONObject job){return job!=null&&job.optString("state").equals("ready")&&preparedLyrics.containsKey(job.optString("id"));}
    private void prepareFirstLyrics(){prepareLyrics(firstPending());}
    private void prepareWarmupLyrics(){for(JSONObject job:warmupJobs.values()){prepareLyrics(job);if(queueLyricsBusy)break;}}
    private void prepareLyrics(JSONObject job){
        if(job==null||!job.optString("state").equals("ready")||queueLyricsBusy||client==null)return;
        String id=job.optString("id");if(preparedLyrics.containsKey(id)||SystemClock.elapsedRealtime()<lyricsWait.getOrDefault(id,0L))return;
        queueLyricsBusy=true;lyricsWait.put(id,SystemClock.elapsedRealtime()+30000);final KtvClient c=client;
        if(!background(()->{JSONArray words=null;long[] entries=null;try{JSONObject response=c.json("/v1/jobs/"+id+"/lyrics",null);words=response.optJSONArray("lines");entries=readEntries(response);}catch(Exception ignored){}final JSONArray found=words;final long[] cues=entries;
            ui.post(()->{queueLyricsBusy=false;if(c!=client)return;if(found!=null&&found.length()>=3){if(preparedLyrics.size()>=80){preparedLyrics.clear();preparedEntries.clear();}preparedLyrics.put(id,found);if(cues!=null)preparedEntries.put(id,cues);lyricsWait.remove(id);}});
        }))queueLyricsBusy=false;
    }
    private long[] readEntries(JSONObject response){if(!Arrays.asList("vocal_activity","vocal_residual").contains(response.optString("cue_source")))return null;JSONArray values=response.optJSONArray("entry_ms");if(values==null||values.length()>1000)return null;long[] result=new long[values.length()];long prior=-1;for(int i=0;i<result.length;i++){long value=values.optLong(i,-1);if(value<0||value>1800000||value<=prior)return null;result[i]=value;prior=value;}return result;}
    private void playNext(){if(!backendSelection.playbackAllowed())return;if(!queueIntent.shouldStart(foreground,loading,current!=null)||!autoRoom)return;JSONObject job=firstPending();if(readyForQueue(job)){warmup=false;play(job,0,true);}else startWarmup();}
    private void prefetchNext(){
        if(client==null||!foreground||current==null||!engine.running)return;
        JSONObject candidate=null;for(JSONObject job:jobs)if(!job.optString("id").equals(currentId)&&!played.contains(job.optString("id"))&&!Arrays.asList("failed","cancelled").contains(job.optString("state"))){if(job.optString("state").equals("ready"))candidate=job;break;}
        if(candidate==null)candidate=current;final String id=candidate.optString("id");
        if(prefetchBusy){if(!id.equals(prefetchId))prefetchCancel.set(true);return;}
        if(id.equals(prefetchedId)||SystemClock.elapsedRealtime()-prefetchAttempt<10000)return;
        prefetchAttempt=SystemClock.elapsedRealtime();prefetchId=id;prefetchBusy=true;prefetchCancel=new AtomicBoolean();
        final AtomicBoolean cancel=prefetchCancel;final JSONObject selected=candidate;final KtvClient c=client;
        try{preloader.execute(()->{boolean complete=false;try{JSONObject media=selected.getJSONObject("media");c.download(media.getJSONObject("instrumental"),cancel);c.download(media.getJSONObject("original"),cancel);JSONObject spec=selected.optJSONObject("video");if(spec!=null&&spec.optString("status").equals("ready"))c.downloadVideo(spec,cancel);complete=!cancel.get();}catch(Exception ignored){}final boolean done=complete;ui.post(()->{prefetchBusy=false;if(done&&foreground&&c==client)prefetchedId=id;});});}catch(RejectedExecutionException ignored){prefetchBusy=false;}
    }
    private void addCacheHashes(Set<String> hashes,JSONObject job,boolean both){
        if(job==null)return;JSONObject media=job.optJSONObject("media");if(media!=null)for(String kind:new String[]{"original","instrumental"}){if(!both&&!kind.equals(original?"original":"instrumental"))continue;JSONObject spec=media.optJSONObject(kind);if(spec!=null)hashes.add(spec.optString("sha256"));}
        JSONObject mv=job.optJSONObject("video");if(mv!=null&&mv.optString("status").equals("ready"))hashes.add(mv.optString("sha256"));
    }
    private void protectCache(){if(client==null)return;Set<String> hashes=new HashSet<>();addCacheHashes(hashes,current,true);for(JSONObject job:jobs)if(!job.optString("id").equals(currentId)&&!played.contains(job.optString("id"))&&!Arrays.asList("failed","cancelled").contains(job.optString("state"))){if(job.optString("state").equals("ready"))addCacheHashes(hashes,job,false);break;}client.mediaCache.protect(hashes);}
    private void rememberJob(JSONObject job){try{if(client==null||!job.optString("state").equals("ready")||!(client.cached(job.getJSONObject("media").getJSONObject("original"),false)||client.cached(job.getJSONObject("media").getJSONObject("instrumental"),false)))return;JSONArray before=new JSONArray(preferences.getString("cached-jobs","[]")),after=new JSONArray();for(int i=0;i<before.length();i++){JSONObject saved=before.optJSONObject(i);if(saved!=null&&saved.optString("id").equals(job.optString("id"))&&saved.toString().equals(job.toString()))return;}after.put(job);for(int i=0;i<before.length()&&after.length()<40;i++){JSONObject old=before.optJSONObject(i);if(old!=null&&!old.optString("id").equals(job.optString("id")))after.put(old);}String encoded=after.toString();if(encoded.length()<262144&&!encoded.equals(preferences.getString("cached-jobs","[]")))preferences.edit().putString("cached-jobs",encoded).apply();}catch(Exception ignored){}}
    private boolean hasPending(){for(JSONObject job:jobs)if(!played.contains(job.optString("id"))&&!Arrays.asList("failed","cancelled").contains(job.optString("state")))return true;return false;}
    private String warmupKey(JSONObject job){return job.optString("audio_origin","song")+":"+job.optString("song_id",job.optString("id"));}
    private void refreshWarmup(JSONArray available){
        // A successful server catalog is authoritative for audio editions. Keep
        // downloaded bytes, but never resurrect a revoked Live/MV job from old
        // metadata. An offline poll never enters this method with a new catalog.
        if(available==null)return;
        warmupJobs.clear();
        Set<String> eligible=new HashSet<>();
        for(int i=0;i<available.length();i++){JSONObject job=available.optJSONObject(i);if(job!=null&&job.optString("state").equals("ready")&&job.optJSONObject("media")!=null){warmupJobs.put(warmupKey(job),job);eligible.add(job.optString("id"));}}
        try{String saved=preferences.getString("cached-jobs","[]");JSONArray cached=new JSONArray(saved),retained=new JSONArray();for(int i=0;i<cached.length();i++){JSONObject job=cached.optJSONObject(i);if(job!=null&&eligible.contains(job.optString("id")))retained.put(job);}String encoded=retained.toString();if(!encoded.equals(saved))preferences.edit().putString("cached-jobs",encoded).apply();}catch(Exception ignored){}
        // Finish the current performance even when its old edition is revoked.
        // Its lyrics are evicted after it ends; future selection uses fresh jobs.
        if(current!=null)eligible.add(currentId);
        preparedLyrics.keySet().retainAll(eligible);preparedEntries.keySet().retainAll(eligible);
        warmupFailed.keySet().retainAll(warmupJobs.keySet());
    }
    private void refreshQueuePlayback(){
        // Warmup is a singable full song. Downloads, lyrics and queue refreshes
        // may prepare the next track but must never interrupt the current one.
        if(loading||current!=null)return;
        showIdle();if(queueIntent.shouldStart(foreground,loading,false))playNext();
    }
    private void startWarmup(){
        if(client==null||!backendSelection.playbackAllowed()||!queueIntent.shouldStart(foreground,loading,current!=null)||!autoRoom)return;
        ArrayList<String> eligible=new ArrayList<>();long now=SystemClock.elapsedRealtime();
        for(String key:warmupJobs.keySet())if(now>=warmupFailed.getOrDefault(key,0L)&&readyForQueue(warmupJobs.get(key)))eligible.add(key);
        String key=warmupShuffle.next(eligible);if(key==null){warmup=false;if(!warmupJobs.isEmpty())notice.setText("正在准备暖场歌曲与歌词…");return;}
        warmup=true;play(warmupJobs.get(key),0,true);
    }
    private void toggleWarmup(){if(!userPlayback())return;if(!autoRoom){autoRoom=true;paused=false;qrBitmap=null;coordinatorSession.requestOpen();poll();return;}if(current!=null&&!engine.running&&!loading){togglePause();return;}if(coordinatorVisible&&!coordinatorStatus.complete()){coordinatorSession.requestOpen();poll();}if(warmup){warmup=false;queueIntent.pause();stopAudio(true);current=null;currentId="";showIdle();return;}queueIntent.awaitNext();playNext();if(current==null)notice.setText("还没有准备好的暖场歌曲。");}
    private void warmupFailure(JSONObject job){warmupFailed.put(warmupKey(job),SystemClock.elapsedRealtime()+60000);warmup=false;current=null;currentId="";loading=false;showIdle();playNext();}
    private AudioDeviceInfo microphone(){ArrayList<AudioDeviceInfo> found=new ArrayList<>();for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_INPUTS))if(d.getType()==AudioDeviceInfo.TYPE_USB_DEVICE || d.getType()==AudioDeviceInfo.TYPE_USB_HEADSET)found.add(d);return found.size()==1?found.get(0):null;}
    private void play(JSONObject job,long position){
        play(job,position,false);
    }
    private void play(JSONObject job,long position,boolean restart){
        if(prefetchBusy&&!job.optString("id").equals(prefetchId))prefetchCancel.set(true);
        if(client==null || !foreground || loading||!backendSelection.playbackAllowed())return;
        AudioDeviceInfo input=vendorMode||!requestedMic()?null:microphone();
        if(input!=null && checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){pendingPermission=job;requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},71);return;}
        JSONObject tracks=job.optJSONObject("media");JSONObject dry=tracks==null?null:tracks.optJSONObject("original"),backing=tracks==null?null:tracks.optJSONObject("instrumental");
        String edition=(dry==null?"":dry.optString("sha256"))+":"+(backing==null?"":backing.optString("sha256"));
        if(playbackTimeline.start(job.optString("id"),edition,restart))preferences.edit().putString("playback-epoch",playbackTimeline.epoch()).putString("playback-song",playbackTimeline.song()).putString("playback-media",playbackTimeline.media()).apply();
        stopAudio(false);warmupButton.setVisibility(View.GONE);loading=true;paused=false;current=job;currentId=job.optString("id");lyricFetchAt=SystemClock.elapsedRealtime();lyricRetries=0;protectCache();resumeFrame=position;final int version=generation.get();final KtvClient c=client;final AtomicBoolean cancelled=transferCancel;final boolean isWarmup=warmup;final String jobId=currentId;final JSONArray knownLyrics=preparedLyrics.get(jobId);final long[] knownEntries=preparedEntries.get(jobId);
        songBackground(job);
        idle.setVisibility(View.VISIBLE);bigQr.setVisibility(View.GONE);smallQr.setVisibility(qrBitmap!=null?View.VISIBLE:View.INVISIBLE);headline.setVisibility(View.VISIBLE);headline.setText(SongTitle.display(job.optString("title","")));notice.setText("马上开唱…");trackTitle.setText(SongTitle.display(job.optString("title","")));
        if(!mediaBackground(()->{try{
            if(cancelled.get())return;
            engine.awaitStopped();JSONObject media=job.getJSONObject("media");
            JSONArray words=knownLyrics;long[] cues=knownEntries;if(words==null)try{JSONObject response=c.json("/v1/jobs/"+jobId+"/lyrics",null);words=response.optJSONArray("lines");cues=readEntries(response);}catch(Exception ignored){}
            if(words==null||words.length()<3){ui.post(()->{if(version!=generation.get()||cancelled.get())return;preparedLyrics.remove(jobId);lyricsWait.put(jobId,SystemClock.elapsedRealtime()+30000);loading=false;current=null;currentId="";warmup=false;showIdle();notice.setText("正在准备歌词，先唱暖场歌曲。");playNext();});return;}
            File originalFile=jobId.equals(currentFilesJob)&&currentOriginal!=null&&currentOriginal.getName().equals(media.getJSONObject("original").optString("sha256")+".wav")?currentOriginal:c.download(media.getJSONObject("original"),cancelled);
            File instrumentalFile=jobId.equals(currentFilesJob)&&currentInstrumental!=null&&currentInstrumental.getName().equals(media.getJSONObject("instrumental").optString("sha256")+".wav")?currentInstrumental:c.download(media.getJSONObject("instrumental"),cancelled);
            File movie=null;JSONObject spec=job.optJSONObject("video");if(spec!=null&&spec.optString("status").equals("ready")&&spec.optString("source_audio_sha256").equals(media.getJSONObject("original").optString("sha256"))){if(jobId.equals(currentFilesJob)&&videoFile!=null&&videoFile.isFile()&&videoFile.getName().equals(spec.optString("sha256")+".mp4"))movie=videoFile;else if(!isWarmup||c.cached(spec,true))try{movie=c.downloadVideo(spec,cancelled);}catch(Exception ignored){}}
            final File clip=movie;final JSONArray text=words==null?new JSONArray():words;final long[] entries=cues;
            ui.post(()->{if(!foreground||version!=generation.get()||cancelled.get())return;loading=false;currentOriginal=originalFile;currentInstrumental=instrumentalFile;currentFilesJob=jobId;if(text.length()>=3){preparedLyrics.put(jobId,text);lyricsWait.remove(jobId);}entryStarts=entries;setLyrics(text,SongTitle.display(job.optString("title","")));showRequester(job);idle.setVisibility(View.GONE);lyricScroll.setVisibility(View.VISIBLE);progress.setVisibility(View.VISIBLE);
                videoFile=clip;videoOffsetMs=spec==null?0:Math.round(spec.optDouble("video_offset_ms",0));videoDurationMs=spec==null?0:Math.round(spec.optDouble("duration_seconds",0)*1000);
                try{engine.mixer.micMuted=vendorMode||!requestedMic();compatSessionSeek=position;compatMute.session(engine.mixer.voiceGateState());engineReleased=false;engine.start(original?currentOriginal:currentInstrumental,input,null,position);syncVendorMic();prepareVideo();}catch(Exception e){stopVocalEffects(6);if(vendorMic!=null)vendorMic.stop();current=null;showIdle();notice.setText(recovery(e.getMessage()));paused=true;queueIntent.pause();}
            });
        }catch(Exception e){ui.post(()->{if(version!=generation.get())return;loading=false;if(isWarmup){warmupFailure(job);return;}current=null;showIdle();notice.setText("音源还没下载好，按遥控器播放键重试。");paused=true;queueIntent.pause();});}})){loading=false;current=null;showIdle();paused=true;queueIntent.pause();}
    }
    private void setLyrics(JSONArray value,String title){
        lines=value;activeLine=-1;lyrics.removeAllViews();lyricViews.clear();int h=Math.max(dp(150),lyricScroll.getHeight()/2);lyrics.setPadding(0,h,0,h);
        lyricStarts=new long[value.length()];for(int i=0;i<value.length();i++)lyricStarts[i]=value.optJSONObject(i).optLong("time_ms");mvPrevious.setText("");mvCurrent.setText("");mvNext.setText("");countdownLine=-1;countdownValue=0;
        if(value.length()==0){TextView line=text(title,38,WHITE);line.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));lyrics.addView(line);TextView pending=text("歌词暂未就绪",18,MUTED);pending.setPadding(0,dp(20),0,0);lyrics.addView(pending);return;}
        for(int i=0;i<value.length();i++){TextView line=text(value.optJSONObject(i).optString("text"),25,MUTED);line.setPadding(0,dp(10),0,dp(10));line.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));line.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);lyrics.addView(line,new LinearLayout.LayoutParams(-1,-2));lyricViews.add(line);}
        lyricScroll.post(()->lyricScroll.smoothScrollTo(0,0));
    }
    private void refreshMissingLyrics(){
        if(current==null||client==null||!engine.running||loading||lines.length()>0||lyricsBusy||lyricRetries>=3||SystemClock.elapsedRealtime()-lyricFetchAt<15000)return;
        lyricsBusy=true;lyricRetries++;lyricFetchAt=SystemClock.elapsedRealtime();final JSONObject job=current;final String id=currentId;final int version=generation.get();final KtvClient c=client;
        if(!background(()->{try{JSONArray words=c.json("/v1/jobs/"+id+"/lyrics",null).optJSONArray("lines");if(words!=null&&words.length()>0)ui.post(()->{if(foreground&&version==generation.get()&&current==job)setLyrics(words,SongTitle.display(job.optString("title","")));});}catch(Exception ignored){}finally{lyricsBusy=false;}}))lyricsBusy=false;
    }
    private void showRequester(JSONObject job){JSONObject who=job.optJSONObject("requester");String nickname=who==null?"朋友":who.optString("nickname","朋友");nickname=nickname.replaceFirst("^(汽水猫|夜航兔|慢慢鲸|摇摆熊|月球旅人|小幽灵|唱片君|星星糖|柠檬汽水|仙人掌|北极企鹅|节拍机器人) [0-9]{2}$","$1").replace("节拍机器人","节拍小子");requester.setText(warmup?"暖场中":nickname+" 点的");requesterAvatar.setVisibility(warmup?View.GONE:View.VISIBLE);requesterAvatar.setImageBitmap(avatar(who==null?0:who.optInt("avatar",0)));byline.setVisibility(View.VISIBLE);updateNext();}
    private Bitmap avatar(int id){if(atlas==null)return null;id=Math.max(0,Math.min(11,id));int w=atlas.getWidth()/4,h=atlas.getHeight()/3;return Bitmap.createBitmap(atlas,(id%4)*w,(id/4)*h,w,h);}
    private void updateNext(){String next="";for(JSONObject job:jobs)if(!job.optString("id").equals(currentId)&&!played.contains(job.optString("id"))&&!Arrays.asList("failed","cancelled").contains(job.optString("state"))){next="下一首  "+SongTitle.display(job.optString("title","歌曲"))+(lyricsWait.containsKey(job.optString("id"))?" · 等待歌词":"");break;}nextSong.setText(next);}
    private void ended(String code,boolean natural){if(backendSelection.switching()||engine.running || loading)return;if(vendorMic!=null&&!natural&&!"stopped".equals(code)){stopVocalEffects(6);micIntent.block();vendorMic.stop();engine.mixer.micMuted=true;}releaseVideo();if((code.startsWith("input_")||code.equals("no_microphone_frames"))&&current!=null&&foreground&&!paused){micIntent.block();engine.mixer.micMuted=true;engine.mixer.safety.latched=true;engine.mixer.safety.reason=3;play(current,engine.playheadFrames);return;}if(warmup&&natural&&foreground){current=null;currentId="";warmup=false;playNext();return;}if(natural && current!=null){String popularity="ktv-plays:"+current.optString("song_id",currentId);preferences.edit().putInt(popularity,Math.min(100000,preferences.getInt(popularity,0)+1)).apply();queueIntent.awaitNext();played.add(currentId);while(played.size()>128)played.remove(played.iterator().next());preferences.edit().putStringSet("played",new HashSet<>(played)).apply();current=null;currentId="";if(foreground){showIdle();playNext();}}else if(current!=null && foreground && !paused){showIdle();bigQr.setVisibility(View.GONE);headline.setText("播放已暂停");notice.setText(recovery(code));paused=true;queueIntent.pause();}}
    private void stopAudio(boolean pause){if(partyEffects!=null)partyEffects.stop();generation.incrementAndGet();transferCancel.set(true);transferCancel=new AtomicBoolean();if(pause){stopVocalEffects(6);if(soundEffects!=null)soundEffects.stop();if(!loading)resumeFrame=engine.playheadFrames;if(vendorMic!=null)vendorMic.stop();}paused=pause;loading=false;engine.stop();releaseVideo();}
    private void togglePause(){if(loading)return;if(!engine.running&&!userPlayback())return;if(engine.running){queueIntent.pause();stopAudio(true);clock.setText("已暂停");}else if(current!=null){queueIntent.awaitNext();JSONObject job=current;current=null;play(job,resumeFrame);}else{queueIntent.awaitNext();paused=false;playNext();}}
    private void next(){if(!userPlayback())return;queueIntent.awaitNext();if(current!=null&&!warmup){played.add(currentId);preferences.edit().putStringSet("played",new HashSet<>(played)).apply();}stopAudio(false);current=null;currentId="";warmup=false;showIdle();playNext();}
    private String recovery(String code){if("input_clipped".equals(code))return "麦克风输入异常，请检查话筒增益后重试。";if("no_microphone_frames".equals(code)||"input_read_failed".equals(code))return "麦克风未收到声音，重新插入 USB 后按播放键重试。";if(code!=null&&code.startsWith("output_"))return "电视输出已变化，确认音响连接后按播放键重试。";return "播放已暂停，按播放键重试；菜单中可检查连接。";}
    private void prepareVideo(){releaseVideo();if(videoFile==null||!picture.isAvailable()||!foreground)return;try{
        videoPrepareCount++;videoClock.reset();videoSeekCount=videoBufferEvents=videoSurfaceFrames=0;videoSpeed=1;videoSpeedSupported=false;videoSurface=new Surface(picture.getSurfaceTexture());final MediaPlayer p=new MediaPlayer();video=p;p.setSurface(videoSurface);p.setDataSource(videoFile.getAbsolutePath());p.setLooping(false);p.setVolume(0,0);p.setVideoScalingMode(MediaPlayer.VIDEO_SCALING_MODE_SCALE_TO_FIT_WITH_CROPPING);
        p.setOnPreparedListener(v->{if(video!=p||!foreground)return;videoReady=true;syncVideo(true);});p.setOnSeekCompleteListener(v->{if(video!=p)return;try{seekResultMs=p.getCurrentPosition();videoSeeking=false;if(engine.running&&foreground){long target=engine.playheadFrames/48+videoOffsetMs;long lag=target-seekResultMs;if(videoSeekCount==1&&lag>180&&lag<1500&&target+lag<videoDurationMs-40){videoSeeking=true;videoSeekCount++;p.seekTo(target+lag,MediaPlayer.SEEK_CLOSEST);}else p.start();}}catch(Exception ignored){if(video==p)releaseVideo();}});p.setOnInfoListener((v,what,extra)->{if(what==MediaPlayer.MEDIA_INFO_BUFFERING_START)videoBufferEvents++;if(what==MediaPlayer.MEDIA_INFO_VIDEO_RENDERING_START&&video==p){picture.setAlpha(1);lyricScroll.setVisibility(View.GONE);trackTitle.setVisibility(View.GONE);}return false;});p.setOnCompletionListener(v->{if(video==p)picture.setAlpha(0);});p.setOnErrorListener((v,what,extra)->{if(video==p)releaseVideo();return true;});p.prepareAsync();
        }catch(Exception ignored){releaseVideo();}}
    private void updateMvLyrics(long ms){
        lyricCue.update(ms,lyricStarts,entryStarts);boolean showVideo=video!=null&&picture.getAlpha()>0&&lines.length()>0;
        int target=lyricCue.countdown>0?lyricCue.entrance:-1;
        if(target!=countdownLine||lyricCue.countdown!=countdownValue){
            if(countdownLine>=0&&countdownLine<lyricViews.size())lyricViews.get(countdownLine).setText(lines.optJSONObject(countdownLine).optString("text"));
            countdownLine=target;countdownValue=lyricCue.countdown;
            if(target>=0&&target<lyricViews.size())lyricViews.get(target).setText(cuedLine(target));
        }
        if(!showVideo){mvLyrics.setVisibility(View.GONE);return;}
        mvLyrics.setVisibility(View.VISIBLE);
        mvPrevious.setText(cuedLine(LyricWindow.previous(lyricCue.current,lines.length())));mvCurrent.setText(cuedLine(lyricCue.current));mvNext.setText(cuedLine(lyricCue.next));
    }
    /** Keeps the just-sung line readable even when a source timestamp advances early. */
    static final class LyricWindow {
        static int previous(int current,int count){return current>0&&current<count?current-1:-1;}
        static int scrollTarget(int previousTop,int currentTop,int currentBottom,int nextBottom,int height){
            if(height<=0)return 0;
            int centered=Math.max(0,(currentTop+currentBottom-height)/2);
            int upper=Math.max(0,previousTop),lower=Math.max(0,nextBottom-height);
            if(lower<=upper)return Math.max(lower,Math.min(centered,upper));
            // Very long wrapped lyrics may exceed the screen: retain previous/current first.
            lower=Math.max(0,currentBottom-height);
            return lower<=upper?Math.max(lower,Math.min(centered,upper)):upper;
        }
    }
    private void highlightScrollingLyrics(int index){
        activeLine=index;
        int previous=LyricWindow.previous(index,lyricViews.size());
        for(int i=0;i<lyricViews.size();i++){
            TextView line=lyricViews.get(i);line.setTextSize(i==index?40:i==previous?28:25);
            line.setTextColor(i==index?(theme.equals("festival")?accent:WHITE):i==previous?0xffe2e2e2:MUTED);
        }
        if(index<0||index>=lyricViews.size())return;
        TextView line=lyricViews.get(index);
        lyrics.post(()->{
            if(activeLine!=index||index>=lyricViews.size()||lyricViews.get(index)!=line)return;
            TextView before=lyricViews.get(Math.max(0,index-1)),after=lyricViews.get(Math.min(lyricViews.size()-1,index+1));
            int y=LyricWindow.scrollTarget(before.getTop(),line.getTop(),line.getBottom(),after.getBottom(),lyricScroll.getHeight());
            if(ValueAnimator.areAnimatorsEnabled())lyricScroll.smoothScrollTo(0,y);else lyricScroll.scrollTo(0,y);
        });
    }
    private CharSequence cuedLine(int index){
        if(index<0||index>=lines.length())return "";String words=lines.optJSONObject(index).optString("text");
        if(index!=lyricCue.entrance||lyricCue.countdown<=0)return words;
        String prefix="●  ●  ●  ●  ●  ";android.text.SpannableString text=new android.text.SpannableString(prefix+words);
        text.setSpan(new android.text.style.RelativeSizeSpan(.55f),0,prefix.length(),0);
        int passed=5-lyricCue.countdown;for(int i=0;i<5;i++)text.setSpan(new android.text.style.ForegroundColorSpan(i<passed?0x33ffffff:WHITE),i*3,i*3+1,0);
        return text;
    }
    private void syncVideo(boolean force){if(video==null||!videoReady||!engine.running||!foreground)return;long target=engine.playheadFrames/48+videoOffsetMs;if(target<0||target>=videoDurationMs-40){try{video.pause();}catch(Exception ignored){}picture.setAlpha(0);return;}try{
        videoTargetMs=target;videoPositionMs=video.getCurrentPosition();if(videoSeeking)return;videoClock.update(target,videoPositionMs,SystemClock.elapsedRealtime(),force);
        if(videoClock.seek){videoSeeking=true;videoSeekCount++;video.seekTo(target,MediaPlayer.SEEK_CLOSEST);}
        else if(!video.isPlaying())video.start();
        }catch(Exception ignored){releaseVideo();}}
    private void releaseVideo(){if(current!=null){lyricScroll.setVisibility(View.VISIBLE);trackTitle.setVisibility(View.VISIBLE);}mvLyrics.setVisibility(View.GONE);videoReady=false;videoSeeking=false;picture.setAlpha(0);if(video!=null){try{video.release();}catch(Exception ignored){}video=null;}if(videoSurface!=null){videoSurface.release();videoSurface=null;}}
    @Override public void onSurfaceTextureAvailable(SurfaceTexture texture,int w,int h){if(engine.running)prepareVideo();}
    @Override public boolean onSurfaceTextureDestroyed(SurfaceTexture texture){releaseVideo();return true;}
    @Override public void onSurfaceTextureSizeChanged(SurfaceTexture texture,int w,int h){}
    @Override public void onSurfaceTextureUpdated(SurfaceTexture texture){videoSurfaceFrames++;}
    private void enlargeQr(){if(qrBitmap==null)return;ImageView view=new ImageView(this);view.setImageBitmap(qrBitmap);view.setPadding(dp(18),dp(18),dp(18),dp(18));new AlertDialog.Builder(this).setTitle("扫码点歌").setView(view).setPositiveButton("返回",(d,w)->{}).show();}
    private void maintenance(){String[] items={"声音","人声效果","连接设置","结束本场"};new AlertDialog.Builder(this).setTitle("房间设置").setItems(items,(dialog,which)->{if(which==0)sound();else if(which==1)vocalEffectsDialog();else if(which==2)connection();else endParty();}).setNegativeButton("返回",(d,w)->{}).show();}
    private boolean vocalPermission(){return checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED;}
    private VocalEffectsPolicy.DryLease vocalDryLease(){
        if(vendorMic==null||vendorMic.readbackAgeMs()>1500)return null;
        JSONObject state=vendorMic.state(),raw=vendorMic.telemetry(),carrier=raw.optJSONObject("zero_output_carrier");
        AudioDeviceInfo input=microphone();long lease=raw.optLong("lease_id");
        if(!state.optBoolean("connected")||state.optBoolean("stopping")||!state.optBoolean("native_capture_owned")||!state.optString("last_error").isEmpty()||input==null||raw.optInt("input_id")!=input.getId()
            ||carrier==null||!carrier.optBoolean("ready")||carrier.optBoolean("released")||raw.optLong("carrier_lease_id")!=lease
            ||carrier.optInt("actual_route_type")!=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)return null;
        int output=carrier.optInt("actual_route");boolean found=false;
        for(AudioDeviceInfo device:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(device.getId()==output&&device.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)found=true;
        return found?new VocalEffectsPolicy.DryLease(raw.optLong("guard_pid"),lease,input.getId(),output):null;
    }
    private void refreshVocalEffects(){
        if(!vendorMode){updateVocalDialog();syncVocalRoomIfReady();return;}
        if(vocalEffects==null||vendorMic==null)return;
        JSONObject guard=vendorMic.telemetry();
        int referenceGain=Math.min(vendorGain,guard.optInt("protection_gain_cap",100));
        boolean muted=!requestedMic()||vendorGain==0;
        vocalEffects.environment(vocalDryLease(),foreground&&autoRoom&&!paused&&micIntent.controlsReady&&vocalPermission(),muted,Math.max(1,referenceGain));
        vocalEffects.mixReady(VocalEffectsPolicy.mixReady(guard.optBoolean("inline_voice_mode"),guard.optBoolean("protection_ready"),guard.optInt("applied_user_gain_setpoint"),vendorGain,
            guard.optInt("gain_setpoint"),referenceGain,guard.optInt("protection_wet"),vocalEffects.requestedProfile()));
        if(vocalEffects.fault()||(guard.optBoolean("protection_latched")&&!vendorMic.state().optBoolean("protection_reset_pending"))){
            if(requestedMic()){micIntent.block();engine.mixer.micMuted=true;vendorMic.setEnabled(false,vendorGain);}
        }
        vendorMic.protection(vocalEffects.protectionReady(),vocalEffects.monitorToken(),vocalEffects.monitorFrames(),vocalEffects.feedbackSequence(),vocalEffects.feedbackLevel(),vocalEffects.feedbackHz(),vocalEffects.reservation());
        updateVocalDialog();
    }
    private JSONObject voiceProtectionReport(boolean muteKnown)throws JSONException{
        if(!vendorMode){
            long gate=engine.mixer.voiceGateState();boolean active=compatSelected()&&engine.running&&engine.inputDevice>0&&(gate>>>3)>0;
            String state=!compatSelected()?"unavailable":engine.mixer.safety.latched?"muted":engine.mixer.safety.attenuated?"attenuated":!requestedMic()?"off":active&&engine.mixer.safety.ready?"monitoring":"preparing";
            int gain=!compatSelected()||engine.mixer.safety.latched?0:Math.round(Math.max(engine.mixer.gainA,engine.mixer.gainB)*engine.mixer.safety.gainCap);
            return new JSONObject().put("state",state).put("effective_gain",Math.max(0,Math.min(100,gain))).put("mute_confirmed",muteKnown);
        }
        JSONObject guard=vendorMic==null?new JSONObject():vendorMic.telemetry();
        String state=!vendorMode?"unavailable":guard.optBoolean("protection_latched")?"muted":vocalEffects!=null&&vocalEffects.fault()?"failed":!requestedMic()?"off":guard.optBoolean("protection_ready")?(guard.optInt("protection_level")>0?"attenuated":"monitoring"):"preparing";
        return new JSONObject().put("state",state).put("effective_gain",Math.max(0,Math.min(100,guard.optInt("gain_setpoint")))).put("mute_confirmed",muteKnown);
    }
    private void stopVocalEffects(int reason){
        localVocal.clear();vocalSyncMessage="";vocalSyncScheduled=0;
        if(vocalEffects!=null)vocalEffects.suspend(reason);
    }
    private void vocalEffectsChanged(){refreshVocalEffects();syncVocalRoomIfReady();}
    private void compatDiagnostics(JSONObject data)throws JSONException{
        CompatVocalEffects.Status status=engine.mixer.compatEffectsStatus();long gate=engine.mixer.voiceGateState();boolean active=compatSelected()&&engine.running&&engine.inputDevice>0;
        data.put("audio_backend_choice",backendSelection.unavailable()?0:vendorMode?2:1).put("audio_backend_selected",backendSelection.selected()).put("audio_backend_selection_pending",backendSelection.unavailable()||backendSelection.switching())
            .put("system_audio_contract_ready",systemAvailable()).put("system_audio_silent_start",systemContract!=null&&systemContract.silentStart()).put("system_audio_cache_only",systemContract!=null&&systemContract.cacheOnly())
            .put("compat_effects_capable",compatSelected()&&status.capable).put("compat_effects_applied",compatSelected()&&engine.running&&status.applied()).put("compat_effects_gated",status.gated)
            .put("compat_voice_gate_frames",gate>>>3).put("compat_voice_zero_gate",(gate&1)!=0);
        if(vendorMode)return;
        data.put("wet_ready",compatSelected()&&engine.running&&status.applied()).put("wet_active",active&&!status.gated&&status.wetPermille>0).put("wet_fault",false)
            .put("wet_requested_profile",requestedVocalProfile()).put("wet_applied_profile",status.applied()?Math.max(0,WetProfiles.id(status.profile)):0).put("wet_command_epoch",status.requestedRevision).put("wet_applied_epoch",status.appliedRevision)
            .put("wet_input_frames",engine.capturedFrames).put("wet_output_frames",engine.renderedFrames).put("wet_rendered",false).put("wet_audition_verified",false).put("wet_process_isolated",false)
            .put("software_voice_processing_active",active).put("software_voice_processing_wet_only",false).put("software_dry_voice_processing_active",active).put("voice_protection_ready",active&&engine.mixer.safety.ready)
            .put("voice_feedback_frames",engine.mixer.safety.frames).put("voice_feedback_sequence",engine.mixer.safety.sequence).put("voice_feedback_level",engine.mixer.safety.severity).put("voice_feedback_hz",Math.max(0,Math.round(engine.mixer.safety.peakHz))).put("voice_feedback_ready",engine.mixer.safety.ready?1:0).put("voice_feedback_latched",engine.mixer.safety.latched?1:0);
    }
    private String requestedVocalKey(){return vendorMode&&vocalEffects!=null?vocalEffects.reportProfile():engine.mixer.compatEffectsStatus().requestedProfile;}
    private int requestedVocalProfile(){int id=WetProfiles.id(requestedVocalKey());return id<0?0:id;}
    private void selectVocal(String key){
        if(WetProfiles.id(key)<0)key="dry";if(!key.equals(preferences.getString("vocal-preset","dry")))preferences.edit().putString("vocal-preset",key).apply();
        if(vendorMode&&vocalEffects!=null)vocalEffects.select(key);else engine.mixer.setVocalPreset(key);
    }
    private JSONArray vocalCapabilities(){
        if(!backendSelection.selected())return new JSONArray().put("dry");
        if(vendorMode&&vocalEffects!=null)return vocalEffects.capabilities();
        JSONArray result=new JSONArray();for(int i=0;i<(compatSelected()&&engine.mixer.compatEffectsStatus().capable?WetProfiles.KEYS.length:1);i++)result.put(WetProfiles.KEYS[i]);return result;
    }
    private String vocalReportState(){
        if(!backendSelection.selected())return "unavailable";
        if(vendorMode&&vocalEffects!=null)return vocalEffects.reportState();
        if(!compatSelected())return "unavailable";CompatVocalEffects.Status status=engine.mixer.compatEffectsStatus();
        if(!engine.running)return "requested";return status.applied()&&status.profile.equals(status.requestedProfile)?"applied":"pending";
    }
    private void updateVocalDialog(){
        if(vocalStatus==null)return;String value;
        if(vendorMode&&vocalEffects!=null)value=!vocalPermission()?"请先开启麦克风权限。":vocalEffects.statusText();
        else if(!compatSelected())value=voiceUnavailableMessage();
        else {CompatVocalEffects.Status status=engine.mixer.compatEffectsStatus();String label=WetProfiles.LABELS[requestedVocalProfile()];
            value=!engine.running?"已选择"+label+"，播放后应用。":!status.applied()?"正在应用"+label+"…":!requestedMic()?(compatMuteKnown()&&compatMute.muted()?"效果已应用 · 麦克风已静音":"效果已应用 · 麦克风状态待确认"):"效果已应用 · "+label;}
        if(!vocalSyncMessage.isEmpty())value+="\n"+vocalSyncMessage;
        if(!value.contentEquals(vocalStatus.getText()))vocalStatus.setText(value);
    }
    private void vocalEffectsDialog(){
        if(!refreshAutomaticBackend()||!backendSelection.selected()){notice.setText(voiceUnavailableMessage());Toast.makeText(this,voiceUnavailableMessage(),Toast.LENGTH_SHORT).show();return;}
        refreshVocalEffects();LinearLayout panel=sheet();panel.addView(text("选择喜欢的人声效果。选择效果不会开启麦克风。",16,0xff26352c));
        final int[] chosen={requestedVocalProfile()};RadioGroup presets=new RadioGroup(this);
        for(int i=0;i<WetProfiles.LABELS.length;i++){RadioButton button=new RadioButton(this);button.setId(600+i);button.setText(WetProfiles.LABELS[i]);button.setTextSize(17);button.setMinHeight(dp(42));presets.addView(button);}
        presets.check(600+chosen[0]);presets.setOnCheckedChangeListener((group,id)->chosen[0]=id-600);panel.addView(presets);
        vocalStatus=text("",15,0xff526059);vocalStatus.setPadding(0,dp(10),0,dp(4));vocalStatus.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);panel.addView(vocalStatus);
        ScrollView scroll=new ScrollView(this);scroll.addView(panel);
        vocalDialog=new AlertDialog.Builder(this).setTitle("人声效果").setView(scroll).setNegativeButton("返回",(d,w)->{}).setNeutralButton("关闭混响",null).setPositiveButton(vendorMode?"开始试听":"应用效果",null).create();
        vocalDialog.setOnShowListener(d->{vocalDialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{
            if(chosen[0]==0){chooseVocalDry();return;}
            if(vendorMode&&!vocalPermission()){requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},72);return;}
            beginVocalTrial(chosen[0]);
        });vocalDialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(v->chooseVocalDry());updateVocalDialog();});
        vocalDialog.setOnDismissListener(d->{vocalDialog=null;vocalStatus=null;});vocalDialog.show();
    }
    private void beginVocalTrial(int profile){
        if(!WetProfiles.valid(profile)||!backendSelection.selected())return;refreshVocalEffects();
        if(vendorMode&&(!foreground||client==null||roomId.isEmpty()||!vocalPermission()||vocalEffects==null||!vocalEffects.canAudition())){
            vocalSyncMessage="请先播放歌曲并开启麦克风，再开始试听。";updateVocalDialog();return;
        }
        vocalRequest=localVocal.request(profile,appliedRevision);vocalSyncScheduled=0;vocalSyncMessage=vendorMode?"本机试听 · 准备后同步手机":"已选择效果 · 正在同步手机";
        if(vendorMode){if(!vocalEffects.audition(profile)){localVocal.failed(vocalRequest);vocalSyncMessage="试听尚未启动";}}
        else selectVocal(WetProfiles.KEYS[profile]);
        updateVocalDialog();syncVocalRoomIfReady();
    }
    private void chooseVocalDry(){
        if(!backendSelection.selected())return;vocalRequest=localVocal.request(0,appliedRevision);vocalSyncScheduled=0;vocalSyncMessage="正在同步关闭混响";
        selectVocal("dry");updateVocalDialog();syncVocalRoomIfReady();
    }
    private void syncVocalRoomIfReady(){
        if(!backendSelection.selected()||!foreground||!autoRoom||client==null||roomId.isEmpty()||!localVocal.current(vocalRequest)||vocalSyncScheduled==vocalRequest)return;
        final int profile=requestedVocalProfile();if(vendorMode&&profile>0&&(vocalEffects==null||!vocalEffects.ready()))return;
        final long request=vocalRequest,epoch=controlEpoch;final KtvClient c=client;final String room=roomId;
        final JSONObject report=playbackReport();vocalSyncScheduled=request;
        try{controlActions.execute(()->{try{
            if(!foreground||epoch!=controlEpoch||c!=client||!localVocal.current(request))return;
            c.controlJson("/v1/tv/playback",report);
            if(!foreground||epoch!=controlEpoch||c!=client||!localVocal.current(request))return;
            JSONObject reply=c.controlJson("/v1/tv/controls",new JSONObject().put("room_id",room).put("vocal_preset",WetProfiles.KEYS[profile]));
            if(!reply.optBoolean("accepted")||!reply.has("revision"))throw new IOException("vocal_control_unavailable");
            ui.post(()->{if(foreground&&epoch==controlEpoch&&c==client&&room.equals(roomId)&&localVocal.current(request)){localVocal.accepted(request,reply.optLong("revision"));vocalSyncMessage="已同步手机";updateVocalDialog();}});
        }catch(Exception error){ui.post(()->{if(foreground&&epoch==controlEpoch&&c==client&&room.equals(roomId)&&localVocal.current(request)){localVocal.failed(request);vocalSyncMessage="本机效果已选择 · 手机状态暂未同步";updateVocalDialog();}});}});
        }catch(RejectedExecutionException error){localVocal.failed(request);vocalSyncMessage="本机效果已选择 · 手机同步繁忙";updateVocalDialog();}
    }
    private LinearLayout sheet(){LinearLayout panel=new LinearLayout(this);panel.setOrientation(LinearLayout.VERTICAL);panel.setPadding(dp(24),dp(16),dp(24),dp(16));return panel;}
    private void connection(){
        LinearLayout panel=sheet();EditText origin=new EditText(this),key=new EditText(this);origin.setSingleLine(true);origin.setTextSize(16);origin.setMinHeight(dp(56));origin.setText(preferences.getString("origin",SetupDefaults.ORIGIN));origin.setContentDescription("NAS 家庭服务地址");key.setSingleLine(true);key.setTextSize(16);key.setMinHeight(dp(56));key.setInputType(129);key.setText(preferences.getString("key",SetupDefaults.KEY));key.setContentDescription("家庭服务连接密钥");panel.addView(text("NAS 家庭服务地址",15,0xff26352c));panel.addView(origin);panel.addView(text("连接密钥",15,0xff26352c));panel.addView(key);ScrollView scroll=new ScrollView(this);scroll.addView(panel);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("服务连接").setView(scroll).setNegativeButton("返回",(d,w)->{}).setPositiveButton("连接",null).create();dialog.setOnShowListener(d->{dialog.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{try{new KtvClient(origin.getText().toString().trim(),key.getText().toString().trim(),new File(getCacheDir(),"audio"));preferences.edit().putString("origin",origin.getText().toString().trim()).putString("key",key.getText().toString().trim()).apply();dialog.dismiss();connect();}catch(Exception error){origin.setError("使用 NAS 的 HTTPS 地址或家庭网络地址，并检查电视连接密钥。");}});});dialog.show();
    }
    private void sound(){
        LinearLayout panel=sheet();
        if(backendSelection.unavailable()){panel.addView(text(voiceUnavailableMessage(),15,0xff526059));Button retry=new Button(this);retry.setText("重新检查麦克风");retry.setOnClickListener(v->{refreshAutomaticBackend();notice.setText(backendSelection.selected()?"麦克风已准备好，请按播放后手动开启。":voiceUnavailableMessage());});panel.addView(retry);}
        slider(panel,"伴奏",Math.round(engine.mixer.musicGain*100),100,v->engine.mixer.musicGain=v/100f);slider(panel,"麦克风",vendorGain,100,this::setMicGain);
        if(!vocalPermission()||(vendorMode&&(vendorMic==null||!vendorMic.permissionReady()))){Button enable=new Button(this);enable.setText("开启麦克风权限");enable.setOnClickListener(v->{if(vendorMode)requestVendorPermission();else requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},74);});panel.addView(enable);}
        Button presets=new Button(this);presets.setText("人声效果 · "+WetProfiles.LABELS[requestedVocalProfile()]);presets.setOnClickListener(v->vocalEffectsDialog());panel.addView(presets);
        ScrollView scroll=new ScrollView(this);scroll.addView(panel);new AlertDialog.Builder(this).setTitle("声音").setView(scroll).setPositiveButton("完成",(d,w)->{}).show();
    }
    private interface Knob{void set(int value);}
    private void slider(LinearLayout panel,String name,int initial,int max,Knob action){TextView label=text(name+" "+initial+"%",16,0xff26352c);panel.addView(label);SeekBar bar=new SeekBar(this);bar.setContentDescription(name);bar.setMax(max);bar.setProgress(initial);bar.setMinimumHeight(dp(48));bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener(){public void onStartTrackingTouch(SeekBar b){}public void onStopTrackingTouch(SeekBar b){}public void onProgressChanged(SeekBar b,int value,boolean user){action.set(value);label.setText(name+" "+value+"%");}});panel.addView(bar);}
    private void reportVendorMic(JSONObject data)throws JSONException{
        data.put("vendor_microphone_mode",vendorMode);
        if(vendorMic==null)return;
        JSONObject state=vendorMic.state(),raw=vendorMic.telemetry();JSONObject carrier=raw.optJSONObject("zero_output_carrier");
        data.put("vendor_mic_enabled",state.optBoolean("enabled")).put("vendor_mic_permission",vendorMic.permissionReady())
            .put("vendor_mic_desired",state.optBoolean("desired")).put("vendor_mic_muted",micIntent.muted||vendorGain==0)
            .put("vendor_mic_state",Math.max(0,raw.optInt("state"))).put("vendor_mic_hal_status",raw.optInt("hal_status",-1)+1)
            .put("vendor_gain_setpoint",vendorGain).put("vendor_mic_gain_applied",Math.max(0,raw.optInt("gain_setpoint")))
            .put("vendor_mic_owned",raw.optBoolean("owns_unresolved_lease")).put("vendor_mic_fault",!state.optString("last_error","").isEmpty())
            .put("vendor_carrier_pending",raw.optBoolean("carrier_resources_pending")).put("software_voice_processing_active",false);
        for(String key:new String[]{"start_calls","stop_calls","lease_id","command_exceptions","started_ms","stopped_ms"})data.put("vendor_mic_"+key,Math.max(0,raw.optLong(key)));
        for(String key:new String[]{"protection_gain_cap","protection_level","protection_reason","protection_sequence","requested_gain_setpoint"})data.put("voice_"+key,Math.max(0,raw.optLong(key)));
        data.put("voice_protection_latched",raw.optBoolean("protection_latched"));
        if(carrier!=null){data.put("vendor_carrier_ready",carrier.optBoolean("ready")).put("vendor_carrier_released",carrier.optBoolean("released"));
            for(String key:new String[]{"frames_written","playback_head_frames","underruns","actual_route","actual_route_type"})data.put("vendor_carrier_"+key,Math.max(0,carrier.optLong(key)));
            data.put("vendor_carrier_fault",!carrier.optString("error","").isEmpty());}
    }
    private JSONObject telemetry(){JSONObject data=new JSONObject();try{reportVendorMic(data);if(vocalEffects!=null)vocalEffects.diagnostics(data);compatDiagnostics(data);long[] ioMetrics=engine.nativeMetrics;int backend=engine.audioBackend;if(benchmarkMicros<0)benchmarkMicros=DspBenchmark.p95Micros();data.put("dsp_benchmark_p95_us",benchmarkMicros);for(String key:DirectMonitorActivity.METRICS)data.put(key,preferences.getLong(key,0));for(String key:DirectMonitorActivity.EXTRA)data.put(key,preferences.getLong(key,0));
        for(String key:ProductionAudioCheckActivity.METRICS)data.put(key,preferences.getLong(key,-1));
        if(client!=null)data.put("tv_cache_bytes",client.mediaCache.bytes()).put("tv_cache_limit",client.mediaCache.limit());
        data.put("video_seek_count",videoSeekCount).put("video_drift_abs_ms",Math.abs(videoClock.drift)).put("video_buffer_events",videoBufferEvents).put("video_surface_frames",videoSurfaceFrames).put("video_speed_supported",videoSpeedSupported).put("apk_version",installedVersion).put("video_position_ms",videoPositionMs).put("video_target_ms",videoTargetMs).put("video_seek_result_ms",seekResultMs).put("video_prepare_count",videoPrepareCount).put("route_restart_count",routeRestartCount).put("next_prefetched",!prefetchedId.isEmpty());
        data.put("warmup_active",warmup).put("warmup_pool",warmupJobs.size()).put("lyric_lines",lines.length());
        data.put("feedback_notches",engine.mixer.feedbackA.count+engine.mixer.feedbackB.count).put("feedback_events",engine.mixer.feedbackA.events+engine.mixer.feedbackB.events).put("feedback_peak_hz",Math.round(Math.max(engine.mixer.feedbackA.peakHz,engine.mixer.feedbackB.peakHz)));
        InputMonitor observed=engine.lastInput;data.put("last_input_frames",observed.frames).put("last_input_peak_a",observed.peakA).put("last_input_peak_b",observed.peakB).put("last_input_clipped_a",observed.clippedA).put("last_input_clipped_b",observed.clippedB).put("last_input_rms_a",observed.rmsA()).put("last_input_rms_b",observed.rmsB()).put("last_input_dc_a",observed.dcA()+32768).put("last_input_dc_b",observed.dcB()+32768).put("last_input_ready",observed.ready).put("last_input_fault",engine.lastInputFault).put("safety_reason",engine.mixer.safety.reason);
        data.put("duplex_attempted",engine.duplexAttempted).put("duplex_open_code",engine.duplexOpenCode).put("duplex_start_code",engine.duplexStartCode).put("duplex_open_failures",engine.duplexOpenFailures).put("duplex_start_failures",engine.duplexStartFailures).put("duplex_input_dropped_available",false).put("audio_mixed_peak",engine.mixedPeak);if(AudioBackendPolicy.duplexMetricsAvailable(backend,ioMetrics))for(int i=0;i<NativeDuplexIO.METRICS.length;i++)data.put(NativeDuplexIO.METRICS[i],Math.max(0,ioMetrics[NativeDuplexIO.METRIC_START+i]));
        data.put("native_open_code",NativeAudioIO.openCode());data.put("input_age_available",engine.inputTimestampAvailable).put("underruns_available",AudioBackendPolicy.outputXrunsAvailable(backend,ioMetrics));data.put("native_clock_estimated",backend==1||backend==2).put("native_hardware_timing_available",AudioBackendPolicy.hardwareTimingAvailable(backend,ioMetrics)).put("native_hardware_xruns_available",AudioBackendPolicy.hardwareXrunsAvailable(backend,ioMetrics));data.put("audio_backend",backend).put("last_session_backend",engine.lastSessionBackend).put("native_open_failures",engine.nativeOpenFailures);String[] ioKeys={"io_presented_frames","io_queued_frames","io_output_device","io_input_device","io_output_buffer","io_input_buffer","io_output_burst","io_input_burst","io_output_performance","io_input_performance","io_input_dropped","io_output_missing","io_output_xruns","io_input_xruns","io_error","io_input_delivery_age_us","io_output_rate","io_input_rate","io_input_queue","io_input_gap_p95_us","io_input_gap_max_us","io_output_callback_frames","io_output_callback_size","io_output_gap_max_us","io_output_api","io_observed_at_ms","io_input_callback_frames"};for(int i=0;i<ioKeys.length;i++)data.put(ioKeys[i],(i==12||i==13)?ioMetrics[i]:Math.max(0,ioMetrics[i]));String[] nativeKeys={"native_output_code","native_output_performance","native_output_burst","native_output_buffer","native_output_sharing","native_output_device","native_output_frames","native_output_peak","native_input_code","native_input_performance","native_input_burst","native_input_buffer","native_input_sharing","native_input_device","native_input_frames","native_input_peak"};for(int i=0;i<nativeKeys.length;i++)data.put(nativeKeys[i],engine.nativeProbe[i]);data.put("has_low_latency",getPackageManager().hasSystemFeature("android.hardware.audio.low_latency")).put("has_pro_audio",getPackageManager().hasSystemFeature("android.hardware.audio.pro"));data.put("input_read_p95_us",engine.inputReadP95Us).put("output_buffer_requested",engine.outputBufferRequested);data.put("input_open_failures",engine.inputOpenFailures).put("input_fallbacks",engine.inputFallbacks);data.put("sdk",android.os.Build.VERSION.SDK_INT).put("input_sample_rate",engine.inputSampleRate).put("output_sample_rate",engine.outputSampleRate).put("mic_target_frames",engine.micTargetFrames).put("mic_peak_queue_frames",engine.micPeakQueueFrames).put("mic_rate_ppm",engine.micRatePpm+1000).put("mic_rebuffers",engine.micRebuffers).put("input_age_max_us",engine.inputAgeMaxNanos/1000);
        data.put("last_session_input_frames",engine.lastSessionInputFrames).put("last_session_dropped",engine.lastSessionDropped).put("last_session_missing",engine.lastSessionMissing).put("last_session_input_age_us",engine.lastSessionInputAgeUs).put("last_session_output_type",engine.lastSessionOutputType).put("last_session_fault",engine.lastSessionFault);
        data.put("backing_switches",engine.backingSwitches).put("backing_failures",engine.backingFailures);data.put("input_source",engine.inputSource).put("input_effects_disabled",engine.inputEffectsDisabled).put("input_effects_failed",engine.inputEffectsFailed);data.put("input_frames",engine.capturedFrames).put("output_frames",engine.renderedFrames).put("mic_peak",engine.micPeak).put("permission",checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED).put("input_device_id",engine.inputDevice).put("output_device_id",engine.outputDevice).put("output_buffer_frames",engine.bufferFrames).put("underruns",engine.underruns).put("mv_ready",videoReady);
        JSONArray inputs=new JSONArray(),usb=new JSONArray();for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_INPUTS)){if(inputs.length()>=16)break;JSONArray channels=new JSONArray();for(int n:d.getChannelCounts())if(channels.length()<16)channels.put(n);inputs.put(new JSONObject().put("id",d.getId()).put("type",d.getType()).put("channels",channels));}
        UsbManager manager=(UsbManager)getSystemService(USB_SERVICE);for(UsbDevice d:manager.getDeviceList().values()){if(usb.length()>=16)break;JSONArray interfaces=new JSONArray();for(int i=0;i<d.getInterfaceCount()&&i<16;i++)interfaces.put(d.getInterface(i).getInterfaceClass());usb.put(new JSONObject().put("vendor",d.getVendorId()).put("product",d.getProductId()).put("interfaces",interfaces));}data.put("inputs",inputs).put("usb",usb).put("output_type",engine.outputType).put("input_buffer_frames",engine.inputBufferFrames).put("input_age_us",engine.inputAgeNanos/1000).put("dsp_max_us",engine.dspNanosMax/1000).put("output_queued_frames",engine.outputQueuedFrames).put("missing_mic_frames",engine.missingMicFrames).put("dropped_mic_frames",engine.droppedMicFrames).put("queued_mic_frames",engine.queuedMicFrames).put("limited_samples",engine.mixer.limitedSamples).put("safety_latched",engine.mixer.safety.latched).put("input_clipped_samples",engine.mixer.safety.clippedSamples).put("performance_mode",engine.performanceModeActual);
    }catch(Exception ignored){}return data;}
    private void endParty(){new AlertDialog.Builder(this).setTitle("结束本场？").setMessage("朋友的点歌二维码将失效。").setNegativeButton("继续本场",(d,w)->{}).setPositiveButton("结束本场",(d,w)->{final KtvClient closingClient=client;final String closingRoom=roomId;autoRoom=false;coordinatorSession.closed();controlEpoch++;pendingPlay=null;stopAudio(true);current=null;showIdle();headline.setText("今晚，唱得尽兴。");bigQr.setVisibility(View.GONE);smallQr.setVisibility(View.INVISIBLE);notice.setText("按播放键重新开启。");if(closingClient!=null)background(()->{try{closingClient.json("/v1/party/close",closingClient.coordinator()?new JSONObject().put("room_id",closingRoom):new JSONObject());}catch(Exception ignored){}});}).show();}
    private boolean mediaBackground(Runnable work){try{mediaNetwork.execute(work);return true;}catch(RejectedExecutionException ignored){return false;}}
    private boolean background(Runnable work){try{network.execute(work);return true;}catch(RejectedExecutionException e){notice.setText("房间正在忙，稍后会自动重试。");return false;}}
    private final AudioDeviceCallback devices=new AudioDeviceCallback(){@Override public void onAudioDevicesAdded(AudioDeviceInfo[] added){routeChanged();}@Override public void onAudioDevicesRemoved(AudioDeviceInfo[] removed){routeChanged();}};
    private void routeChanged(){stopVocalEffects(7);if(engine.running){boolean outputPresent=false,inputPresent=engine.inputDevice<0;for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))if(d.getId()==engine.outputDevice)outputPresent=true;for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_INPUTS))if(d.getId()==engine.inputDevice)inputPresent=true;if(outputPresent&&inputPresent)return;routeRestartCount++;micIntent.block();engine.mixer.micMuted=true;JSONObject job=current;long frame=engine.playheadFrames;stopAudio(true);ui.postDelayed(()->{if(foreground&&job!=null)play(job,frame);},400);}}
    private String time(long ms){long seconds=Math.max(0,ms/1000);return String.format(Locale.ROOT,"%02d:%02d",seconds/60,seconds%60);}
    private void syncPartyEffects(){
        if(partyEffects==null||soundEffects==null)return;
        if("playing".equals(soundEffects.state)){
            if(shownSfx!=soundEffects.sequence){shownSfx=soundEffects.sequence;partyEffects.show(soundEffects.action);}
        }else if(!"pending".equals(soundEffects.state)){partyEffects.stop();}
    }
    private final Runnable tick=new Runnable(){public void run(){if(!foreground)return;refreshCoordinatorStatus();syncPartyEffects();refreshVocalEffects();if(ticks++%5==0){poll();refreshMissingLyrics();}if(engine.running&&current!=null){long ms=engine.playheadFrames/48;updateMvLyrics(ms);int index=-1;for(int i=0;i<lines.length();i++){if(lines.optJSONObject(i).optLong("time_ms")<=ms)index=i;else break;}if(index!=activeLine)highlightScrollingLyrics(index);
            JSONObject media=current.optJSONObject("media");JSONObject song=media==null?null:media.optJSONObject("original");long duration=song==null?0:Math.round(song.optDouble("duration_seconds")*1000);progress.setProgress(duration>0?(int)Math.min(10000,ms*10000/duration):0);clock.setText(time(ms)+"  /  "+time(duration)+"     ↑ 原唱/伴奏   ↓ 静音   ← 重唱   → 切歌"+microphoneLabel());if(!vendorMode&&engine.mixer.safety.latched&&!micIntent.blocked){micIntent.block();engine.mixer.micMuted=true;compatMute.request(true,engine.mixer.voiceGateState());reconcileMicHardware(true);}if(ticks%5==0){syncVideo(false);engine.checkNativeRoute();}
        }ui.postDelayed(this,100);}};
    private void remoteAction(RemoteShortcuts.Action action){
        if(action==RemoteShortcuts.Action.NEXT||action==RemoteShortcuts.Action.REPLAY)pendingPlay=null;
        if(action==RemoteShortcuts.Action.NEXT){next();Toast.makeText(this,"已切歌",Toast.LENGTH_SHORT).show();return;}
        if(action==RemoteShortcuts.Action.REPLAY){if(!userPlayback())return;if(current!=null){JSONObject job=current;stopAudio(false);queueIntent.awaitNext();play(job,0,true);Toast.makeText(this,"从头重唱",Toast.LENGTH_SHORT).show();}return;}
        if(client==null)return;
        if(action==RemoteShortcuts.Action.MUTE&&(!refreshAutomaticBackend()||!backendSelection.selected())){notice.setText(voiceUnavailableMessage());Toast.makeText(this,voiceUnavailableMessage(),Toast.LENGTH_SHORT).show();return;}
        if(action==RemoteShortcuts.Action.MUTE&&vendorMode&&(!vendorMic.permissionReady()||!vocalPermission())){requestVendorPermission();return;}
        if(action==RemoteShortcuts.Action.MUTE&&!vendorMode&&!vocalPermission()){requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},74);return;}
        if(action==RemoteShortcuts.Action.MUTE&&(microphone()==null||lines.length()<3)&&(micIntent.muted||micIntent.blocked||backendSelection.microphoneHeld())){Toast.makeText(this,"请先连接麦克风并等待歌词就绪",Toast.LENGTH_SHORT).show();return;}
        final String field=action==RemoteShortcuts.Action.MUTE?"mic_muted":"original";final int value=action==RemoteShortcuts.Action.MUTE?((micIntent.muted||micIntent.blocked||backendSelection.microphoneHeld())?0:1):(original?0:1);
        final LocalControlIntent intent=field.equals("original")?localOriginal:localMute;
        final long request=intent.request(value,SystemClock.elapsedRealtime());
        if(field.equals("original")){original=value==1;switchMusic();}else setMicMuted(value==1);
        final KtvClient c=client;final String room=roomId;final long epoch=controlEpoch;
        Toast.makeText(this,field.equals("original")?(value==1?"原唱":"伴奏"):(value==1?"麦克风已静音":"正在开启麦克风"),Toast.LENGTH_SHORT).show();
        try{controlActions.execute(()->{try{
            if(!foreground||epoch!=controlEpoch||c!=client||!intent.isCurrent(request,SystemClock.elapsedRealtime()))return;
            JSONObject reply=c.controlJson("/v1/tv/controls",new JSONObject().put("room_id",room).put(field,value));
            if(!reply.optBoolean("accepted")||!reply.has("revision"))throw new IOException("control_unavailable");
            ui.post(()->{if(c==client&&room.equals(roomId))intent.accepted(request,reply.optLong("revision"));});
        }catch(Exception e){ui.post(()->{if(c==client&&room.equals(roomId)){intent.failed(request);Toast.makeText(this,"本机已操作，手机状态暂未同步",Toast.LENGTH_SHORT).show();}});}});
        }catch(RejectedExecutionException ignored){intent.failed(request);Toast.makeText(this,"操作同步繁忙",Toast.LENGTH_SHORT).show();}

    }
    @Override public boolean dispatchKeyEvent(KeyEvent event){boolean enabled=foreground&&hasWindowFocus()&&current!=null;
        RemoteShortcuts.Action shortcut=remoteShortcuts.press(event.getKeyCode(),event.getAction()==KeyEvent.ACTION_DOWN,event.getRepeatCount(),enabled);
        if(enabled&&RemoteShortcuts.direction(event.getKeyCode())){if(shortcut!=RemoteShortcuts.Action.NONE)remoteAction(shortcut);return true;}
        if(event.getKeyCode()==KeyEvent.KEYCODE_DPAD_CENTER&&(warmup||current==null&&!hasPending())&&!smallQr.hasFocus()&&!warmupButton.hasFocus()){if(event.getAction()==KeyEvent.ACTION_DOWN&&event.getRepeatCount()==0)toggleWarmup();return true;}return super.dispatchKeyEvent(event);}
    @Override public boolean onKeyDown(int code,KeyEvent event){if(event.getRepeatCount()==0){if(code==KeyEvent.KEYCODE_MENU){maintenance();return true;}if(code==KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE||code==KeyEvent.KEYCODE_MEDIA_PLAY){if(warmup||current==null&&!hasPending()){toggleWarmup();}else if(!autoRoom){if(!userPlayback())return true;autoRoom=true;paused=false;queueIntent.awaitNext();qrBitmap=null;coordinatorSession.requestOpen();poll();}else togglePause();return true;}if(code==KeyEvent.KEYCODE_MEDIA_NEXT){next();return true;}}return super.onKeyDown(code,event);}
    @Override protected void onStart(){
        super.onStart();foreground=true;if(vendorMic!=null)vendorMic.setForeground(true);
        boolean permissionResume=micIntent.takePermissionResume();
        if(permissionResume&&autoRoom&&current!=null){queueIntent.awaitNext();play(current,resumeFrame);}
        else if(current!=null&&!engine.running)clock.setText("已暂停");
        refreshAutomaticBackend();syncVendorMic();ui.post(tick);ui.post(controlTick);if(preferences.getBoolean("audio-backend-owner-cleanup",false)&&vendorMic!=null)vendorMic.stop();
    }
    @Override protected void onStop(){foreground=false;controlEpoch++;pendingPlay=null;stopVocalEffects(6);micIntent.leaveForeground();if(vendorMic!=null)vendorMic.setForeground(false);prefetchCancel.set(true);ui.removeCallbacks(tick);ui.removeCallbacks(controlTick);localOriginal.clear();localMute.clear();queueIntent.pause();engine.mixer.micMuted=true;stopAudio(true);super.onStop();}
    @Override protected void onDestroy(){songBackdrop.select("");backdropNetwork.shutdownNow();if(vocalEffects!=null)vocalEffects.close();if(vendorMic!=null)vendorMic.close();if(soundEffects!=null)soundEffects.close();audio.unregisterAudioDeviceCallback(devices);transferCancel.set(true);engine.stop();releaseVideo();network.shutdownNow();mediaNetwork.shutdownNow();controlNetwork.shutdownNow();controlActions.shutdownNow();preloader.shutdownNow();audioLifecycle.shutdown();super.onDestroy();}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants){super.onRequestPermissionsResult(request,permissions,grants);if(request==74){readinessAt=0;Toast.makeText(this,grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED?"麦克风权限已开启，请再按麦克风开启":"未获得麦克风权限，保持静音",Toast.LENGTH_SHORT).show();return;}if(request==73){Toast.makeText(this,grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED?"语音保护权限已开启，请再按麦克风开启":"未获得语音保护权限，保持麦克风静音",Toast.LENGTH_SHORT).show();return;}if(request==72){Toast.makeText(this,grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED?"权限已开启，请再次点开始试听":"混响试听尚未获得麦克风权限",Toast.LENGTH_SHORT).show();updateVocalDialog();return;}if(request==71){JSONObject job=pendingPermission;pendingPermission=null;if(grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED&&job!=null&&foreground)play(job,resumeFrame);else{micIntent.block();engine.mixer.micMuted=true;if(job!=null&&foreground)play(job,resumeFrame);}}}
}
