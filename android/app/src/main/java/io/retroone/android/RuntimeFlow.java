package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.IOException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Explicit trial and recovery for standalone cartridge/disc cores; never auto-confirms playback.
 */
final class RuntimeFlow {
  private final Activity activity;
  private final ExecutorService io;
  private final Runnable changed;
  private final RetroArchFlow shared;
  private final RuntimeAdapter adapter;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private boolean resumed, busy, awaiting, paused;
  private long started;
  private String task, fingerprint;

  RuntimeFlow(Activity activity, ExecutorService io, Runnable changed, RetroArchFlow shared) {
    this.activity = activity;
    this.io = io;
    this.changed = changed;
    this.shared = shared;
    adapter = new RuntimeAdapter(activity);
  }

  void onPause() {
    resumed = false;
    if (awaiting) paused = true;
  }

  void onResume() {
    resumed = true;
    if (!awaiting || !paused) return;
    awaiting = false;
    DownloadStore.Item item = RetroApplication.from(activity).downloads.get(task);
    if (item == null) return;
    if (SystemClock.elapsedRealtime() - started < 6000) {
      new AlertDialog.Builder(activity)
          .setTitle("继续启动游戏")
          .setMessage("RetroArch 切换内容时可能先退出旧实例。可再次启动；若仍返回，请检查对应核心、BIOS 和镜像格式。")
          .setPositiveButton("再次启动", (d, w) -> play(item))
          .setNeutralButton("查看设置", (d, w) -> show(item.platform))
          .setNegativeButton("取消", null)
          .show();
    } else if (!adapter.verified(item)) {
      new AlertDialog.Builder(activity)
          .setTitle("这款游戏运行正常吗？")
          .setMessage("请在进入真实游戏并试过方向、动作键后确认。双屏游戏还需检查触屏；只有模拟器菜单、片头或报错画面不算通过。")
          .setPositiveButton(
              "画面与操作正常",
              (d, w) -> {
                try {
                  adapter.confirm(item, fingerprint);
                  changed.run();
                } catch (Exception e) {
                  error(e);
                }
              })
          .setNegativeButton("尚未正常", (d, w) -> show(item.platform))
          .show();
    }
  }

  void show() {
    String[] ids = {"n64", "nds", "psx", "dreamcast", "saturn"};
    String[] labels = new String[ids.length];
    for (int i = 0; i < ids.length; i++) labels[i] = RuntimePolicy.spec(ids[i]).label;
    new AlertDialog.Builder(activity)
        .setTitle("本机模拟器设置")
        .setItems(labels, (d, w) -> show(ids[w]))
        .setNegativeButton("返回", null)
        .show();
  }

  private void show(String platform) {
    RuntimePolicy.Spec spec = RuntimePolicy.spec(platform);
    if (spec == null || activity.isFinishing() || activity.isDestroyed()) return;
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    int pad = (int) (20 * activity.getResources().getDisplayMetrics().density);
    content.setPadding(pad, pad / 2, pad, pad / 2);
    TextView details = new TextView(activity);
    details.setText(
        spec.label
            + " · RetroArch ARM64\n\n核心："
            + spec.core
            + "\n\n共享目录："
            + adapter.retroarch.directoryLabel()
            + "\n\n"
            + spec.notes
            + "\n\n核心与 BIOS 请在 RetroArch 中配置；本应用尚不能读取其私有核心文件或确认 BIOS 目录。每款游戏仍需实际运行验证。");
    content.addView(details);
    AlertDialog dialog =
        new AlertDialog.Builder(activity)
            .setTitle(spec.label + " 设置")
            .setNegativeButton("返回", null)
            .create();
    button(
        content,
        "选择共享目录",
        () -> {
          dialog.dismiss();
          shared.chooseSharedDirectory(() -> show(platform));
        });
    button(
        content,
        "打开 RetroArch 设置",
        () -> {
          try {
            Intent launch =
                activity.getPackageManager().getLaunchIntentForPackage(RetroArchPolicy.PACKAGE);
            dialog.dismiss();
            if (launch == null) {
              shared.show();
              return;
            }
            activity.startActivity(launch);
          } catch (Exception e) {
            error(e);
          }
        });
    button(
        content,
        "核心或 BIOS 已更换，重新验证",
        () -> {
          adapter.reset(spec);
          changed.run();
          dialog.dismiss();
          show(platform);
        });
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(content);
    dialog.setView(scroll);
    dialog.show();
  }

