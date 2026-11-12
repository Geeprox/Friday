package local.friday.ktv;

import java.util.HashSet;
import java.util.Set;

/** Android D-pad key codes; consumes a press once, including duplicate down events. */
public final class RemoteShortcuts {
    public enum Action { NONE, ORIGINAL, MUTE, REPLAY, NEXT }
    private final Set<Integer> held=new HashSet<>();
    public static boolean direction(int code){return code>=19&&code<=22;}
    public Action press(int code,boolean down,int repeat,boolean enabled){
        if(!enabled){held.clear();return Action.NONE;}
        if(!down){held.remove(code);return Action.NONE;}
        if(!direction(code)||repeat!=0||!held.add(code))return Action.NONE;
        return code==19?Action.ORIGINAL:code==20?Action.MUTE:code==21?Action.REPLAY:Action.NEXT;
    }
}
