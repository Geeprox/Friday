package local.friday.ktv;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

public final class TvUpdateSpecTest {
    private static final String HASH = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
    private static final String PACKAGE = "local.friday.ktv";
    private static final byte[][] SIGNERS = {{1, 2}, {3, 4}};
    private static final String PATH = "/v1/tv/update.apk";

    @FunctionalInterface private interface Check { void run() throws Exception; }

    private static void require(boolean condition, String description) {
        if (!condition) throw new AssertionError(description);
    }

    private static void rejects(Check check, String description) throws Exception {
        try {
            check.run();
        } catch (IOException expected) {
            return;
        }
        throw new AssertionError(description);
    }

    public static void main(String[] args) throws Exception {
        TvUpdateSpec release = TvUpdateSpec.checked(73, "0.73", 3L, HASH, PATH);
        require(release.newerThan(72) && !release.newerThan(73) && !release.newerThan(74) && !release.newerThan(0),
            "只接受比当前安装值更高的版本");
        require(release.sameRelease(TvUpdateSpec.checked(73L, "0.73", 3, HASH, PATH)), "候选版本一致");
        require(!release.sameRelease(null) && !release.sameRelease(TvUpdateSpec.checked(74, "0.74", 3, HASH, PATH)),
            "候选撤回或变动时重新检查");
        rejects(() -> TvUpdateSpec.checked(73, "0.73", 3, HASH, "https://other.example" + PATH), "不向其他端点传递密钥");
        rejects(() -> TvUpdateSpec.checked(73, "0.73", 3, HASH, PATH + "?redirect=1"), "只允许固定包路径");
        rejects(() -> TvUpdateSpec.checked(73.0, "0.73", 3, HASH, PATH), "版本字段需为整数");
        rejects(() -> TvUpdateSpec.checked(73, "0.73", 3.2, HASH, PATH), "拒绝小数长度");
        rejects(() -> TvUpdateSpec.checked(73, "0.73", TvUpdateSpec.MAX_BYTES + 1, HASH, PATH), "拒绝越界包体");
        rejects(() -> TvUpdateSpec.checked(73, "0.73", 0, HASH, PATH), "拒绝空包");
        rejects(() -> TvUpdateSpec.checked(73, "0.73", 3, "bad", PATH), "拒绝无效摘要");

        require(release.matchesArchive(PACKAGE, 72, PACKAGE, 73, "0.73", SIGNERS, new byte[][]{{3, 4}, {1, 2}}),
            "允许当前证书集合完全一致且顺序不同");
        require(!release.matchesArchive(PACKAGE, 73, PACKAGE, 73, "0.73", SIGNERS, SIGNERS), "归档不能重复安装");
        require(!release.matchesArchive(PACKAGE, 72, PACKAGE + ".audio", 73, "0.73", SIGNERS, SIGNERS), "音频伴随包不可安装");
        require(!release.matchesArchive(PACKAGE, 72, PACKAGE, 74, "0.73", SIGNERS, SIGNERS), "归档版本代码必须匹配");
        require(!release.matchesArchive(PACKAGE, 72, PACKAGE, 73, "0.74", SIGNERS, SIGNERS), "归档版本名必须匹配");
        require(!release.matchesArchive(PACKAGE, 72, PACKAGE, 73, "0.73", SIGNERS, new byte[][]{{1, 2}, {3, 5}}),
            "更换其中任一签名均拒绝");
        require(!release.matchesArchive(PACKAGE, 72, PACKAGE, 73, "0.73", SIGNERS, new byte[][]{{1, 2}}),
            "缺少签名拒绝");
        require(!release.matchesArchive(PACKAGE, 72, PACKAGE, 73, "0.73", SIGNERS, null), "签名不可读时拒绝");

        byte[] input = "abc".getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        release.copyVerified(new ByteArrayInputStream(input), output, () -> false);
        require(Arrays.equals(input, output.toByteArray()), "将精确字节交给安装会话");
        rejects(() -> copy(release, "abcd", false), "拒绝超长包体");
        rejects(() -> copy(release, "ab", false), "拒绝截断包体");
        rejects(() -> copy(release, "abd", false), "拒绝相同长度摘要不同");
        rejects(() -> copy(release, "abc", true), "连接改变或超时时停止写入");

        require(TvUpdateSpec.idleForInstall(true, true, false, false, false, false), "只在空闲焦点进入安装");
        require(!TvUpdateSpec.idleForInstall(false, true, false, false, false, false), "后台不提交");
        require(!TvUpdateSpec.idleForInstall(true, false, false, false, false, false), "失去焦点不提交");
        require(!TvUpdateSpec.idleForInstall(true, true, true, false, false, false), "黑屏调试期间不提交");
        require(!TvUpdateSpec.idleForInstall(true, true, false, true, false, false), "暂停的当前曲不丢失");
        require(!TvUpdateSpec.idleForInstall(true, true, false, false, true, false), "音频加载期间不提交");
        require(!TvUpdateSpec.idleForInstall(true, true, false, false, false, true), "播放期间不提交");
        System.out.println("TvUpdateSpecTest passed");
    }

    private static void copy(TvUpdateSpec release, String payload, boolean cancelled) throws Exception {
        release.copyVerified(new ByteArrayInputStream(payload.getBytes(StandardCharsets.UTF_8)),
            new ByteArrayOutputStream(), () -> cancelled);
    }
}
