package local.friday.ktv;

import java.io.*;
import java.nio.ByteBuffer;

/** 复用实际电视裁切器验收外部真实解码结果，未连接音频设备。 */
public final class CompactPcmTest {
    public static void main(String[] args)throws Exception{
        long frames=Long.parseLong(args[2]),delay=1024,padding=(-(frames+delay)&1023);
        try(InputStream in=new FileInputStream(args[0]);RandomAccessFile out=new RandomAccessFile(args[1],"rw")){
            CompactPcmWriter writer=new CompactPcmWriter(out,frames,delay,padding);
            byte[] data=new byte[257*4];int count;
            while((count=in.read(data))!=-1)writer.append(ByteBuffer.wrap(data),0,count);
            writer.finish((frames+delay+padding)/1024);
        }
        try(WavSource decoded=new WavSource(new File(args[1]))){
            if(decoded.frames!=frames)throw new AssertionError("timeline_frames");
            decoded.seek(Math.max(0,frames-240));if(decoded.read(new short[480],240)!=Math.min(frames,240))throw new AssertionError("last_samples");
        }
        System.out.println("AAC trimming contract accepted "+frames+" aligned frames");
    }
}
