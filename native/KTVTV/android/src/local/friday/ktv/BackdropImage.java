package local.friday.ktv;

import android.app.ActivityManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ColorSpace;
import android.os.Debug;
import java.io.File;
import java.io.IOException;
import java.util.concurrent.atomic.AtomicBoolean;

final class BackdropImage {
    static void verify(File file, String encoding) throws IOException {
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(file.getAbsolutePath(), options);
        if (!("image/" + encoding).equals(options.outMimeType)
                || !BackdropImagePolicy.validSource(options.outWidth, options.outHeight)) {
            throw new IOException("invalid_background");
        }
    }

    static Bitmap decode(File file, boolean wantsUhd, ActivityManager memory, AtomicBoolean cancelled) throws IOException {
        if (cancelled.get()) return null;
        String path = file.getAbsolutePath();
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(path, bounds);
        int width = bounds.outWidth;
        int height = bounds.outHeight;
        if (!BackdropImagePolicy.validSource(width, height)) throw new IOException("invalid_background_dimensions");
        int compatible = BackdropImagePolicy.compatibleSample(width, height);
        if (wantsUhd && compatible > 1 && hasUhdHeadroom(memory, width, height)) {
            Bitmap full = decodeSample(path, 1, cancelled);
            if (full != null) return full;
        }
        Bitmap sampled = decodeSample(path, compatible, cancelled);
        if (sampled != null) return sampled;
        return decodeSample(path, compatible * 2, cancelled);
    }

    private static boolean hasUhdHeadroom(ActivityManager manager, int width, int height) {
        if (manager == null || manager.isLowRamDevice()) return false;
        try {
            ActivityManager.MemoryInfo system = new ActivityManager.MemoryInfo();
            manager.getMemoryInfo(system);
            Runtime runtime = Runtime.getRuntime();
            long managed = runtime.totalMemory() - runtime.freeMemory();
            long processSpare = runtime.maxMemory() - managed - Debug.getNativeHeapAllocatedSize();
            return BackdropImagePolicy.canDecodeUhd(width, height, system.lowMemory, processSpare, system.availMem - system.threshold);
        } catch (RuntimeException unavailable) {
            return false;
        }
    }

    private static Bitmap decodeSample(String path, int sample, AtomicBoolean cancelled) {
        if (cancelled.get()) return null;
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inSampleSize = sample;
        options.inScaled = false;
        options.inPreferredConfig = Bitmap.Config.ARGB_8888;
        options.inPreferredColorSpace = ColorSpace.get(ColorSpace.Named.SRGB);
        try {
            Bitmap decoded = BitmapFactory.decodeFile(path, options);
            if (decoded != null && cancelled.get()) {
                decoded.recycle();
                return null;
            }
            return decoded;
        } catch (OutOfMemoryError refused) {
            return null;
        }
    }
}
