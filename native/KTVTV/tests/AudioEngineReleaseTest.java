package local.friday.ktv;

import java.io.IOException;
import java.lang.reflect.*;
import java.util.concurrent.*;

/** Real release/start/wait guards with only inert threads and fake ownership.
 * The AudioEngine constructor and every Android/JNI/audio entry remain unused. */
public final class AudioEngineReleaseTest {
    static int checks;
    static void yes(boolean value,String note){checks++;if(!value)throw new AssertionError(note);}
    static AudioEngine empty()throws Exception{
        Class<?> unsafe=Class.forName("sun.misc.Unsafe");Field f=unsafe.getDeclaredField("theUnsafe");f.setAccessible(true);
        return (AudioEngine)unsafe.getMethod("allocateInstance",Class.class).invoke(f.get(null),AudioEngine.class);
    }
    static void field(AudioEngine engine,String name,Object value)throws Exception{Field f=AudioEngine.class.getDeclaredField(name);f.setAccessible(true);f.set(engine,value);}
    static Thread paused(CountDownLatch entered,CountDownLatch exit){Thread t=new Thread(()->{entered.countDown();try{exit.await();}catch(InterruptedException e){throw new AssertionError(e);}},"fake-owner");t.start();return t;}
    static void await(CountDownLatch latch)throws Exception{if(!latch.await(2,TimeUnit.SECONDS))throw new AssertionError("fake owner timeout");}
    static void rejectsWait(AudioEngine engine,String code)throws Exception{
        try{engine.awaitStopped();throw new AssertionError("unreleased owner accepted");}catch(IOException expected){yes(code.equals(expected.getMessage()),"specific unresolved owner: "+code);}
    }
    static final class FakeNativeOwner implements AudioBackendIO {
        public boolean start(){throw new AssertionError("no audio");}public int read(short[] d,int f){throw new AssertionError("no audio");}
        public int write(short[] d,int o,int s){throw new AssertionError("no audio");}public void metrics(long[] d){throw new AssertionError("no audio");}
        public int routeDevice(){throw new AssertionError("no audio");}public void stop(){throw new AssertionError("no audio");}public void close(){throw new AssertionError("no audio");}
    }
    public static void main(String[] args)throws Exception{
        AudioEngine engine=empty();yes(engine.released(),"never-owned engine is released");engine.awaitStopped();
        CountDownLatch entered=new CountDownLatch(1),exit=new CountDownLatch(1);Thread capture=paused(entered,exit);await(entered);field(engine,"capture",capture);
        try{
            yes(!engine.running&&!engine.released(),"playback stopped cannot hide a live capture thread");rejectsWait(engine,"input_stop_timeout");
            try{engine.start(null,null,null,0);throw new AssertionError("new backend overlapped old capture");}
            catch(IOException expected){yes("audio_busy".equals(expected.getMessage()),"new start fails before Android manager/audio access");}
        }finally{exit.countDown();capture.join(2000);}
        yes(!capture.isAlive()&&engine.released(),"actual capture exit releases thread ownership");engine.awaitStopped();
        FakeNativeOwner nativeOwner=new FakeNativeOwner();field(engine,"nativeIO",nativeOwner);
        yes(!engine.released(),"retained native owner blocks release");rejectsWait(engine,"audio_stopping");field(engine,"nativeIO",null);
        engine.running=true;yes(!engine.released(),"pending start blocks release");engine.running=false;
        CountDownLatch outputEntered=new CountDownLatch(1),outputExit=new CountDownLatch(1);Thread render=paused(outputEntered,outputExit);await(outputEntered);field(engine,"render",render);
        CountDownLatch waiting=new CountDownLatch(1),waited=new CountDownLatch(1);Throwable[] failure={null};
        Thread waiter=new Thread(()->{waiting.countDown();try{engine.awaitStopped();}catch(Throwable e){failure[0]=e;}finally{waited.countDown();}});waiter.start();await(waiting);
        yes(!engine.released()&&waited.getCount()==1,"render owner has not exited");outputExit.countDown();await(waited);waiter.join(2000);
        yes(failure[0]==null&&engine.released(),"awaitStopped observes actual final output exit");
        System.out.println("AudioEngineReleaseTest: "+checks+" checks passed; blocked capture prevents new backend without Android/JNI/audio calls");
    }
}
