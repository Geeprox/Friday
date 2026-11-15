package local.friday.ktv;

public final class NativeVendorContractTest {
    private static void yes(boolean v,String label){if(!v)throw new AssertionError(label);}
    public static void main(String[] args){
        long[] valid={0,123,1041,31,48000,2,1,0x123456789abcdef0L};
        String route="1.1.3+friday.123456789abcdef0";
        yes(NativeVendorContract.fromReceipt(valid,route).ready(),"closed live metadata contract");
        yes(!NativeVendorContract.fromReceipt(null,route).ready(),"empty is not evidence");
        for(int index=0;index<valid.length;index++){
            long[] wrong=valid.clone();wrong[index]=index==0?-13:0;
            yes(!NativeVendorContract.fromReceipt(wrong,route).ready(),"every field is mandatory "+index);
        }
        for(int bit=0;bit<5;bit++){
            long[] wrong=valid.clone();wrong[3]=31^(1<<bit);
            yes(!NativeVendorContract.fromReceipt(wrong,route).ready(),"partial capabilities cannot open native dry");
        }
        long[] wrong=valid.clone();wrong[3]=63;yes(NativeVendorContract.fromReceipt(wrong,route).ready()&&NativeVendorContract.fromReceipt(wrong,route).inlineVoice(),"closed same-stream capability requires the same host cookie");
        wrong=valid.clone();wrong[3]=127;yes(!NativeVendorContract.fromReceipt(wrong,route).ready(),"unknown capability dialect rejected");
        wrong=valid.clone();wrong[2]=10000;yes(!NativeVendorContract.fromReceipt(wrong,route).ready(),"ordinary app cannot impersonate platform endpoint");
        wrong=valid.clone();wrong[4]=96000;yes(!NativeVendorContract.fromReceipt(wrong,route).ready(),"cache transport format must match");
        yes(!NativeVendorContract.fromReceipt(new long[]{0,123,1041,31,48000,2,1,1,1},route).ready(),"trailing fields rejected");
        for(String other:new String[]{"1.1.3","1.1.3+friday.123456789abcdef1","1.1.3+friday.123456789ABCDEF0","",null})
            yes(!NativeVendorContract.fromReceipt(valid,other).ready(),"same UID or matching file cannot replace control-instance proof");
        yes("framework_route_mismatch".equals(NativeVendorContract.fromReceipt(valid,"").reason()),"HELLO passed but control route did not");
        String[] reasons={"bridge_not_listening","bridge_access_denied","bridge_timed_out","bridge_contract_invalid"};
        int[] failures={-111,-13,-110,-71};
        for(int i=0;i<failures.length;i++){
            long[] received=new long[8];received[0]=failures[i];
            NativeVendorContract.Result stopped=NativeVendorContract.fromReceipt(received,route);
            yes(!stopped.ready()&&!stopped.inlineVoice()&&reasons[i].equals(stopped.reason()),"fixed diagnostic with microphone closed "+i);
        }
        valid[7]=-1;yes(NativeVendorContract.fromReceipt(valid,"1.1.3+friday.ffffffffffffffff").ready(),"cookie is unsigned64, not a signed timestamp");
        System.out.println("NativeVendorContractTest passed");
    }
}
