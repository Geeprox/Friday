package local.friday.ktv;

/** Client PCM is 48 kHz; read/write counts are samples, and read requests are frames. */
public interface AudioBackendIO extends AutoCloseable {
    int METRIC_COUNT=91;
    boolean start();
    int read(short[] data,int frames);
    int write(short[] data,int offset,int samples);
    void metrics(long[] data);
    int routeDevice();
    void stop();
    void close();

    /** Failed startup owns no stream when a later backend is considered. */
    static boolean startOrClose(AudioBackendIO io){
        boolean started=false;
        try{started=io.start();return started;}
        finally{if(!started)try{io.stop();}finally{io.close();}}
    }
}
