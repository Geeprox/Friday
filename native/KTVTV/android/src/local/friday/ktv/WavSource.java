package local.friday.ktv;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;

/** Only the authenticated Mac service's exact 48 kHz stereo PCM16 WAV contract is accepted. */
public final class WavSource implements AutoCloseable {
    private final RandomAccessFile file;
    public final long frames;
    public long position;
    private final byte[] bytes = new byte[960];
    private static int u16(byte[] b, int p) { return (b[p] & 255) | ((b[p+1] & 255) << 8); }
    private static long u32(byte[] b, int p) { return (u16(b,p) | ((long)u16(b,p+2) << 16)) & 0xffffffffL; }
    private static boolean tag(byte[] b, int p, String s) {
        for (int i=0;i<s.length();i++) if (b[p+i] != (byte)s.charAt(i)) return false;
        return true;
    }
    public WavSource(File source) throws IOException {
        file = new RandomAccessFile(source,"r");
        try {
            byte[] h = new byte[44]; file.readFully(h);
            long length = file.length();
            if (!tag(h,0,"RIFF") || !tag(h,8,"WAVE") || !tag(h,12,"fmt ") || !tag(h,36,"data")
                    || u32(h,4) != length-8 || u32(h,16) != 16 || u16(h,20) != 1 || u16(h,22) != 2
                    || u32(h,24) != 48000 || u32(h,28) != 192000 || u16(h,32) != 4 || u16(h,34) != 16
                    || u32(h,40) != length-44 || (length-44)%4 != 0 || length <= 44 || length > 345600044L)
                throw new IOException("invalid_audio_contract");
            frames = (length-44)/4;
        } catch (IOException error) { file.close(); throw error; }
    }
    public int read(short[] output, int requested) throws IOException {
        if (requested > 240 || requested < 0 || output.length < requested*2) throw new IOException("invalid_block");
        int count=(int)Math.min(requested, frames-position);
        file.readFully(bytes,0,count*4);
        for (int i=0;i<count*2;i++) output[i]=(short)u16(bytes,i*2);
        java.util.Arrays.fill(output,count*2,requested*2,(short)0); position+=count;
        return count;
    }
    public void seek(long frame) throws IOException { position=Math.max(0,Math.min(frame,frames)); file.seek(44+position*4); }
    public void close() throws IOException { file.close(); }
}
