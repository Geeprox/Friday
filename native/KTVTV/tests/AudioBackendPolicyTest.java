package local.friday.ktv;

public final class AudioBackendPolicyTest {
    private static void check(boolean value,String why){if(!value)throw new AssertionError(why);}
    public static void main(String[] args){
        check(AudioBackendPolicy.duplexOutput(1,7,7,1,2,0)==2,"default built-in candidate rejected");
        check(AudioBackendPolicy.duplexOutput(1,7,7,1,2,2)==2,"explicit matching built-in rejected");
        check(AudioBackendPolicy.duplexOutput(2,7,7,1,2,0)==0,"ambiguous USB accepted");
        check(AudioBackendPolicy.duplexOutput(1,7,8,1,2,0)==0,"another input accepted");
        check(AudioBackendPolicy.duplexOutput(1,7,7,2,2,0)==0,"ambiguous built-in accepted");
        check(AudioBackendPolicy.duplexOutput(1,7,7,0,0,0)==0,"missing output accepted");
        check(AudioBackendPolicy.duplexOutput(1,7,7,1,2,9)==0,"explicit other output overridden");
        long[] metrics=new long[AudioBackendIO.METRIC_COUNT];metrics[63]=1;metrics[4]=2308;metrics[5]=2048;metrics[6]=960;metrics[7]=240;metrics[35]=5000;
        int demand=AudioBackendPolicy.outputDemandFrames(2,metrics);
        LiveVoiceBuffer voice=new LiveVoiceBuffer((int)metrics[7],demand,2);voice.configureNativeStartup(AudioBackendPolicy.inputDeliveryFrames(2,metrics),demand);
        check(demand==960&&voice.targetFrames==1200,"HAL allocation inflated the 25 ms voice target");
        metrics[4]=11520;check(AudioBackendPolicy.outputDemandFrames(2,metrics)==960,"capacity leaked into output demand");
        metrics[4]=2308;check(AudioBackendPolicy.outputDemandFrames(1,metrics)==2308,"legacy buffer policy changed");
        metrics[19]=60000;check(AudioBackendPolicy.inputDeliveryFrames(1,metrics)==2880,"legacy capture timing changed");
        metrics[35]=10000;check(AudioBackendPolicy.inputDeliveryFrames(2,metrics)==480,"new input read timing not used");
        metrics[35]=Long.MAX_VALUE;check(AudioBackendPolicy.inputDeliveryFrames(2,metrics)==3840,"large read timing overflowed");
        metrics[12]=metrics[13]=-1;
        check(!AudioBackendPolicy.outputXrunsAvailable(2,metrics)&&!AudioBackendPolicy.hardwareXrunsAvailable(2,metrics),"unknown xrun became zero measured xruns");
        metrics[12]=0;check(AudioBackendPolicy.outputXrunsAvailable(2,metrics)&&!AudioBackendPolicy.hardwareXrunsAvailable(2,metrics),"input xrun availability inferred from output");
        metrics[13]=0;check(!AudioBackendPolicy.hardwareXrunsAvailable(2,metrics),"API xrun counters claimed physical HAL observation");
        metrics[50]=1;check(!AudioBackendPolicy.hardwareTimingAvailable(2,metrics),"one timestamp claimed duplex timing");
        metrics[52]=1;check(!AudioBackendPolicy.hardwareTimingAvailable(2,metrics),"AAudio timestamps claimed physical HAL timing");
        check(!AudioBackendPolicy.hardwareTimingAvailable(1,metrics)&&!AudioBackendPolicy.outputXrunsAvailable(1,metrics),"fallback inherited duplex availability");
        metrics[63]=0;check(!AudioBackendPolicy.outputXrunsAvailable(2,metrics),"uninitialized snapshot claimed xruns");
        check(AudioBackendPolicy.platformFresh(0,1000,1500),"bounded startup grace missing");
        check(!AudioBackendPolicy.platformFresh(0,1500,1500),"unknown route timing survived startup deadline");
        check(AudioBackendPolicy.platformFresh(1000,2500,0)&&!AudioBackendPolicy.platformFresh(1000,2501,0),"platform snapshot age boundary incorrect");
        check(!AudioBackendPolicy.platformFresh(3000,2500,0),"future platform timestamp accepted");
        check(AudioBackendPolicy.routeChanged(2,0,2),"lost duplex route treated as unchanged");
        check(AudioBackendPolicy.routeChanged(2,3,2)&&!AudioBackendPolicy.routeChanged(2,2,2),"duplex exact route guard changed");
        check(!AudioBackendPolicy.routeChanged(1,0,2),"legacy unknown-route behavior changed");
        System.out.println("Route eligibility, batch-based 1200-frame prefill, legacy policy, unknown availability and freshness contracts passed; no device opened");
    }
}
