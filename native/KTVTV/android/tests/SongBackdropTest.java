import local.friday.ktv.SongBackdrop;
import local.friday.ktv.MediaRoute;
import java.util.Map;

/** 合成歌曲与局域网地址，不连接任何电视或媒体服务。 */
public final class SongBackdropTest {
    private static void require(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args)throws Exception{
        String a=new String(new char[64]).replace('\0','a'),b=new String(new char[64]).replace('\0','b');
        SongBackdrop owner=new SongBackdrop();require(owner.select("one"));long first=owner.begin("one",a);require(first>0);
        require(owner.begin("one",a)==0);require(owner.select("two"));require(!owner.accept("one",a,first));
        long second=owner.begin("two",b);require(owner.accept("two",b,second));require(owner.begin("two",b)==0);
        owner.select("three");for(int i=0;i<3;i++){long attempt=owner.begin("three",a);require(attempt>0);owner.fail(attempt);}require(owner.begin("three",a)==0);
        String path="/v1/jobs/12345678-1234-1234-1234-123456789012/media/background",ticket="fixture-media-ticket-12345678901234567890";
        MediaRoute route=MediaRoute.worker("http://192.168.40.20:8769",path,"X-KTV-Media",ticket,1060,1000,"background");
        Map<String,String> headers=route.headers("never-forward-control-key","instance",true,true);
        require(headers.size()==1&&ticket.equals(headers.get("X-KTV-Media"))&&!headers.containsKey("X-KTV-TV-Key"));
        System.out.println("Song background generation and scoped media route passed.");
    }
}
