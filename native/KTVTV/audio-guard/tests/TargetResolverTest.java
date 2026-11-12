import local.friday.ktv.audio.protocol.ProbePolicy;
import local.friday.ktv.audio.protocol.Protocol;
import local.friday.ktv.audio.protocol.TargetResolver;
import local.friday.ktv.audio.protocol.TargetResolver.*;

/** Descriptor snapshots only: no Android, USB connection, PCM or device action. */
public final class TargetResolverTest {
    private static int checks;
    private static void check(boolean value,String reason){checks++;if(!value)throw new AssertionError(reason);}
    private static Input input(int id,String address){return new Input(id,TargetResolver.USB_DEVICE,true,address,new int[]{2},new int[]{48000},new int[]{TargetResolver.PCM16});}
    private static Usb capture(String identity,int vendor,int product){return new Usb(identity,vendor,product,0,new Interface[]{new Interface(1,1,new Endpoint[0]),new Interface(1,2,new Endpoint[]{new Endpoint(128,1)})});}
    private static Usb capture(String identity){return capture(identity,0x1234,0xabcd);}
    private static Result resolve(Input[] inputs,Usb[] usb,int selected){return TargetResolver.resolve(inputs,usb,selected);}
    private static void rejected(Input[] inputs,Usb[] usb,int selected,int code){Result result=resolve(inputs,usb,selected);check(!result.ready()&&result.target==null&&result.code==code,"fixed refusal "+TargetResolver.reason(code)+" got "+result.reason());}
    public static void main(String[] args){
        Input a=input(12,"card=4;device=0");Usb u=capture("usb-a");Result initial=resolve(new Input[]{a},new Usb[]{u},0);
        check(initial.ready()&&initial.target.inputId==12&&initial.target.card==4&&initial.target.device==0,"canonical framework address, no guessed card");
        check(initial.target.vendor==0x1234&&initial.target.product==0xabcd,"arbitrary valid descriptor IDs are observation, not eligibility whitelist");
        check(Protocol.start(initial.target).equals("61696=4,0,2,0"),"only actual card/device enter closed START");
        check(!initial.target.explicitSelection&&!initial.target.descriptorBound&&initial.inputCandidates==1&&initial.usbCandidates==1,"unique association is explicit in result");
        check(ProbePolicy.cardMatches(initial.target,"0 [built-in]: platform audio\n4 [generic]: USB-Audio - Synthetic receiver\n"),"framework route corroborated by actual USB-Audio card marker");
        check(!ProbePolicy.cardMatches(initial.target,"0 [built-in]: platform audio\n5 [generic]: USB-Audio - Synthetic receiver\n"),"name cannot replace missing selected card");
        check(!ProbePolicy.cardMatches(initial.target,"4 [brand-name]: PCI - Synthetic USB input\n"),"brand text does not qualify a non-USB card driver");
        check(!ProbePolicy.cardMatches(initial.target,"4 [generic]: USB-Audio - Receiver\n4 [duplicate]: USB-Audio - Receiver\n"),"duplicate card fails closed");
        check(!ProbePolicy.cardMatches(initial.target,"4 [generic]: USB-Audio - Receiver\0\n"),"unbounded/native-invalid text refused");
        check(ProbePolicy.cardMatches(initial.target,"0 [built-in]: platform\r\n4 [generic]: USB-Audio - 接收器\r\n9 [speaker]: USB-Audio - Output\r\n"),"model-independent marker supports CRLF and actual Unicode labels");
        for(int[] pair:new int[][]{{1,1},{65535,65535},{0x9999,0x8888}})check(resolve(new Input[]{a},new Usb[]{capture("usb-a",pair[0],pair[1])},0).ready(),"valid USB descriptor range");
        for(String address:new String[]{"","4,0","card=04;device=0","card=4;device=00","card=4;device=0;61699=9","card=4;device=0\n","card=32;device=0","card=4;device=32","card=-1;device=0"})rejected(new Input[]{input(12,address)},new Usb[]{u},0,TargetResolver.INVALID_ADDRESS);
        rejected(new Input[0],new Usb[]{u},0,TargetResolver.NO_INPUT);
        rejected(null,new Usb[]{u},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a},null,0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a,a},new Usb[]{u},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a},new Usb[]{u},99,TargetResolver.SELECTED_INPUT_MISSING);
        rejected(new Input[]{a},new Usb[]{u},-1,TargetResolver.INVALID_INVENTORY);
        Input b=input(13,"card=4;device=1");
        rejected(new Input[]{a,b},new Usb[]{u},0,TargetResolver.AMBIGUOUS_INPUT);
        Result chosen=resolve(new Input[]{a,b},new Usb[]{u},13);check(chosen.ready()&&chosen.target.inputId==13&&chosen.target.device==1&&chosen.target.explicitSelection&&chosen.inputCandidates==2,"explicit framework input selection resolves multiple logical inputs");
        check(Protocol.start(chosen.target).equals("61696=4,1,2,0"),"selected device number preserved");
        rejected(new Input[]{a},new Usb[]{u,capture("usb-b")},0,TargetResolver.AMBIGUOUS_USB);
        rejected(new Input[]{a},new Usb[]{u,capture("usb-b")},12,TargetResolver.AMBIGUOUS_USB);
        Input bound=new Input(12,TargetResolver.USB_DEVICE,true,"card=4;device=0",new int[]{2},new int[]{48000},new int[]{2},"usb-b");
        Result proven=resolve(new Input[]{bound},new Usb[]{u,capture("usb-b",0x4444,0x5555)},12);
        check(proven.ready()&&proven.target.descriptorBound&&proven.target.vendor==0x4444&&proven.usbCandidates==2,"an authoritative registry binding can resolve multiple physical devices");
        Input missingBinding=new Input(12,TargetResolver.USB_DEVICE,true,"card=4;device=0",new int[]{2},new int[]{48000},new int[]{2},"unknown-device");
        rejected(new Input[]{missingBinding},new Usb[]{u},12,TargetResolver.BINDING_UNKNOWN);
        rejected(new Input[]{a},new Usb[]{u,capture("usb-a")},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a},new Usb[]{capture("usb-a",0,1)},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a},new Usb[]{capture("usb-a",1,0)},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a},new Usb[]{capture("",1,1)},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a},new Usb[]{capture("usb\nunsafe",1,1)},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{a},new Usb[0],0,TargetResolver.NO_UAC_CAPTURE);
        for(Interface face:new Interface[]{new Interface(1,1,new Endpoint[]{new Endpoint(128,1)}),new Interface(1,2,new Endpoint[]{new Endpoint(0,1)}),new Interface(1,2,new Endpoint[]{new Endpoint(128,2)}),new Interface(1,2,new Endpoint[]{new Endpoint(128,3)})})rejected(new Input[]{a},new Usb[]{new Usb("usb-a",1,1,0,new Interface[]{face})},0,TargetResolver.NO_UAC_CAPTURE);
        Usb hid=new Usb("hid",1,2,0,new Interface[]{new Interface(3,1,new Endpoint[]{new Endpoint(128,3)})});
        Usb speaker=new Usb("speaker",3,4,0,new Interface[]{new Interface(1,2,new Endpoint[]{new Endpoint(0,1)})});
        check(resolve(new Input[]{new Input(30,15,true,"",new int[]{1},new int[]{48000},new int[]{2}),a,new Input(31,11,false,"card=8;device=0",new int[]{2},new int[]{48000},new int[]{2})},new Usb[]{hid,u,speaker},0).ready(),"built-in mic, USB output and unrelated HID cannot become the capture target");
        check(resolve(new Input[]{new Input(12,TargetResolver.USB_HEADSET,true,"card=4;device=0",new int[]{1,2},new int[]{44100,48000,96000},new int[]{2,4})},new Usb[]{u},0).ready(),"headset type and additional formats retain exact supported contract");
        rejected(new Input[]{new Input(12,11,true,"card=4;device=0",new int[]{1},new int[]{48000},new int[]{2})},new Usb[]{u},0,TargetResolver.UNSUPPORTED_CHANNELS);
        rejected(new Input[]{new Input(12,11,true,"card=4;device=0",new int[]{2},new int[]{44100},new int[]{2})},new Usb[]{u},0,TargetResolver.UNSUPPORTED_RATE);
        rejected(new Input[]{new Input(12,11,true,"card=4;device=0",new int[]{2},new int[]{48000},new int[]{4})},new Usb[]{u},0,TargetResolver.UNSUPPORTED_ENCODING);
        rejected(new Input[]{new Input(12,11,true,"card=4;device=0",new int[0],new int[]{48000},new int[]{2})},new Usb[]{u},0,TargetResolver.INVALID_INVENTORY);
        rejected(new Input[]{new Input(12,11,true,"card=4;device=0",new int[]{2},null,new int[]{2})},new Usb[]{u},0,TargetResolver.INVALID_INVENTORY);
        int[] channels={2},rates={48000},encodings={2};Input frozen=new Input(12,11,true,"card=4;device=0",channels,rates,encodings);channels[0]=1;rates[0]=0;encodings[0]=0;
        check(resolve(new Input[]{frozen},new Usb[]{u},0).ready(),"capability snapshot cannot mutate after selection");
        Result same=resolve(new Input[]{a},new Usb[]{u},0),replaced=resolve(new Input[]{a},new Usb[]{capture("usb-replacement")},0);
        check(initial.target.same(same.target),"same current registry identity remains comparable");
        check(!initial.target.same(replaced.target),"same IDs reused by a replacement USB identity cannot pass stale target identity");
        check(TargetResolver.reason(100).equals("invalid_device_inventory"),"unknown errors remain fixed and bounded");
        System.out.println("TargetResolverTest: "+checks+" checks passed; generic descriptor fixtures only, no hardware/USB/PCM or audio latency claim");
    }
}
