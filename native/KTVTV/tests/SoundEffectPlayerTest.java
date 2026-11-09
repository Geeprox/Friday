package local.friday.ktv;

import android.content.Context;
import android.media.AudioTrack;
import java.lang.reflect.Field;
import java.util.concurrent.*;
import java.util.function.BooleanSupplier;

/** Runs the production player against JVM fakes; it never starts real audio. */
public final class SoundEffectPlayerTest {
    private static void yes(boolean value,String note){if(!value)throw new AssertionError(note);}
    private static void await(BooleanSupplier condition)throws Exception {
        long end=System.nanoTime()+TimeUnit.SECONDS.toNanos(2);
        while(!condition.getAsBoolean()){if(System.nanoTime()>=end)throw new AssertionError("test worker did not progress");Thread.sleep(2);}
    }
    private static void fence(SoundEffectPlayer player)throws Exception {
        Field field=SoundEffectPlayer.class.getDeclaredField("worker");field.setAccessible(true);
        ((ExecutorService)field.get(player)).submit(()->{}).get(2,TimeUnit.SECONDS);
    }
    private static AudioTrack started(int count)throws Exception {
        await(()->AudioTrack.outputs.size()>=count&&AudioTrack.outputs.get(count-1).played);
        return AudioTrack.outputs.get(count-1);
    }
    public static void main(String[] args)throws Exception {
        AudioTrack.outputs.clear();SoundEffectPlayer player=new SoundEffectPlayer(new Context());
        try {
            fence(player);yes(player.ready,"fixed fixtures loaded");
            long now=System.currentTimeMillis()/1000;
            player.command(1,"sfx_applause",now,now+5);AudioTrack first=started(1);
            yes(player.sequence==1&&player.state.equals("pending"),"no playing report before head progress");
            first.head=1;await(()->player.state.equals("playing"));
            player.command(2,"sfx_stop",now-10,now-5);
            int observed=first.reads.get();await(()->first.reads.get()>=observed+2);
            yes(player.sequence==2&&player.action.equals("sfx_stop")&&player.state.equals("expired"),"old worker cannot publish playing for expired stop");
            yes(!first.released,"expired command is not executed");
            now=System.currentTimeMillis()/1000;player.command(3,"sfx_stop",now,now+5);fence(player);
            yes(first.released&&player.state.equals("stopped")&&player.sequence==3,"valid stop releases output");
            player.command(4,"sfx_cheer",now,now+5);AudioTrack second=started(2);
            second.head=1;await(()->player.state.equals("playing"));player.resetRoom();fence(player);
            yes(second.released&&player.sequence==0&&player.action.isEmpty()&&player.state.equals("idle"),"room reset cannot be overwritten by old worker");
            player.command(1,"sfx_applause",now-10,now-5);fence(player);
            yes(AudioTrack.outputs.size()==2&&player.state.equals("expired"),"expired play creates no output");
        } finally {player.close();}
        for(AudioTrack output:AudioTrack.outputs)yes(output.released,"all fake outputs released");
        System.out.println("SoundEffectPlayerTest passed: pending, expired-worker isolation, stop release and room reset; no hardware audio");
    }
}
