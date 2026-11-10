package local.friday.ktv.audio;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioManager;
import java.lang.reflect.Method;
import local.friday.ktv.audio.protocol.*;

/** Reflection keeps a denied/absent vendor-facing method a caught diagnostic failure. */
final class VendorBridge {
    private final Context context;private final AudioManager audio;
    private volatile Method getter,setter;
    VendorBridge(Context context,AudioManager audio){this.context=context;this.audio=audio;}
    private Method method(boolean write)throws ReflectiveOperationException{
        Method m=write?setter:getter;if(m!=null)return m;
        synchronized(this){m=write?setter:getter;if(m==null){m=AudioManager.class.getMethod(write?"setParameters":"getParameters",String.class);if(write)setter=m;else getter=m;}return m;}
    }
    String read(String key){
        boolean allowed=false;for(String item:Protocol.READ_KEYS)allowed|=item.equals(key);
        if(!allowed)throw new IllegalArgumentException("closed_query");
        try{Object value=method(false).invoke(audio,key);return value instanceof String?(String)value:"";}
        catch(ReflectiveOperationException|LinkageError|SecurityException failure){throw new IllegalStateException("vendor_read_unavailable");}
    }
    private void write(String value){
        if(context.checkSelfPermission(Manifest.permission.MODIFY_AUDIO_SETTINGS)!=PackageManager.PERMISSION_GRANTED)throw new IllegalStateException("audio_settings_permission");
        try{method(true).invoke(audio,value);}
        catch(ReflectiveOperationException|LinkageError|SecurityException failure){throw new IllegalStateException("vendor_write_unavailable");}
    }
    void start(ProbePolicy.Target target){write(Protocol.start(target));}
    void stop(){write(Protocol.STOP_COMMAND);}
    void micZero(){write(Protocol.MIC_ZERO);}
    void systemZero(){write(Protocol.SYSTEM_ZERO);}
    void micLow(){write(Protocol.MIC_LOW);}
    void micGain(int gain){write(Protocol.micGain(gain));}
    void protectionEq(FeedbackProtection protection){write(protection.eqCommand());}
    void enableFlag(){write(Protocol.FLAG_ENABLE);}
    void disableFlag(){write(Protocol.FLAG_DISABLE);}
    void systemLow(){write(Protocol.SYSTEM_LOW);}
}
