package local.friday.ktv;
import java.io.*;
import java.nio.*;
import java.nio.file.*;
public final class BackingSwitchTest {
 static void yes(boolean value,String note){if(!value)throw new AssertionError(note);}
 static File wave(File root,String name,int offset)throws Exception{
  byte[] bytes=new byte[44+48000*8*4];ByteBuffer b=ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
  b.put("RIFF".getBytes("US-ASCII")).putInt(bytes.length-8).put("WAVEfmt ".getBytes("US-ASCII"));
  b.putInt(16).putShort((short)1).putShort((short)2).putInt(48000).putInt(192000).putShort((short)4).putShort((short)16).put("data".getBytes("US-ASCII")).putInt(bytes.length-44);
  for(int frame=0;frame<48000*8;frame++){short value=(short)(frame%3000-1500+offset);b.putShort(value).putShort(value);}
  File file=new File(root,name);Files.write(file.toPath(),bytes);return file;
 }
 public static void main(String[] args)throws Exception{
  File root=new File(args[0]);root.mkdirs();File dry=wave(root,"dry.wav",0),voice=wave(root,"voice.wav",3000);
  short[] out=new short[480];long expected=48000;int offset=0;
  try(SwitchingWavSource source=new SwitchingWavSource(dry)){
   source.seek(expected);
   for(int block=0;block<900;block++){
    boolean change=block%7==0;int next=change?3000-offset:offset;
    if(change)source.request(next==0?dry:voice);
    yes(source.read(out,240)==240,"full block");
    for(int frame=0;frame<240;frame++){
     int target=(int)((expected+frame)%3000)-1500+(change?Math.round(offset*(1-(frame+1f)/240)+next*((frame+1f)/240)):offset);
     yes(Math.abs(out[frame*2]-target)<=1,"sample-aligned transition without restart or rewind");
    }
    expected+=240;offset=next;yes(source.position==expected,"continuous clock");
   }
   source.request(new File(root,"missing.wav"));source.read(out,240);expected+=240;
   yes(source.position==expected&&source.failures==1,"failed preparation leaves current stream running");
   source.request(dry);source.request(voice);source.read(out,240);yes(source.position==expected+240,"latest switch wins");
  }
  System.out.println("BackingSwitchTest passed: 900 continuous blocks, repeated switches, missing replacement, latest intent");
 }
}
