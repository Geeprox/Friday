package local.friday.ktv;

/** 主屏只显示点歌和播放所需的状态。 */
public final class CoordinatorStatus {
    // 与 NAS 的电视在线窗口一致，跨过个别连接超时。
    private static final long ROOM_STALE_MS = 12_000;
    // 电视能力每三秒上报一次。至少跨过一轮上报再显示异常。
    private static final long DEVICE_ISSUE_MS = 3_000;

    private enum DeviceIssue { NONE, OUTPUT, STORAGE }

    private long roomSeenAt = -1;
    private long issueSince;
    private DeviceIssue issue = DeviceIssue.NONE;

    public void reset() {
        roomSeenAt = -1;
        issue = DeviceIssue.NONE;
        issueSince = 0;
    }

    public void observeRoom(long receivedAt, boolean outputUnavailable, boolean storageUnavailable) {
        roomSeenAt = receivedAt;
        DeviceIssue reported = outputUnavailable ? DeviceIssue.OUTPUT
            : storageUnavailable ? DeviceIssue.STORAGE : DeviceIssue.NONE;
        if (issue != reported) {
            issue = reported;
            issueSince = receivedAt;
        }
    }

    public String text(long now, boolean hasRoomQr, boolean playing) {
        if (roomSeenAt < 0) return "";
        if (now - roomSeenAt >= ROOM_STALE_MS) {
            return playing ? "房间连接中 · 本地播放继续" : "正在重新连接房间…";
        }
        if (!hasRoomQr) return "正在准备点歌二维码…";
        if (roomSeenAt - issueSince >= DEVICE_ISSUE_MS) {
            if (issue == DeviceIssue.OUTPUT) return "电视暂时无法播放";
            if (issue == DeviceIssue.STORAGE) return "电视存储暂不可用";
        }
        return "房间已就绪";
    }
}
