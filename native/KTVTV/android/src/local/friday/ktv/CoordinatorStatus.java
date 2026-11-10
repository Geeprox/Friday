package local.friday.ktv;

import java.util.Map;

/** A reachable API alone cannot mark the room fully ready. No free-form server text is shown. */
public final class CoordinatorStatus {
    public static final String[] GATES={"nas_api","nas_data","nas_media","qr","link_worker","link_tv","worker_source","worker_model","worker_download","worker_lyrics","worker_media","tv_playback","tv_audio","tv_media"};
    private static final String[] LABELS={"房间服务","房间数据","歌曲文件","点歌二维码","歌曲准备电脑","电视连接","音源账号","伴奏处理","歌曲下载","歌词处理","成品文件","电视播放","麦克风与音频权限","电视缓存"};
    private boolean complete;private String text="正在检查房间…";
    public void observe(boolean active,boolean ready,Map<String,Boolean> gates){
        complete=active&&ready&&gates!=null;StringBuilder missing=new StringBuilder();int count=0;
        for(int i=0;i<GATES.length;i++)if(gates==null||!Boolean.TRUE.equals(gates.get(GATES[i]))){complete=false;if(count++<2){if(missing.length()>0)missing.append("、");missing.append(LABELS[i]);}}
        text=complete?"房间已就绪":!active?"本场尚未开启":"部分功能未就绪 · "+(missing.length()>0?missing:"正在检查");
    }
    public void disconnected(boolean playing){complete=false;text=playing?"房间连接中 · 本地播放继续":"正在重新连接房间…";}
    public boolean expire(long age,boolean playing){if(age>6000){String prior=text;disconnected(playing);return !prior.equals(text);}return false;}
    public boolean complete(){return complete;}
    public String text(){return text;}
}
