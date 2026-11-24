package local.friday.ktv;

import java.util.HashSet;
import java.util.Set;

/** 舞台方向键每次只执行一次，点歌码有焦点时保留导航语义。 */
public final class RemoteShortcuts {
    public enum Action { NONE, ORIGINAL, REPLAY, NEXT, FOCUS_QR, FOCUS_STAGE }
    private final Set<Integer> held=new HashSet<>();
    public static boolean direction(int code){return code==19||code==21||code==22;}
    public static boolean consumes(int code,boolean playing,boolean qrAvailable){
        return playing&&(direction(code)||qrAvailable&&code==20);
    }
    public Action press(int code,boolean down,int repeat,boolean enabled,boolean qrAvailable,boolean qrFocused){
        if(!enabled){held.clear();return Action.NONE;}
        if(!down){held.remove(code);return Action.NONE;}
        if(!consumes(code,enabled,qrAvailable)||repeat!=0||!held.add(code))return Action.NONE;
        if(code==20)return Action.FOCUS_QR;
        if(qrFocused)return code==19?Action.FOCUS_STAGE:Action.NONE;
        return code==19?Action.ORIGINAL:code==21?Action.REPLAY:Action.NEXT;
    }
}
