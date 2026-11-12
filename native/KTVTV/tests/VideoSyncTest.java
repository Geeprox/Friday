package local.friday.ktv;
public final class VideoSyncTest {
    private static void check(boolean value,String reason){if(!value)throw new AssertionError(reason);}
    public static void main(String[] args){
        VideoSync sync=new VideoSync();sync.update(2000,0,0,true);check(sync.seek,"initial alignment");
        for(int i=1;i<30;i++){sync.update(i*500,i*500-200,i*500,false);check(!sync.seek,"ordinary clock wobble must not seek");check(sync.speed>=1&&sync.speed<=1.02f,"bounded video speed");}
        sync.update(20000,19000,15000,false);check(!sync.seek,"one outlier must not seek");
        sync.update(20500,20480,15500,false);check(!sync.seek&&sync.speed==1,"in-sync resets outliers");
        for(int i=0;i<3;i++)sync.update(22000+i*500,21000+i*500,16000+i*500,false);
        check(sync.seek,"sustained large drift eventually realigns");
        for(int i=0;i<10;i++){sync.update(25000,20000,18000+i*500,false);check(!sync.seek,"seek cooldown prevents visible jump loop");}
        sync.reset();sync.update(1000,1200,0,false);check(sync.speed<1&&!sync.seek,"video ahead slows down");
        System.out.println("Video clock contracts passed; no player started");
    }
}
