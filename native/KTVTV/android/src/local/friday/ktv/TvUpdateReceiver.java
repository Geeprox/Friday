package local.friday.ktv;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 系统可按当前权限通知用户重新打开更新后的 KTV。 */
public final class TvUpdateReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) {
            if (TvUpdater.applied(context)) showReopen(context);
        } else {
            TvUpdater.acceptResult(context, intent);
        }
    }

    static void showReopen(Context context) {
        NotificationManager notifications = context.getSystemService(NotificationManager.class);
        if (notifications == null || !notifications.areNotificationsEnabled()) return;
        try {
            String channel = "ktv-software-update";
            notifications.createNotificationChannel(new NotificationChannel(channel, "软件更新", NotificationManager.IMPORTANCE_DEFAULT));
            Intent open = new Intent(context, MainActivity.class).setAction(Intent.ACTION_MAIN)
                .addCategory(Intent.CATEGORY_LEANBACK_LAUNCHER).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            PendingIntent button = PendingIntent.getActivity(context, 0, open, PendingIntent.FLAG_IMMUTABLE);
            notifications.notify(72, new Notification.Builder(context, channel).setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("Friday KTV 已更新").setContentText("打开新版，继续点歌。")
                .setContentIntent(button).setAutoCancel(true).build());
        } catch (RuntimeException ignored) {
            // 如果电视没有通知界面，仍可从电视应用列表打开新版。
        }
    }
}
