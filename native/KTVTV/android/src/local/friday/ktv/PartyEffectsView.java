package local.friday.ktv;

import android.animation.ValueAnimator;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.view.View;

/** Bounded edge decorations. Never covers lyrics with a full-screen flash. */
final class PartyEffectsView extends View {
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private ValueAnimator animator;
    private float phase;
    private boolean cheer;
    PartyEffectsView(Context context) {
        super(context);
        setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);
        setVisibility(GONE);
    }
    void show(String action) {
        stop();
        cheer = action.equals("sfx_cheer");
        phase = 0;
        setVisibility(VISIBLE);
        if (!ValueAnimator.areAnimatorsEnabled()) {
            invalidate();
            postDelayed(hide, 2800);
            return;
        }
        animator = ValueAnimator.ofFloat(0, 1);
        animator.setDuration(2800);
        animator.addUpdateListener(value -> {
            phase = (float) value.getAnimatedValue();
            invalidate();
            if (phase >= 1) stop();
        });
        animator.start();
    }
    private final Runnable hide = this::stop;
    void stop() {
        removeCallbacks(hide);
        if (animator != null) { animator.cancel(); animator = null; }
        setVisibility(GONE);
    }
    @Override protected void onDetachedFromWindow() { stop(); super.onDetachedFromWindow(); }
    @Override protected void onDraw(Canvas canvas) {
        float w = getWidth(), h = getHeight(), d = getResources().getDisplayMetrics().density;
        int alpha = Math.round(220 * Math.min(1, (1 - phase) * 4));
        int[] colors = {0xffffcf69, 0xffff8794, 0xffb0ded2};
        if (ValueAnimator.areAnimatorsEnabled()) {
            for (int i = 0; i < 36; i++) {
                float t = (phase * .9f + (i % 9) / 9f) % 1;
                boolean left = i % 2 == 0;
                float x = (left ? .018f : .982f) * w + (left ? 1 : -1) * (i % 5) * .008f * w;
                float y = cheer ? t * h : h * (.4f + .45f * t);
                paint.setColor(colors[i % colors.length]); paint.setAlpha(alpha);
                canvas.save(); canvas.translate(x, y); canvas.rotate(i * 37 + phase * 90);
                if (cheer) canvas.drawRect(-3*d, -6*d, 3*d, 6*d, paint);
                else canvas.drawCircle(0, 0, (2 + i % 3) * d, paint);
                canvas.restore();
            }
        }
        paint.setColor(0xffffffff); paint.setAlpha(alpha); paint.setTextSize(21*d);
        paint.setTextAlign(Paint.Align.CENTER); paint.setShadowLayer(4*d, 0, d, 0xcc000000);
        canvas.drawText(cheer ? "唱得漂亮！" : "掌声送给你", w / 2, 48*d, paint);
        paint.clearShadowLayer();
    }
}
