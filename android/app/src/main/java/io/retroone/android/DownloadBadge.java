package io.retroone.android;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.View;

/** In-app progress bubble: no overlay permission and no obstruction of the emulator. */
final class DownloadBadge extends View {
  private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
  private DownloadSummary summary = new DownloadSummary();

  DownloadBadge(Context context) {
    super(context);
    setFocusable(true);
    setClickable(true);
    setContentDescription("下载管理");
  }

  void update(DownloadSummary next) {
    summary = next;
    setContentDescription(next.description());
    setTooltipText(next.description());
    invalidate();
  }

  @Override
  protected void onDraw(Canvas canvas) {
    super.onDraw(canvas);
    float d = getResources().getDisplayMetrics().density;
    float cx = getWidth() / 2f, cy = getHeight() / 2f, radius = Math.min(cx, cy) - 5 * d;
    paint.setStyle(Paint.Style.FILL);
    paint.setColor(MainActivity.PANEL);
    canvas.drawCircle(cx, cy, radius, paint);
    paint.setStyle(Paint.Style.STROKE);
    paint.setStrokeWidth((isFocused() ? 2.5f : 2) * d);
    paint.setColor(isFocused() ? MainActivity.ACCENT : 0xff395269);
    canvas.drawCircle(cx, cy, radius, paint);
    paint.setColor(
        summary.active == 0 && summary.failed > 0 && summary.ready == 0
            ? 0xffffc890
            : MainActivity.ACCENT);
    if (summary.active > 0)
      canvas.drawArc(
          new RectF(cx - radius, cy - radius, cx + radius, cy + radius),
          -90,
          summary.percent < 0 ? 85 : summary.percent * 3.6f,
          false,
          paint);
    paint.setStyle(Paint.Style.FILL);
    paint.setTextAlign(Paint.Align.CENTER);
    paint.setTextSize((summary.active > 0 ? 10 : 20) * d);
    canvas.drawText(summary.label(), cx, cy - (paint.ascent() + paint.descent()) / 2, paint);
    if (summary.count() > 0) {
      float x = getWidth() - 7 * d, y = 8 * d;
      paint.setColor(summary.ready > 0 && summary.active == 0 ? 0xff8cd7b1 : MainActivity.ACCENT);
      canvas.drawCircle(x, y, 7 * d, paint);
      paint.setColor(MainActivity.BG);
      paint.setTextSize(9 * d);
      canvas.drawText(
          summary.count() > 9 ? "9+" : String.valueOf(summary.count()),
          x,
          y - (paint.ascent() + paint.descent()) / 2,
          paint);
    }
  }
}
