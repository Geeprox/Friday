package local.friday.audio;
import android.content.Context;
import org.json.JSONObject;
/** No Binder, microphone, native library or audio output is used. */
public final class WetClient {
 public interface Listener {void changed(String value);}
 public static WetClient latest;public static int instances;
 public final Listener listener;public int starts,stops,bypasses,configures,heartbeats,closes,binds;
 public int gain;public boolean muted,bypass,connected,failBypass;public String profile="dry";private long sequence;
 public WetClient(Context context,Listener listener){this.listener=listener;latest=this;instances++;}
 public void bind(){binds++;}
 public boolean connected(){return connected;}
 public long lastRequestId(){return sequence;}
 public void connect(){connected=true;++sequence;}
 public boolean start(long lease,int input,int output,String profile,float wet,float gain,boolean muted){starts++;++sequence;this.profile=profile;this.gain=Math.round(gain*1000);this.muted=muted;bypass=false;return true;}
 public void configure(String profile,float wet,float gain,boolean muted,boolean bypass){configures++;++sequence;this.profile=profile;this.gain=Math.round(gain*1000);this.muted=muted;this.bypass=bypass;}
 public void heartbeat(long lease,int input,int output,boolean dry,float gain,boolean muted){heartbeats++;++sequence;this.gain=Math.round(gain*1000);this.muted=muted;}
 public void bypass(){bypasses++;++sequence;bypass=true;if(failBypass){connected=false;try{emit(new JSONObject().put("stage","WET_PROCESS_LOST").put("active",false));}catch(Exception e){throw new AssertionError(e);}}}
 public void stop(){stops++;++sequence;}
 public void close(){closes++;stop();connected=false;}
 public void emit(JSONObject value){listener.changed(value.toString());}
}
