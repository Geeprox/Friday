package local.friday.ktv.audio.protocol;

/** Closed protocol surface. No IPC argument can supply an AudioManager parameter. */
public final class Protocol {
    public static final String GUARD="local.friday.ktv.audio", CONTROLLER="local.friday.ktv";
    public static final String PERMISSION=GUARD+".CONTROL";
    public static final int READ=1, SELF_TEST=2, START=3, LOW=4, STOP=5, REPLY=6, MUTE=7, PROTECTION=8, PROTECTION_RESET=9;
    public static final String STOP_COMMAND="61697=null";
    public static final String MIC_ZERO="61698=0", SYSTEM_ZERO="61713=0";
    // Local setpoints only; there is no gain or OEM flag readback.
    public static final String MIC_LOW="61698=5", SYSTEM_LOW="61713=100";
    public static final String FLAG_ENABLE="GlobalMicLowLatency=enable", FLAG_DISABLE="GlobalMicLowLatency=disable";
    public static boolean gainAllowed(int gain){return gain>=0&&gain<=100;}
    public static String micGain(int gain){if(!gainAllowed(gain))throw new IllegalArgumentException("gain");return "61698="+gain;}
    public static final String[] READ_KEYS={"61961","61958","61959","61957"};
    private Protocol(){}
    public static String start(ProbePolicy.Target target){
        if(target==null||!target.valid())throw new IllegalArgumentException("target");
        return "61696="+target.card+","+target.device+",2,0";
    }
    public static String value(String key,String reply){
        if(reply==null||!reply.startsWith(key+"="))return "";
        return reply.substring(key.length()+1).trim();
    }
    public static int status(String reply){
        String v=value("61959",reply);
        return "-1".equals(v)?-1:"0".equals(v)?0:"2".equals(v)?2:Integer.MIN_VALUE;
    }
}
