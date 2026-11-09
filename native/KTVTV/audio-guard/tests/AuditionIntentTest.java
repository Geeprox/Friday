import local.friday.ktv.audio.protocol.AuditionIntent;
public final class AuditionIntentTest {
    static int count;
    static void check(boolean value){if(!value)throw new AssertionError("case "+count);count++;}
    public static void main(String[] args){
        AuditionIntent a=new AuditionIntent();
        check(!a.consumeZero(120,1,110,true,true,true,true));
        a.arm(100,0);check(!a.consumeZero(105,0,0,false,false,true,true));
        check(a.consumeZero(120,1,110,true,true,true,true));
        check(!a.consumeZero(121,1,110,true,true,true,true));
        a.arm(200,1);check(!a.consumeZero(210,1,110,true,true,true,true));
        check(!a.consumeZero(220,2,205,true,true,true,true));
        a.arm(300,2);check(!a.consumeZero(320,3,310,true,true,false,true));
        check(!a.consumeZero(321,3,310,true,true,true,true));
        a.arm(400,3);check(!a.consumeZero(420,4,410,true,true,true,false));
        a.arm(500,4);check(a.consumeZero(3510,5,510,true,true,true,true));
        a.arm(600,5);a.cancel();check(!a.consumeZero(620,6,610,true,true,true,true));
        a.arm(700,6);check(!a.consumeZero(720,7,699,true,true,true,true));
        a.arm(800,7);check(!a.consumeZero(820,8,821,true,true,true,true));
        a.arm(900,8);check(!a.consumeZero(899,9,899,true,true,true,true));
        a.arm(1000,9);check(!a.consumeZero(1010,10,1005,true,false,true,true));
        check(a.consumeZero(1020,10,1005,true,true,true,true));
        System.out.println("AuditionIntentTest: "+count+" checks passed");
    }
}
