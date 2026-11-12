package android.content.res;

import java.io.*;
import java.nio.*;

/** Fixed, generated PCM fixture. Never accesses app or household files. */
public final class AssetManager {
    public InputStream open(String name)throws IOException {
        if(!name.equals("sfx/applause.wav")&&!name.equals("sfx/cheer.wav"))throw new FileNotFoundException(name);
        byte[] wav=new byte[44+4800*4];ByteBuffer data=ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        data.put("RIFF".getBytes("US-ASCII")).putInt(wav.length-8).put("WAVEfmt ".getBytes("US-ASCII"));
        data.putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(192000);
        data.putShort((short)4).putShort((short)16).put("data".getBytes("US-ASCII")).putInt(wav.length-44);
        for(int i=0;i<4800;i++){short value=(short)(i%40*20-400);data.putShort(value).putShort(value);}
        return new ByteArrayInputStream(wav);
    }
}
