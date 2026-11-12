package local.friday.audio;
public final class WetPolicyTest {
    private static void check(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args){
        check(WetProfiles.id("dry")==0&&WetProfiles.id("power")==3&&WetProfiles.id("singer")==-1);
        check(WetProfiles.KEYS.length==7&&WetProfiles.LABELS.length==7&&WetProfiles.DEFAULT_WET.length==7);
        check(WetProfiles.id("studio")==5&&WetProfiles.id("echo")==6);
        check(WetProfiles.DEFAULT_WET[5]==.18f&&WetProfiles.DEFAULT_WET[6]==.16f);
        for(int i=0;i<WetProfiles.KEYS.length;i++){
            check(WetProfiles.id(WetProfiles.KEYS[i])==i&&WetProfiles.valid(i));
            check(WetProfiles.permille(WetProfiles.DEFAULT_WET[i])<=360&&WetProfiles.LABELS[i].length()>0);
        }
        check(!WetProfiles.valid(-1)&&!WetProfiles.valid(7));
        check(WetProfiles.permille(.32f)==320);
        try{WetProfiles.permille(Float.NaN);throw new AssertionError();}catch(IllegalArgumentException expected){}
        check(WetProfiles.canArm(0,false,false,200)); // monitor/dry still emits zero PCM
        check(!WetProfiles.canArm(0,true,false,200)&&!WetProfiles.canArm(0,false,true,200));
        check(!WetProfiles.canArm(0,false,false,0)&&!WetProfiles.canArm(7,false,false,200));
        check(WetProfiles.canArm(5,false,false,200)&&WetProfiles.canArm(6,false,false,200));
        WetPolicy p=new WetPolicy();check(!p.start(0,10,20,100));check(p.start(7,10,20,100));
        p.opened(99,200);check(!p.armed());check(!p.confirm(98,300,300));check(!p.confirm(99,199,300));check(!p.confirm(99,301,300));check(p.confirm(99,300,300));
        check(p.heartbeat(7,10,20,true,500));check(p.healthy(1900));check(!p.healthy(2001));
        check("dry_owner_heartbeat_stale".equals(p.reason()));
        for(int kind=0;kind<4;kind++){
            check(p.start(7,10,20,3000));
            check(!p.heartbeat(kind==0?8:7,kind==1?11:10,kind==2?21:20,kind!=3,3001));
            check(!p.active()&&"dry_lease_or_route_changed".equals(p.reason()));
        }
        check(p.start(8,10,20,4000));p.stop("dry_selected");check(!p.active());
        check(p.start(9,10,20,4100));p.stop("native_crash_disconnected");check(!p.active());
        // The policy has no dry-control interface. Wet loss cannot issue a dry STOP.
        System.out.println("WetPolicy: profile contract, lease/route/owner loss, freshness and wet-only closure passed");
    }
}
