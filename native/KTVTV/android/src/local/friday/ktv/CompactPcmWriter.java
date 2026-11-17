package local.friday.ktv;

import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/** 显式裁切共享采样时间线，流式输出精确帧 PCM，独立于 Android 解码器。 */
public final class CompactPcmWriter {
    private final RandomAccessFile output;
    private final long frames,delay,padding;
    private final byte[] block=new byte[65536];
    private long decoded,written;

    public CompactPcmWriter(RandomAccessFile output,long frames,long delay,long padding)throws IOException{
        if(!MediaTimeline.compact(frames,Math.round(frames/48.0),frames,delay,padding,256000,frames/48000.0))
            throw new IOException("invalid_compact_timeline");
        this.output=output;this.frames=frames;this.delay=delay;this.padding=padding;
        ByteBuffer data=ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        data.put(new byte[]{'R','I','F','F'}).putInt((int)(36+frames*4)).put(new byte[]{'W','A','V','E','f','m','t',' '})
            .putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(192000)
            .putShort((short)4).putShort((short)16).put(new byte[]{'d','a','t','a'}).putInt((int)(frames*4));
        output.setLength(0);output.write(data.array());
    }

    public void append(ByteBuffer bytes,int offset,int length)throws IOException{
        if(length<0||length%4!=0||offset<0||offset>bytes.capacity()-length)throw new IOException("invalid_decoded_audio");
        long end=decoded+length/4;
        if(end>delay+frames+padding)throw new IOException("audio_timeline_mismatch");
        long begin=Math.max(delay,decoded),last=Math.min(delay+frames,end);
        if(last>begin){
            ByteBuffer samples=bytes.duplicate();
            samples.limit(offset+(int)((last-decoded)*4));samples.position(offset+(int)((begin-decoded)*4));
            while(samples.hasRemaining()){
                int take=Math.min(samples.remaining(),block.length);
                samples.get(block,0,take);output.write(block,0,take);written+=take/4;
            }
        }
        decoded=end;
    }

    public void finish(long packets)throws IOException{
        if(written!=frames||decoded<delay+frames||packets*1024!=delay+frames+padding)
            throw new IOException("audio_timeline_mismatch");
        output.getFD().sync();
    }
}