  private void button(LinearLayout content, String title, Runnable action) {
    Button button = new Button(activity);
    button.setText(title);
    button.setOnClickListener(v -> action.run());
    content.addView(button);
  }

  void play(DownloadStore.Item item) {
    if (busy || item == null) return;
    RuntimePolicy.Spec spec = RuntimePolicy.spec(item.platform);
    if (spec == null) return;
    if (!adapter.retroarch.installed() || !adapter.retroarch.configured()) {
      show(item.platform);
      return;
    }
    if (!adapter.verified(item)) {
      new AlertDialog.Builder(activity)
          .setTitle("试运行 " + spec.label)
          .setMessage(
              spec.notes
                  + "\n\n请先在 RetroArch 安装 "
                  + spec.core
                  + "。结构检查不能保证游戏兼容，首次启动后请确认画面和操作。\n\n"
                  + "沿用 RetroArch 的存档；本入口暂不按玩家隔离，也不与云端同步。切换前请保存上一局。")
          .setPositiveButton("准备启动", (d, w) -> prepare(item))
          .setNeutralButton("查看设置", (d, w) -> show(item.platform))
          .setNegativeButton("取消", null)
          .show();
    } else prepare(item);
  }

  private void prepare(DownloadStore.Item item) {
    if (busy || !resumed) return;
    busy = true;
    AtomicBoolean cancelled = new AtomicBoolean();
    AlertDialog progress =
        new AlertDialog.Builder(activity)
            .setMessage("正在核对下载和镜像，准备启动副本…")
            .setCancelable(false)
            .setNegativeButton("取消", null)
            .create();
    AtomicReference<Thread> worker = new AtomicReference<>();
    Runnable work =
        () -> {
          synchronized (worker) {
            worker.set(Thread.currentThread());
          }
          String path = null;
          Exception problem = null;
          try {
            if (cancelled.get()) throw new IOException("准备已取消");
            path = adapter.prepare(item);
          } catch (Exception e) {
            problem = e;
          } finally {
            synchronized (worker) {
              worker.set(null);
            }
          }
          String result = path;
          Exception failure = problem;
          handler.post(
              () -> {
                busy = false;
                if (activity.isDestroyed() || activity.isFinishing()) return;
                progress.dismiss();
                if (cancelled.get()) return;
                if (failure != null) {
                  error(failure);
                  return;
                }
                if (!resumed) return;
                String last =
                    activity.getSharedPreferences("retroarch", 0).getString("lastLaunch", "");
                if (!last.isEmpty() && !last.equals(result)) {
                  new AlertDialog.Builder(activity)
                      .setTitle("切换游戏")
                      .setMessage("请在 RetroArch 保存并结束上一局，再继续启动。若切换后返回此页，可以再次启动。")
                      .setPositiveButton("已保存，继续", (d, w) -> open(item, result))
                      .setNegativeButton("取消", null)
                      .show();
                } else open(item, result);
              });
        };
    progress.setOnShowListener(
        d ->
            progress
                .getButton(AlertDialog.BUTTON_NEGATIVE)
                .setOnClickListener(
                    v -> {
                      cancelled.set(true);
                      synchronized (worker) {
                        Thread current = worker.get();
                        if (current != null) current.interrupt();
                      }
                      progress.setMessage("正在取消并清理临时副本…");
                      progress.getButton(AlertDialog.BUTTON_NEGATIVE).setEnabled(false);
                    }));
    progress.show();
    io.execute(work);
  }

  private void open(DownloadStore.Item item, String path) {
    if (!resumed) return;
    try {
      fingerprint = adapter.fingerprint(item);
      if (fingerprint.isEmpty()) throw new IOException("启动环境已变化，请重新准备");
      task = item.id;
      started = SystemClock.elapsedRealtime();
      awaiting = true;
      paused = false;
      activity.startActivity(adapter.retroarch.intent(path, item.platform));
      RetroApplication.from(activity).downloads.played(item.id);
      activity.getSharedPreferences("retroarch", 0).edit().putString("lastLaunch", path).apply();
    } catch (Exception e) {
      awaiting = false;
      error(e);
    }
  }

  private void error(Exception e) {
    new AlertDialog.Builder(activity)
        .setTitle("本机启动未完成")
        .setMessage(ApiClient.message(e))
        .setPositiveButton("知道了", null)
        .show();
  }
}
