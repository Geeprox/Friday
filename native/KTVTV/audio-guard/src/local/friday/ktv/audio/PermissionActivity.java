package local.friday.ktv.audio;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Normal companion permission entry. Never binds the service or starts audio. */
public final class PermissionActivity extends Activity {
    private static final int REQUEST_MIC=71;
    @Override public void onCreate(Bundle saved){
        super.onCreate(saved);
        if(checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED){setResult(RESULT_OK);finish();return;}
        LinearLayout box=new LinearLayout(this);box.setOrientation(LinearLayout.VERTICAL);box.setGravity(Gravity.CENTER);box.setPadding(40,32,40,32);
        TextView title=new TextView(this);title.setText("允许麦克风返听");title.setTextSize(28);box.addView(title);
        TextView detail=new TextView(this);detail.setText("授权后返回 Friday KTV。开启麦克风时使用，不保存录音。");detail.setTextSize(19);detail.setPadding(0,18,0,18);box.addView(detail);
        Button allow=new Button(this);allow.setText("允许使用麦克风");allow.setOnClickListener(v->requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},REQUEST_MIC));box.addView(allow);
        Button back=new Button(this);back.setText("返回");back.setOnClickListener(v->finish());box.addView(back);setContentView(box);allow.requestFocus();
    }
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants){
        super.onRequestPermissionsResult(request,permissions,grants);
        if(request==REQUEST_MIC){setResult(grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED?RESULT_OK:RESULT_CANCELED);finish();}
    }
}
