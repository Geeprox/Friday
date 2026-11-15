package local.friday.ktv;

/** 电视曲目背景的输入、显示和解码预算。 */
final class BackdropImagePolicy {
    private static final int COMPAT_WIDTH = 1920, COMPAT_HEIGHT = 1080;
    private static final long MIB = 1024L * 1024;

    static boolean validSource(int width, int height) {
        return width >= 256 && height >= 256 && width <= 4096 && height <= 4096
            && (long) width * height <= 9_000_000;
    }

    static boolean benefitsFromUhd(int modeWidth, int modeHeight, int viewWidth, int viewHeight) {
        return modeWidth >= 3840 && modeHeight >= 2160 && viewWidth > COMPAT_WIDTH && viewHeight > COMPAT_HEIGHT;
    }

    static int compatibleSample(int width, int height) {
        int sample = 1;
        while ((width + sample - 1) / sample > COMPAT_WIDTH || (height + sample - 1) / sample > COMPAT_HEIGHT) {
            sample *= 2;
        }
        return sample;
    }

    static boolean canDecodeUhd(int width, int height, boolean lowMemory, long processSpare, long systemSpare) {
        if (!validSource(width, height) || lowMemory) return false;
        long pixels = (long) width * height * 4;
        // 现有图像已计入进程用量。还要留出解码、渲染上传和播放缓冲的空间。
        return processSpare >= pixels + 16 * MIB && systemSpare >= 2 * pixels + 32 * MIB;
    }
}
