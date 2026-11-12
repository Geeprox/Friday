package local.friday.ktv.audio.protocol;

import java.util.HashSet;
import java.util.Set;

/** Pure capability resolver. Adapters supply current framework/USB descriptors;
 * no product name, VID/PID whitelist, PCM, device open or vendor command is used.
 * A nonempty boundUsbIdentity may be supplied only by an authoritative registry
 * association, never inferred from product names or an arbitrary IPC pairing. */
public final class TargetResolver {
    public static final int USB_DEVICE=11,USB_HEADSET=22,PCM16=2;
    public static final int OK=0,NO_INPUT=1,AMBIGUOUS_INPUT=2,SELECTED_INPUT_MISSING=3,
        INVALID_INVENTORY=4,INVALID_ADDRESS=5,UNSUPPORTED_CHANNELS=6,UNSUPPORTED_RATE=7,
        UNSUPPORTED_ENCODING=8,NO_UAC_CAPTURE=9,AMBIGUOUS_USB=10,BINDING_UNKNOWN=11;
    private static final String[] REASONS={"ready","no_usb_input","ambiguous_usb_input","selected_input_missing",
        "invalid_device_inventory","invalid_alsa_address","unsupported_channels","unsupported_sample_rate",
        "unsupported_pcm_encoding","no_uac_capture_interface","ambiguous_usb_descriptor","usb_binding_unknown"};
    private TargetResolver(){}
    public static String reason(int code){return code>=0&&code<REASONS.length?REASONS[code]:"invalid_device_inventory";}
    public static final class Input {
        public final int id,type;public final boolean source;public final String address,boundUsbIdentity;
        public final int[] channels,rates,encodings;
        public Input(int id,int type,boolean source,String address,int[] channels,int[] rates,int[] encodings){this(id,type,source,address,channels,rates,encodings,"");}
        public Input(int id,int type,boolean source,String address,int[] channels,int[] rates,int[] encodings,String boundUsbIdentity){
            this.id=id;this.type=type;this.source=source;this.address=address;this.channels=copy(channels);this.rates=copy(rates);this.encodings=copy(encodings);this.boundUsbIdentity=boundUsbIdentity;
        }
        public boolean usbInput(){return source&&(type==USB_DEVICE||type==USB_HEADSET);}
    }
    public static final class Endpoint {
        public final int direction,type;
        public Endpoint(int direction,int type){this.direction=direction;this.type=type;}
    }
    public static final class Interface {
        public final int deviceClass,subclass;public final Endpoint[] endpoints;
        public Interface(int deviceClass,int subclass,Endpoint[] endpoints){this.deviceClass=deviceClass;this.subclass=subclass;this.endpoints=endpoints==null||endpoints.length>32?null:endpoints.clone();}
    }
    public static final class Usb {
        public final String identity;public final int vendor,product,deviceClass;public final Interface[] interfaces;
        public Usb(String identity,int vendor,int product,int deviceClass,Interface[] interfaces){this.identity=identity;this.vendor=vendor;this.product=product;this.deviceClass=deviceClass;this.interfaces=interfaces==null||interfaces.length>64?null:interfaces.clone();}
        public boolean valid(){return text(identity)&&vendor>0&&vendor<=65535&&product>0&&product<=65535&&deviceClass>=0&&deviceClass<=255&&interfaces!=null&&interfaces.length<=64;}
        public boolean capture(){
            if(!valid())return false;
            for(Interface face:interfaces){
                if(face==null||face.endpoints==null||face.endpoints.length>32)continue;
                if(face.deviceClass!=1||face.subclass!=2)continue;
                for(Endpoint endpoint:face.endpoints)if(endpoint!=null&&endpoint.direction==128&&endpoint.type==1)return true;
            }return false;
        }
    }
    public static final class Result {
        public final ProbePolicy.Target target;public final int code,inputCandidates,usbCandidates;
        private Result(ProbePolicy.Target target,int code,int inputs,int usb){this.target=target;this.code=code;inputCandidates=inputs;usbCandidates=usb;}
        public boolean ready(){return code==OK&&target!=null&&target.valid();}
        public String reason(){return TargetResolver.reason(code);}
    }
    private static int[] copy(int[] values){return values==null||values.length>64?null:values.clone();}
    private static boolean text(String value){if(value==null||value.isEmpty()||value.length()>256)return false;for(int i=0;i<value.length();i++)if(Character.isISOControl(value.charAt(i)))return false;return true;}
    private static boolean contains(int[] values,int wanted){if(values==null||values.length>64)return false;for(int value:values)if(value==wanted)return true;return false;}
    private static boolean formatKnown(Input input){return input.channels!=null&&input.rates!=null&&input.encodings!=null&&input.channels.length>0&&input.rates.length>0&&input.encodings.length>0&&input.channels.length<=64&&input.rates.length<=64&&input.encodings.length<=64;}
    private static Result reject(int code,int inputs,int usb){return new Result(null,code,inputs,usb);}
    public static Result resolve(Input[] inputs,Usb[] usb,int selectedInputId){
        if(inputs==null||usb==null||inputs.length>64||usb.length>64||selectedInputId<0)return reject(INVALID_INVENTORY,0,0);
        int count=0;Input selected=null;Set<Integer> ids=new HashSet<>();
        for(Input input:inputs){
            if(input==null)return reject(INVALID_INVENTORY,count,0);
            if(!input.usbInput())continue;
            if(input.id<=0||!ids.add(input.id))return reject(INVALID_INVENTORY,count,0);
            count++;if(selectedInputId==0||input.id==selectedInputId)selected=input;
        }
        if(count==0)return reject(NO_INPUT,0,0);
        if(selectedInputId==0&&count!=1)return reject(AMBIGUOUS_INPUT,count,0);
        if(selected==null)return reject(SELECTED_INPUT_MISSING,count,0);
        int[] address=ProbePolicy.address(selected.address);
        if(address[0]<0||address[1]<0)return reject(INVALID_ADDRESS,count,0);
        if(!formatKnown(selected))return reject(INVALID_INVENTORY,count,0);
        // The current direct/strict-cache DSP contract is stereo PCM16 at 48k.
        // Mono/unknown hardware is recognized but cannot silently add a dry FIFO.
        if(!contains(selected.channels,2))return reject(UNSUPPORTED_CHANNELS,count,0);
        if(!contains(selected.rates,48000))return reject(UNSUPPORTED_RATE,count,0);
        if(!contains(selected.encodings,PCM16))return reject(UNSUPPORTED_ENCODING,count,0);
        Set<String> identities=new HashSet<>();int captures=0;Usb chosen=null;
        boolean bound=selected.boundUsbIdentity!=null&&!selected.boundUsbIdentity.isEmpty();
        if(bound&&!text(selected.boundUsbIdentity))return reject(BINDING_UNKNOWN,count,0);
        for(Usb descriptor:usb){
            if(descriptor==null||!descriptor.valid()||!identities.add(descriptor.identity))return reject(INVALID_INVENTORY,count,captures);
            if(!descriptor.capture())continue;
            captures++;
            if(!bound||selected.boundUsbIdentity.equals(descriptor.identity))chosen=descriptor;
        }
        if(captures==0)return reject(NO_UAC_CAPTURE,count,0);
        if(bound&&chosen==null)return reject(BINDING_UNKNOWN,count,captures);
        if(!bound&&captures!=1)return reject(AMBIGUOUS_USB,count,captures);
        ProbePolicy.Target target=new ProbePolicy.Target(selected.id,selected.address,count,captures,chosen.vendor,chosen.product,chosen.deviceClass,
            true,true,true,true,selectedInputId>0,bound,chosen.identity);
        if(!target.valid())return reject(INVALID_INVENTORY,count,captures);
        return new Result(target,OK,count,captures);
    }
}
