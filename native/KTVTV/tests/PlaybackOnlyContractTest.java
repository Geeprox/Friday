package local.friday.ktv;

import android.media.AudioDeviceInfo;
import java.io.IOException;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Arrays;
import org.json.JSONObject;

/** 用合成旧房间设置验证正式播放路径，不创建 Android 音频对象。 */
public final class PlaybackOnlyContractTest {
    private static void check(boolean value,String reason){if(!value)throw new AssertionError(reason);}

    private static <T> T empty(Class<T> type)throws Exception{
        Class<?> unsafe=Class.forName("sun.misc.Unsafe");
        Field access=unsafe.getDeclaredField("theUnsafe");
        access.setAccessible(true);
        return type.cast(unsafe.getMethod("allocateInstance",Class.class).invoke(access.get(null),type));
    }
    private static Field field(Object object,String name)throws Exception{
        Field field=object.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return field;
    }
    private static final class OldRoomControls extends JSONObject {
        long revision;
        @Override public long optLong(String key,long fallback){
            if(key.equals("control_revision"))return revision;
            if(key.equals("mic_revision"))return revision;
            return fallback;
        }
        @Override public int optInt(String key,int fallback){
            switch(key){
                case "original": return 1;
                case "music_gain": return 43;
                case "sfx_gain": return 31;
                case "mic_muted": return 0;
                case "mic_gain": return 100;
                default: return fallback;
            }
        }
        @Override public String optString(String key,String fallback){return key.equals("vocal_preset")?"ktv":fallback;}
    }
    private static void legacyRoomControls()throws Exception{
        MainActivity activity=empty(MainActivity.class);
        AudioEngine playback=empty(AudioEngine.class);
        field(playback,"playbackOnly").setBoolean(playback,true);
        field(playback,"mixer").set(playback,new PcmMixer());
        SoundEffectPlayer sfx=empty(SoundEffectPlayer.class);
        field(sfx,"lock").set(sfx,new Object());
        field(activity,"engine").set(activity,playback);
        field(activity,"soundEffects").set(activity,sfx);
        field(activity,"localOriginal").set(activity,new LocalControlIntent());
        field(activity,"appliedRevision").setLong(activity,-1);

        OldRoomControls controls=empty(OldRoomControls.class);
        for(long revision:new long[]{24,24,25}){
            controls.revision=revision;
            activity.applyRoomControls(controls);
            check(playback.mixer.micMuted&&playback.mixer.gainA==.5f&&playback.mixer.gainB==.5f,
                "旧房间的开麦和增益不改变播放引擎输入状态");
            check(playback.mixer.compatEffectsStatus().requestedProfile.equals("dry"),"旧预设不会加载人声效果");
        }
        check(field(activity,"original").getBoolean(activity),"原唱命令仍生效");
        check(playback.mixer.musicGain==.43f&&field(sfx,"gain").getInt(sfx)==31,"歌曲音量和氛围音量仍生效");
        check(field(activity,"appliedRevision").getLong(activity)==25,"房间声音更新继续确认");
        AudioDeviceInfo device=empty(AudioDeviceInfo.class);
        try{playback.start(null,device,null,0);throw new AssertionError("播放引擎接收了录音设备");}
        catch(IOException denied){check(denied.getMessage().equals("separate_microphone_owner"),"打开任何 Android 对象前拒绝输入路由");}
        try{playback.start(null,null,device,0);throw new AssertionError("播放引擎接受指定路由");}
        catch(IOException denied){check(denied.getMessage().equals("system_output_required"),"音频继续使用电视系统默认输出");}
    }
    private static void unchangedBacking(){
        PcmMixer before=new PcmMixer(), after=new PcmMixer();
        short[] music={32767,-30000,-24000,32767,25,-1,1000,-1000};
        short[] silence=new short[4],reference=new short[8],backing=new short[8];
        for(float gain:new float[]{0,.43f,1,.72f}){
            before.musicGain=after.musicGain=gain;
            before.mix(music,silence,1,4,0,reference);
            after.mixBacking(music,4,backing);
            check(Arrays.equals(reference,backing),"纯歌曲 PCM 与旧版无录音输入的音量及限幅完全相同");
        }
    }
    public static void main(String[] args)throws Exception{
        legacyRoomControls();
        unchangedBacking();
        String source=new String(Files.readAllBytes(Paths.get("native/KTVTV/android/src/local/friday/ktv/MainActivity.java")),StandardCharsets.UTF_8);
        check(source.contains("engine=AudioEngine.forPlayback(")&&source.contains("engine.startBacking("),"正式入口和播放都使用无录音播放入口");
        for(String removed:new String[]{"VendorMicClient","requestPermissions(","getDevices(AudioManager.GET_DEVICES_INPUTS)",
            "麦克风","话筒","人声效果","setStreamVolume(","setPreferredDevice("}){
            check(!source.contains(removed),"正式入口不含旧控制、输入读取或设备改动："+removed);
        }
        System.out.println("PlaybackOnlyContractTest passed");
    }
}
