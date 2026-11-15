package local.friday.ktv;

import java.util.Map;

/** 合成歌曲与局域网地址，不连接任何电视或媒体服务。 */
public final class SongBackdropTest {
    private static void require(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args)throws Exception{
        String a=new String(new char[64]).replace('\0','a'),b=new String(new char[64]).replace('\0','b');
        SongBackdrop owner=new SongBackdrop();require(owner.select("one"));long first=owner.begin("one",a,true);require(first>0);
        require(owner.begin("one",a,true)==0&&owner.begin("one",a,false)==0);
        require(owner.select("two"));require(!owner.accept("one",a,first));
        long second=owner.begin("two",b,false);require(owner.accept("two",b,second));require(owner.begin("two",b,false)==0);
        long upgraded=owner.begin("two",b,true);require(upgraded>0);require(owner.accept("two",b,upgraded));
        require(owner.begin("two",b,true)==0);
        owner.select("three");for(int i=0;i<3;i++){long attempt=owner.begin("three",a,true);require(attempt>0);owner.fail(attempt);}
        require(owner.begin("three",a,true)==0);require(owner.begin("three",b,true)>0);
        require(BackdropImagePolicy.validSource(3840,2160)&&BackdropImagePolicy.validSource(1536,864));
        require(!BackdropImagePolicy.validSource(4096,2300)&&!BackdropImagePolicy.validSource(3841,4097));
        require(!BackdropImagePolicy.validSource(0,2160)&&!BackdropImagePolicy.validSource(-1,2160));
        require(BackdropImagePolicy.benefitsFromUhd(3840,2160,3840,2160));
        require(BackdropImagePolicy.benefitsFromUhd(3840,2160,3780,2100));
        require(!BackdropImagePolicy.benefitsFromUhd(3840,2160,1920,1080));
        require(!BackdropImagePolicy.benefitsFromUhd(1920,1080,3840,2160));
        require(!BackdropImagePolicy.benefitsFromUhd(3840,2160,0,0));
        require(BackdropImagePolicy.compatibleSample(3840,2160)==2);
        require(BackdropImagePolicy.compatibleSample(1921,1080)==2);
        require(BackdropImagePolicy.compatibleSample(1536,864)==1);
        long bytes=3840L*2160*4, mib=1024*1024;
        long process=bytes+16*mib,system=2*bytes+32*mib;
        require(BackdropImagePolicy.canDecodeUhd(3840,2160,false,process,system));
        require(!BackdropImagePolicy.canDecodeUhd(3840,2160,true,process,system));
        require(!BackdropImagePolicy.canDecodeUhd(3840,2160,false,process-1,system));
        require(!BackdropImagePolicy.canDecodeUhd(3840,2160,false,process,system-1));
        String path="/v1/jobs/12345678-1234-1234-1234-123456789012/media/background",ticket="fixture-media-ticket-12345678901234567890";
        MediaRoute route=MediaRoute.worker("http://192.168.40.20:8769",path,"X-KTV-Media",ticket,1060,1000,"background");
        Map<String,String> headers=route.headers("never-forward-control-key","instance",true,true);
        require(headers.size()==1&&ticket.equals(headers.get("X-KTV-Media"))&&!headers.containsKey("X-KTV-TV-Key"));
        System.out.println("SongBackdropTest passed: verified 4K bounds, active display, decode budgets, async song ownership and scoped media route.");
    }
}
