package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import java.io.IOException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/** Explicit cold start, manual return, trial confirmation, and post-exit file-grant cleanup. */
final class DolphinFlow {
  private static final String RETURN_NOTE =
      "若显示 Dolphin 游戏列表，请按系统返回继续原游戏；也可从最近任务返回 Dolphin。这里只打开应用，不会重新发送游戏。";
  private final Activity activity;
  private final ExecutorService io;
  private final Runnable changed;
  private final Dolphin adapter;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private boolean resumed, busy, awaiting, paused;
  private long started;

  DolphinFlow(Activity activity, ExecutorService io, Runnable changed) {
    this.activity = activity;
    this.io = io;
    this.changed = changed;
    adapter = new Dolphin(activity);
  }

  void onPause() {
    resumed = false;
    if (awaiting) paused = true;
  }

  void onResume() {
    resumed = true;
    if (!awaiting || !paused) return;
    awaiting = false;
    DownloadStore.Item item = RetroApplication.from(activity).downloads.get(adapter.activeId());
    if (item == null) return;
    if (SystemClock.elapsedRealtime() - started < 6000) {
      new AlertDialog.Builder(activity).setTitle("Dolphin 已返回")
          .setMessage("尚不能确认游戏是否启动。若停在列表，可用系统返回查看原游戏；若已退出或启动报错，请确认退出后重新启动。")
          .setPositiveButton("打开 Dolphin", (d, w) -> open())
          .setNeutralButton("已退出，重新启动", (d, w) -> confirmExit(() -> play(item)))
          .setNegativeButton("保留当前状态", null).show();
    } else if (!adapter.verified(item)) confirmTrial(item);
  }

  private boolean visible() { return !activity.isFinishing() && !activity.isDestroyed(); }

  void show() {
    if (!visible()) return;
    String active = adapter.activeId();
    String[] options = active.isEmpty()
        ? new String[] {adapter.installed() ? "打开 Dolphin" : "官方安装页面", "支持范围与存档说明"}
        : new String[] {"打开 Dolphin", "确认本款运行情况", "已退出游戏，回收授权…", "支持范围与存档说明"};
    new AlertDialog.Builder(activity).setTitle("GameCube · Dolphin")
        .setItems(options, (d, which) -> {
          if (which == 0) {
            if (adapter.installed()) explainOpen();
            else {
              try { activity.startActivity(new Intent(Intent.ACTION_VIEW,
                  Uri.parse("https://dolphin-emu.org/download/"))); }
              catch (Exception e) { error(e); }
            }
          } else if (!active.isEmpty() && which == 1) {
            DownloadStore.Item item = RetroApplication.from(activity).downloads.get(active);
            if (item != null) confirmTrial(item);
          } else if (!active.isEmpty() && which == 2) confirmExit(null);
          else new AlertDialog.Builder(activity).setTitle("Dolphin 使用说明")
              .setMessage("当前接入 GameCube 单文件 ISO / GCM / RVZ，Wii 尚未开放。首次启动需在 Dolphin 完成初始化，每款游戏需实际确认画面与操作。\n\n"
                  + "直接读取已下载原件，无需复制镜像。存档由 Dolphin 管理，暂不按家庭玩家隔离，也未接入云同步。\n\n"
                  + RETURN_NOTE)
              .setPositiveButton("知道了", null).show();
        }).setNegativeButton("返回", null).show();
  }

  void play(DownloadStore.Item item) {
    if (busy || item == null || !resumed) return;
    if (!adapter.installed()) { show(); return; }
    if (!adapter.activeId().isEmpty()) {
      if (!adapter.activeId().equals(item.id)) { confirmExit(() -> play(item)); return; }
      new AlertDialog.Builder(activity).setTitle(adapter.activeTitle())
          .setMessage(RETURN_NOTE + "\n\n若已在 Dolphin 退出，请选择重新启动。")
          .setPositiveButton("打开 Dolphin", (d, w) -> open())
          .setNeutralButton("已退出，重新启动", (d, w) -> confirmExit(() -> play(item)))
          .setNegativeButton("返回", null).show();
      return;
    }
    new AlertDialog.Builder(activity)
        .setTitle(adapter.verified(item) ? "用 Dolphin 启动" : "试运行 GameCube")
        .setMessage("请先在 Dolphin 保存并退出其他游戏。"
            + (adapter.verified(item) ? "" : "首次请进入真实游戏并试过方向、动作键后，再确认运行正常。")
            + "\n\n存档由 Dolphin 管理，暂不按玩家隔离或与云端同步。")
        .setPositiveButton("已退出其他游戏，启动", (d, w) -> prepare(item))
        .setNeutralButton("模拟器设置", (d, w) -> show())
        .setNegativeButton("取消", null).show();
  }

