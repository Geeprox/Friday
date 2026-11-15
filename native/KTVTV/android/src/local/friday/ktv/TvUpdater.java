package local.friday.ktv;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class TvUpdater {
    static final String RESULT = "local.friday.ktv.UPDATE_RESULT";
    private static final String TOKEN = "local.friday.ktv.UPDATE_TOKEN";
    private static final long CHECK_INTERVAL = 15 * 60_000L, RETRY_INTERVAL = 30_000L;
    private static final long INSTALL_TIMEOUT = 90_000L;
    private static final Object JOURNAL_LOCK = new Object();
    private static TvUpdater process;

    private enum Work { RECOVERING, IDLE, DOWNLOADING, STAGING, INSTALLING }

    private final Context app;
    private final PackageManager packages;
    private final PackageInstaller installer;
    private final SharedPreferences journal;
    private final File download;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService lane = Executors.newSingleThreadExecutor();
    private volatile KtvClient connection;
    private Work work = Work.RECOVERING;
    private TvUpdateSpec prepared;
    private Receipt receipt;
    private Intent confirmation;
    private long nextCheck, installationDeadline;
    private String problem = "";
    private boolean sourcePermissionNeeded, cancelling;

    static TvUpdater forApp(Context context) {
        if (process == null) process = new TvUpdater(context.getApplicationContext());
        return process;
    }

    private TvUpdater(Context app) {
        this.app = app;
        packages = app.getPackageManager();
        installer = packages.getPackageInstaller();
        journal = journal(app);
        download = new File(app.getCacheDir(), "friday-ktv-update.apk");
        lane.execute(this::recover);
    }

    private void recover() {
        // 回收当前应用为自身创建的会话，包括上次在写入日志前中断的会话。
        Receipt previous = Receipt.read(journal);
        if (previous != null && !discardSession(previous)) {
            ui.postDelayed(() -> lane.execute(this::recover), 3000);
            return;
        }
        try {
            for (PackageInstaller.SessionInfo session : installer.getMySessions()) {
                if (ownsSession(session)) installer.abandonSession(session.getSessionId());
            }
        } catch (RuntimeException ignored) {
            // 未记录的会话还没有提交，不能安装更新。
        }
        download.delete();
        ui.post(() -> work = Work.IDLE);
    }

    private static SharedPreferences journal(Context app) {
        return app.getSharedPreferences("friday-ktv-update", Context.MODE_PRIVATE);
    }

    static long installedCode(Context context) {
        try {
            return version(context.getPackageManager().getPackageInfo(context.getPackageName(), 0));
        } catch (PackageManager.NameNotFoundException missing) {
            return 0;
        }
    }

    private static long version(PackageInfo info) {
        return Build.VERSION.SDK_INT >= 28 ? info.getLongVersionCode() : info.versionCode;
    }

    private static byte[][] signers(PackageInfo info) {
        Signature[] signatures = Build.VERSION.SDK_INT >= 28
            ? (info.signingInfo == null ? null : info.signingInfo.getApkContentsSigners()) : info.signatures;
        if (signatures == null) return null;
        byte[][] encoded = new byte[signatures.length][];
        for (int index = 0; index < signatures.length; index++) encoded[index] = signatures[index].toByteArray();
        return encoded;
    }

    private void verifyArchive(TvUpdateSpec release) throws Exception {
        int flags = Build.VERSION.SDK_INT >= 28 ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES;
        PackageInfo installed = packages.getPackageInfo(app.getPackageName(), flags);
        PackageInfo archive = packages.getPackageArchiveInfo(download.getAbsolutePath(), flags);
        if (archive == null || !release.matchesArchive(app.getPackageName(), version(installed), archive.packageName,
                version(archive), archive.versionName, signers(installed), signers(archive))) {
            throw new IOException("invalid_update_identity");
        }
    }

    void newConnection(KtvClient client) {
        if (connection == client) return;
        connection = client;
        prepared = null;
        sourcePermissionNeeded = false;
        nextCheck = 0;
        if (work == Work.IDLE) lane.execute(download::delete);
    }

    // 由前台 Activity 推动。下载和签名校验可与演唱并发，提交安装只发生在无待恢复的当前曲时。
    boolean advance(MainActivity activity, KtvClient client) {
        long now = SystemClock.elapsedRealtime();
        if (work == Work.INSTALLING) {
            if (now >= installationDeadline) cancelInstallation("更新等待超时，可在房间设置重试。");
            else if (!cancelling && confirmation != null && activity.canApplyTvUpdate()) {
                Intent intent = confirmation;
                confirmation = null;
                try { activity.startActivity(intent); }
                catch (RuntimeException unavailable) { cancelInstallation("电视未能打开系统安装确认。"); }
            }
        }
        if (work == Work.IDLE && client != null && client == connection && client.negotiated() && client.coordinator()) {
            if (prepared != null && activity.canApplyTvUpdate()) {
                sourcePermissionNeeded = !canInstall();
                if (!sourcePermissionNeeded) stage(activity, client, prepared);
            } else if (prepared == null && now >= nextCheck) {
                check(client);
            }
        }
        return blocksPlayback();
    }

    boolean blocksPlayback() {
        return work == Work.RECOVERING || work == Work.STAGING || work == Work.INSTALLING;
    }

    private void check(KtvClient client) {
        work = Work.DOWNLOADING;
        problem = "";
        lane.execute(() -> {
            TvUpdateSpec result = null;
            boolean failed = false;
            try {
                TvUpdateSpec release = client.televisionUpdate();
                if (release != null && release.newerThan(installedCode(app))
                        && journal.getLong("declined_version", 0) != release.versionCode && connection == client) {
                    if (download.getParentFile().getUsableSpace() < release.bytes + 8L * 1024 * 1024)
                        throw new IOException("update_cache_full");
                    client.downloadTelevisionUpdate(release, download, () -> connection != client);
                    verifyArchive(release);
                    result = release;
                }
            } catch (Exception failure) {
                failed = true;
            }
            final TvUpdateSpec verified = result;
            final boolean retry = failed;
            if (verified == null || connection != client) download.delete();
            ui.post(() -> {
                work = Work.IDLE;
                if (connection != client) return;
                prepared = verified;
                nextCheck = SystemClock.elapsedRealtime() + (retry ? RETRY_INTERVAL : CHECK_INTERVAL);
                if (retry) problem = "暂时无法检查或下载更新。";
            });
        });
    }

    private void stage(MainActivity activity, KtvClient client, TvUpdateSpec release) {
        work = Work.STAGING;
        lane.execute(() -> {
            Receipt staged = null;
            String error = "";
            try {
                TvUpdateSpec active = client.televisionUpdate();
                if (connection == client && release.sameRelease(active) && release.newerThan(installedCode(app))) {
                    staged = writeSession(release, client);
                }
            } catch (Exception failure) {
                error = "暂时无法准备安装，可在房间设置重试。";
            }
            final Receipt result = staged;
            final String reason = error;
            ui.post(() -> {
                if (result != null && (connection != client || !activity.canApplyTvUpdate())) {
                    lane.execute(() -> discardSession(result));
                    work = Work.IDLE;
                    return;
                }
                prepared = null;
                lane.execute(download::delete);
                if (connection != client || result == null) {
                    work = Work.IDLE;
                    nextCheck = SystemClock.elapsedRealtime() + (reason.isEmpty() ? 1000 : RETRY_INTERVAL);
                    problem = reason;
                } else {
                    commit(result);
                }
            });
        });
    }

    private Receipt writeSession(TvUpdateSpec release, KtvClient owner) throws Exception {
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(app.getPackageName());
        params.setSize(release.bytes);
        if (Build.VERSION.SDK_INT >= 31) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        Receipt next = new Receipt(installer.createSession(params), release.versionCode, UUID.randomUUID().toString());
        boolean complete = false;
        try {
            synchronized (JOURNAL_LOCK) {
                if (!journal.edit().putInt("session", next.id).putLong("version", next.version).putString("token", next.token).commit())
                    throw new IOException("update_session_journal");
            }
            try (PackageInstaller.Session session = installer.openSession(next.id);
                    FileInputStream input = new FileInputStream(download);
                    OutputStream output = session.openWrite("base.apk", 0, release.bytes)) {
                release.copyVerified(input, output, () -> connection != owner);
                session.fsync(output);
            }
            complete = true;
            return next;
        } finally {
            if (!complete) discardSession(next);
        }
    }

    private void commit(Receipt next) {
        receipt = next;
        work = Work.INSTALLING;
        installationDeadline = SystemClock.elapsedRealtime() + INSTALL_TIMEOUT;
        Intent callback = new Intent(app, TvUpdateReceiver.class).setAction(RESULT).putExtra(TOKEN, next.token);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 31) flags |= PendingIntent.FLAG_MUTABLE;
        try (PackageInstaller.Session session = installer.openSession(next.id)) {
            PendingIntent result = PendingIntent.getBroadcast(app, next.id, callback, flags);
            session.commit(result.getIntentSender());
        } catch (Exception failed) {
            cancelInstallation("系统暂时无法安装更新。");
        }
    }

    private void finishInstallation(String reason) {
        receipt = null;
        confirmation = null;
        cancelling = false;
        work = Work.IDLE;
        problem = reason;
        nextCheck = SystemClock.elapsedRealtime() + CHECK_INTERVAL;
    }

    private void cancelInstallation(String reason) {
        if (cancelling || receipt == null) return;
        cancelling = true;
        confirmation = null;
        Receipt target = receipt;
        lane.execute(() -> {
            boolean cancelled = discardSession(target);
            ui.post(() -> {
                if (receipt != target) return;
                if (cancelled) finishInstallation(reason);
                else {
                    cancelling = false;
                    installationDeadline = SystemClock.elapsedRealtime() + 3000;
                }
            });
        });
    }

    private boolean ownsSession(PackageInstaller.SessionInfo session) {
        return session != null && app.getPackageName().equals(session.getAppPackageName())
            && app.getPackageName().equals(session.getInstallerPackageName());
    }

    private boolean discardSession(Receipt old) {
        try {
            if (ownsSession(installer.getSessionInfo(old.id))) installer.abandonSession(old.id);
            if (ownsSession(installer.getSessionInfo(old.id))) return false;
        } catch (RuntimeException ignored) {
            return false;
        }
        if (old.version > installedCode(app)) {
            old.clear(journal);
        }
        return true;
    }

    static boolean applied(Context context) {
        SharedPreferences journal = journal(context);
        Receipt target = Receipt.read(journal);
        if (target == null || installedCode(context) < target.version) return false;
        target.clear(journal);
        return true;
    }

    static void acceptResult(Context context, Intent event) {
        if (!RESULT.equals(event.getAction())) return;
        Receipt target = Receipt.read(journal(context));
        if (target == null || target.id != event.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, -1)
                || !target.token.equals(event.getStringExtra(TOKEN))) return;
        int status = event.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (status == PackageInstaller.STATUS_SUCCESS) {
            if (applied(context)) TvUpdateReceiver.showReopen(context);
            return;
        }
        TvUpdater updater = process;
        if (updater == null || updater.receipt == null || !target.token.equals(updater.receipt.token)) return;
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            updater.confirmation = Build.VERSION.SDK_INT >= 33
                ? event.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class) : event.getParcelableExtra(Intent.EXTRA_INTENT);
            if (updater.confirmation == null) updater.cancelInstallation("电视未提供系统安装确认。");
        } else {
            boolean declined = status == PackageInstaller.STATUS_FAILURE_ABORTED && !updater.cancelling;
            if (declined) updater.journal.edit().putLong("declined_version", target.version).apply();
            target.clear(updater.journal);
            updater.finishInstallation(declined ? "已取消软件更新。" : "系统安装更新未完成。");
        }
    }

    private boolean canInstall() {
        try {
            return packages.canRequestPackageInstalls();
        } catch (RuntimeException unavailable) {
            return false;
        }
    }

    boolean needsSourcePermission() {
        return sourcePermissionNeeded && !canInstall();
    }

    void checkAgain() {
        journal.edit().remove("declined_version").apply();
        nextCheck = 0;
    }

    String idleHint() {
        if (work == Work.STAGING || work == Work.INSTALLING) return stateText();
        return needsSourcePermission() ? "新版已就绪，可在房间设置批准更新" : "";
    }

    String stateText() {
        if (work == Work.STAGING) return "正在准备更新…";
        if (work == Work.INSTALLING) return confirmation == null ? "正在安装更新…" : "请确认电视系统安装提示";
        if (work == Work.DOWNLOADING) return "正在检查及下载更新…";
        if (needsSourcePermission()) return "请在电视系统设置中允许 Friday KTV 安装新版。";
        if (prepared != null) return "新版 " + prepared.versionName + " 已就绪，将在当前歌曲结束后安装。";
        return problem.isEmpty() ? "电视启动后会自动检查更新。" : problem;
    }

    private static final class Receipt {
        final int id;
        final long version;
        final String token;

        Receipt(int id, long version, String token) {
            this.id = id;
            this.version = version;
            this.token = token;
        }

        static Receipt read(SharedPreferences journal) {
            int id = journal.getInt("session", -1);
            long version = journal.getLong("version", 0);
            String token = journal.getString("token", "");
            return id >= 0 && version > 0 && !token.isEmpty() ? new Receipt(id, version, token) : null;
        }

        void clear(SharedPreferences journal) {
            synchronized (JOURNAL_LOCK) {
                if (token.equals(journal.getString("token", ""))) {
                    journal.edit().remove("session").remove("version").remove("token").apply();
                }
            }
        }
    }
}
