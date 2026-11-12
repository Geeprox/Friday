package local.friday.ktv;

public final class VendorMicDesiredStateTest {
    private static int checks;
    private static void yes(boolean value){checks++;if(!value)throw new AssertionError("check "+checks);}
    public static void main(String[] args){
        VendorMicClient.DesiredState d=new VendorMicClient.DesiredState();yes(!d.mayStart()&&d.gain==20);
        d.enabled(true,20);yes(!d.mayStart());d.foreground(true);yes(!d.mayStart());d.enabled(true,20);yes(d.mayStart());
        d.enabled(false,20);yes(!d.mayStart()&&d.gain==20);d.enabled(false,70);yes(!d.mayStart()&&d.gain==70);d.enabled(true,70);yes(d.mayStart());
        d.foreground(false);yes(!d.mayStart()&&!d.desired);d.foreground(true);yes(!d.mayStart());d.enabled(true,40);yes(d.mayStart());d.stop();yes(!d.mayStart());
        d.enabled(true,0);yes(!d.mayStart()&&d.gain==0);d.enabled(true,60);yes(d.mayStart()&&d.gain==60);
        for(int gain=1;gain<=100;gain++){
            yes(VendorMicClient.normalizeGain(gain)==gain);
            yes(VendorMicClient.bootstrapGain(gain)>0&&VendorMicClient.bootstrapGain(gain)<=gain&&VendorMicClient.bootstrapGain(gain)<=5);
        }
        d.closing=true;d.enabled(true,100);yes(!d.mayStart());yes(VendorMicClient.normalizeGain(50)==50);yes(VendorMicClient.normalizeGain(-1)==0);yes(VendorMicClient.normalizeGain(1000)==100);
        System.out.println("VendorMicDesiredStateTest: "+checks+" checks passed; background/close cannot resurrect desired audio; muted gain edits stay muted");
    }
}