  private void confirmTrial(DownloadStore.Item item) {
    if (!visible()) return;
    if (adapter.verified(item)) {
      new AlertDialog.Builder(activity).setMessage("这款游戏已由你确认画面与操作正常。")
          .setPositiveButton("知道了", null).show();
      return;
    }
    String trial = adapter.trialFingerprint();
    new AlertDialog.Builder(activity).setTitle("这款游戏运行正常吗？")
        .setMessage("请在真实游戏中试过画面、方向与动作键后确认。只看到模拟器列表、片头或错误提示不算通过。确认不会结束游戏。")
        .setPositiveButton("画面与操作正常", (d, w) -> {
          try { adapter.confirm(item, trial); changed.run(); }
          catch (Exception e) { error(e); }
        })
        .setNeutralButton("打开 Dolphin", (d, w) -> explainOpen())
        .setNegativeButton("尚未正常", null).show();
  }

  private void explainOpen() {
    new AlertDialog.Builder(activity).setTitle("打开 Dolphin").setMessage(RETURN_NOTE)
        .setPositiveButton("打开", (d, w) -> open()).setNegativeButton("返回", null).show();
  }

  private void open() {
    if (!resumed) return;
    try {
      started = SystemClock.elapsedRealtime(); awaiting = true; paused = false;
      adapter.open(activity::startActivity);
    } catch (Exception e) { awaiting = false; error(e); }
  }

  private void confirmExit(Runnable next) {
    if (!visible()) return;
    new AlertDialog.Builder(activity).setTitle("确认已结束 Dolphin 游戏")
        .setMessage("请先在 Dolphin 保存并退出「" + adapter.activeTitle()
            + "」。只回到桌面不算退出。确认后回收这份文件的读取授权，不会强制关闭模拟器或改动存档。")
        .setPositiveButton("已保存并退出", (d, w) -> {
          try {
            adapter.releaseAfterConfirmedExit(); changed.run();
            if (next != null) next.run();
          } catch (Exception e) { error(e); }
        }).setNegativeButton("尚未退出", null).show();
  }

  private void prepare(DownloadStore.Item item) {
    if (busy || !resumed) return;
    busy = true;
    AtomicBoolean cancelled = new AtomicBoolean();
    AtomicReference<Thread> worker = new AtomicReference<>();
    AlertDialog progress = new AlertDialog.Builder(activity).setMessage("正在检查游戏文件…")
        .setCancelable(false).setNegativeButton("取消", null).create();
    progress.setOnShowListener(d -> progress.getButton(AlertDialog.BUTTON_NEGATIVE)
        .setOnClickListener(v -> {
          cancelled.set(true);
          synchronized (worker) {
            Thread thread = worker.get(); if (thread != null) thread.interrupt();
          }
          progress.getButton(AlertDialog.BUTTON_NEGATIVE).setEnabled(false);
        }));
    progress.show();
    io.execute(() -> {
      Dolphin.Prepared result = null;
      Exception failure = null;
      synchronized (worker) { worker.set(Thread.currentThread()); }
      try {
        if (cancelled.get()) throw new IOException("准备已取消");
        result = adapter.prepare(item);
      } catch (Exception e) { failure = e; }
      finally {
        synchronized (worker) { worker.set(null); }
        Thread.interrupted();
      }
      Dolphin.Prepared prepared = result;
      Exception problem = failure;
      handler.post(() -> {
        busy = false;
        if (!visible()) return;
        progress.dismiss();
        if (cancelled.get() || !resumed) return;
        if (problem != null) { error(problem); return; }
        try {
          started = SystemClock.elapsedRealtime(); awaiting = true; paused = false;
          adapter.launch(item, prepared, activity::startActivity);
          RetroApplication.from(activity).downloads.played(item.id);
          changed.run();
        } catch (Exception e) { awaiting = false; error(e); }
      });
    });
  }

  private void error(Exception e) {
    if (visible()) new AlertDialog.Builder(activity).setTitle("Dolphin 启动未完成")
        .setMessage(ApiClient.message(e)).setPositiveButton("知道了", null).show();
  }
}
