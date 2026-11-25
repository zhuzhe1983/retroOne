package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import java.io.IOException;
import java.util.concurrent.ExecutorService;

final class FbneoFlow {
  private final Activity activity;
  private final ExecutorService io;
  private final Runnable changed;
  private final RetroArchFlow nesFlow;
  private final Fbneo adapter;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private boolean resumed, busy, awaiting, paused;
  private long started;
  private String task, fingerprint;

  FbneoFlow(Activity activity, ExecutorService io, Runnable changed, RetroArchFlow nesFlow) {
    this.activity = activity;
    this.io = io;
    this.changed = changed;
    this.nesFlow = nesFlow;
    adapter = new Fbneo(activity);
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
          .setTitle("继续启动街机游戏")
          .setMessage("RetroArch 切换内容时可能先退出旧实例。可再启动一次；若仍返回，请检查 FinalBurn Neo 核心和归档版本。")
          .setPositiveButton("再次启动", (d, w) -> play(item))
          .setNeutralButton("检查设置", (d, w) -> show())
          .setNegativeButton("取消", null)
          .show();
    } else if (!adapter.verified(item)) {
      new AlertDialog.Builder(activity)
          .setTitle("街机启动结果")
          .setMessage("是否已经进入真实游戏，且投币、开始和方向／动作键正常？白屏报错、只有菜单或仅打开模拟器不算通过。")
          .setPositiveButton(
              "画面与按键正常",
              (d, w) -> {
                try {
                  adapter.confirm(item, fingerprint);
                  changed.run();
                } catch (Exception e) {
                  error(e);
                }
              })
          .setNegativeButton("尚未正常", (d, w) -> show())
          .show();
    }
  }

  void show() {
    String core;
    try {
      core = adapter.retroarch.corePath("fbneo");
    } catch (Exception e) {
      core = "请先安装官方 RetroArch ARM64";
    }
    new AlertDialog.Builder(activity)
        .setTitle("街机 · FinalBurn Neo")
        .setMessage(
            "请在 RetroArch 安装 FinalBurn Neo 核心，并保存默认全局配置。更新核心后请重新验证。\n\n"
                + core
                + "\n\n共享目录："
                + adapter.retroarch.directoryLabel()
                + "\n\n逐个核对 ZIP 内容与官方 DAT；只直启唯一匹配的独立完整集。未知 MOD、缺失父集／BIOS或版本不符会明确报错，原下载保持不变。"
                + "\n\n首次启动后需确认真实画面和输入。存档由 RetroArch 管理，尚无街机云同步或玩家隔离。")
        .setPositiveButton("选择共享目录", (d, w) -> nesFlow.chooseSharedDirectory(this::show))
        .setNeutralButton(
            "打开核心设置",
            (d, w) -> {
              try {
                Intent intent =
                    activity.getPackageManager().getLaunchIntentForPackage(RetroArchPolicy.PACKAGE);
                if (intent == null) {
                  nesFlow.show();
                  return;
                }
                adapter.reset();
                changed.run();
                activity.startActivity(intent);
              } catch (Exception e) {
                error(e);
              }
            })
        .setNegativeButton("返回", null)
        .show();
  }

  void play(DownloadStore.Item item) {
    if (busy) return;
    if (!adapter.retroarch.installed() || !adapter.retroarch.configured()) {
      show();
      return;
    }
    String issue = adapter.issue(item);
    if (!issue.isEmpty()) {
      new AlertDialog.Builder(activity)
          .setTitle("ROM 版本待核对")
          .setMessage(issue)
          .setPositiveButton(
              "重新核对",
              (d, w) -> {
                adapter.clearIssue(item);
                play(item);
              })
          .setNegativeButton("返回", null)
          .show();
      return;
    }
    busy = true;
    AlertDialog progress =
        new AlertDialog.Builder(activity)
            .setMessage("正在校验 ZIP 与 ROM 集，准备启动副本…")
            .setCancelable(false)
            .show();
    io.execute(
        () -> {
          String path = null;
          Exception problem = null;
          try {
            path = adapter.prepare(item);
          } catch (Exception e) {
            problem = e;
          }
          String result = path;
          Exception failure = problem;
          handler.post(
              () -> {
                busy = false;
                if (activity.isDestroyed() || activity.isFinishing()) return;
                progress.dismiss();
                if (failure != null) {
                  changed.run();
                  error(failure);
                  return;
                }
                if (!resumed) return;
                String last =
                    activity.getSharedPreferences("retroarch", 0).getString("lastLaunch", "");
                if (!adapter.verified(item) || (!last.isEmpty() && !last.equals(result))) {
                  new AlertDialog.Builder(activity)
                      .setTitle("启动 FinalBurn Neo")
                      .setMessage(
                          "ROM 集已精确匹配："
                              + new java.io.File(result).getName()
                              + "。请确认核心已安装，并在 RetroArch 保存上一局。切换时若先返回，可再次启动。\n\n存档暂不与云端同步，也不按玩家隔离。")
                      .setPositiveButton("已保存，启动", (d, w) -> open(item, result))
                      .setNegativeButton("取消", null)
                      .show();
                } else open(item, result);
              });
        });
  }

  private void open(DownloadStore.Item item, String path) {
    if (!resumed) return;
    try {
      fingerprint = adapter.fingerprint(item);
      if (fingerprint.isEmpty()) throw new IOException("配置已失效，请重新设置");
      task = item.id;
      started = SystemClock.elapsedRealtime();
      awaiting = true;
      paused = false;
      activity.startActivity(adapter.retroarch.intent(path, "fbneo"));
      RetroApplication.from(activity).downloads.played(item.id);
      activity.getSharedPreferences("retroarch", 0).edit().putString("lastLaunch", path).apply();
    } catch (Exception e) {
      awaiting = false;
      error(e);
    }
  }

  private void error(Exception e) {
    new AlertDialog.Builder(activity)
        .setTitle("街机启动未完成")
        .setMessage(ApiClient.message(e))
        .setPositiveButton("知道了", null)
        .show();
  }
}
