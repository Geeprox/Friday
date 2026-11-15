package local.friday.ktv;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.HashSet;
import java.util.Set;
import java.util.function.BooleanSupplier;

final class TvUpdateSpec {
    static final String APK_PATH = "/v1/tv/update.apk";
    static final long MAX_BYTES = 64L * 1024 * 1024;

    final long versionCode;
    final String versionName;
    final long bytes;
    final String sha256;

    static TvUpdateSpec checked(Object code, Object name, Object size, Object sha, Object path) throws IOException {
        if (!wholeNumber(code) || !wholeNumber(size) || !(name instanceof String)
                || !(sha instanceof String) || !APK_PATH.equals(path)) throw invalid();
        long version = ((Number) code).longValue();
        long bytes = ((Number) size).longValue();
        String label = (String) name;
        String digest = (String) sha;
        if (version <= 0 || bytes <= 0 || bytes > MAX_BYTES || !label.matches("[0-9]+\\.[0-9]+")
                || label.length() > 24 || !digest.matches("[0-9a-f]{64}")) throw invalid();
        return new TvUpdateSpec(version, label, bytes, digest);
    }

    private TvUpdateSpec(long version, String name, long bytes, String sha256) {
        this.versionCode = version;
        this.versionName = name;
        this.bytes = bytes;
        this.sha256 = sha256;
    }

    private static boolean wholeNumber(Object value) {
        return value instanceof Integer || value instanceof Long;
    }

    private static IOException invalid() {
        return new IOException("invalid_update_manifest");
    }

    boolean newerThan(long installed) {
        return installed > 0 && versionCode > installed;
    }

    boolean sameRelease(TvUpdateSpec other) {
        return other != null && versionCode == other.versionCode && bytes == other.bytes
            && versionName.equals(other.versionName) && sha256.equals(other.sha256);
    }

    static boolean idleForInstall(boolean foreground, boolean focused, boolean debugging,
            boolean hasCurrent, boolean loading, boolean playing) {
        return foreground && focused && !debugging && !hasCurrent && !loading && !playing;
    }

    boolean matchesArchive(String ownPackage, long installed, String archivePackage,
            long archiveCode, String archiveName, byte[][] installedSigners, byte[][] archiveSigners) {
        return newerThan(installed) && ownPackage.equals(archivePackage) && archiveCode == versionCode
            && versionName.equals(archiveName) && sameSigners(installedSigners, archiveSigners);
    }

    private static boolean sameSigners(byte[][] installed, byte[][] archive) {
        if (installed == null || archive == null || installed.length == 0 || installed.length != archive.length) return false;
        Set<String> expected = new HashSet<>();
        for (byte[] signer : installed) {
            if (signer == null || signer.length == 0 || !expected.add(Base64.getEncoder().encodeToString(signer))) return false;
        }
        for (byte[] signer : archive) {
            if (signer == null || !expected.remove(Base64.getEncoder().encodeToString(signer))) return false;
        }
        return expected.isEmpty();
    }

    void copyVerified(InputStream input, OutputStream output, BooleanSupplier cancelled) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] buffer = new byte[65536];
        long remaining = bytes;
        while (remaining > 0) {
            if (cancelled.getAsBoolean()) throw new IOException("update_cancelled");
            int count = input.read(buffer, 0, (int) Math.min(buffer.length, remaining));
            if (count <= 0) throw new IOException("incomplete_update");
            digest.update(buffer, 0, count);
            output.write(buffer, 0, count);
            remaining -= count;
        }
        if (cancelled.getAsBoolean()) throw new IOException("update_cancelled");
        if (input.read() != -1 || !MessageDigest.isEqual(digest.digest(), expectedHash()))
            throw new IOException("invalid_update_bytes");
    }

    private byte[] expectedHash() {
        byte[] hash = new byte[32];
        for (int i = 0; i < hash.length; i++) {
            hash[i] = (byte) Integer.parseInt(sha256.substring(i * 2, i * 2 + 2), 16);
        }
        return hash;
    }
}
