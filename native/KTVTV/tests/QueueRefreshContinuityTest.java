package local.friday.ktv;

import java.lang.reflect.*;
import org.json.JSONObject;

/** Run the actual queue-refresh gate with deliberately absent Android resources.
 * Any attempt to stop/start audio or redraw the current song fails the test. */
public final class QueueRefreshContinuityTest {
    private static Object empty(Class<?> type)throws Exception {
        Class<?> unsafe=Class.forName("sun.misc.Unsafe");Field access=unsafe.getDeclaredField("theUnsafe");access.setAccessible(true);
        return unsafe.getMethod("allocateInstance",Class.class).invoke(access.get(null),type);
    }
    private static Field field(String name)throws Exception {Field value=MainActivity.class.getDeclaredField(name);value.setAccessible(true);return value;}
    private static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
    public static void main(String[] args)throws Exception {
        MainActivity activity=(MainActivity)empty(MainActivity.class);
        JSONObject singing=(JSONObject)empty(JSONObject.class);
        Method refresh=MainActivity.class.getDeclaredMethod("refreshQueuePlayback");refresh.setAccessible(true);
        field("current").set(activity,singing);field("currentId").set(activity,"current-performance");
        field("foreground").setBoolean(activity,true);
        for(boolean warmup:new boolean[]{true,false}){
            field("warmup").setBoolean(activity,warmup);
            for(int i=0;i<100;i++)refresh.invoke(activity);
            yes(field("current").get(activity)==singing,"queue refresh retains current song");
            yes(field("currentId").get(activity).equals("current-performance"),"current identity retained");
            yes(field("warmup").getBoolean(activity)==warmup,"warmup/current selection retained");
        }
        field("current").set(activity,null);field("loading").setBoolean(activity,true);
        refresh.invoke(activity); // A pending start cannot be replaced either.
        yes(field("loading").getBoolean(activity),"download in progress retained");
        QueuePlaybackIntent intent=new QueuePlaybackIntent();
        yes(!intent.shouldStart(true,false,true),"playing owns the queue until completion");
        yes(intent.shouldStart(true,false,false),"natural completion may select the prepared next song");
        intent.pause();yes(!intent.shouldStart(true,false,false),"pause does not advance");
        intent.awaitNext();yes(intent.shouldStart(true,false,false),"explicit resume/next retains control");
        System.out.println("QueueRefreshContinuityTest passed: warmup, ordered song and loading retain ownership; no device actions");
    }
}
