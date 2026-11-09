package local.friday.ktv;
import android.content.Context;
import android.os.SystemClock;
import local.friday.audio.WetClient;
import org.json.JSONObject;

/** Production orchestration, transport/clock fakes, no Android/native audio. */
public final class VoiceProtectionSessionTest {
    static final VocalEffectsPolicy.DryLease LEASE=new VocalEffectsPolicy.DryLease(30,4,5,6);
    static void yes(boolean v,String message){if(!v)throw new AssertionError(message);}
    static JSONObject closed()throws Exception{return new JSONObject().put("input_closed",1).put("output_closed",1);}
    static JSONObject n(int profile,int gain)throws Exception{return new JSONObject().put("profile_id",profile).put("wet_permille",VocalEffectsPolicy.WET_PERMILLE[profile])
        .put("monitor_gain_permille",gain*10).put("mic_muted",0).put("bypass",0).put("command_epoch",3).put("applied_epoch",3)
        .put("output_actual_device_id",6).put("input_closed",0).put("output_closed",0).put("created_ns",1001)
        .put("feedback_ready",1).put("feedback_latched",0).put("feedback_frames",4096).put("feedback_sequence",0).put("feedback_level",0)
        .put("running",1).put("fault",0).put("output_callbacks",10);}
    static JSONObject state(WetClient w,boolean active,JSONObject n)throws Exception{return new JSONObject().put("active",active).put("native_ready",active).put("pid",11).put("uid",20)
        .put("lease",4).put("input_device",5).put("output_device",6).put("request_id",w.lastRequestId()).put("reason",active?"":"local_wet_stop").put("native",n);}
    public static void main(String[] args)throws Exception{
        yes(!VocalEffectsPolicy.resetRequired(false,0,false,100),"healthy soft-unmute preserves the existing observer");
        yes(VocalEffectsPolicy.resetRequired(true,0,false,100)&&VocalEffectsPolicy.resetRequired(false,1,false,100)
            &&VocalEffectsPolicy.resetRequired(false,0,true,100)&&VocalEffectsPolicy.resetRequired(false,0,false,50),"fault or attenuation needs an explicit zero-before-reset restore");
        SystemClock.now=100;int instances=WetClient.instances;
        VocalEffectsController c=new VocalEffectsController(new Context(),()->{},true);
        c.environment(null,true,false,70);yes(WetClient.instances==instances,"no lease cannot open observer");
        c.environment(LEASE,true,true,70);yes(WetClient.instances==instances,"muted choice never auto-starts");
        c.environment(LEASE,true,false,70);WetClient w=WetClient.latest;w.connect();w.emit(state(w,false,closed()));
        yes(w.starts==1&&w.profile.equals("dry"),"requested mic starts zero-output monitor once");
        w.emit(state(w,true,n(0,70).put("feedback_ready",0)));yes(!c.protectionReady(),"not ready from open/ACK alone");
        w.emit(state(w,true,n(0,70)));yes(c.protectionReady()&&c.capabilities().length()==7&&c.ready(),"monitor native readiness opens all seven presets");
        c.mixReady(true);c.select("pop");yes(w.profile.equals("dry")&&!c.ready()&&c.reservation()==250,"reserve dry headroom before wet output");
        c.mixReady(true);yes(w.profile.equals("pop")&&!c.ready(),"pending config is never applied");
        w.emit(state(w,true,n(1,70)));yes(c.ready(),"native config confirms wet");
        c.select("opera");yes(w.profile.equals("dry")&&!c.ready(),"old headroom ACK cannot enable a larger return before new budget");c.mixReady(true);w.emit(state(w,true,n(4,70)));
        int starts=w.starts,stops=w.stops;c.select("dry");yes(w.profile.equals("dry")&&w.starts==starts&&w.stops==stops,"dry preserves the same input/protection session");
        yes(c.reservation()==360,"old wet return retains headroom until zero config is acknowledged");
        w.emit(state(w,true,n(0,70).put("applied_epoch",2)));yes(c.reservation()==360,"requested zero is not callback ACK");
        w.emit(state(w,true,n(0,70)));yes(c.reservation()==360,"zero ACK retains two queued callback buffers");
        w.emit(state(w,true,n(0,70).put("output_callbacks",12)));yes(c.reservation()==0,"zero output drains application buffers before headroom release");
        c.environment(LEASE,true,true,70);yes(w.muted&&w.bypass&&w.starts==starts,"manual mute keeps observer, gates wet");
        c.environment(LEASE,true,false,35);yes(w.gain==350&&w.starts==starts,"protected cap applies to wet reference without reopening");
        w.emit(state(w,true,n(0,35).put("feedback_sequence",3).put("feedback_level",3).put("feedback_latched",1)));
        yes(!c.protectionReady()&&c.feedbackSequence()==3&&c.feedbackLevel()==3,"latch is delivered even when readiness is revoked");
        c.environment(new VocalEffectsPolicy.DryLease(31,4,5,6),true,false,35);
        yes(c.fault()&&c.monitorToken()==0&&c.feedbackSequence()==0,"changed dry identity faults and cannot replay old monitor packet");
        c.environment(LEASE,true,false,35);yes(w.starts==starts,"poll cannot recover a protection fault");
        c.resetProtection();w.emit(state(w,false,closed()));c.environment(LEASE,true,false,35);
        WetClient replacement=WetClient.latest;yes(replacement!=w,"explicit reset closes old binding before replacement");
        replacement.connect();replacement.emit(state(replacement,false,closed()));replacement.emit(state(replacement,true,n(0,35).put("created_ns",2001)));
        yes(c.protectionReady()&&c.monitorToken()==2001,"new monitor has independent replay identity");
        w.emit(state(w,true,n(0,35).put("feedback_level",3).put("feedback_latched",1)));
        yes(c.protectionReady()&&c.monitorToken()==2001,"late old callback cannot mute/rearm a replacement");
        SystemClock.now+=501;yes(!c.protectionReady(),"stale process must revoke readiness before Guard deadline");
        // The production Main policy and Controller must actually leave dry on
        // an inline host. No separate wet reservation or app output route exists.
        SystemClock.now=2000;
        VocalEffectsController inline=new VocalEffectsController(new Context(),()->{},true);
        inline.environment(LEASE,true,false,70);WetClient iw=WetClient.latest;iw.connect();iw.emit(state(iw,false,closed()));
        JSONObject zero=n(0,70).put("output_actual_device_id",0).put("inline_voice_mode",1).put("cache_server_uid",1041)
            .put("cache_capabilities",63).put("cache_generation",7).put("inline_flags",77).put("inline_output_uses_owned_dry_stream",true);
        iw.emit(state(iw,true,zero));yes(!inline.protectionReady()&&!inline.ready(),"render progress cannot replace a completed native output write");
        zero.put("inline_flags",333).put("inline_output_io_ready",true);
        iw.emit(state(iw,true,zero));yes(inline.protectionReady()&&inline.reservation()==0,"inline observer is ready without a second output route or budget");
        inline.select("pop");yes(iw.profile.equals("dry"),"profile waits for the actual gain gate");
        boolean mixing=VocalEffectsPolicy.mixReady(true,true,70,70,70,70,0,1);
        yes(mixing,"inline reference gain and zero external reservation open the shared mix");inline.mixReady(mixing);
        yes(iw.profile.equals("pop"),"production inline path can actually select pop");
        JSONObject pop=n(1,70).put("output_actual_device_id",0).put("inline_voice_mode",1).put("cache_server_uid",1041)
            .put("cache_capabilities",63).put("cache_generation",7).put("inline_flags",333).put("inline_output_uses_owned_dry_stream",true).put("inline_output_io_ready",true)
            .put("command_epoch",5).put("applied_epoch",4);
        iw.emit(state(iw,true,pop));yes(!inline.ready(),"inline request ACK is not callback application");
        pop.put("applied_epoch",5);iw.emit(state(iw,true,pop));yes(inline.ready(),"inline callback application confirms pop without OpenSL route fiction");
        pop.put("inline_flags",77).put("inline_output_io_ready",false);iw.emit(state(iw,true,pop));
        yes(!inline.ready()&&!inline.protectionReady(),"loss of output completion revokes ready even while render and input counters advance");
        yes(!VocalEffectsPolicy.mixReady(true,true,70,70,70,70,250,1),"legacy reservation cannot be mistaken for inline readiness");
        yes(!VocalEffectsPolicy.mixReady(false,true,70,70,70,70,0,1),"legacy still requires its separate dry/wet budget");
        System.out.println("VoiceProtectionSessionTest passed: protected bootstrap, shared observer, ordered mix budget, latch, stale identity and explicit reset");
    }
}
