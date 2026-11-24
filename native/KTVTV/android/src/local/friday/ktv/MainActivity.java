package local.friday.ktv;

import android.animation.ValueAnimator;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.*;
import android.graphics.drawable.*;
import android.media.*;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.text.Spannable;
import android.text.SpannableString;
import android.text.style.ForegroundColorSpan;
import android.text.style.RelativeSizeSpan;
import android.util.TypedValue;
import android.view.*;
import android.view.animation.PathInterpolator;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** Audience display: room QR, then synchronized lyrics and optional silent MV. */
public final class MainActivity extends Activity implements TextureView.SurfaceTextureListener {
    private final Handler ui=new Handler(Looper.getMainLooper());
    private final ExecutorService roomNetwork=Executors.newSingleThreadExecutor();
    private final ExecutorService roomReports=Executors.newSingleThreadExecutor();
    private final ExecutorService playbackStarts=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(4));
    private final PlaybackReleaseQueue playbackReleases=new PlaybackReleaseQueue(Executors.newSingleThreadExecutor(),
        SystemClock::elapsedRealtime,ui::post,ui::postDelayed,ui::removeCallbacks);
    private final ExecutorService roomQrNetwork=Executors.newSingleThreadExecutor();
    private final ExecutorService lyricsNetwork=new ThreadPoolExecutor(2,2,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(2));
    private final ExecutorService network=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(8));
    private final ThreadPoolExecutor backingNetwork=new ThreadPoolExecutor(2,2,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(2));
    private final ExecutorService videoNetwork=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(1));
    private volatile long connectionEpoch;
    private long roomEpoch;
    private volatile long controlEpoch;
    private JSONObject pendingPlay;
    private String currentFilesJob="";
    private final ExecutorService controlNetwork=Executors.newSingleThreadExecutor();
    private final PlaybackReportThrottle playbackReports=new PlaybackReportThrottle();
    private final ExecutorService backdropNetwork=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(1));
    private final SongBackdrop songBackdrop=new SongBackdrop();
    private JSONObject backdropJob;
    private boolean backdropBusy,backgroundsEnabled=true;
    private volatile boolean aiBackdropVisible;
    private final ExecutorService controlActions=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(16));
    private final LocalControlIntent localOriginal=new LocalControlIntent();
    private boolean controlBusy,protocolReady;
    private File currentOriginal,currentInstrumental;
    private final Set<Boolean> backingLoads=new HashSet<>();
    private static final long[] BACKING_RETRY_MS={350,1200,3000};
    private final int[] backingStartFailures=new int[2];
    private long backingRetryAt;
    private boolean backingRetryOriginal;
    private JSONArray startingLyrics;
    private long[] startingEntries;
    private long alternateRetryAt;
    private JSONObject failedAlternateSpec;
    private String videoRequestedSha="";
    private long videoRetryAt;
    private boolean backingReceipts;
    private boolean queueClaims;
    private String presentedSong="",refusedStartSong="";
    private long waitForQueueRead;
    private PlaybackStartClaim playbackStart,activePlaybackClaim;
    private boolean waitingForRenderer;
    private final AtomicInteger generation=new AtomicInteger();
    private final ExecutorService preloader=new ThreadPoolExecutor(1,1,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<Runnable>(1));
    private AtomicBoolean prefetchCancel=new AtomicBoolean();
    private boolean prefetchBusy;private String prefetchId="",prefetchEdition="",prefetchedId="",prefetchedEdition="";private long prefetchAttempt;
    private AtomicBoolean transferCancel=new AtomicBoolean();
    private final ArrayList<JSONObject> jobs=new ArrayList<>();
    private final ArrayList<TextView> lyricViews=new ArrayList<>();
    private final Set<String> played=new LinkedHashSet<>();
    private SharedPreferences preferences;
    private final String tvInstance=UUID.randomUUID().toString();
    private CoordinatorSession coordinatorSession=new CoordinatorSession();
    private final CoordinatorStatus coordinatorStatus=new CoordinatorStatus();
    private PlaybackTimeline playbackTimeline;
    private volatile long readinessAt;
    private long coordinatorReceivedAt;
    private boolean coordinatorVisible;
    private volatile KtvClient client;
    private AudioEngine engine;
    private SoundEffectPlayer soundEffects;
    private TvUpdater updater;
    private PartyEffectsView partyEffects;
    private long shownSfx;
    private AudioManager audio;
    private FrameLayout page,smallQr;
    private View debugShade,lyricContrast;
    private ImageView background,smallQrImage,bigQr,requesterAvatar;
    private TextureView picture;
    private LinearLayout idle,lyrics,byline;
    private ScrollView lyricScroll;
    private LinearLayout mvLyrics;
    private TextView mvPrevious,mvCurrent,mvNext;
    private int countdownLine=-1,countdownValue;
    private int visibleMvLyric=-2;
    private final LyricCue lyricCue=new LyricCue();
    private static final String LYRIC_LEAD="●  ●  ●  ●  ●  ";
    private static final int STAGE_MOTION_MS=150;
    private ValueAnimator lyricMotion;
    private int lyricMotionTarget;
    private boolean alignmentPosted,motionEnabled,noticeWaiting,awaitingPlaybackPresentation;
    private long[] lyricStarts=new long[0];
    private long[] entryStarts;
    private TextView headline,notice,trackTitle,requester,nextSong,clock,connectionBadge;
    private TextView updateBadge,qrCue;
    private ProgressBar progress,roomProgress;
    private MediaPlayer video;
    private Surface videoSurface;
    private File videoFile;
    private Bitmap atlas,qrBitmap;
    private AlertDialog qrPreview;
    private JSONArray lines=new JSONArray();
    private JSONObject current,currentManifest;
    private String theme="content",currentId="";
    private volatile boolean foreground;
    private volatile boolean debugScreen;
    private volatile boolean autoRoom=true;
    private boolean pollBusy,roomReportBusy,roomQrBusy,jobsBusy,loading,paused=true,videoReady,videoRendered,videoSeeking,original;
    private Button warmupButton;private final QueuePlaybackIntent queueIntent=new QueuePlaybackIntent();
    private boolean warmup;
    private final Map<String,JSONObject> warmupJobs=new LinkedHashMap<>();
    private final Map<String,Long> warmupFailed=new HashMap<>(),lyricsWait=new HashMap<>();
    private final Map<String,JSONArray> preparedLyrics=new HashMap<>();
    private final Map<String,long[]> preparedEntries=new HashMap<>();
    private final Map<String,String> lyricEditions=new HashMap<>();
    private final Map<String,String> lyricManifestVersions=new HashMap<>();
    private final Map<String,String> exhaustedLyricVersions=new HashMap<>();
    private final Map<String,Object> lyricsInFlight=new HashMap<>();
    private final WarmupShuffle warmupShuffle=new WarmupShuffle();
    private final RemoteShortcuts remoteShortcuts=new RemoteShortcuts();
    private String roomId="";
    private String roomInvitation="";
    private AlertDialog endRoomDialog;
    private long lastCommand,appliedRevision=-1;
    private long roomOriginalRevision=-1;
    private int roomOriginal;
    private boolean controlsInitialized;
    private long nextConnectAttempt;private int connectionFailures;
    private long nextJobPoll,nextQrPoll,nextDiagnosticPoll,lastControlStateAt;
    private long roomReadSequence,lastAppliedRoomRead;
    private JSONArray roomOrder,roomUpcoming,roomPlayable;
    private long queueRevision=-1;
    private String roomMessage="正在连接房间…";
    private boolean roomRetry;
    private int installedVersion;
    private long resumeFrame,videoOffsetMs,videoDurationMs;
    private final VideoSync videoClock=new VideoSync();
    private long videoSeekCount,videoBufferEvents,videoSurfaceFrames,videoPositionMs,videoTargetMs,seekResultMs,videoPrepareCount,routeRestartCount;
    private float videoSpeed=1;private boolean videoSpeedSupported=true;
    private int activeLine=-1,ticks,accent=0xfffa6670;
    private long shownSecond=-1,shownDurationSecond=-1;
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
        engine=AudioEngine.forPlayback(this,(code,natural)->ui.post(()->ended(code,natural)));
        soundEffects=new SoundEffectPlayer(this);
        updater=TvUpdater.forApp(this);
        build();audio.registerAudioDeviceCallback(devices,ui);
        try(InputStream in=getAssets().open("avatars.png")){atlas=BitmapFactory.decodeStream(in);}catch(Exception ignored){}
        connect();
        if(state!=null&&state.getBoolean("debug_screen"))setDebugScreen(true);
    }
    private boolean userPlayback(){
        if(debugScreen||updater.blocksPlayback())return false;
        queueIntent.enablePlayback();
        return true;
    }
    boolean canApplyTvUpdate(){
        return TvUpdateSpec.idleForInstall(foreground&&!isFinishing()&&!isDestroyed(),hasWindowFocus(),
            debugScreen,current!=null,loading,engine.running);
    }
    private boolean tryTvUpdate(){
        boolean busy=updater.advance(this,client);
        String hint=foreground&&!debugScreen&&current==null?updater.idleHint():"";
        if(!hint.contentEquals(updateBadge.getText())){
            updateBadge.setText(hint);
            updateBadge.setVisibility(hint.isEmpty()?View.GONE:View.VISIBLE);
        }
        return busy;
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
    private void toast(String message){Toast.makeText(this,message,Toast.LENGTH_SHORT).show();}
    private TextView text(String value,int size,int color){TextView v=new TextView(this);v.setText(value);v.setTextSize(size);v.setTextColor(color);v.setIncludeFontPadding(false);v.setLineSpacing(0,1.25f);return v;}
    private void textShadow(TextView label,boolean active){
        float density=getResources().getDisplayMetrics().density;
        label.setShadowLayer((active?3.4f:2.2f)*density,0,(active?.9f:.6f)*density,
            active?0xf4000000:0xe8000000);
    }
    private FrameLayout.LayoutParams box(int width,int height,int gravity,int x,int y){FrameLayout.LayoutParams p=new FrameLayout.LayoutParams(width<0?width:dp(width),height<0?height:dp(height),gravity);p.setMargins(dp(x),dp(y),dp(x),dp(y));return p;}
    private GradientDrawable surface(int color,int radius){GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(radius));return d;}
    private void build(){
        FrameLayout root=new FrameLayout(this);root.setBackgroundColor(Color.BLACK);setContentView(root);
        page=new FrameLayout(this);page.setBackgroundColor(0xff0c1110);page.setFocusable(true);
        page.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        root.addView(page,new FrameLayout.LayoutParams(-1,-1));
        background=new ImageView(this);background.setScaleType(ImageView.ScaleType.CENTER_CROP);page.addView(background,new FrameLayout.LayoutParams(-1,-1));
        background.addOnLayoutChangeListener((view,left,top,right,bottom,oldLeft,oldTop,oldRight,oldBottom)->{
            if(right-left!=oldRight-oldLeft||bottom-top!=oldBottom-oldTop)loadSongBackground();
        });
        picture=new TextureView(this);picture.setSurfaceTextureListener(this);picture.setAlpha(0);page.addView(picture,new FrameLayout.LayoutParams(-1,-1));
        lyricContrast=new View(this){
            private final Paint shade=new Paint();
            @Override protected void onSizeChanged(int w,int h,int oldw,int oldh){
                super.onSizeChanged(w,h,oldw,oldh);
                if(w>0)shade.setShader(new LinearGradient(0,0,w,0,
                    new int[]{0x54000000,0x49000000,0x1c000000,0x00000000},
                    new float[]{0,.14f,.34f,.55f},Shader.TileMode.CLAMP));
            }
            @Override protected void onDraw(Canvas canvas){canvas.drawRect(0,0,getWidth(),getHeight(),shade);}
        };
        lyricContrast.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        lyricContrast.setVisibility(View.GONE);page.addView(lyricContrast,new FrameLayout.LayoutParams(-1,-1));
        TextView brand=text("Friday KTV",20,WHITE);brand.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));brand.setContentDescription("Friday KTV，长按打开房间维护");brand.setOnLongClickListener(v->{maintenance();return true;});page.addView(brand,box(-2,-2,Gravity.TOP|Gravity.START,32,27));
        updateBadge=text("",13,MUTED);updateBadge.setVisibility(View.GONE);
        updateBadge.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        page.addView(updateBadge,box(-2,-2,Gravity.TOP|Gravity.START,32,61));
        connectionBadge=text("",13,MUTED);connectionBadge.setMaxLines(2);connectionBadge.setGravity(Gravity.END);connectionBadge.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);FrameLayout.LayoutParams connection=box(-1,-2,Gravity.TOP|Gravity.END,150,30);connection.leftMargin=dp(230);page.addView(connectionBadge,connection);
        smallQr=new FrameLayout(this);
        smallQrImage=new ImageView(this);
        smallQrImage.setScaleType(ImageView.ScaleType.FIT_CENTER);
        smallQrImage.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        smallQr.addView(smallQrImage,new FrameLayout.LayoutParams(dp(86),dp(86),Gravity.CENTER));
        smallQr.setContentDescription("扫码点歌，按确定放大二维码");
        smallQr.setFocusable(true);smallQr.setClickable(true);smallQr.setOnClickListener(v->enlargeQr());
        qrCue=text("↓ 手机点歌",13,MUTED);qrCue.setGravity(Gravity.CENTER);
        qrCue.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        page.addView(qrCue,box(100,-2,Gravity.TOP|Gravity.END,28,132));
        smallQr.setOnFocusChangeListener((v,focus)->updateQrFrame(focus));updateQrFrame(false);
        showSmallQr(false);page.addView(smallQr,box(100,100,Gravity.TOP|Gravity.END,28,25));
        idle=new LinearLayout(this);idle.setOrientation(LinearLayout.VERTICAL);idle.setGravity(Gravity.CENTER);idle.setPadding(dp(20),dp(4),dp(20),dp(4));
        headline=text("",36,WHITE);headline.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));headline.setGravity(Gravity.CENTER);idle.addView(headline);
        bigQr=new ImageView(this);bigQr.setPadding(dp(12),dp(12),dp(12),dp(12));bigQr.setBackground(surface(0xffffffff,18));bigQr.setContentDescription("本场点歌二维码");bigQr.setVisibility(View.GONE);
        int heightDp=(int)(getResources().getDisplayMetrics().heightPixels/getResources().getDisplayMetrics().density);int qrSize=Math.min(226,Math.max(150,heightDp-285));LinearLayout.LayoutParams qr=new LinearLayout.LayoutParams(dp(qrSize),dp(qrSize));qr.topMargin=dp(22);qr.bottomMargin=dp(16);idle.addView(bigQr,qr);
        warmupButton=new Button(this);warmupButton.setText("播放暖场");warmupButton.setTextSize(17);warmupButton.setAllCaps(false);warmupButton.setMinWidth(dp(160));warmupButton.setPadding(dp(24),0,dp(24),0);warmupButton.setFocusable(true);warmupButton.setStateListAnimator(null);warmupButton.setContentDescription("随机播放已准备好的暖场歌曲");warmupButton.setTextColor(WHITE);warmupButton.setBackground(surface(0xcc1c2421,24));warmupButton.setOnFocusChangeListener((v,focus)->{warmupButton.setTextColor(focus?0xff101612:WHITE);GradientDrawable d=surface(focus?WHITE:0xcc1c2421,24);d.setStroke(dp(2),focus?WHITE:0x77ffffff);warmupButton.setBackground(d);});warmupButton.setOnClickListener(v->toggleWarmup());page.addView(warmupButton,box(-2,48,Gravity.BOTTOM|Gravity.CENTER_HORIZONTAL,32,70));
        LinearLayout roomFeedback=new LinearLayout(this);
        roomFeedback.setGravity(Gravity.CENTER);
        roomProgress=new ProgressBar(this,null,android.R.attr.progressBarStyleSmall);
        roomProgress.setIndeterminate(true);
        roomProgress.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(MUTED));
        roomProgress.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        LinearLayout.LayoutParams indicator=new LinearLayout.LayoutParams(dp(18),dp(18));
        indicator.setMarginEnd(dp(12));
        roomFeedback.addView(roomProgress,indicator);
        notice=text("正在连接房间…",16,MUTED);notice.setGravity(Gravity.CENTER);notice.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        roomFeedback.addView(notice);idle.addView(roomFeedback);
        page.addView(idle,box(-1,-2,Gravity.CENTER,140,60));
        lyricScroll=new ScrollView(this);lyricScroll.setFillViewport(false);lyricScroll.setVerticalScrollBarEnabled(false);lyricScroll.setOverScrollMode(View.OVER_SCROLL_NEVER);lyricScroll.setClipToPadding(false);lyricScroll.setVisibility(View.GONE);
        lyricScroll.addOnLayoutChangeListener((v,left,top,right,bottom,oldLeft,oldTop,oldRight,oldBottom)->{
            if(right-left!=oldRight-oldLeft||bottom-top!=oldBottom-oldTop)requestLyricAlignment();
        });
        lyrics=new LinearLayout(this);lyrics.setOrientation(LinearLayout.VERTICAL);lyricScroll.addView(lyrics,new ScrollView.LayoutParams(-1,-2));FrameLayout.LayoutParams lyricArea=box(-1,-1,Gravity.TOP|Gravity.START,64,94);lyricArea.rightMargin=dp(190);lyricArea.bottomMargin=dp(104);page.addView(lyricScroll,lyricArea);
        mvLyrics=new LinearLayout(this);mvLyrics.setOrientation(LinearLayout.VERTICAL);mvLyrics.setPadding(dp(64),dp(32),dp(64),dp(20));mvLyrics.setVisibility(View.GONE);
        mvPrevious=text("",26,0xffe2e2e2);mvPrevious.setPadding(0,0,0,dp(8));textShadow(mvPrevious,false);mvLyrics.addView(mvPrevious);
        mvCurrent=text("",34,WHITE);mvCurrent.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));textShadow(mvCurrent,true);mvLyrics.addView(mvCurrent);
        mvNext=text("",22,0xffdddddd);mvNext.setPadding(0,dp(9),0,0);textShadow(mvNext,false);mvLyrics.addView(mvNext);FrameLayout.LayoutParams cues=new FrameLayout.LayoutParams(-1,-2,Gravity.BOTTOM);cues.bottomMargin=dp(98);page.addView(mvLyrics,cues);
        trackTitle=text("",19,WHITE);trackTitle.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));FrameLayout.LayoutParams title=box(-1,-2,Gravity.TOP|Gravity.START,32,62);title.rightMargin=dp(170);page.addView(trackTitle,title);
        LinearLayout footer=new LinearLayout(this);footer.setOrientation(LinearLayout.VERTICAL);footer.setPadding(dp(32),dp(12),dp(32),dp(20));
        byline=new LinearLayout(this);byline.setGravity(Gravity.CENTER_VERTICAL);requesterAvatar=new ImageView(this);byline.addView(requesterAvatar,new LinearLayout.LayoutParams(dp(30),dp(30)));requester=text("",14,MUTED);LinearLayout.LayoutParams name=new LinearLayout.LayoutParams(0,-2,1);name.setMarginStart(dp(10));byline.addView(requester,name);nextSong=text("",13,MUTED);nextSong.setGravity(Gravity.END);nextSong.setMaxLines(2);byline.addView(nextSong,new LinearLayout.LayoutParams(0,-2,1));footer.addView(byline);
        progress=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);progress.setMax(10000);progress.setProgressTintList(android.content.res.ColorStateList.valueOf(WHITE));progress.setProgressBackgroundTintList(android.content.res.ColorStateList.valueOf(0x80000000));LinearLayout.LayoutParams bar=new LinearLayout.LayoutParams(-1,dp(3));bar.topMargin=dp(12);footer.addView(progress,bar);
        clock=text("",11,MUTED);clock.setFontFeatureSettings("tnum");LinearLayout.LayoutParams time=new LinearLayout.LayoutParams(-1,-2);time.topMargin=dp(7);footer.addView(clock,time);page.addView(footer,new FrameLayout.LayoutParams(-1,-2,Gravity.BOTTOM));byline.setVisibility(View.GONE);progress.setVisibility(View.INVISIBLE);setTheme("content");
        for(TextView label:new TextView[]{brand,updateBadge,connectionBadge,headline,notice,trackTitle,requester,nextSong,clock})textShadow(label,label==headline);
        partyEffects=new PartyEffectsView(this);page.addView(partyEffects,new FrameLayout.LayoutParams(-1,-1));
        debugShade=new View(this);
        debugShade.setBackgroundColor(Color.BLACK);
        debugShade.setContentDescription("黑屏调试中，按返回或确定恢复画面");
        debugShade.setFocusableInTouchMode(true);
        debugShade.setVisibility(View.GONE);
        root.addView(debugShade,new FrameLayout.LayoutParams(-1,-1));
    }
    private void updateQrFrame(boolean focused){
        GradientDrawable frame=surface(Color.WHITE,12);
        if(focused)frame.setStroke(dp(3),accent);
        smallQr.setBackground(frame);
        String hint=focused?"确定 · 放大\n↑ 返回":"↓ 手机点歌";
        if(!hint.contentEquals(qrCue.getText()))qrCue.setText(hint);
        shownSecond=-1;
    }
    private void showSmallQr(boolean visible){
        smallQr.setVisibility(visible?View.VISIBLE:View.INVISIBLE);
        qrCue.setVisibility(visible&&current!=null?View.VISIBLE:View.INVISIBLE);
    }
    private void setDebugScreen(boolean value){
        if(debugScreen==value)return;
        debugScreen=value;readinessAt=0;controlEpoch++;
        if(value){
            pendingPlay=null;
            queueIntent.holdPlayback();
            stopAudio(true);
            page.setVisibility(View.INVISIBLE);debugShade.setVisibility(View.VISIBLE);debugShade.requestFocus();
        }else{
            debugShade.setVisibility(View.GONE);page.setVisibility(View.VISIBLE);
            showIdle();
            if(warmupButton.getVisibility()==View.VISIBLE)warmupButton.requestFocus();
        }
    }
    private void setTheme(String value){
        if(!Arrays.asList("content","festival","live").contains(value))return;
        if(background.getDrawable()!=null && theme.equals(value))return;theme=value;accent=theme.equals("festival")?0xffeee353:0xfffa6670;
        if(!aiBackdropVisible)showThemeBackdrop();
        if(activeLine>=0 && activeLine<lyricViews.size())lyricViews.get(activeLine).setTextColor(theme.equals("festival")?accent:WHITE);
        if(smallQr.hasFocus())updateQrFrame(true);
    }
    private void showThemeBackdrop(){
        aiBackdropVisible=false;
        try(InputStream in=getAssets().open(theme+".png")){background.setImageBitmap(BitmapFactory.decodeStream(in));}catch(Exception ignored){}
        updateLyricContrast();
    }
    private void updateLyricContrast(){
        lyricContrast.setVisibility(aiBackdropVisible&&lyricScroll.getVisibility()==View.VISIBLE?View.VISIBLE:View.GONE);
    }
    private void showScrollingLyrics(boolean show){
        int visibility=show?View.VISIBLE:View.GONE;
        if(lyricScroll.getVisibility()!=visibility){
            if(!show){
                cancelLyricAlignment();
                lyricScroll.animate().cancel();
                lyricScroll.setAlpha(1f);
            }
            lyricScroll.setVisibility(visibility);
            if(show)requestLyricAlignment();
        }
        updateLyricContrast();
    }
    private void showSongBackgrounds(boolean allowed){
        if(backgroundsEnabled==allowed)return;
        backgroundsEnabled=allowed;
        if(!allowed){
            songBackdrop.clear();
            if(aiBackdropVisible)showThemeBackdrop();
        }
    }
    private void songBackground(JSONObject job){
        final String song=job==null?"":job.optString("id","");
        if(songBackdrop.select(song)&&aiBackdropVisible)showThemeBackdrop();
        backdropJob=job;
        loadSongBackground();
    }
    private boolean uhdBackdrop(){
        Display display=background.getDisplay();
        if(display==null)return false;
        Display.Mode mode=display.getMode();
        return BackdropImagePolicy.benefitsFromUhd(mode.getPhysicalWidth(),mode.getPhysicalHeight(),
            background.getWidth(),background.getHeight());
    }
    private static JSONObject readyBackdrop(JSONObject job){
        JSONObject descriptor=job==null?null:job.optJSONObject("background");
        return descriptor!=null&&descriptor.optString("status").equals("ready")?descriptor:null;
    }
    private void loadSongBackground(){
        if(backdropBusy||!backgroundsEnabled||!foreground||debugScreen||client==null||current==null||backdropJob==null
            ||background.getWidth()==0||background.getHeight()==0)return;
        final String song=backdropJob.optString("id","");
        final JSONObject descriptor=readyBackdrop(backdropJob);
        if(!currentId.equals(song)||descriptor==null)return;
        final String hash=descriptor.optString("sha256");
        final boolean wantsUhd=uhdBackdrop();
        final long request=songBackdrop.begin(song,hash,wantsUhd,SystemClock.elapsedRealtime());
        if(request==0)return;

        final KtvClient owner=client;
        final int version=generation.get();
        final AtomicBoolean cancelled=transferCancel;
        final ActivityManager memory=getSystemService(ActivityManager.class);
        backdropBusy=true;
        try{
            backdropNetwork.execute(()->{
                Bitmap decoded=null;
                try{
                    File image=owner.downloadBackground(descriptor,cancelled);
                    decoded=BackdropImage.decode(image,wantsUhd,memory,cancelled);
                }catch(Exception|OutOfMemoryError ignored){}
                final Bitmap bitmap=decoded;
                ui.post(()->{
                    backdropBusy=false;
                    JSONObject desired=readyBackdrop(backdropJob);
                    boolean wanted=backgroundsEnabled&&foreground&&!debugScreen&&owner==client&&version==generation.get()&&current!=null
                        &&currentId.equals(song)&&desired!=null&&hash.equals(desired.optString("sha256"))&&wantsUhd==uhdBackdrop();
                    if(wanted&&bitmap!=null&&songBackdrop.accept(song,hash,request)){
                        bitmap.prepareToDraw();
                        background.setImageBitmap(bitmap);
                        aiBackdropVisible=true;
                        updateLyricContrast();
                    }else{
                        songBackdrop.fail(request,SystemClock.elapsedRealtime());
                        if(bitmap!=null)bitmap.recycle();
                    }
                    if(!wanted)loadSongBackground();
                });
            });
        }catch(RejectedExecutionException ignored){
            backdropBusy=false;
            songBackdrop.fail(request,SystemClock.elapsedRealtime());
        }
    }
    private void connect(){
        prefetchCancel.set(true);
        coordinatorSession.closed();coordinatorSession=new CoordinatorSession();
        autoRoom=true;
        coordinatorSession.requestOpen();readinessAt=0;coordinatorVisible=false;coordinatorStatus.disconnected(false);if(connectionBadge!=null)connectionBadge.setText("");
        connectionEpoch++;controlEpoch++;roomEpoch++;pendingPlay=null;stopAudio(true);protocolReady=false;roomId="";currentFilesJob="";currentOriginal=currentInstrumental=null;client=null;current=null;currentId="";jobs.clear();qrBitmap=null;
        playbackReleases.connectionChanged(null);
        resetRoomQueue();nextConnectAttempt=nextJobPoll=nextQrPoll=nextDiagnosticPoll=lastControlStateAt=0;connectionFailures=0;
        roomMessage="正在连接房间…";roomRetry=false;showIdle();
        String origin=preferences.getString("origin",SetupDefaults.ORIGIN),key=preferences.getString("key",SetupDefaults.KEY);
        try{client=new KtvClient(origin,key,new File(getCacheDir(),"audio"));client.instance(tvInstance);notice.setText("正在连接房间…");poll();}
        catch(Exception e){client=null;headline.setText("连接你的房间");setRoomNotice("按遥控器菜单键设置家庭服务。",false);}
        playbackReleases.connectionChanged(client);
        updater.newConnection(client);
    }
    private boolean ownsConnection(KtvClient c,long epoch){return foreground&&autoRoom&&c==client&&epoch==connectionEpoch;}
    private void poll(){
        long now=SystemClock.elapsedRealtime();
        if(client==null||pollBusy||!foreground||!autoRoom||now<nextConnectAttempt
            ||protocolReady&&lastControlStateAt!=0&&now-lastControlStateAt<1250)return;
        pollBusy=true;
        final KtvClient c=client;
        final long epoch=connectionEpoch;
        final long read=++roomReadSequence;
        final CoordinatorSession session=coordinatorSession;
        try{roomNetwork.execute(()->{try{
            JSONObject party=null;
            if(!c.negotiated()){
                JSONObject state=c.controlJson("/v1/status",null);
                int protocol=state.optInt("protocol");
                if(protocol!=1&&!(protocol==2&&state.optBoolean("coordinator")))throw new IOException("invalid_protocol");
                if(state.optBoolean("simulated")){
                    ui.post(()->{if(ownsConnection(c,epoch))roomConnection("模拟房间 · 测试音源",false);});
                    return;
                }
                c.observeStatus(state);
                party=state.optJSONObject("party");
            }
            if(party==null)party=c.controlJson("/v1/party",null);
            if("preparation_worker".equals(party.optString("role")))throw new IOException("nas_connection_required");
            String startRequest=session.claimOpen(ownsConnection(c,epoch),autoRoom,SystemClock.elapsedRealtime());
            if(!startRequest.isEmpty()){
                try{
                    if(c.coordinator()||!party.optBoolean("active"))
                        party=c.controlJson("/v1/party",c.coordinator()?new JSONObject().put("instance",tvInstance).put("request_id",startRequest):new JSONObject());
                }catch(KtvClient.HttpFailure failure){session.refused(startRequest,failure.status);throw failure;}
                if(party.optBoolean("active")&&!party.optString("room_id").isEmpty())session.acknowledged(startRequest);
            }
            final JSONObject room=party;
            final String id=room.optString("room_id");
            final boolean opened=room.optBoolean("active")&&!id.isEmpty()&&!session.pending();
            ui.post(()->{if(ownsConnection(c,epoch))publishRoom(c,room,opened,read);});
        }catch(Exception failure){
            ui.post(()->{if(ownsConnection(c,epoch)&&read>lastAppliedRoomRead)roomPollFailed(failure);});
        }finally{ui.post(()->pollBusy=false);}});}catch(RejectedExecutionException ignored){pollBusy=false;}
    }
    private boolean acceptRoomSnapshot(JSONObject room,boolean opened,long read){
        // 两路轮询共享请求顺序，较早请求的迟到结果不能覆盖邀请地址。
        if(read<=lastAppliedRoomRead)return false;
        lastAppliedRoomRead=read;
        if(opened)updateRoomInvitation(room);
        return true;
    }
    private void publishRoom(KtvClient c,JSONObject room,boolean opened,long read){
        if(!acceptRoomSnapshot(room,opened,read))return;
        connectionFailures=0;nextConnectAttempt=0;
        protocolReady=opened;
        coordinatorVisible=opened&&c.coordinator();
        if(!opened){
            connectionBadge.setText("");
            if(!coordinatorSession.pending()){
                if(!roomId.isEmpty()&&Boolean.FALSE.equals(room.opt("active"))){
                    roomEpoch++;controlEpoch++;pendingPlay=null;stopAudio(true);current=null;currentId="";warmup=false;
                    jobs.clear();resetRoomQueue();
                }
                qrBitmap=null;roomId="";showSmallQr(false);bigQr.setVisibility(View.GONE);
                roomConnection("本场已结束，可重新开启房间。",true);
                if(current==null)showIdle();
            }else roomOpening();
            return;
        }
        if(coordinatorVisible)observeReadiness(true,room.optJSONObject("readiness"));
        if(!roomId.equals(room.optString("room_id"))){
            showSmallQr(false);bigQr.setVisibility(View.GONE);qrBitmap=null;
            nextJobPoll=nextQrPoll=readinessAt=0;
            applyRoom(room);
        }
        setTheme(room.optString("theme","content"));
        roomConnection("房间已开启，正在准备点歌二维码…",false);
        if(current==null&&!loading)showIdle();
        refreshTvReadiness(c,roomId,connectionEpoch);
        refreshRoomQr(c,roomId,connectionEpoch);
        refreshRoomJobs(c,roomEpoch);
    }
    private void refreshTvReadiness(KtvClient c,String room,long epoch){
        long now=SystemClock.elapsedRealtime();
        if(!c.coordinator()||roomReportBusy||(readinessAt!=0&&now-readinessAt<3000))return;
        roomReportBusy=true;readinessAt=now;
        final boolean claims=queueClaims;
        try{roomReports.execute(()->{
            try{
                if(!ownsConnection(c,epoch))return;
                JSONObject ready=c.controlJson("/v1/tv/readiness",readiness(c,claims));
                ui.post(()->{if(ownsConnection(c,epoch)&&room.equals(roomId))observeReadiness(true,ready);});
            }catch(Exception ignored){}
            finally{ui.post(()->roomReportBusy=false);}
        });}catch(RejectedExecutionException ignored){roomReportBusy=false;}
    }
    private void refreshRoomQr(KtvClient c,String room,long epoch){
        if(roomQrBusy||SystemClock.elapsedRealtime()<nextQrPoll)return;
        final String invitation=roomInvitation;
        roomQrBusy=true;
        try{roomQrNetwork.execute(()->{
            final byte[] qr=ownsConnection(c,epoch)?roomQr(c):null;
            ui.post(()->{
                roomQrBusy=false;
                if(!acceptsRoomQr(c,room,epoch,invitation))return;
                Bitmap decoded=qr==null?null:BitmapFactory.decodeByteArray(qr,0,qr.length);
                if(decoded!=null){
                    qrBitmap=decoded;smallQrImage.setImageBitmap(decoded);bigQr.setImageBitmap(decoded);
                    if(current!=null)showSmallQr(true);
                    else if(!loading)showIdle();
                }
                nextQrPoll=SystemClock.elapsedRealtime()+(decoded!=null?30000:1000);
            });
        });}catch(RejectedExecutionException ignored){roomQrBusy=false;}
    }
    private boolean acceptsRoomQr(KtvClient c,String room,long epoch,String invitation){
        return protocolReady&&ownsConnection(c,epoch)&&room.equals(roomId)&&invitation.equals(roomInvitation);
    }
    private void updateRoomInvitation(JSONObject room){
        String invitation=room.optString("join_url");
        if(invitation.isEmpty()||invitation.equals(roomInvitation))return;
        roomInvitation=invitation;
        qrBitmap=null;
        if(qrPreview!=null)qrPreview.dismiss();
        showSmallQr(false);
        bigQr.setVisibility(View.GONE);
        nextQrPoll=0;
    }
    private void roomOpening(){
        switch(coordinatorSession.refusal()){
            case 401:
            case 403:roomConnection("电视连接密钥未通过验证，请按菜单键检查设置。",true);break;
            case 409:roomConnection("电视的房间连接已变化，可以重新连接。",true);break;
            case 410:roomConnection("上次申请的房间已结束，可以重新开启。",true);break;
            case 400:roomConnection("房间服务拒绝了开房请求，请检查连接设置。",true);break;
            default:
                boolean slow=coordinatorSession.slow();
                roomConnection(slow?"仍在等待房间服务，正在自动重试…":"正在开启房间…",slow);
        }
    }
    private void roomConnection(String message,boolean canRetry){
        boolean focusRetry=canRetry&&!roomRetry;
        roomMessage=message;roomRetry=canRetry;
        if(current==null&&qrBitmap==null&&autoRoom){
            setRoomNotice(message,!canRetry);
            if(!protocolReady){warmupButton.setText("重试连接");warmupButton.setVisibility(canRetry?View.VISIBLE:View.GONE);if(focusRetry)warmupButton.requestFocus();}
        }
    }
    private void roomPollFailed(Exception failure){
        connectionFailures=Math.min(4,connectionFailures+1);
        nextConnectAttempt=SystemClock.elapsedRealtime()+(1L<<connectionFailures)*500;
        refreshCoordinatorStatus();
        String code=failure.getMessage();
        if("nas_connection_required".equals(code))roomConnection("请按菜单键，将服务连接更新为 NAS。",true);
        else if("unauthorized".equals(code))roomConnection("电视连接密钥未通过验证，请按菜单键检查设置。",true);
        else if(coordinatorSession.refusal()!=0||coordinatorSession.slow())roomOpening();
        else if(connectionFailures>=2)roomConnection("暂时连不上房间服务，正在重试。",true);
        else roomConnection("正在重新连接房间…",false);
    }
    private void refreshRoomJobs(KtvClient c,long epoch){
        long now=SystemClock.elapsedRealtime();
        if(jobsBusy||now<nextJobPoll)return;
        nextJobPoll=now+1000;jobsBusy=true;
        final boolean sendDiagnostic=now>=nextDiagnosticPoll;
        if(sendDiagnostic)nextDiagnosticPoll=now+3000;
        try{network.execute(()->{
            try{
                JSONObject response=c.json("/v1/jobs",null);
                JSONArray incoming=response.optJSONArray("jobs");
                boolean artworkEnabled=response.optBoolean("backgrounds_enabled",true);
                JSONArray values=new JSONArray();
                if(incoming!=null)for(int i=0;i<incoming.length();i++){
                    JSONObject job=incoming.optJSONObject(i);
                    if(job!=null&&c.manifestValid(job))values.put(job);
                }
                if(incoming!=null)ui.post(()->{
                    if(!foreground||!autoRoom||c!=client||epoch!=roomEpoch||!protocolReady)return;
                    showSongBackgrounds(artworkEnabled);
                    jobs.clear();
                    boolean refreshedCurrent=false;
                    for(int i=0;i<values.length();i++){
                        JSONObject job=values.optJSONObject(i);
                        String id=job.optString("id"),version=lyricVersion(job);
                        if(!version.equals(lyricManifestVersions.put(id,version)))lyricsWait.remove(id);
                        if(roomOrder==null||orderIndex(roomOrder,job.optString("id"))!=Integer.MAX_VALUE)jobs.add(job);
                        if(updateCurrentManifest(job))refreshedCurrent=true;
                    }
                    refreshWarmup(values);sortJobs();
                    if(refreshedCurrent){
                        songBackground(currentManifest);
                        maybeLoadVideo(currentManifest);
                        loadAlternateBacking();
                    }
                    runPendingPlay();refreshMissingLyrics();prepareFirstLyrics();refreshQueuePlayback();protectCache();updateNext();prefetchNext();
                });
            }catch(Exception ignored){}
            try{if(sendDiagnostic&&foreground&&autoRoom&&c==client)c.controlJson("/v1/tv/diagnostics",telemetry());}catch(Exception ignored){}
            finally{ui.post(()->jobsBusy=false);}
        });}catch(RejectedExecutionException ignored){jobsBusy=false;}
    }
    private byte[] roomQr(KtvClient c){try{return c.image("/v1/party/qr");}catch(Exception notReady){return null;}}
    /** 只检查歌曲输出和缓存是否可用，房间协议保留旧录音就绪位。 */
    private JSONObject readiness(KtvClient c,boolean claims)throws JSONException{
        int speakers=0;
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))
            if(d.isSink()&&d.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)speakers++;
        boolean playback=false;
        try{playback=AudioTrack.getMinBufferSize(48000,AudioFormat.CHANNEL_OUT_STEREO,AudioFormat.ENCODING_PCM_16BIT)>0;}
        catch(RuntimeException ignored){}
        JSONObject result=new JSONObject().put("instance",tvInstance).put("protocol",1)
            .put("playback",playback&&speakers==1&&!debugScreen).put("audio",false).put("media",c.storageReady());
        if(c.compactNegotiated())result.put("audio_formats",new JSONArray().put("pcm_s16le_wav").put("aac_lc_m4a"));
        if(claims)result.put("queue_claims",true);
        return result;
    }
    private void observeReadiness(boolean active,JSONObject ready){
        coordinatorReceivedAt=SystemClock.elapsedRealtime();
        Map<String,Boolean> gates=new HashMap<>();JSONObject values=ready==null?null:ready.optJSONObject("gates");
        if(values!=null)for(String key:CoordinatorStatus.GATES)gates.put(key,Boolean.TRUE.equals(values.opt(key)));
        coordinatorStatus.observe(active,!coordinatorSession.pending()&&ready!=null&&Boolean.TRUE.equals(ready.opt("ready")),gates);
        if(!coordinatorStatus.text().contentEquals(connectionBadge.getText()))connectionBadge.setText(coordinatorStatus.text());
    }
    private void refreshCoordinatorStatus(){if(coordinatorVisible&&coordinatorStatus.expire(SystemClock.elapsedRealtime()-coordinatorReceivedAt,engine.running))connectionBadge.setText(coordinatorStatus.text());}
    private int orderIndex(JSONArray values,String id){for(int i=0;i<values.length();i++)if(id.equals(values.optString(i)))return i;return Integer.MAX_VALUE;}
    private void resetRoomQueue(){
        queueClaims=false;
        presentedSong=refusedStartSong="";
        waitForQueueRead=0;
        roomOrder=null;
        roomUpcoming=null;
        roomPlayable=null;
        currentManifest=null;
        queueRevision=-1;
        warmupJobs.clear();
        warmupFailed.clear();
        preparedLyrics.clear();
        preparedEntries.clear();
        lyricEditions.clear();
        lyricManifestVersions.clear();
        exhaustedLyricVersions.clear();
        lyricsWait.clear();
        lyricsInFlight.clear();
    }
    private void sortJobs(){
        if(roomOrder!=null)jobs.sort(Comparator.comparingInt(job->orderIndex(roomOrder,job.optString("id"))));
    }
    private void applyQueue(JSONObject room){
        long revision=room.optLong("queue_revision",-1);
        if(revision>=0&&revision<queueRevision)return;
        boolean changed=revision<0||revision!=queueRevision||roomOrder==null;
        if(revision>=0)queueRevision=revision;
        if(changed)roomOrder=room.optJSONArray("order");
        roomUpcoming=room.optJSONArray("upcoming");
        roomPlayable=room.optJSONArray("playable");
        if(pendingPlay!=null&&!remoteTargetEligible(pendingPlay.optString("song")))pendingPlay=null;
        if(changed&&roomOrder!=null){
            jobs.removeIf(job->orderIndex(roomOrder,job.optString("id"))==Integer.MAX_VALUE);
            if(pendingPlay!=null&&orderIndex(roomOrder,pendingPlay.optString("song"))==Integer.MAX_VALUE)pendingPlay=null;
            sortJobs();
        }
        if(waitForQueueRead!=0&&lastAppliedRoomRead>waitForQueueRead){waitForQueueRead=0;refusedStartSong="";}
        if(queueClaims&&current!=null&&!presentedCurrent()&&roomOrder!=null
            &&orderIndex(roomOrder,currentId)==Integer.MAX_VALUE)discardPlaybackSelection(false);
        if(prefetchBusy&&!prefetchId.equals(nextSelectionId()))prefetchCancel.set(true);
    }
    private final Runnable controlTick=new Runnable(){public void run(){
        if(!foreground)return;
        if(client!=null&&autoRoom&&protocolReady&&!controlBusy){
            controlBusy=true;final KtvClient c=client;final long epoch=controlEpoch;
            final long read=++roomReadSequence;
            try{controlNetwork.execute(()->{try{
                JSONObject room=c.controlJson("/v1/party",null);
                ui.post(()->handleControlState(c,epoch,room,read));
            }catch(Exception ignored){ui.post(()->controlBusy=false);}});}catch(RejectedExecutionException ignored){controlBusy=false;}
        }
        ui.postDelayed(this,150);
    }};
    private void handleControlState(KtvClient c,long epoch,JSONObject room,long read){
        if(!foreground||!autoRoom||epoch!=controlEpoch||c!=client||!protocolReady
            ||!room.optBoolean("active")||!roomId.equals(room.optString("room_id"))
            ||!acceptRoomSnapshot(room,true,read)){
            controlBusy=false;return;
        }
        lastControlStateAt=SystemClock.elapsedRealtime();
        String priorTheme=theme;
        setTheme(room.optString("theme","content"));
        applyRoom(room);
        if(c.coordinator())observeReadiness(true,room.optJSONObject("readiness"));
        refreshTvReadiness(c,roomId,connectionEpoch);
        refreshRoomQr(c,roomId,connectionEpoch);
        refreshRoomJobs(c,roomEpoch);
        if(!priorTheme.equals(theme)&&current==null&&!loading)showIdle();
        // 在主线程应用当前状态后再生成同一轮回报，避免多等一次网络轮询。
        final JSONObject report=playbackReport();
        try{controlNetwork.execute(()->{try{
            if(epoch!=controlEpoch||!foreground||c!=client)return;
            long position=report.optLong("position_ms");
            report.remove("position_ms");
            String payload=report.toString();
            boolean playing="playing".equals(report.optString("state"));
            if(playbackReports.shouldSend(epoch,payload,playing,SystemClock.elapsedRealtime())
                &&c.controlJson(KtvClient.PLAYBACK_REPORT,report.put("position_ms",position)).optBoolean("accepted"))
                playbackReports.accepted(epoch,payload,SystemClock.elapsedRealtime());
        }catch(KtvClient.HttpFailure refused){
            if(refused.televisionChanged())ui.post(()->televisionOwnershipLost(c,epoch));
        }catch(Exception ignored){}finally{ui.post(()->controlBusy=false);}});}catch(RejectedExecutionException ignored){controlBusy=false;}
    }
    private void televisionOwnershipLost(KtvClient owner,long epoch){
        if(!foreground||!autoRoom||owner!=client||epoch!=controlEpoch||!owner.coordinator())return;
        autoRoom=protocolReady=coordinatorVisible=false;
        connectionEpoch++;controlEpoch++;roomEpoch++;
        coordinatorSession.closed();queueIntent.holdPlayback();stopAudio(true);
        if(qrPreview!=null)qrPreview.dismiss();
        current=null;currentId="";pendingPlay=null;roomId="";roomInvitation="";qrBitmap=null;warmup=false;
        jobs.clear();resetRoomQueue();connectionBadge.setText("");
        roomMessage="本场已由另一台电视接管。";
        showIdle();
    }
    private void switchMusic(){
        if(current==null)return;
        if(loading){loadSelectedBacking();return;}
        if(!engine.running)return;
        File file=original?currentOriginal:currentInstrumental;
        if(file!=null)engine.switchBacking(file);
        else loadAlternateBacking();
    }

    void applyRoomControls(JSONObject controls){
        if(controls==null)return;
        long revision=controls.optLong("control_revision",0);
        if(revision<appliedRevision)return;
        int originalValue=controls.optInt("original",0);
        if(!controlsInitialized||roomOriginal!=originalValue){roomOriginal=originalValue;roomOriginalRevision=revision;}
        boolean nextOriginal=localOriginal.resolve(originalValue,revision,roomOriginalRevision)!=0;
        if(nextOriginal!=original){original=nextOriginal;switchMusic();}
        if(revision!=appliedRevision){
            soundEffects.gain(controls.optInt("sfx_gain",60));
            engine.mixer.musicGain=Math.max(0,Math.min(100,controls.optInt("music_gain",70)))/100f;
        }
        controlsInitialized=true;
        appliedRevision=revision;
    }
    private void applyRoom(JSONObject room){
        String id=room.optString("room_id","");
        if(!id.equals(roomId)){
            roomEpoch++;controlEpoch++;pendingPlay=null;jobs.clear();resetRoomQueue();
            currentOriginal=currentInstrumental=null;currentFilesJob="";
            prefetchCancel.set(true);prefetchedId="";warmup=false;
            stopAudio(true);current=null;currentId="";resumeFrame=0;
            if(!id.equals(preferences.getString("played-room",""))){
                played.clear();
                preferences.edit().remove("played").putString("played-room",id).apply();
            }
            roomId=id;shownSfx=0;soundEffects.resetRoom();localOriginal.clear();
            lastCommand=preferences.getLong("command:"+id,0);
            controlsInitialized=false;appliedRevision=-1;
            queueIntent.roomChanged();
        }
        backingReceipts=room.optBoolean("backing_receipts");
        boolean claims=client!=null&&client.coordinator()&&Boolean.TRUE.equals(room.opt("queue_claims"));
        boolean resumeWithoutClaim=queueClaims&&!claims&&playbackStart!=null;
        if(claims!=queueClaims){queueClaims=claims;readinessAt=0;}
        applyQueue(room);
        if(resumeWithoutClaim)cancelPlaybackStart();
        applyRoomControls(room.optJSONObject("controls"));
        if(resumeWithoutClaim&&loading)loadSelectedBacking();

        JSONArray commands=room.optJSONArray("commands");
        KtvClient owner=client;
        if(commands==null||owner==null)return;
        if(!queueIntent.remoteCommandsFresh()){
            for(int i=0;i<commands.length();i++){JSONObject command=commands.optJSONObject(i);if(command!=null)lastCommand=Math.max(lastCommand,command.optLong("seq",0));}
            preferences.edit().putLong("command:"+roomId,lastCommand).apply();return;
        }
        for(int i=0;i<commands.length();i++){JSONObject command=commands.optJSONObject(i);if(command==null)continue;long seq=command.optLong("seq",0);if(seq<=lastCommand)continue;String action=command.optString("action"),target=command.optString("song");long created=command.optLong("created");
            if(Arrays.asList("sfx_applause","sfx_cheer","sfx_stop").contains(action)){
                if(!queueIntent.playbackEnabled()){lastCommand=seq;preferences.edit().putLong("command:"+roomId,lastCommand).apply();continue;}
                soundEffects.command(seq,action,created,command.optLong("expires"),owner::serverSeconds);
                lastCommand=seq;preferences.edit().putLong("command:"+roomId,lastCommand).apply();continue;
            }
            if(!owner.playbackCommandFresh(created)){lastCommand=seq;continue;}
            lastCommand=seq;preferences.edit().putLong("command:"+roomId,lastCommand).apply();
            if(action.equals("play")){
                pendingPlay=command;
                runPendingPlay();
            }
            else if(action.equals("next")&&target.equals(reportedCurrent()))next();
            else if(action.equals("pause")){
                pendingPlay=null;
                pausePlayback();
            }
            else if(action.equals("resume")){queueIntent.awaitNext();if(!engine.running&&!loading)togglePause();}
        }
    }
    private boolean remoteTargetEligible(String target){
        // 当前曲目和已唱歌曲的显式重唱不属于待唱顺序。
        return !target.isEmpty()&&(roomPlayable==null||target.equals(reportedCurrent())||played.contains(target)
            ||orderIndex(roomPlayable,target)!=Integer.MAX_VALUE);
    }
    private void runPendingPlay(){
        if(pendingPlay==null||!foreground||!autoRoom||client==null||waitForQueueRead!=0)return;
        if(!client.playbackCommandFresh(pendingPlay.optLong("created"))){pendingPlay=null;return;}
        String target=pendingPlay.optString("song");
        if(!remoteTargetEligible(target)){
            pendingPlay=null;
            return;
        }
        JSONObject job=target.equals(reportedCurrent())?playbackManifest():queuedJob(target);
        if(lyricsUnavailable(job)){pendingPlay=null;return;}
        if(!readyForPerformance(job)){
            prepareLyrics(job);
            return;
        }
        if(updater.blocksPlayback())return;
        if(!userPlayback()){pendingPlay=null;return;}
        pendingPlay=null;
        queueIntent.awaitNext();
        if(current!=null&&!warmup&&!currentId.equals(target))played.add(currentId);
        warmup=false;
        played.remove(target);
        stopAudio(false);
        current=null;
        play(job,0,true);
    }
    private String reportedCurrent(){
        if(queueClaims&&!presentedCurrent())return "";
        return selectedRoomSong();
    }
    private String selectedRoomSong(){
        return QueuePlaybackIntent.reportedCurrent(current!=null,warmup,currentId);
    }
    private boolean presentedCurrent(){
        return current!=null&&(currentId.equals(presentedSong)
            ||awaitingPlaybackPresentation&&engine.backingPresented());
    }
    private JSONArray playbackHistory(){
        JSONArray done=new JSONArray();
        if(roomOrder!=null){
            for(int i=0;i<roomOrder.length()&&done.length()<80;i++){
                String id=roomOrder.optString(i);
                if(played.contains(id))done.put(id);
            }
        }else{
            for(JSONObject job:jobs){
                String id=job.optString("id");
                if(played.contains(id)&&done.length()<80)done.put(id);
            }
        }
        return done;
    }
    private JSONObject playbackReport(){
        JSONObject data=new JSONObject();
        try{
            String selected=nextSelectionId();
            boolean waiting=!paused&&queueIntent.waitingForRequest(foreground,loading,current!=null,selected);
            boolean presented=engine.backingPresented();
            String state=loading||waiting||(engine.running&&!presented)?"preparing":presented?"playing":paused?"paused":"idle";
            data.put("audio_capabilities",new JSONObject().put("vocal_presets",new JSONArray().put("dry"))
                .put("sfx",soundEffects.ready&&queueIntent.playbackEnabled()))
                .put("sfx_seq",soundEffects.sequence).put("sfx_action",soundEffects.action).put("sfx_state",soundEffects.state);
            JSONObject next=queuedJob(selected);
            String lyricsWaiting=audioReady(next)&&!lyricsReady(next)?next.optString("id"):"";
            data.put("playback_epoch",playbackTimeline.epoch()).put("room_id",roomId).put("lyrics_waiting",lyricsWaiting)
                .put("current",reportedCurrent()).put("played",playbackHistory()).put("state",state)
                .put("mic_muted",true).put("safety_latched",false)
                .put("position_ms",Math.min(1800000,Math.max(0,engine.playheadFrames/48)))
                .put("last_command",lastCommand).put("applied_revision",Math.max(0,appliedRevision));
            if(backingReceipts&&(state.equals("playing")||state.equals("preparing"))){
                File actual=presented?engine.activeBacking():null;
                int mode=-1;
                if(actual!=null){
                    File chosen=original?currentOriginal:currentInstrumental;
                    if(actual.equals(chosen))mode=original?1:0;
                    else if(actual.equals(currentOriginal))mode=1;
                    else if(actual.equals(currentInstrumental))mode=0;
                }
                data.put("backing_original",mode);
            }
        }catch(JSONException ignored){}
        return data;
    }
    private void setRoomNotice(String text,boolean waiting){
        if(!text.contentEquals(notice.getText()))notice.setText(text);
        noticeWaiting=waiting;
        roomProgress.setVisibility(waiting&&motionEnabled?View.VISIBLE:View.GONE);
    }
    private void showIdle(){
        if(current==null)songBackground(null);
        idle.setVisibility(View.VISIBLE);
        showScrollingLyrics(false);
        byline.setVisibility(View.GONE);
        progress.setVisibility(View.INVISIBLE);
        clock.setText("");
        trackTitle.setText("");
        picture.setAlpha(0);
        mvLyrics.setVisibility(View.GONE);
        visibleMvLyric=-2;

        boolean joining=qrBitmap!=null&&autoRoom;
        boolean retained=current!=null;
        String next=retained?"":nextSelectionId();
        JSONObject queued=retained?current:queuedJob(next);
        boolean hasRequest=retained||hasPending();
        boolean unavailable=protocolReady&&!retained&&hasRequest&&next.isEmpty();
        boolean preparing=!next.isEmpty()&&!readyForPerformance(queued);
        boolean showJoinCode=joining&&(!hasRequest||unavailable);
        headline.setVisibility(joining&&!hasRequest?View.GONE:View.VISIBLE);
        headline.setText(unavailable?"等待可唱歌曲":queued!=null?SongTitle.display(queued.optString("title","下一首")):hasRequest?"下一首歌曲":"");
        bigQr.setVisibility(showJoinCode?View.VISIBLE:View.GONE);
        showSmallQr(joining&&!showJoinCode);
        String message=joining?"":roomMessage;
        if(unavailable)message="可在手机上查看已点歌曲";
        else if(protocolReady&&preparing)message=queuePreparation(queued);
        else if(protocolReady&&!next.isEmpty()&&paused)message="歌曲已就绪";
        setRoomNotice(message,!retained&&!unavailable&&autoRoom&&!roomRetry&&(!joining||preparing));

        String playLabel=current!=null?"继续播放":hasRequest?"开始播放":"播放暖场";
        boolean showPlay=!unavailable&&(!hasRequest||paused||!queueIntent.playbackEnabled());
        if(!autoRoom){playLabel="开启房间";showPlay=true;}
        else if(!protocolReady&&!joining){playLabel="重试连接";showPlay=roomRetry;}
        boolean becameVisible=showPlay&&warmupButton.getVisibility()!=View.VISIBLE;
        warmupButton.setText(playLabel);
        warmupButton.setContentDescription(playLabel);
        warmupButton.setVisibility(showPlay?View.VISIBLE:View.GONE);
        if(becameVisible&&!smallQr.hasFocus())warmupButton.requestFocus();
    }
    private static String audioEdition(JSONObject job){
        JSONObject media=job.optJSONObject("media");
        JSONObject original=media==null?null:media.optJSONObject("original");
        JSONObject instrumental=media==null?null:media.optJSONObject("instrumental");
        return (original==null?"":original.optString("sha256"))+":"+(instrumental==null?"":instrumental.optString("sha256"));
    }
    private static boolean sameAudioEdition(JSONObject first,JSONObject second){
        return first.optString("id").equals(second.optString("id"))&&audioEdition(first).equals(audioEdition(second));
    }
    private boolean updateCurrentManifest(JSONObject job){
        if(current==null||!sameAudioEdition(job,current))return false;
        currentManifest=job;
        return true;
    }
    private JSONObject playbackManifest(){
        return currentManifest==null?current:currentManifest;
    }
    private static JSONObject trackSpec(JSONObject job,boolean original){
        JSONObject media=job==null?null:job.optJSONObject("media");
        return media==null?null:media.optJSONObject(original?"original":"instrumental");
    }
    private String nextPendingId(boolean skipUnavailable){
        if(roomUpcoming!=null)return firstUnplayed(roomUpcoming,skipUnavailable);
        List<String> ordered=new ArrayList<>(jobs.size());
        for(JSONObject job:jobs){
            String state=job.optString("state");
            if(!state.equals("failed")&&!state.equals("cancelled"))ordered.add(job.optString("id"));
        }
        return firstUnplayed(ordered,skipUnavailable);
    }
    private String firstUnplayed(JSONArray values,boolean skipUnavailable){
        List<String> ordered=new ArrayList<>(values.length());
        for(int i=0;i<values.length();i++)ordered.add(values.optString(i));
        return firstUnplayed(ordered,skipUnavailable);
    }
    private String firstUnplayed(List<String> ordered,boolean skipUnavailable){
        return QueuePlaybackIntent.firstPending(ordered,played,selectedRoomSong(),
            id->id.equals(refusedStartSong)||skipUnavailable&&lyricsUnavailable(queuedJob(id)));
    }
    private String nextPlayableId(){
        if(roomPlayable!=null)return firstUnplayed(roomPlayable,true);
        List<String> ready=new ArrayList<>();
        for(JSONObject job:jobs){
            if(audioReady(job)&&(job.optBoolean("synced_lyrics")||lyricsReady(job)))ready.add(job.optString("id"));
        }
        return firstUnplayed(ready,true);
    }
    private String nextSelectionId(){
        String playable=nextPlayableId();
        return playable.isEmpty()?nextPendingId(true):playable;
    }
    private JSONObject queuedJob(String id){
        if(id.isEmpty())return null;
        for(JSONObject job:jobs)if(id.equals(job.optString("id")))return job;
        return null;
    }
    private JSONObject firstPending(){return queuedJob(nextSelectionId());}
    private boolean audioReady(JSONObject job){
        JSONObject track=trackSpec(job,original);
        if(track==null)return false;
        if(track.optJSONArray("sources")==null)return job.optString("state").equals("ready");
        return client!=null&&(client.hasMediaRoute(track)||client.cached(track,false));
    }
    private static String lyricVersion(JSONObject job){
        String version=job.optString("version");
        return version.isEmpty()?audioEdition(job):version;
    }
    private boolean lyricsUnavailable(JSONObject job){
        return job!=null&&QueuePlaybackIntent.lyricsUnavailable(job.optInt("lyric_state"),job.optBoolean("synced_lyrics"),
            lyricVersion(job),exhaustedLyricVersions.get(job.optString("id")));
    }
    private int verifiedLyricLines(JSONObject job){
        if(job==null)return 0;
        String id=job.optString("id");
        JSONArray lyrics=preparedLyrics.get(id);
        return lyrics!=null&&audioEdition(job).equals(lyricEditions.get(id))?lyrics.length():0;
    }
    private boolean lyricsReady(JSONObject job){return verifiedLyricLines(job)>=QueuePlaybackIntent.MIN_LYRIC_LINES;}
    private boolean readyForPerformance(JSONObject job){return !lyricsUnavailable(job)&&QueuePlaybackIntent.canPlay(audioReady(job),verifiedLyricLines(job));}
    private String queuePreparation(JSONObject job){
        if(job==null)return "正在同步下一首歌曲…";
        return audioReady(job)?"正在准备歌词…":"正在准备所选音轨…";
    }
    private void prepareFirstLyrics(){
        prepareLyrics(firstPending());
        if(!hasPending()||(roomPlayable!=null&&!nextPlayableId().isEmpty()))return;
        // 可唱顺序为空时，检查有音源的候选，兼容只在歌词接口返回终态的服务。
        for(JSONObject job:jobs){
            String id=job.optString("id");
            if(lyricsInFlight.size()>=2)return;
            if(!played.contains(id)&&!id.equals(reportedCurrent()))prepareLyrics(job);
        }
    }
    private void prepareLyrics(JSONObject job){
        if(job==null||job.optJSONObject("media")==null||lyricsUnavailable(job)||client==null||!foreground||lyricsInFlight.size()>=2)return;
        String id=job.optString("id"),edition=audioEdition(job);
        String version=lyricVersion(job);
        if(edition.equals(lyricEditions.get(id))||lyricsInFlight.containsKey(id))return;
        long now=SystemClock.elapsedRealtime();
        if(now<lyricsWait.getOrDefault(id,0L))return;
        Object request=new Object();
        lyricsInFlight.put(id,request);
        lyricsWait.put(id,now+8000);
        final KtvClient owner=client;
        final long epoch=roomEpoch;
        try{lyricsNetwork.execute(()->{
            JSONArray words=null;
            long[] entries=null;
            boolean exhausted=false;
            try{
                JSONObject reply=owner.controlJson("/v1/jobs/"+id+"/lyrics",null);
                words=readLyrics(reply.optJSONArray("lines"));
                entries=readEntries(reply);
                exhausted=Boolean.TRUE.equals(reply.opt("retry_exhausted"));
            }catch(Exception ignored){}
            final JSONArray ready=words;
            final long[] cues=entries;
            final boolean unavailable=exhausted;
            ui.post(()->{
                if(lyricsInFlight.get(id)!=request)return;
                lyricsInFlight.remove(id);
                if(owner!=client||epoch!=roomEpoch||!foreground||(ready==null&&!unavailable))return;
                JSONObject latest=queuedJob(id);
                if(latest==null)latest=warmupJobs.get(warmupKey(job));
                boolean performing=current!=null&&sameAudioEdition(job,current);
                if(ready==null){
                    // 某个版本的终态不能阻止后续版本补齐歌词。
                    if(latest==null||!sameAudioEdition(job,latest)||!version.equals(lyricVersion(latest))
                        ||latest.optBoolean("synced_lyrics")||latest.optInt("lyric_state")==1)return;
                    exhaustedLyricVersions.put(id,version);
                }else{
                    if(!performing&&(latest==null||!sameAudioEdition(job,latest)))return;
                    exhaustedLyricVersions.remove(id,version);
                    lyricEditions.put(id,edition);
                    preparedLyrics.put(id,ready);
                    preparedEntries.put(id,cues);
                    if(performing&&!loading&&engine.running&&lines.length()==0){
                        entryStarts=cues;
                        setLyrics(ready,SongTitle.display(current.optString("title","")));
                    }
                }
                lyricsWait.remove(id);
                runPendingPlay();
                prepareFirstLyrics();
                refreshQueuePlayback();
                updateNext();
            });
        });}catch(RejectedExecutionException ignored){
            lyricsInFlight.remove(id);
            lyricsWait.remove(id);
        }
    }
    private static JSONArray readLyrics(JSONArray words){
        if(words==null||words.length()==0||words.length()>1000)return null;
        long last=-1;
        JSONArray valid=new JSONArray();
        for(int i=0;i<words.length();i++){
            JSONObject line=words.optJSONObject(i);
            if(line==null)return null;
            long time=line.optLong("time_ms",-1);
            if(time<0||time>1800000||time<last)return null;
            if(!line.optString("text").trim().isEmpty())valid.put(line);
            last=time;
        }
        return valid.length()>=QueuePlaybackIntent.MIN_LYRIC_LINES?valid:null;
    }
    private long[] readEntries(JSONObject response){if(!Arrays.asList("vocal_activity","vocal_residual").contains(response.optString("cue_source")))return null;JSONArray values=response.optJSONArray("entry_ms");if(values==null||values.length()>1000)return null;long[] result=new long[values.length()];long prior=-1;for(int i=0;i<result.length;i++){long value=values.optLong(i,-1);if(value<0||value>1800000||value<=prior)return null;result[i]=value;prior=value;}return result;}
    private void playNext(){
        if(!queueIntent.playbackEnabled()||!queueIntent.shouldStart(foreground,loading,current!=null)||!autoRoom||waitForQueueRead!=0)return;
        if(tryTvUpdate())return;
        String next=nextSelectionId();
        if(next.isEmpty()){
            if(hasPending())showIdle();
            else startWarmup();
            return;
        }
        JSONObject job=queuedJob(next);
        boolean eligible=roomPlayable==null||next.equals(nextPlayableId());
        if(eligible&&readyForPerformance(job)){
            warmup=false;
            play(job,0,true);
        }else{
            prepareLyrics(job);
            setRoomNotice(queuePreparation(job),true);
        }
    }
    private void prefetchNext(){
        if(client==null||!foreground||current==null||!engine.running)return;
        JSONObject candidate=firstPending();
        if(!audioReady(candidate))return;
        String id=candidate.optString("id"),edition=audioEdition(candidate)+(original?":original":":instrumental");
        JSONObject media=candidate.optJSONObject("media");
        JSONObject selected=media.optJSONObject(original?"original":"instrumental");
        if(selected==null)return;
        if(prefetchBusy){
            if(!id.equals(prefetchId)||!edition.equals(prefetchEdition))prefetchCancel.set(true);
            return;
        }
        if(id.equals(prefetchedId)&&edition.equals(prefetchedEdition)&&client.cached(selected,false))return;
        if(id.equals(prefetchId)&&edition.equals(prefetchEdition)&&SystemClock.elapsedRealtime()-prefetchAttempt<8000)return;
        prefetchAttempt=SystemClock.elapsedRealtime();
        prefetchId=id;
        prefetchEdition=edition;
        prefetchBusy=true;
        prefetchCancel=new AtomicBoolean();
        final AtomicBoolean cancel=prefetchCancel;
        final KtvClient owner=client;
        final long epoch=roomEpoch;
        try{preloader.execute(()->{
            boolean complete=false;
            try{
                owner.download(selected,cancel);
                complete=!cancel.get();
            }catch(Exception ignored){}
            final boolean done=complete;
            ui.post(()->{
                prefetchBusy=false;
                if(done&&foreground&&owner==client&&epoch==roomEpoch){
                    prefetchedId=id;
                    prefetchedEdition=edition;
                }
            });
        });}catch(RejectedExecutionException ignored){prefetchBusy=false;}
    }
    private void addCacheHashes(Set<String> hashes,JSONObject job){
        if(job==null)return;
        JSONObject media=job.optJSONObject("media");
        if(media!=null)for(String kind:new String[]{"original","instrumental"}){
            JSONObject stem=media.optJSONObject(kind);
            if(stem!=null)hashes.add(stem.optString("sha256"));
        }
        for(String kind:new String[]{"video","background"}){
            JSONObject asset=job.optJSONObject(kind);
            if(asset!=null&&asset.optString("status").equals("ready"))hashes.add(asset.optString("sha256"));
        }
    }
    private void protectCache(){
        if(client==null)return;
        Set<String> hashes=new HashSet<>();
        addCacheHashes(hashes,current);
        addCacheHashes(hashes,firstPending());
        client.mediaCache.protect(hashes);
    }
    private boolean hasPending(){return !nextPendingId(false).isEmpty();}
    private String warmupKey(JSONObject job){return job.optString("audio_origin","song")+":"+job.optString("song_id",job.optString("id"));}
    private void refreshWarmup(JSONArray available){
        // A successful server catalog is authoritative for audio editions. Keep
        // downloaded bytes, but never resurrect a revoked Live/MV job from old
        // metadata. An offline poll never enters this method with a new catalog.
        if(available==null)return;
        warmupJobs.clear();
        Set<String> eligible=new HashSet<>();
        for(int i=0;i<available.length();i++){JSONObject job=available.optJSONObject(i);if(job!=null&&job.optString("state").equals("ready")&&job.optJSONObject("media")!=null){warmupJobs.put(warmupKey(job),job);eligible.add(job.optString("id"));}}
        // Finish the current performance even when its old edition is revoked.
        // Its lyrics are evicted after it ends; future selection uses fresh jobs.
        if(current!=null)eligible.add(currentId);
        preparedLyrics.keySet().retainAll(eligible);
        preparedEntries.keySet().retainAll(eligible);
        lyricEditions.keySet().retainAll(eligible);
        lyricManifestVersions.keySet().retainAll(eligible);
        exhaustedLyricVersions.keySet().retainAll(eligible);
        lyricsWait.keySet().retainAll(eligible);
        warmupFailed.keySet().retainAll(warmupJobs.keySet());
    }
    private void refreshQueuePlayback(){
        // Warmup is a singable full song. Downloads, lyrics and queue refreshes
        // may prepare the next track but must never interrupt the current one.
        if(loading||current!=null||waitForQueueRead!=0)return;
        showIdle();if(queueIntent.shouldStart(foreground,loading,false))playNext();
    }
    private void startWarmup(){
        if(client==null||hasPending()||!queueIntent.playbackEnabled()||!queueIntent.shouldStart(foreground,loading,current!=null)||!autoRoom)return;
        ArrayList<String> eligible=new ArrayList<>();long now=SystemClock.elapsedRealtime();
        boolean preparing=false;
        for(String key:warmupJobs.keySet()){
            if(now<warmupFailed.getOrDefault(key,0L))continue;
            JSONObject job=warmupJobs.get(key);
            if(queueClaims&&roomOrder!=null&&orderIndex(roomOrder,job.optString("id"))==Integer.MAX_VALUE)continue;
            if(readyForPerformance(job))eligible.add(key);
            else if(!lyricsUnavailable(job)){preparing=true;prepareLyrics(job);}
        }
        String key=warmupShuffle.next(eligible);
        if(key==null){
            warmup=false;
            if(preparing)setRoomNotice("正在准备暖场歌曲…",true);
            return;
        }
        warmup=true;play(warmupJobs.get(key),0,true);
    }
    private void retryRoom(){
        if(client==null){connection();return;}
        int refusal=coordinatorSession.refusal();
        if(!coordinatorSession.pending()||refusal==409||refusal==410)coordinatorSession.requestOpen();
        else coordinatorSession.retry();
        autoRoom=true;nextConnectAttempt=0;connectionFailures=0;
        roomConnection("正在连接房间…",false);poll();
    }
    private void toggleWarmup(){if(!autoRoom||!protocolReady){if(!autoRoom||roomRetry)retryRoom();return;}if(!userPlayback())return;if(current!=null&&!engine.running&&!loading){togglePause();return;}if(warmup){warmup=false;queueIntent.pause();stopAudio(true);current=null;currentId="";showIdle();return;}queueIntent.awaitNext();playNext();if(current==null&&!hasPending())setRoomNotice("还没有准备好的暖场歌曲。",false);}
    private void warmupFailure(JSONObject job){warmupFailed.put(warmupKey(job),SystemClock.elapsedRealtime()+60000);warmup=false;current=null;currentId="";loading=false;showIdle();playNext();}
    private void play(JSONObject job,long position){
        play(job,position,false);
    }
    private void play(JSONObject job,long position,boolean restart){
        String jobId=job.optString("id");
        String edition=audioEdition(job);
        if(client==null||!foreground||loading||!queueIntent.playbackEnabled()||updater.blocksPlayback())return;
        JSONObject manifest=job==current?playbackManifest():job;
        if(!readyForPerformance(manifest)){prepareLyrics(manifest);return;}
        final JSONArray verifiedLyrics=preparedLyrics.get(jobId);
        final long[] verifiedEntries=preparedEntries.get(jobId);
        if(prefetchBusy&&(!jobId.equals(prefetchId)||!prefetchEdition.equals(edition+(original?":original":":instrumental"))))prefetchCancel.set(true);
        if(playbackTimeline.start(jobId,edition,restart)){
            preferences.edit().putString("playback-epoch",playbackTimeline.epoch()).putString("playback-song",playbackTimeline.song())
                .putString("playback-media",playbackTimeline.media()).apply();
        }
        JSONObject media=job.optJSONObject("media");
        currentOriginal=jobId.equals(currentFilesJob)?reusableStem(media.optJSONObject("original"),currentOriginal):null;
        currentInstrumental=jobId.equals(currentFilesJob)?reusableStem(media.optJSONObject("instrumental"),currentInstrumental):null;
        currentFilesJob=jobId;
        stopAudio(false);
        if(!jobId.equals(presentedSong))presentedSong="";
        if(!smallQr.hasFocus())page.requestFocus();
        warmupButton.setVisibility(View.GONE);
        showScrollingLyrics(false);
        byline.setVisibility(View.GONE);
        progress.setVisibility(View.INVISIBLE);
        clock.setText("");
        loading=true;
        startingLyrics=verifiedLyrics;
        startingEntries=verifiedEntries;
        paused=false;
        current=job;
        currentManifest=manifest;
        currentId=jobId;
        videoFile=null;
        protectCache();
        resumeFrame=position;
        String title=SongTitle.display(job.optString("title",""));
        songBackground(manifest);
        idle.setVisibility(View.VISIBLE);
        bigQr.setVisibility(View.GONE);
        showSmallQr(qrBitmap!=null);
        headline.setVisibility(View.VISIBLE);
        headline.setText(title);
        trackTitle.setText(title);
        loadSelectedBacking();
    }
    private void loadSelectedBacking(){
        if(backingRetryAt!=0){
            if(backingRetryOriginal==original)return;
            backingRetryAt=0;
        }
        if((playbackStart!=null||waitingForRenderer)&&startVerifiedBacking())return;
        setRoomNotice(original?"正在加载原唱…":"正在加载伴奏…",true);
        loadBacking(original,trackSpec(playbackManifest(),original));
    }
    private void advanceBackingRetry(long now){
        if(backingRetryAt==0||now-backingRetryAt<0)return;
        backingRetryAt=0;
        if(foreground&&loading&&original==backingRetryOriginal)loadSelectedBacking();
    }
    private boolean retryBackingStart(boolean loadOriginal,JSONObject attempted){
        final int track=loadOriginal?1:0;
        final int attempt=backingStartFailures[track];
        if(attempt>=BACKING_RETRY_MS.length)return false;
        JSONObject latest=trackSpec(playbackManifest(),loadOriginal);
        boolean newRoute=client.hasNewMediaRoute(attempted,latest);
        if(warmup&&!newRoute)return false;
        // 每首歌的每条音轨都有独立重试窗口。续签媒体票据不会重置次数。
        backingStartFailures[track]++;
        backingRetryOriginal=loadOriginal;
        long now=System.nanoTime();
        backingRetryAt=now+TimeUnit.MILLISECONDS.toNanos(newRoute?0:BACKING_RETRY_MS[attempt]);
        if(newRoute)advanceBackingRetry(now);
        return true;
    }
    private void loadBacking(boolean loadOriginal,JSONObject spec){
        if(current==null||!backingLoads.add(loadOriginal))return;
        final JSONObject job=current;
        final int version=generation.get();
        final KtvClient owner=client;
        final AtomicBoolean cancelled=transferCancel;
        final File previous=loadOriginal?currentOriginal:currentInstrumental;
        try{
            backingNetwork.execute(()->{
                File file=null;
                try{
                    if(cancelled.get())return;
                    file=loadStem(owner,spec,previous,cancelled);
                }catch(Exception ignored){}
                final File loaded=file;
                ui.post(()->{
                    if(!foreground||version!=generation.get()||cancelled.get()||owner!=client||current!=job)return;
                    finishBackingLoad(loadOriginal,spec,loaded);
                });
            });
        }catch(RejectedExecutionException ignored){finishBackingLoad(loadOriginal,spec,null);}
    }
    private void finishBackingLoad(boolean loadOriginal,JSONObject attempted,File file){
        backingLoads.remove(loadOriginal);
        boolean selected=original==loadOriginal;
        if(file==null){
            if(loading&&selected){
                if(retryBackingStart(loadOriginal,attempted))return;
                if(warmup){
                    JSONObject job=current;
                    stopAudio(false);
                    warmupFailure(job);
                }else playbackLoadFailed(loadOriginal?"本首原唱暂时无法加载，按播放键重试。":"本首伴奏暂时无法加载，按播放键重试。");
            }else{
                failedAlternateSpec=attempted;
                alternateRetryAt=SystemClock.elapsedRealtime()+8000;
            }
            return;
        }
        if(loadOriginal)currentOriginal=file;else currentInstrumental=file;
        if(!selected)return;
        if(loading)startLoadedBacking(file);
        else if(engine.running)engine.switchBacking(file);
    }
    private void startLoadedBacking(File file){
        if(!foreground||!protocolReady||!loading||current==null)return;
        if(!rendererReadyForStart())return;
        if(queueClaims&&!authorizePlaybackStart(SystemClock.elapsedRealtime()))return;
        JSONObject job=current;
        try{engine.startBacking(file,resumeFrame);}
        catch(Exception failure){playbackLoadFailed(recovery(failure.getMessage()));return;}
        activePlaybackClaim=playbackStart;
        playbackStart=null;
        loading=false;
        entryStarts=startingEntries;
        setLyrics(startingLyrics,SongTitle.display(job.optString("title","")));
        startingLyrics=null;
        startingEntries=null;
        awaitingPlaybackPresentation=true;
        setRoomNotice("正在开始播放…",true);
        client.mediaCache.played(file);
        loadAlternateBacking();
    }
    private boolean rendererReadyForStart(){
        waitingForRenderer=!engine.released();
        if(waitingForRenderer){setRoomNotice("正在切换歌曲…",true);return false;}
        retireActivePlayback(null);
        return true;
    }
    private void advancePlaybackStart(long now){
        if(!loading)return;
        if(waitingForRenderer){
            if(engine.released()&&!startVerifiedBacking()){
                waitingForRenderer=false;
                loadSelectedBacking();
            }
        }else if(playbackStart!=null&&playbackStart.canRequest(now))startVerifiedBacking();
    }
    private boolean startVerifiedBacking(){
        File file=reusableStem(trackSpec(playbackManifest(),original),original?currentOriginal:currentInstrumental);
        if(file==null)return false;
        startLoadedBacking(file);
        return true;
    }
    private boolean authorizePlaybackStart(long now){
        if(playbackStart!=null&&playbackStart.expired(now))cancelPlaybackStart();
        if(playbackStart==null)playbackStart=new PlaybackStartClaim(client,roomId,currentId,connectionEpoch,generation.get());
        PlaybackStartClaim claim=playbackStart;
        if(claim.canStart(now))return true;
        if(!claim.canRequest(now))return false;
        setRoomNotice("正在确认点唱…",true);
        claim.begin();
        try{playbackStarts.execute(()->{
            if(!claim.markSending())return;
            long requestedAt=SystemClock.elapsedRealtime();
            int lease=0;
            Exception refusal=null;
            try{lease=claim.owner.claimPlaybackStart(claim.room,claim.song,claim.request);}
            catch(Exception failure){refusal=failure;}
            final int granted=lease;
            final Exception error=refusal;
            ui.post(()->finishPlaybackStart(claim,requestedAt,granted,error));
        });}catch(RejectedExecutionException ignored){claim.retry(now);}
        return false;
    }
    private void finishPlaybackStart(PlaybackStartClaim claim,long requestedAt,int lease,Exception error){
        if(playbackStart!=claim||!loading||!protocolReady||generation.get()!=claim.generation
            ||!ownsConnection(claim.owner,claim.connection)||!claim.room.equals(roomId)
            ||!claim.song.equals(currentId))return;
        if(error==null){
            claim.accepted(requestedAt,lease);
            if(!startVerifiedBacking())loadSelectedBacking();
            return;
        }
        String code=error.getMessage();
        if(error instanceof KtvClient.HttpFailure&&((KtvClient.HttpFailure)error).televisionChanged()){
            televisionOwnershipLost(claim.owner,controlEpoch);
            return;
        }
        if("song_unavailable".equals(code)){discardPlaybackSelection(true);return;}
        if("playback_start_expired".equals(code)||"playback_start_released".equals(code)){
            cancelPlaybackStart();
            if(!startVerifiedBacking())loadSelectedBacking();
            return;
        }
        if(error instanceof KtvClient.HttpFailure){
            nextConnectAttempt=0;lastControlStateAt=0;
        }
        claim.retry(SystemClock.elapsedRealtime());
        setRoomNotice("正在恢复房间连接…",true);
    }
    private void cancelPlaybackStart(){
        PlaybackStartClaim claim=playbackStart;
        playbackStart=null;
        playbackReleases.enqueue(claim,null);
    }
    private void releaseStoppedPlayback(){
        if(activePlaybackClaim!=null&&!engine.running)retireActivePlayback(engine.playbackWorker());
    }
    private void retireActivePlayback(Thread stopping){
        if(activePlaybackClaim==null)return;
        playbackReleases.enqueue(activePlaybackClaim,stopping);
        activePlaybackClaim=null;
    }
    private void discardPlaybackSelection(boolean refresh){
        if(refresh){refusedStartSong=currentId;waitForQueueRead=roomReadSequence;}
        stopAudio(paused);
        current=null;currentId="";currentManifest=null;presentedSong="";warmup=false;
        nextJobPoll=0;
        showIdle();
        if(refresh)setRoomNotice("正在同步歌单…",true);
    }
    private void presentBacking(){
        presentedSong=currentId;
        awaitingPlaybackPresentation=false;
        showRequester(current);
        idle.setVisibility(View.GONE);
        setRoomNotice("",false);
        trackTitle.setVisibility(View.VISIBLE);
        showScrollingLyrics(true);
        if(motionEnabled){
            lyricScroll.animate().cancel();
            lyricScroll.setAlpha(.84f);
            lyricScroll.animate().alpha(1f).setDuration(STAGE_MOTION_MS).setInterpolator(stageEase()).start();
        }
        progress.setVisibility(View.VISIBLE);
        maybeLoadVideo(playbackManifest());
    }
    private void playbackLoadFailed(String notice){
        stopAudio(true);
        queueIntent.pause();
        showIdle();
        if(current!=null){
            warmupButton.setText("重试播放");
            warmupButton.setContentDescription("重试播放");
        }
        setRoomNotice(notice,false);
    }
    private static File reusableStem(JSONObject spec,File file){
        return spec!=null&&file!=null&&file.isFile()&&file.getName().equals(spec.optString("sha256")+".wav")?file:null;
    }
    private static File loadStem(KtvClient owner,JSONObject spec,File previous,AtomicBoolean cancelled)throws Exception{
        if(reusableStem(spec,previous)!=null)return previous;
        return owner.download(spec,cancelled);
    }
    private void loadAlternateBacking(){
        if(current==null||loading||!engine.running)return;
        boolean loadOriginal=currentOriginal==null;
        if(!loadOriginal&&currentInstrumental!=null)return;
        if(backingLoads.contains(loadOriginal))return;
        JSONObject spec=trackSpec(playbackManifest(),loadOriginal);
        if(SystemClock.elapsedRealtime()<alternateRetryAt&&!client.hasNewMediaRoute(failedAlternateSpec,spec))return;
        if(spec!=null&&(client.hasMediaRoute(spec)||client.cached(spec,false)))loadBacking(loadOriginal,spec);
    }
    private static boolean movieReady(JSONObject movie,JSONObject original){
        return movie!=null&&original!=null&&movie.optString("status").equals("ready")
            &&movie.optString("source_audio_sha256").equals(original.optString("sha256"));
    }
    private void maybeLoadVideo(JSONObject job){
        if(current==null||loading||awaitingPlaybackPresentation||!sameAudioEdition(job,current)||!engine.running)return;
        JSONObject original=job.optJSONObject("media").optJSONObject("original");
        JSONObject spec=job.optJSONObject("video");
        KtvClient owner=client;
        if(!movieReady(spec,original)||(warmup&&!owner.cached(spec,true)))return;
        String sha=spec.optString("sha256");
        if(sha.equals(videoRequestedSha)||SystemClock.elapsedRealtime()<videoRetryAt)return;
        int version=generation.get();
        AtomicBoolean cancelled=transferCancel;
        videoRequestedSha=sha;
        try{videoNetwork.execute(()->{
            try{
                if(cancelled.get())return;
                File movie=owner.downloadVideo(spec,cancelled);
                ui.post(()->{
                    if(!foreground||version!=generation.get()||cancelled.get()||!sha.equals(videoRequestedSha))return;
                    videoFile=movie;
                    videoOffsetMs=Math.round(spec.optDouble("video_offset_ms",0));
                    videoDurationMs=Math.round(spec.optDouble("duration_seconds",0)*1000);
                    owner.mediaCache.played(movie);
                    prepareVideo();
                });
            }catch(Exception ignored){ui.post(()->videoLoadFailed(version,sha));}
        });}catch(RejectedExecutionException ignored){videoLoadFailed(version,sha);}
    }
    private void videoLoadFailed(int version,String sha){
        if(version!=generation.get()||!sha.equals(videoRequestedSha))return;
        videoRequestedSha="";
        videoRetryAt=SystemClock.elapsedRealtime()+8000;
    }
    private void setLyrics(JSONArray value,String title){
        cancelLyricAlignment();
        lyricScroll.scrollTo(0,0);
        lines=value;activeLine=-1;lyrics.removeAllViews();lyricViews.clear();
        int margin=Math.max(dp(150),lyricScroll.getHeight()/2);lyrics.setPadding(0,margin,0,margin);
        lyricStarts=new long[value.length()];
        mvPrevious.setText("");mvCurrent.setText("");mvNext.setText("");
        countdownLine=-1;countdownValue=0;visibleMvLyric=-2;
        Typeface face=Typeface.create("sans-serif-medium",Typeface.NORMAL);
        if(value.length()==0){TextView line=text(title,38,WHITE);line.setTypeface(face);textShadow(line,true);lyrics.addView(line);return;}
        int padding=dp(10);
        for(int i=0;i<value.length();i++){
            JSONObject cue=value.optJSONObject(i);
            lyricStarts[i]=cue.optLong("time_ms");
            // 所有歌词以演唱字号排版，切换高亮时保留换行和位置。
            TextView line=text(cue.optString("text"),40,MUTED);
            line.setPadding(0,padding,0,padding);line.setTypeface(face);textShadow(line,false);
            line.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
            lyrics.addView(line,new LinearLayout.LayoutParams(-1,-2));lyricViews.add(line);
        }
    }
    private void refreshMissingLyrics(){
        if(current!=null&&(loading||engine.running)&&lines.length()==0)prepareLyrics(current);
    }
    private void showRequester(JSONObject job){JSONObject who=job.optJSONObject("requester");String nickname=who==null?"朋友":who.optString("nickname","朋友");nickname=nickname.replaceFirst("^(汽水猫|夜航兔|慢慢鲸|摇摆熊|月球旅人|小幽灵|唱片君|星星糖|柠檬汽水|仙人掌|北极企鹅|节拍机器人) [0-9]{2}$","$1").replace("节拍机器人","节拍小子");requester.setText(warmup?"暖场中":nickname+" 点的");requesterAvatar.setVisibility(warmup?View.GONE:View.VISIBLE);requesterAvatar.setImageBitmap(avatar(who==null?0:who.optInt("avatar",0)));byline.setVisibility(View.VISIBLE);updateNext();}
    private Bitmap avatar(int id) {
        if (atlas == null) return null;
        int size = atlas.getWidth() / 4;
        int rows = atlas.getHeight() / size;
        id = Math.max(0, Math.min(rows * 4 - 1, id));
        return Bitmap.createBitmap(atlas, (id % 4) * size, (id / 4) * size, size, size);
    }
    private void updateNext(){
        String selected=nextSelectionId();
        JSONObject next=queuedJob(selected);
        String text="";
        if(next!=null){
            String title=SongTitle.display(next.optString("title","歌曲"));
            text=selected.equals(nextPlayableId())?"下一首  "+title:"准备中  "+title;
        }
        else if(!selected.isEmpty())text="正在同步下一首…";
        else if(hasPending())text="等待可唱歌曲";
        if(!text.contentEquals(nextSong.getText()))nextSong.setText(text);
    }
    private void ended(String code,boolean natural){
        releaseStoppedPlayback();
        if(engine.running||loading)return;
        cancelPlaybackStart();
        awaitingPlaybackPresentation=false;
        releaseVideo();
        if(warmup&&natural&&foreground){
            current=null;currentId="";warmup=false;showIdle();playNext();return;
        }
        if(natural&&current!=null){
            String popularity="ktv-plays:"+current.optString("song_id",currentId);
            preferences.edit().putInt(popularity,Math.min(100000,preferences.getInt(popularity,0)+1)).apply();
            queueIntent.awaitNext();played.add(currentId);
            while(played.size()>128)played.remove(played.iterator().next());
            preferences.edit().putStringSet("played",new HashSet<>(played)).apply();
            current=null;currentId="";
            if(foreground){showIdle();playNext();}
        }else if(current!=null&&foreground&&!paused){
            paused=true;queueIntent.pause();
            resumeFrame=engine.playheadFrames;
            showIdle();
            bigQr.setVisibility(View.GONE);headline.setText("播放已暂停");
            setRoomNotice(recovery(code),false);
        }
    }
    private void stopAudio(boolean pause){
        if(partyEffects!=null)partyEffects.stop();
        if(awaitingPlaybackPresentation&&engine.backingPresented())presentedSong=currentId;
        cancelPlaybackStart();
        generation.incrementAndGet();transferCancel.set(true);transferCancel=new AtomicBoolean();
        backingNetwork.getQueue().clear();
        backingLoads.clear();startingLyrics=null;startingEntries=null;
        waitingForRenderer=false;
        backingRetryAt=0;Arrays.fill(backingStartFailures,0);
        awaitingPlaybackPresentation=false;shownSecond=-1;
        failedAlternateSpec=null;alternateRetryAt=0;videoRequestedSha="";videoRetryAt=0;
        if(pause){
            if(soundEffects!=null)soundEffects.stop();
            if(!loading)resumeFrame=engine.playheadFrames;
        }
        paused=pause;loading=false;
        engine.stop();retireActivePlayback(engine.playbackWorker());releaseVideo();
    }
    private void pausePlayback(){
        final boolean preparing=loading;
        queueIntent.pause();
        if(engine.running||loading)stopAudio(true);else paused=true;
        if(preparing||current==null)showIdle();
        if(preparing)setRoomNotice("已暂停",false);
        clock.setText("已暂停");
    }
    private void togglePause(){
        if(loading||engine.running)pausePlayback();
        else if(!userPlayback())return;
        else if(current!=null){
            queueIntent.awaitNext();play(current,resumeFrame);
        }else{
            queueIntent.awaitNext();paused=false;playNext();
        }
    }
    private void next(){
        pendingPlay=null;
        if(!userPlayback())return;
        queueIntent.awaitNext();
        if(current!=null&&!warmup){
            played.add(currentId);
            preferences.edit().putStringSet("played",new HashSet<>(played)).apply();
        }
        stopAudio(false);
        current=null;
        currentId="";
        warmup=false;
        showIdle();
        playNext();
    }
    private String recovery(String code){
        if(code!=null&&code.startsWith("output_"))return "电视声音输出已变化，按播放键重试。";
        return "播放已暂停，按播放键重试；菜单中可检查连接。";
    }
    private void prepareVideo(){
        releaseVideo();
        if(videoFile==null||!picture.isAvailable()||!foreground||awaitingPlaybackPresentation)return;
        try{
            videoPrepareCount++;videoClock.reset();videoSeekCount=videoBufferEvents=videoSurfaceFrames=0;
            videoSpeed=1;videoSpeedSupported=false;videoSurface=new Surface(picture.getSurfaceTexture());
            MediaPlayer player=new MediaPlayer();video=player;
            player.setSurface(videoSurface);player.setDataSource(videoFile.getAbsolutePath());
            player.setLooping(false);player.setVolume(0,0);
            player.setVideoScalingMode(MediaPlayer.VIDEO_SCALING_MODE_SCALE_TO_FIT_WITH_CROPPING);
            player.setOnPreparedListener(v->{if(video!=v||!foreground)return;videoReady=true;syncVideo(true);});
            player.setOnSeekCompleteListener(v->{
                if(video!=v)return;
                try{
                    seekResultMs=v.getCurrentPosition();videoSeeking=false;
                    if(!engine.running||!foreground)return;
                    long target=engine.playheadFrames/48+videoOffsetMs;
                    if(target<0||target>=videoDurationMs-40)return;
                    long lag=target-seekResultMs;
                    if(videoSeekCount==1&&lag>180&&lag<1500&&target+lag<videoDurationMs-40){
                        videoSeeking=true;videoSeekCount++;v.seekTo(target+lag,MediaPlayer.SEEK_CLOSEST);
                    }else v.start();
                }catch(Exception ignored){releaseVideo();}
            });
            player.setOnInfoListener((v,what,extra)->{
                if(video!=v||!foreground)return false;
                if(what==MediaPlayer.MEDIA_INFO_BUFFERING_START)videoBufferEvents++;
                if(what==MediaPlayer.MEDIA_INFO_VIDEO_RENDERING_START){
                    videoRendered=true;
                    showVideoIfReady();
                }
                return false;
            });
            player.setOnCompletionListener(v->{if(video==v)releaseVideo();});
            player.setOnErrorListener((v,what,extra)->{if(video==v)releaseVideo();return true;});
            player.prepareAsync();
        }catch(Exception ignored){releaseVideo();}
    }
    private void updateMvLyrics(long ms){
        lyricCue.update(ms,lyricStarts,entryStarts);
        int previousCue=countdownLine;
        int target=lyricCue.countdown>0?lyricCue.entrance:-1;
        boolean cueChanged=target!=previousCue||lyricCue.countdown!=countdownValue;
        if(cueChanged){
            countdownLine=target;
            countdownValue=lyricCue.countdown;
            if(previousCue!=target&&previousCue>=0&&previousCue<lyricViews.size())
                refreshLyricCue(lyricViews.get(previousCue),previousCue,previousCue,false);
            if(target>=0&&target<lyricViews.size())refreshLyricCue(lyricViews.get(target),target,previousCue,false);
        }
        if(video==null||picture.getAlpha()==0||lines.length()==0){
            if(visibleMvLyric!=-2){mvLyrics.setVisibility(View.GONE);visibleMvLyric=-2;}
            return;
        }
        boolean lineChanged=visibleMvLyric!=lyricCue.current;
        if(!lineChanged&&!cueChanged)return;
        if(visibleMvLyric==-2)mvLyrics.setVisibility(View.VISIBLE);
        visibleMvLyric=lyricCue.current;
        refreshLyricCue(mvPrevious,LyricWindow.previous(lyricCue.current,lines.length()),previousCue,lineChanged);
        refreshLyricCue(mvCurrent,lyricCue.current,previousCue,lineChanged);
        refreshLyricCue(mvNext,lyricCue.next,previousCue,lineChanged);
    }
    private void refreshLyricCue(TextView view,int row,int previousCue,boolean lineChanged){
        if(row<0||row>=lines.length()){
            if(lineChanged&&view.length()>0)view.setText("");
            return;
        }
        boolean cue=row==countdownLine;
        if(!lineChanged&&cue==(row==previousCue)){
            if(cue&&view.getText() instanceof Spannable)colorCueDots((Spannable)view.getText());
            return;
        }
        String words=lines.optJSONObject(row).optString("text");
        if(!cue){view.setText(words);return;}
        SpannableString lead=new SpannableString(LYRIC_LEAD+words);
        lead.setSpan(new RelativeSizeSpan(.55f),0,LYRIC_LEAD.length(),Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        colorCueDots(lead);
        view.setText(lead,TextView.BufferType.SPANNABLE);
    }
    private void colorCueDots(Spannable lead){
        // 每秒只变化圆点颜色，同一行不会因倒计时反复换行。
        int passed=5-countdownValue;
        for(int i=0;i<5;i++){
            int start=i*3,color=i<passed?0x33ffffff:WHITE;
            ForegroundColorSpan[] prior=lead.getSpans(start,start+1,ForegroundColorSpan.class);
            if(prior.length==1&&prior[0].getForegroundColor()==color)continue;
            for(ForegroundColorSpan span:prior)lead.removeSpan(span);
            lead.setSpan(new ForegroundColorSpan(color),start,start+1,Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
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
        int former=activeLine;
        activeLine=index;
        int formerPrevious=LyricWindow.previous(former,lyricViews.size());
        int previous=LyricWindow.previous(index,lyricViews.size());
        updateLyricTone(former,index,previous);
        updateLyricTone(formerPrevious,index,previous);
        if(previous!=former&&previous!=formerPrevious)updateLyricTone(previous,index,previous);
        if(index!=former&&index!=formerPrevious)updateLyricTone(index,index,previous);
        requestLyricAlignment();
    }
    private void updateLyricTone(int row,int currentLine,int previousLine){
        if(row<0||row>=lyricViews.size())return;
        TextView line=lyricViews.get(row);
        boolean active=row==currentLine;
        line.setTextColor(active?(theme.equals("festival")?accent:WHITE):row==previousLine?0xffe2e2e2:MUTED);
        if(line.isSelected()!=active){
            line.setSelected(active);
            line.setBackgroundColor(active?0x80303335:Color.TRANSPARENT);
        }
        textShadow(line,active);
    }
    private static PathInterpolator stageEase(){return new PathInterpolator(.2f,0f,0f,1f);}
    private void requestLyricAlignment(){
        if(alignmentPosted||!foreground||lyricScroll.getVisibility()!=View.VISIBLE)return;
        alignmentPosted=true;
        lyricScroll.getViewTreeObserver().addOnPreDrawListener(alignLyrics);
        lyricScroll.postInvalidateOnAnimation();
    }
    private void cancelLyricAlignment(){
        if(alignmentPosted){
            lyricScroll.getViewTreeObserver().removeOnPreDrawListener(alignLyrics);
            alignmentPosted=false;
        }
        if(lyricMotion!=null){lyricMotion.cancel();lyricMotion=null;}
    }
    private final ViewTreeObserver.OnPreDrawListener alignLyrics=new ViewTreeObserver.OnPreDrawListener(){
        @Override public boolean onPreDraw(){
            lyricScroll.getViewTreeObserver().removeOnPreDrawListener(this);
            alignmentPosted=false;
            alignDisplayedLyrics();
            return true;
        }
    };
    private void alignDisplayedLyrics(){
        int index=activeLine;
        if(!foreground||lyricScroll.getVisibility()!=View.VISIBLE||index<0||index>=lyricViews.size())return;
        TextView line=lyricViews.get(index);
        TextView before=lyricViews.get(Math.max(0,index-1)),after=lyricViews.get(Math.min(lyricViews.size()-1,index+1));
        int y=LyricWindow.scrollTarget(before.getTop(),line.getTop(),line.getBottom(),after.getBottom(),lyricScroll.getHeight());
        if(lyricMotion!=null&&lyricMotion.isRunning()&&lyricMotionTarget==y)return;
        if(lyricMotion!=null){lyricMotion.cancel();lyricMotion=null;}
        int displayed=lyricScroll.getScrollY();
        if(displayed==y)return;
        if(!motionEnabled){lyricScroll.scrollTo(0,y);return;}
        // 从屏幕已经显示的位置继续，较密的歌词可随时替换旧目标。
        lyricMotionTarget=y;
        lyricMotion=ValueAnimator.ofInt(displayed,y);
        lyricMotion.setDuration(STAGE_MOTION_MS);
        lyricMotion.setInterpolator(stageEase());
        lyricMotion.addUpdateListener(animation->lyricScroll.scrollTo(0,(Integer)animation.getAnimatedValue()));
        lyricMotion.start();
    }
    private void showVideoIfReady(){
        if(picture.getAlpha()!=0||!videoRendered||!videoReady||videoSeeking||!foreground||!engine.backingPresented())return;
        long ms=engine.playheadFrames/48,target=ms+videoOffsetMs;
        if(target<0||target>=videoDurationMs-40)return;
        picture.setAlpha(1);
        // 新画面先显示同一播放时刻的歌词，再收起滚动歌词。
        updateMvLyrics(ms);
        showScrollingLyrics(false);
        trackTitle.setVisibility(View.GONE);
    }
    private void hideVideoSurface(){
        if(picture.getAlpha()==0)return;
        picture.setAlpha(0);
        mvLyrics.setVisibility(View.GONE);
        visibleMvLyric=-2;
        boolean hasSong=current!=null;
        showScrollingLyrics(hasSong);
        trackTitle.setVisibility(hasSong?View.VISIBLE:View.GONE);
    }
    private void syncVideo(boolean force){
        MediaPlayer player=video;
        if(player==null||!videoReady||!engine.backingPresented()||!foreground)return;
        long target=engine.playheadFrames/48+videoOffsetMs;
        if(target>=videoDurationMs-40){releaseVideo();return;}
        try{
            if(target<0){
                // 负偏移先保留滚动歌词，已准备但未开始的播放器可以直接等待。
                if(player.isPlaying())player.pause();
                hideVideoSurface();
                return;
            }
            videoTargetMs=target;videoPositionMs=player.getCurrentPosition();
            if(videoSeeking)return;
            videoClock.update(target,videoPositionMs,SystemClock.elapsedRealtime(),force);
            if(videoClock.seek){videoSeeking=true;videoSeekCount++;player.seekTo(target,MediaPlayer.SEEK_CLOSEST);}
            else if(!player.isPlaying())player.start();
        }catch(Exception ignored){releaseVideo();}
    }
    private void releaseVideo(){
        hideVideoSurface();
        videoReady=false;videoRendered=false;videoSeeking=false;
        if(video!=null){try{video.release();}catch(Exception ignored){}video=null;}
        if(videoSurface!=null){videoSurface.release();videoSurface=null;}
    }
    @Override public void onSurfaceTextureAvailable(SurfaceTexture texture,int w,int h){if(engine.running)prepareVideo();}
    @Override public boolean onSurfaceTextureDestroyed(SurfaceTexture texture){releaseVideo();return true;}
    @Override public void onSurfaceTextureSizeChanged(SurfaceTexture texture,int w,int h){}
    @Override public void onSurfaceTextureUpdated(SurfaceTexture texture){videoSurfaceFrames++;showVideoIfReady();}
    private void enlargeQr(){
        if(qrBitmap==null||qrPreview!=null)return;
        final boolean returnToQr=smallQr.hasFocus();
        ImageView view=new ImageView(this);
        view.setImageBitmap(qrBitmap);
        view.setPadding(dp(18),dp(18),dp(18),dp(18));
        qrPreview=new AlertDialog.Builder(this).setTitle("扫码点歌").setView(view).setPositiveButton("返回",null).create();
        if(!ValueAnimator.areAnimatorsEnabled()&&qrPreview.getWindow()!=null)qrPreview.getWindow().setWindowAnimations(0);
        qrPreview.setOnDismissListener(dialog->{
            if(qrPreview!=dialog)return;
            qrPreview=null;
            if(returnToQr&&qrBitmap!=null&&smallQr.getVisibility()==View.VISIBLE)smallQr.requestFocus();
        });
        qrPreview.show();
    }
    private void maintenance(){
        List<String> items=new ArrayList<>();
        if(qrBitmap!=null)items.add("手机点歌");
        items.addAll(Arrays.asList("声音","连接设置","软件更新","黑屏调试"));
        if(client!=null&&!roomId.isEmpty())items.add("结束本场");
        new AlertDialog.Builder(this).setTitle("房间设置").setItems(items.toArray(new String[0]),(dialog,which)->{
            switch(items.get(which)){
                case "手机点歌":enlargeQr();break;
                case "声音":sound();break;
                case "连接设置":connection();break;
                case "软件更新":softwareUpdate();break;
                case "黑屏调试":setDebugScreen(true);break;
                case "结束本场":endParty();break;
                default:break;
            }
        }).setNegativeButton("返回",(d,w)->{}).show();
    }
    private void softwareUpdate(){
        boolean permission=updater.needsSourcePermission();
        new AlertDialog.Builder(this).setTitle("软件更新")
            .setMessage("当前版本 "+installedVersion+"\n"+updater.stateText())
            .setNegativeButton("返回",(d,w)->{})
            .setPositiveButton(permission?"打开系统设置":"重新检查",(d,w)->{
                if(permission){
                    try{
                        startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,Uri.parse("package:"+getPackageName())));
                    }catch(RuntimeException unavailable){
                        toast("请在电视的应用安装设置中允许 Friday KTV 更新。");
                    }
                }else updater.checkAgain();
            }).show();
    }
    private LinearLayout sheet(){LinearLayout panel=new LinearLayout(this);panel.setOrientation(LinearLayout.VERTICAL);panel.setPadding(dp(24),dp(16),dp(24),dp(16));return panel;}
    private void connection(){
        LinearLayout panel=sheet();EditText origin=new EditText(this),key=new EditText(this);origin.setSingleLine(true);origin.setTextSize(16);origin.setMinHeight(dp(56));origin.setText(preferences.getString("origin",SetupDefaults.ORIGIN));origin.setContentDescription("NAS 家庭服务地址");key.setSingleLine(true);key.setTextSize(16);key.setMinHeight(dp(56));key.setInputType(129);key.setText(preferences.getString("key",SetupDefaults.KEY));key.setContentDescription("家庭服务连接密钥");panel.addView(text("NAS 家庭服务地址",15,0xff26352c));panel.addView(origin);panel.addView(text("连接密钥",15,0xff26352c));panel.addView(key);ScrollView scroll=new ScrollView(this);scroll.addView(panel);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("服务连接").setView(scroll).setNegativeButton("返回",(d,w)->{}).setPositiveButton("连接",null).create();dialog.setOnShowListener(d->{dialog.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{try{new KtvClient(origin.getText().toString().trim(),key.getText().toString().trim(),new File(getCacheDir(),"audio"));preferences.edit().putString("origin",origin.getText().toString().trim()).putString("key",key.getText().toString().trim()).apply();dialog.dismiss();connect();}catch(Exception error){origin.setError("使用 NAS 的 HTTPS 地址或家庭网络地址，并检查电视连接密钥。");}});});dialog.show();
    }
    private void sound(){
        LinearLayout panel=sheet();
        ScrollView scroll=new ScrollView(this);scroll.addView(panel);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("声音").setView(scroll).setPositiveButton("完成",(d,w)->{}).create();
        slider(panel,"歌曲音量",Math.round(engine.mixer.musicGain*100),100,v->engine.mixer.musicGain=v/100f);
        dialog.show();
    }
    private interface Knob{void set(int value);}
    private void slider(LinearLayout panel,String name,int initial,int max,Knob action){TextView label=text(name+" "+initial+"%",16,0xff26352c);panel.addView(label);SeekBar bar=new SeekBar(this);bar.setContentDescription(name);bar.setMax(max);bar.setProgress(initial);bar.setMinimumHeight(dp(48));bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener(){public void onStartTrackingTouch(SeekBar b){}public void onStopTrackingTouch(SeekBar b){}public void onProgressChanged(SeekBar b,int value,boolean user){action.set(value);label.setText(name+" "+value+"%");}});panel.addView(bar);}
    private JSONObject telemetry(){
        JSONObject data=new JSONObject();
        try{
            KtvClient owner=client;
            if(owner!=null)data.put("tv_cache_bytes",owner.mediaCache.bytes()).put("tv_cache_limit",owner.mediaCache.limit());
            data.put("apk_version",installedVersion)
                .put("warmup_active",warmup).put("warmup_pool",warmupJobs.size()).put("lyric_lines",lines.length())
                .put("song_background_visible",aiBackdropVisible&&!videoReady)
                .put("next_prefetched",!prefetchedId.isEmpty())
                .put("video_seek_count",videoSeekCount).put("video_drift_abs_ms",Math.abs(videoClock.drift))
                .put("video_buffer_events",videoBufferEvents).put("video_surface_frames",videoSurfaceFrames)
                .put("video_speed_supported",videoSpeedSupported)
                .put("video_position_ms",videoPositionMs).put("video_target_ms",videoTargetMs)
                .put("video_seek_result_ms",seekResultMs).put("video_prepare_count",videoPrepareCount).put("mv_ready",videoReady)
                .put("audio_backend",engine.audioBackend).put("audio_mixed_peak",engine.mixedPeak)
                .put("output_frames",engine.renderedFrames).put("output_sample_rate",engine.outputSampleRate)
                .put("output_device_id",engine.outputDevice).put("output_type",engine.outputType)
                .put("output_buffer_frames",engine.bufferFrames).put("output_queued_frames",engine.outputQueuedFrames)
                .put("output_buffer_requested",engine.outputBufferRequested).put("underruns",engine.underruns)
                .put("backing_switches",engine.backingSwitches).put("backing_failures",engine.backingFailures)
                .put("route_restart_count",routeRestartCount).put("input_frames",engine.capturedFrames);
        }catch(JSONException ignored){}
        return data;
    }
    private void endParty(){
        if(client==null||roomId.isEmpty()||endRoomDialog!=null)return;
        final KtvClient closingClient=client;
        final String closingRoom=roomId;
        final long epoch=connectionEpoch;
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("结束本场？").setMessage("本场歌单将关闭，朋友需扫新码重新点歌。")
            .setNegativeButton("继续本场",null).setPositiveButton("结束本场",null).create();
        endRoomDialog=dialog;
        dialog.setOnDismissListener(d->{if(endRoomDialog==dialog)endRoomDialog=null;});
        dialog.setOnShowListener(d->dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{
            if(!ownsConnection(closingClient,epoch)||!closingRoom.equals(roomId)){dialog.dismiss();return;}
            dialogueClosing(dialog,true);
            try{roomNetwork.execute(()->{
                boolean closed=false;
                try{if(ownsConnection(closingClient,epoch))closed=closingClient.closeParty(closingRoom);}
                catch(Exception ignored){/* 读取失败时保留本场和重试操作。 */}
                final boolean confirmed=closed;
                ui.post(()->finishRoomClose(dialog,closingClient,closingRoom,epoch,confirmed));
            });}catch(RejectedExecutionException ignored){finishRoomClose(dialog,closingClient,closingRoom,epoch,false);}
        }));
        dialog.show();
    }
    private void dialogueClosing(AlertDialog dialog,boolean waiting){
        dialog.setCancelable(!waiting);
        dialog.setCanceledOnTouchOutside(false);
        dialog.setMessage(waiting?"正在结束本场…":"暂未确认本场已结束，可以重试。");
        dialog.getButton(AlertDialog.BUTTON_NEGATIVE).setEnabled(!waiting);
        Button confirm=dialog.getButton(AlertDialog.BUTTON_POSITIVE);
        confirm.setEnabled(!waiting);
        if(!waiting)confirm.setText("重试");
    }
    private void finishRoomClose(AlertDialog dialog,KtvClient owner,String room,long epoch,boolean closed){
        if(endRoomDialog!=dialog||isDestroyed())return;
        if(client!=owner||connectionEpoch!=epoch||!room.equals(roomId)){dialog.dismiss();return;}
        if(!closed){dialogueClosing(dialog,false);return;}
        autoRoom=false;protocolReady=coordinatorVisible=false;connectionEpoch++;roomEpoch++;controlEpoch++;
        coordinatorSession.closed();pendingPlay=null;qrBitmap=null;roomId="";roomInvitation="";
        jobs.clear();resetRoomQueue();stopAudio(true);current=null;currentId="";currentManifest=null;
        showIdle();connectionBadge.setText("");headline.setText("本场已结束");setRoomNotice("选择开启房间即可继续。",false);
        dialog.dismiss();
    }
    private final AudioDeviceCallback devices=new AudioDeviceCallback(){
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] added){routeChanged();}
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] removed){routeChanged();}
    };
    private void routeChanged(){
        if(!engine.running||engine.outputDevice<0)return;
        for(AudioDeviceInfo d:audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS))
            if(d.getId()==engine.outputDevice)return;
        routeRestartCount++;
        JSONObject job=current;
        long frame=engine.playheadFrames;
        stopAudio(true);
        int stopped=generation.get();
        ui.postDelayed(()->{if(foreground&&job!=null&&current==job&&generation.get()==stopped)play(job,frame);},400);
    }
    private String time(long ms){long seconds=Math.max(0,ms/1000);return String.format(Locale.ROOT,"%02d:%02d",seconds/60,seconds%60);}
    private void syncPartyEffects(){
        if(partyEffects==null||soundEffects==null)return;
        if("playing".equals(soundEffects.state)){
            if(shownSfx!=soundEffects.sequence){shownSfx=soundEffects.sequence;partyEffects.show(soundEffects.action);}
        }else if(!"pending".equals(soundEffects.state)){partyEffects.stop();}
    }
    private void refreshMotionPreference(){
        boolean enabled=ValueAnimator.areAnimatorsEnabled();
        if(enabled==motionEnabled)return;
        motionEnabled=enabled;
        roomProgress.setVisibility(noticeWaiting&&enabled?View.VISIBLE:View.GONE);
        if(!enabled){
            cancelLyricAlignment();
            lyricScroll.animate().cancel();lyricScroll.setAlpha(1f);
            requestLyricAlignment();
        }
    }
    private void renderPlaybackProgress(long ms){
        JSONObject song=trackSpec(current,true);
        long duration=song==null?0:Math.round(song.optDouble("duration_seconds")*1000);
        int value=duration>0?(int)Math.min(10000,ms*10000/duration):0;
        if(progress.getProgress()!=value)progress.setProgress(value);
        if(shownSecond==ms/1000&&shownDurationSecond==duration/1000)return;
        shownSecond=ms/1000;
        shownDurationSecond=duration/1000;
        String shortcuts=smallQr.hasFocus()?"":"     ↑ 原唱/伴奏   ← 重唱   → 切歌";
        clock.setText(time(ms)+"  /  "+time(duration)+shortcuts);
    }
    private final Runnable tick=new Runnable(){
        public void run(){
            if(!foreground)return;
            refreshMotionPreference();advanceBackingRetry(System.nanoTime());refreshCoordinatorStatus();syncPartyEffects();
            advancePlaybackStart(SystemClock.elapsedRealtime());
            if(ticks++%5==0){poll();refreshMissingLyrics();tryTvUpdate();}
            if(engine.running&&current!=null&&engine.backingPresented()){
                if(awaitingPlaybackPresentation)presentBacking();
                long ms=engine.playheadFrames/48;
                updateMvLyrics(ms);
                if(lyricCue.current!=activeLine)highlightScrollingLyrics(lyricCue.current);
                renderPlaybackProgress(ms);
                if(ticks%5==0){syncVideo(false);loadAlternateBacking();}
            }
            ui.postDelayed(this,100);
        }
    };
    private void remoteAction(RemoteShortcuts.Action action){
        if(action==RemoteShortcuts.Action.NEXT||action==RemoteShortcuts.Action.REPLAY)pendingPlay=null;
        if(action==RemoteShortcuts.Action.NEXT){next();return;}
        if(action==RemoteShortcuts.Action.REPLAY){if(!userPlayback())return;if(current!=null){JSONObject job=current;stopAudio(false);queueIntent.awaitNext();play(job,0,true);Toast.makeText(this,"从头重唱",Toast.LENGTH_SHORT).show();}return;}
        if(client==null)return;
        final int value=original?0:1;
        final long request=localOriginal.request(value,roomOriginalRevision);
        original=value==1;
        switchMusic();
        final KtvClient c=client;final String room=roomId;final long epoch=controlEpoch;
        toast(original?"原唱":"伴奏");
        try{controlActions.execute(()->{try{
            if(!foreground||epoch!=controlEpoch||c!=client||!localOriginal.isCurrent(request))return;
            JSONObject reply=c.controlJson("/v1/tv/controls",new JSONObject().put("room_id",room).put("original",value));
            if(!reply.optBoolean("accepted")||!reply.has("revision"))throw new IOException("control_unavailable");
            ui.post(()->{if(c==client&&room.equals(roomId))localOriginal.accepted(request,reply.optLong("revision"));});
        }catch(Exception e){ui.post(()->{if(c==client&&room.equals(roomId)&&localOriginal.isCurrent(request))toast("本机已操作，手机状态暂未同步");});}});
        }catch(RejectedExecutionException ignored){toast("本机已操作，手机状态暂未同步");}

    }
    @Override public boolean dispatchKeyEvent(KeyEvent event){
        if(debugScreen){
            int key=event.getKeyCode();
            if(event.getAction()==KeyEvent.ACTION_UP&&(key==KeyEvent.KEYCODE_BACK||key==KeyEvent.KEYCODE_DPAD_CENTER||key==KeyEvent.KEYCODE_ENTER))
                setDebugScreen(false);
            return true;
        }
        boolean enabled=foreground&&hasWindowFocus()&&current!=null;
        boolean qrAvailable=smallQr.getVisibility()==View.VISIBLE;
        RemoteShortcuts.Action shortcut=remoteShortcuts.press(event.getKeyCode(),event.getAction()==KeyEvent.ACTION_DOWN,
            event.getRepeatCount(),enabled,qrAvailable,smallQr.hasFocus());
        if(RemoteShortcuts.consumes(event.getKeyCode(),enabled,qrAvailable)){
            if(shortcut==RemoteShortcuts.Action.FOCUS_QR)smallQr.requestFocus();
            else if(shortcut==RemoteShortcuts.Action.FOCUS_STAGE)page.requestFocus();
            else if(shortcut!=RemoteShortcuts.Action.NONE)remoteAction(shortcut);
            return true;
        }
        if(event.getKeyCode()==KeyEvent.KEYCODE_DPAD_CENTER&&(warmup||current==null&&!hasPending())&&!smallQr.hasFocus()&&!warmupButton.hasFocus()){if(event.getAction()==KeyEvent.ACTION_DOWN&&event.getRepeatCount()==0)toggleWarmup();return true;}return super.dispatchKeyEvent(event);}
    @Override public boolean onKeyDown(int code,KeyEvent event){
        if(event.getRepeatCount()==0){
            if(code==KeyEvent.KEYCODE_MENU){maintenance();return true;}
            if(code==KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE||code==KeyEvent.KEYCODE_MEDIA_PLAY){
                if(!autoRoom||!protocolReady||warmup||current==null&&!hasPending())toggleWarmup();
                else togglePause();
                return true;
            }
            if(code==KeyEvent.KEYCODE_MEDIA_NEXT){next();return true;}
        }
        return super.onKeyDown(code,event);
    }
    @Override public void onBackPressed(){if(debugScreen)setDebugScreen(false);else super.onBackPressed();}
    @Override protected void onSaveInstanceState(Bundle state){super.onSaveInstanceState(state);state.putBoolean("debug_screen",debugScreen);}
    @Override protected void onStart(){
        super.onStart();foreground=true;
        refreshMotionPreference();
        if(current!=null&&!engine.running)clock.setText("已暂停");
        ui.post(tick);ui.post(controlTick);
    }
    @Override protected void onStop(){
        foreground=false;controlEpoch++;pendingPlay=null;
        cancelLyricAlignment();lyricScroll.animate().cancel();lyricScroll.setAlpha(1f);
        prefetchCancel.set(true);ui.removeCallbacks(tick);ui.removeCallbacks(controlTick);
        localOriginal.clear();queueIntent.pause();stopAudio(true);
        super.onStop();
    }
    @Override protected void onDestroy(){
        if(endRoomDialog!=null)endRoomDialog.dismiss();
        cancelPlaybackStart();
        songBackdrop.select("");backdropJob=null;background.setImageDrawable(null);backdropNetwork.shutdownNow();soundEffects.close();
        audio.unregisterAudioDeviceCallback(devices);transferCancel.set(true);engine.stop();releaseVideo();
        retireActivePlayback(engine.playbackWorker());playbackReleases.close();
        playbackStarts.shutdown();
        roomNetwork.shutdownNow();roomReports.shutdownNow();roomQrNetwork.shutdownNow();lyricsNetwork.shutdownNow();
        network.shutdownNow();backingNetwork.shutdownNow();videoNetwork.shutdownNow();controlNetwork.shutdownNow();
        controlActions.shutdownNow();preloader.shutdownNow();
        super.onDestroy();
    }
}
