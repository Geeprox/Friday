package local.friday.ktv;

import android.media.AudioDeviceInfo;
import java.io.IOException;
import java.lang.reflect.Field;
import sun.misc.Unsafe;

/** Calls the real entry guard without creating a Context, microphone or output. */
public final class PlaybackOnlyOwnershipTest {
    public static void main(String[] args) throws Exception {
        Field access=Unsafe.class.getDeclaredField("theUnsafe");access.setAccessible(true);
        Unsafe unsafe=(Unsafe)access.get(null);
        AudioEngine engine=(AudioEngine)unsafe.allocateInstance(AudioEngine.class);
        Field mode=AudioEngine.class.getDeclaredField("playbackOnly");mode.setAccessible(true);mode.setBoolean(engine,true);
        AudioDeviceInfo input=(AudioDeviceInfo)unsafe.allocateInstance(AudioDeviceInfo.class);
        try {engine.start(null,input,null,0);throw new AssertionError("secondary capture accepted");}
        catch(IOException expected){if(!"separate_microphone_owner".equals(expected.getMessage()))throw expected;}
        if(engine.running||engine.capturedFrames!=0||engine.duplexAttempted)throw new AssertionError("audio started");
        System.out.println("PlaybackOnlyOwnershipTest: a vendor-owned microphone is rejected before any Android or native audio call");
    }
}
