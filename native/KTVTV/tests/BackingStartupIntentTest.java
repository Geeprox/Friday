package local.friday.ktv;

import java.io.*;
import java.lang.reflect.*;
import java.nio.*;
import java.nio.file.*;

/** Exercises real switch/install methods before publication, without Context,
 * AudioTrack, microphone, focus, a render thread, or an AudioEngine constructor. */
public final class BackingStartupIntentTest {
    private static void yes(boolean value,String note){if(!value)throw new AssertionError(note);}
    private static AudioEngine emptyEngine()throws Exception {
        Class<?> type=Class.forName("sun.misc.Unsafe");Field access=type.getDeclaredField("theUnsafe");access.setAccessible(true);
        return (AudioEngine)type.getMethod("allocateInstance",Class.class).invoke(access.get(null),AudioEngine.class);
    }
    private static void initial(AudioEngine engine,File initial)throws Exception {
        // The known window after start has selected a file and before its
        // render worker publishes musicSource. No Android entry point runs.
        Field desired=AudioEngine.class.getDeclaredField("desiredBacking");desired.setAccessible(true);desired.set(engine,initial);
        Field source=AudioEngine.class.getDeclaredField("musicSource");source.setAccessible(true);source.set(engine,null);
    }
    private static void install(AudioEngine engine,SwitchingWavSource source,File initial)throws Exception {
        Method method=AudioEngine.class.getDeclaredMethod("installMusicSource",SwitchingWavSource.class,File.class);
        method.setAccessible(true);method.invoke(engine,source,initial);
    }
    private static File wave(Path root,String name,int value)throws Exception {
        byte[] pcm=new byte[44+48000*4];ByteBuffer data=ByteBuffer.wrap(pcm).order(ByteOrder.LITTLE_ENDIAN);
        data.put("RIFF".getBytes("US-ASCII")).putInt(pcm.length-8).put("WAVEfmt ".getBytes("US-ASCII"));
        data.putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(192000).putShort((short)4).putShort((short)16);
        data.put("data".getBytes("US-ASCII")).putInt(pcm.length-44);
        for(int i=0;i<48000;i++){data.putShort((short)value);data.putShort((short)value);}
        Path path=root.resolve(name);Files.write(path,pcm);return path.toFile();
    }
    public static void main(String[] args)throws Exception {
        Path root=Files.createTempDirectory("ktv-backing-startup-");File original=null,alternate=null;
        try {
            original=wave(root,"original.wav",1000);alternate=wave(root,"alternate.wav",5000);
            AudioEngine engine=emptyEngine();short[] output=new short[480];
            initial(engine,original);
            engine.switchBacking(alternate);
            try(SwitchingWavSource source=new SwitchingWavSource(original)){
                source.seek(480);install(engine,source,original);yes(source.read(output,240)==240,"full initial block");
                yes(source.switches==1&&source.position==720,"pre-publication intent retained without rewind");
                yes(output[0]>1000&&output[0]<5000&&output[478]==5000,"queued stem reaches the render stream");
            }
            initial(engine,original);engine.switchBacking(alternate);engine.switchBacking(original);
            try(SwitchingWavSource source=new SwitchingWavSource(original)){
                install(engine,source,original);source.read(output,240);
                yes(source.switches==0&&output[478]==1000,"latest pre-publication choice wins");
            }
            for(String name:new String[]{"track","record","render","capture"}){
                Field field=AudioEngine.class.getDeclaredField(name);field.setAccessible(true);yes(field.get(engine)==null,"no audio resource: "+name);
            }
            yes(!engine.running&&engine.capturedFrames==0,"no microphone or rendering started");
            System.out.println("BackingStartupIntentTest passed: startup switch retained and latest choice wins; no Android audio calls");
        } finally {
            if(original!=null)Files.deleteIfExists(original.toPath());if(alternate!=null)Files.deleteIfExists(alternate.toPath());Files.delete(root);
        }
    }
}
