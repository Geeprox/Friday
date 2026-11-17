package local.friday.ktv;

import android.media.AudioFormat;
import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import java.io.*;
import java.nio.ByteBuffer;
import java.util.concurrent.atomic.AtomicBoolean;

/** 后台解码显式首尾裁切的 AAC 双轨，原子发布到现有 PCM 播放缓存。 */
public final class CompactAudioDecoder {
    private CompactAudioDecoder(){}

    private static void check(AtomicBoolean cancel)throws IOException{
        if(cancel.get()||Thread.currentThread().isInterrupted())throw new IOException("cancelled");
    }

    public static boolean cached(File wav,File receipt,long frames,long delay,long padding){
        if(!wav.isFile()||wav.length()!=44+frames*4||!receipt.isFile()||receipt.length()>128)return false;
        try(DataInputStream in=new DataInputStream(new FileInputStream(receipt))){
            return in.readLong()==frames&&in.readInt()==delay&&in.readInt()==padding
                &&in.readUTF().matches("[a-f0-9]{64}");
        }catch(IOException invalid){return false;}
    }

    private static File verified(File wav,File receipt,long frames,long delay,long padding,
                                 AtomicBoolean cancel,MediaCache cache)throws Exception{
        if(!cached(wav,receipt,frames,delay,padding))return null;
        try(DataInputStream in=new DataInputStream(new FileInputStream(receipt))){
            in.readLong();in.readInt();in.readInt();String sha=in.readUTF();
            return MediaTransfer.existing(wav,44+frames*4,sha,
                file->{try(WavSource source=new WavSource(file)){if(source.frames!=frames)throw new IOException("media_timeline_mismatch");}},
                cancel,cache);
        }
    }

    public static File existing(File root,String sha,long frames,long delay,long padding,
                                AtomicBoolean cancel,MediaCache cache)throws Exception{
        return verified(new File(root,sha+".wav"),new File(root,sha+".pcm"),frames,delay,padding,cancel,cache);
    }

    public static File decode(File encoded,String sha,long frames,long delay,long padding,
                              AtomicBoolean cancel,MediaCache cache)throws Exception{
        File root=encoded.getParentFile(),target=new File(root,sha+".wav"),receipt=new File(root,sha+".pcm");
        File found=verified(target,receipt,frames,delay,padding,cancel,cache);
        if(found!=null)return found;
        // 损坏或中断的旧解码副本先退役，替换时不重复计入整轨容量。
        if(target.exists()&&!target.delete())throw new IOException("cache_unavailable");
        if(receipt.exists()&&!receipt.delete())throw new IOException("cache_unavailable");
        try(AutoCloseable pinned=cache.pin(encoded);
            MediaCache.Reservation reservation=cache.reserveTransfer(target,44+frames*4+128)){
            File partial=cache.beginPartial(sha),metadata=null;
            try{
                write(encoded,partial,frames,delay,padding,cancel);
                check(cancel);
                String decodedSha=MediaTransfer.hash(partial);
                metadata=cache.beginPartial(sha);
                try(DataOutputStream out=new DataOutputStream(new FileOutputStream(metadata))){
                    out.writeLong(frames);out.writeInt((int)delay);out.writeInt((int)padding);out.writeUTF(decodedSha);
                }
                check(cancel);
                if(!partial.renameTo(target)||!metadata.renameTo(receipt))throw new IOException("cache_publish_failed");
                cache.used(target);cache.used(receipt);
                return target;
            }finally{
                if(partial.exists())partial.delete();
                MediaCache.endPartial(partial);
                if(metadata!=null){if(metadata.exists())metadata.delete();MediaCache.endPartial(metadata);}
            }
        }
    }

    private static void write(File encoded,File output,long frames,long delay,long padding,AtomicBoolean cancel)throws Exception{
        MediaExtractor extractor=new MediaExtractor();MediaCodec codec=null;
        try(RandomAccessFile wav=new RandomAccessFile(output,"rw")){
            extractor.setDataSource(encoded.getAbsolutePath());
            if(extractor.getTrackCount()!=1)throw new IOException("invalid_compact_audio");
            MediaFormat format=extractor.getTrackFormat(0);
            if(!"audio/mp4a-latm".equals(format.getString(MediaFormat.KEY_MIME))
                    ||format.getInteger(MediaFormat.KEY_SAMPLE_RATE)!=48000
                    ||format.getInteger(MediaFormat.KEY_CHANNEL_COUNT)!=2)throw new IOException("invalid_compact_audio");
            // 容器不使用 edit list，清单规定的裁切由本层统一执行。
            format.setInteger("encoder-delay",0);format.setInteger("encoder-padding",0);
            format.setInteger(MediaFormat.KEY_PCM_ENCODING,AudioFormat.ENCODING_PCM_16BIT);
            codec=MediaCodec.createDecoderByType("audio/mp4a-latm");
            codec.configure(format,null,null,0);codec.start();extractor.selectTrack(0);
            CompactPcmWriter pcm=new CompactPcmWriter(wav,frames,delay,padding);
            MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
            long packet=0;
            boolean inputEnd=false,outputEnd=false;long deadline=System.nanoTime()+180000000000L;
            while(!outputEnd){
                check(cancel);
                if(System.nanoTime()>deadline)throw new IOException("audio_decode_timeout");
                if(!inputEnd){
                    int index=codec.dequeueInputBuffer(10000);
                    if(index>=0){
                        ByteBuffer input=codec.getInputBuffer(index);
                        if(input==null)throw new IOException("audio_decode_failed");
                        input.clear();int count=extractor.readSampleData(input,0);
                        inputEnd=count<0;
                        codec.queueInputBuffer(index,0,inputEnd?0:count,packet*1024*1000000L/48000,
                            inputEnd?MediaCodec.BUFFER_FLAG_END_OF_STREAM:0);
                        if(!inputEnd){packet++;extractor.advance();}
                    }
                }
                int index=codec.dequeueOutputBuffer(info,10000);
                if(index==MediaCodec.INFO_OUTPUT_FORMAT_CHANGED){
                    MediaFormat actual=codec.getOutputFormat();
                    if(actual.getInteger(MediaFormat.KEY_SAMPLE_RATE)!=48000||actual.getInteger(MediaFormat.KEY_CHANNEL_COUNT)!=2
                            ||actual.containsKey(MediaFormat.KEY_PCM_ENCODING)&&actual.getInteger(MediaFormat.KEY_PCM_ENCODING)!=AudioFormat.ENCODING_PCM_16BIT)
                        throw new IOException("invalid_decoded_audio");
                }else if(index>=0){
                    try{
                        if(info.size>0&&(info.flags&MediaCodec.BUFFER_FLAG_CODEC_CONFIG)==0){
                            ByteBuffer bytes=codec.getOutputBuffer(index);
                            if(bytes==null)throw new IOException("audio_decode_failed");
                            pcm.append(bytes,info.offset,info.size);
                        }
                        outputEnd=(info.flags&MediaCodec.BUFFER_FLAG_END_OF_STREAM)!=0;
                    }finally{codec.releaseOutputBuffer(index,false);}
                }
            }
            pcm.finish(packet);
        }finally{
            if(codec!=null){try{codec.stop();}finally{codec.release();}}
            extractor.release();
        }
        try(WavSource verified=new WavSource(output)){if(verified.frames!=frames)throw new IOException("audio_timeline_mismatch");}
    }
}
