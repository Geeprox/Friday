package local.friday.ktv;

import android.app.AlertDialog;
import android.content.Context;
import android.graphics.Bitmap;
import android.view.View;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.TextView;
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
    private static final class RoomSnapshot extends JSONObject {
        String invitation;
        @Override public String optString(String key){return invitation;}
    }
    private static final class QrContainer extends FrameLayout {
        int visibility;
        QrContainer(Context context){super(context);}
        @Override public void setVisibility(int value){visibility=value;}
    }
    private static final class QrImage extends ImageView {
        int visibility;
        QrImage(Context context){super(context);}
        @Override public void setVisibility(int value){visibility=value;}
    }
    private static final class Cue extends TextView {
        int visibility;
        Cue(Context context){super(context);}
        @Override public void setVisibility(int value){visibility=value;}
    }
    private static final class QrPreview extends AlertDialog {
        boolean dismissed;
        QrPreview(Context context){super(context);}
        @Override public void dismiss(){dismissed=true;}
    }
    private static void checkSameRoomInvitationRefresh()throws Exception {
        MainActivity activity=(MainActivity)empty(MainActivity.class);
        JSONObject singing=(JSONObject)empty(JSONObject.class);
        KtvClient client=(KtvClient)empty(KtvClient.class);
        QrContainer small=(QrContainer)empty(QrContainer.class);
        QrImage big=(QrImage)empty(QrImage.class);
        Cue cue=(Cue)empty(Cue.class);
        QrPreview preview=(QrPreview)empty(QrPreview.class);
        RoomSnapshot old=(RoomSnapshot)empty(RoomSnapshot.class), fresh=(RoomSnapshot)empty(RoomSnapshot.class);
        old.invitation="https://old.example.test:9443/ktv/";
        fresh.invitation="https://new.example.test/ktv/";
        field("roomId").set(activity,"same-room");
        field("roomInvitation").set(activity,old.invitation);
        field("current").set(activity,singing);
        field("smallQr").set(activity,small);
        field("qrCue").set(activity,cue);
        field("bigQr").set(activity,big);
        field("qrPreview").set(activity,preview);
        field("qrBitmap").set(activity,empty(Bitmap.class));
        field("nextQrPoll").setLong(activity,30000);
        field("client").set(activity,client);
        field("foreground").setBoolean(activity,true);
        field("autoRoom").setBoolean(activity,true);
        field("protocolReady").setBoolean(activity,true);
        field("connectionEpoch").setLong(activity,1L);

        Method accept=MainActivity.class.getDeclaredMethod("acceptRoomSnapshot",JSONObject.class,boolean.class,long.class);
        Method receive=MainActivity.class.getDeclaredMethod("acceptsRoomQr",KtvClient.class,String.class,long.class,String.class);
        accept.setAccessible(true);receive.setAccessible(true);
        yes((Boolean)accept.invoke(activity,fresh,true,2L),"same-room snapshot accepts the new invitation");
        yes(field("current").get(activity)==singing&&field("roomId").get(activity).equals("same-room"),
            "invitation refresh retains the room and active performance");
        yes(field("qrBitmap").get(activity)==null&&preview.dismissed&&small.visibility==View.INVISIBLE
            &&cue.visibility==View.INVISIBLE&&big.visibility==View.GONE&&field("nextQrPoll").getLong(activity)==0,
            "obsolete QR and enlarged preview disappear, and retrieval can restart immediately");
        yes(!(Boolean)accept.invoke(activity,old,true,1L)&&field("roomInvitation").get(activity).equals(fresh.invitation),
            "delayed snapshot cannot restore the old address");
        yes(!(Boolean)receive.invoke(activity,client,"same-room",1L,old.invitation)
            &&(Boolean)receive.invoke(activity,client,"same-room",1L,fresh.invitation),
            "only the newest invitation on the same live room can receive an image");
        field("protocolReady").setBoolean(activity,false);
        yes(!(Boolean)receive.invoke(activity,client,"same-room",1L,fresh.invitation),
            "a late image cannot publish during connection recovery");
    }
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
        checkSameRoomInvitationRefresh();
        System.out.println("QueueRefreshContinuityTest passed: current performance, loading and same-room QR ordering; no device actions");
    }
}
