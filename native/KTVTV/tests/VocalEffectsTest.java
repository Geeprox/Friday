package local.friday.ktv;
import android.content.Context;
import android.os.SystemClock;
import local.friday.audio.WetClient;
import org.json.JSONObject;

/** Production controller with transport/clock fakes; no device/audio actions. */
public final class VocalEffectsTest {
 static final VocalEffectsPolicy.DryLease LEASE=new VocalEffectsPolicy.DryLease(30,4,5,6);
 static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
 static JSONObject nativeState(int profile,int gain,boolean muted)throws Exception{return new JSONObject().put("profile_id",profile).put("wet_permille",VocalEffectsPolicy.WET_PERMILLE[profile]).put("monitor_gain_permille",gain*10).put("mic_muted",muted?1:0).put("bypass",muted?1:0).put("command_epoch",3).put("applied_epoch",3).put("output_actual_device_id",6).put("input_closed",0).put("output_closed",0).put("rendered_wet_samples",40);}
 static JSONObject state(WetClient client,boolean active,boolean ready,String reason,JSONObject n)throws Exception{return new JSONObject().put("active",active).put("native_ready",ready).put("pid",11).put("uid",20).put("lease",4).put("input_device",5).put("output_device",6).put("request_id",client.lastRequestId()).put("reason",reason).put("native",n);}
 static JSONObject closed()throws Exception{return new JSONObject().put("input_closed",1).put("output_closed",1).put("never_opened",true);}
 static VocalEffectsController ready()throws Exception{
  VocalEffectsController c=new VocalEffectsController(new Context(),()->{});c.environment(LEASE,true,false,20);
  yes(c.audition(1),"explicit start accepted");WetClient w=WetClient.latest;w.connect();w.emit(state(w,false,false,"",closed()));
  yes(w.starts==1,"exactly one start after connection");w.emit(state(w,true,true,"",nativeState(1,20,false)));yes(c.ready()&&c.available(),"matching native grants capability");
  yes(c.capabilities().length()==7&&!c.released(),"ready native exposes all seven profiles and still owns resources");return c;
 }
 public static void main(String[] args)throws Exception{
  int before=WetClient.instances;VocalEffectsController c=new VocalEffectsController(new Context(),()->{});
  yes(c.released(),"never-opened controller is released");
  c.environment(LEASE,true,false,20);c.select("pop");yes(WetClient.instances==before&&!c.available(),"poll and selection never bind");
  yes(c.audition(1),"audition binds");WetClient canceled=WetClient.latest;c.suspend(6);canceled.connect();canceled.emit(state(canceled,false,false,"",closed()));
  yes(canceled.starts==0&&canceled.closes==1,"cancel before connect cannot late start");

  c=new VocalEffectsController(new Context(),()->{});c.environment(LEASE,true,false,20);c.audition(1);WetClient notStarted=WetClient.latest;notStarted.connect();c.select("dry");
  notStarted.emit(state(notStarted,false,false,"local_wet_stop",new JSONObject()));yes(!c.canAudition(),"empty native JSON is not release evidence");
  notStarted.emit(state(notStarted,false,false,"local_wet_stop",closed()));yes(c.canAudition()&&notStarted.starts==0,"explicit never-opened closure releases closing");

  c=ready();WetClient w=WetClient.latest;int starts=w.starts;
  c.environment(LEASE,true,true,20);yes(w.bypasses>0&&w.muted&&w.bypass&&w.starts==starts,"mute immediately sends zero gate without new start");
  yes(!c.ready(),"old unmuted receipt cannot acknowledge mute");w.emit(state(w,true,true,"",nativeState(1,20,true)));yes(c.ready()&&c.available(),"muted native config retains capability");
  c.environment(LEASE,true,false,20);yes(!w.muted&&!w.bypass&&w.starts==starts,"unmute configures existing wet engine");
  c.environment(LEASE,true,true,0);yes(w.gain==0&&w.bypass,"gain zero gates output");
  c.environment(LEASE,true,false,40);yes(w.gain==400&&!w.bypass&&w.starts==starts,"gain restore uses same authorized lease");
  w.emit(state(w,true,true,"",nativeState(1,40,false)));c.select("opera");
  w.emit(state(w,true,true,"",nativeState(1,40,false)));yes(!c.ready()&&"pending".equals(c.reportState()),"stale profile native readback not applied");
  JSONObject changing=nativeState(4,40,false).put("applied_epoch",2);w.emit(state(w,true,true,"",changing));yes(!c.ready(),"pending native epoch rejected");
  w.emit(state(w,true,true,"",nativeState(4,40,false)));yes(c.ready(),"current native configuration applied");
  for(int profile=5;profile<=6;profile++){
   c.select(VocalEffectsPolicy.KEYS[profile]);yes(!c.ready(),"new preset waits for actual native application");
   w.emit(state(w,true,true,"",nativeState(profile,40,false)));yes(c.ready(),"new native profile revision applies");
  }

  c.select("dry");yes(w.stops>0&&w.starts==starts,"dry closes only wet");c.select("ktv");yes(w.starts==starts,"new preset waits for STOP readback");
  yes(!c.released(),"STOP request alone cannot release backend ownership");
  w.emit(state(w,false,false,"local_wet_stop",closed()));yes(w.starts==starts+1,"already-authorized phone preset can reopen after close");
  w.emit(state(w,true,true,"",nativeState(2,40,false)));yes(c.ready(),"reopened native matches");
  w.emit(state(w,false,false,"native_wet_fault_1",closed()));yes(c.fault()&&!c.available(),"native unexpected stop faults and drops capability");
  int afterFault=w.starts;c.environment(LEASE,true,false,40);c.select("pop");yes(w.starts==afterFault,"room polls never retry a fault");

  c=ready();w=WetClient.latest;c.environment(new VocalEffectsPolicy.DryLease(31,4,5,6),true,false,20);yes(w.stops>0&&!c.available(),"Guard PID change invalidates reused lease number");
  w.emit(state(w,false,false,"local_wet_stop",closed()));c.environment(LEASE,true,false,20);yes(w.starts==1,"new environment cannot reauthorize");
  c=ready();w=WetClient.latest;c.environment(LEASE,false,false,20);yes(w.stops>0&&!c.available(),"pause/background revoke");
  yes(!c.released(),"pause cannot invent a closed receipt");w.emit(state(w,false,false,"local_wet_stop",closed()));yes(c.released(),"confirmed input/output closure releases backend ownership");
  c=ready();w=WetClient.latest;w.failBypass=true;c.environment(LEASE,true,true,20);yes(c.fault()&&w.closes==1,"synchronous Binder failure during mute cannot fall through to configure");

  c=ready();w=WetClient.latest;JSONObject stats=nativeState(1,20,false).put("input_timestamp_result",0).put("input_age_estimate_ns",4000000).put("input_timestamp_ns",1).put("input_timestamp_sample_ns",900000000);
  w.emit(state(w,true,true,"",stats));JSONObject out=new JSONObject();c.diagnostics(out);yes(out.optBoolean("wet_timestamp_available")&&out.optLong("wet_input_timestamp_age_us")==4000,"age uses native estimate, not timestamp sample gap");
  stats.put("input_age_estimate_ns",-1);w.emit(state(w,true,true,"",stats));out=new JSONObject();c.diagnostics(out);yes(!out.optBoolean("wet_timestamp_available")&&out.optLong("wet_input_timestamp_age_us")==0,"unknown age remains unavailable");
  SystemClock.now+=1300;yes(!c.ready(),"stale wet readback cannot claim applied");

  VocalEffectsIntent intent=new VocalEffectsIntent();long a=intent.request(1,7);yes(intent.resolve(0,7)==1,"old dry does not cancel audition");intent.failed(a);yes(intent.resolve(0,7)==1&&intent.failed(),"HTTP failure preserves local audition without retry");yes(intent.resolve(4,8)==4&&!intent.current(a),"new phone revision takes over");
  long b=intent.request(2,8);intent.accepted(a,50);yes(intent.resolve(0,8)==2,"late old ACK ignored");intent.accepted(b,9);yes(intent.resolve(0,8)==2&&intent.resolve(2,9)==2&&!intent.pending(),"wait for matching ACK revision");
  b=intent.request(1,9);intent.clear();intent.accepted(b,10);yes(intent.resolve(0,9)==0,"background/reset discards late ACK");
  System.out.println("VocalEffectsTest passed: real adapter lifecycle, native readback, zero gate, stop/late start, fault latch, identity and revision races");
 }
}
