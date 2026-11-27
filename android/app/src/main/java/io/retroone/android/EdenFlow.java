package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.SystemClock;

final class EdenFlow {
  private final Activity activity;
  private final Eden adapter;
  private final Runnable changed;
  private boolean resumed, awaiting, paused;
  private long started;

  EdenFlow(Activity activity, Runnable changed) {
    this.activity = activity;
    this.adapter = new Eden(activity);
    this.changed = changed;
  }

  void onPause() {
    resumed = false;
    if (awaiting) paused = true;
  }

  void onResume() {
    resumed = true;
    if (!awaiting || !paused) return;
    awaiting = false;
    if (SystemClock.elapsedRealtime() - started < 8000) {
      new AlertDialog.Builder(activity)
          .setTitle("Eden 已返回")
          .setMessage("尚不能确认游戏是否启动。若黑屏或报错，请检查镜像、固件、密钥和驱动；进入应用不代表游戏兼容。若只是回到桌面，读取授权仍会保留。")
          .setPositiveButton(
              "已退出，重新启动",
              (d, w) -> {
                DownloadStore.Item item =
                    RetroApplication.from(activity).downloads.get(adapter.activeId());
                confirmExit(item == null ? null : () -> play(item));
              })
          .setNeutralButton("已退出游戏，回收授权", (d, w) -> confirmExit(null))
          .setNegativeButton("保留当前状态", null)
          .show();
    }
  }

  void show() {
    AlertDialog.Builder dialog =
        new AlertDialog.Builder(activity)
            .setTitle("Switch · Eden")
            .setMessage(
                (adapter.installed() ? "Eden 已安装。" : "未发现可用的 Eden。")
                    + "\n需要在 Eden 中自行配置合法来源的固件和密钥。RK3588 的性能和图形兼容性因游戏而异；部分大型 3D 游戏可能低帧率或显示异常。"
                    + "\n\n使用已下载原件，无需复制大镜像。存档由 Eden 管理，尚未接入云同步或家庭玩家隔离。"
                    + (adapter.activeId().isEmpty() ? "" : "\n\n保留读取授权：" + adapter.activeTitle()))
            .setPositiveButton(
                adapter.installed() ? "打开 Eden" : "官方安装页面",
                (d, w) -> {
                  try {
                    Intent intent =
                        adapter.installed()
                            ? activity.getPackageManager().getLaunchIntentForPackage(Eden.PACKAGE)
                            : new Intent(Intent.ACTION_VIEW, Uri.parse("https://eden-emu.dev/"));
                    if (intent == null) throw new IllegalStateException("Eden 入口不可用");
                    activity.startActivity(intent);
                  } catch (Exception e) {
                    error(e);
                  }
                })
            .setNegativeButton("返回", null);
    if (!adapter.activeId().isEmpty())
      dialog.setNeutralButton("已退出游戏…", (d, w) -> confirmExit(null));
    dialog.show();
  }

  void play(DownloadStore.Item item) {
    if (!adapter.installed()) {
      show();
      return;
    }
    if (!adapter.activeId().isEmpty() && !adapter.activeId().equals(item.id)) {
      confirmExit(() -> play(item));
      return;
    }
    if (adapter.activeId().equals(item.id)) {
      new AlertDialog.Builder(activity)
          .setTitle("继续 Eden 会话")
          .setMessage("若游戏仍在后台，请继续原会话。若已在 Eden 退出或应用已关闭，请选择重新启动。")
          .setPositiveButton("继续会话", (d, w) -> open(item))
          .setNeutralButton("已退出，重新启动", (d, w) -> confirmExit(() -> play(item)))
          .setNegativeButton("取消", null)
          .show();
      return;
    }
    new AlertDialog.Builder(activity)
        .setTitle("使用 Eden 启动")
        .setMessage(
            "请先在 Eden 配好固件和密钥，并保存、退出其中正在运行的其他游戏。\n\n"
                + "Switch 游戏的速度与图形兼容性因设备和游戏而异。存档由 Eden 管理，暂不与云端同步，也不按 RetroOne 玩家隔离。")
        .setPositiveButton("已配置，启动", (d, w) -> open(item))
        .setNeutralButton("查看设置", (d, w) -> show())
        .setNegativeButton("取消", null)
        .show();
  }

  private void open(DownloadStore.Item item) {
    if (!resumed) return;
    try {
      started = SystemClock.elapsedRealtime();
      awaiting = true;
      paused = false;
      adapter.launch(
          RetroApplication.from(activity).downloads.get(item.id), activity::startActivity);
      RetroApplication.from(activity).downloads.played(item.id);
      changed.run();
    } catch (Exception e) {
      awaiting = false;
      error(e);
    }
  }

  private void resume() {
    DownloadStore.Item item = RetroApplication.from(activity).downloads.get(adapter.activeId());
    if (item == null) {
      show();
      return;
    }
    open(item);
  }

  private void confirmExit(Runnable next) {
    new AlertDialog.Builder(activity)
        .setTitle("确认已结束 Eden 游戏")
        .setMessage(
            "请先在 Eden 中保存并退出「"
                + adapter.activeTitle()
                + "」。只切回桌面不算退出。确认后会回收这份游戏文件的读取授权；不会强制结束模拟器或改动存档。")
        .setPositiveButton(
            "已保存并退出",
            (d, w) -> {
              try {
                adapter.releaseAfterConfirmedExit();
                changed.run();
                if (next != null) next.run();
              } catch (Exception e) {
                error(e);
              }
            })
        .setNegativeButton("尚未退出", null)
        .show();
  }

  private void error(Exception e) {
    new AlertDialog.Builder(activity)
        .setTitle("Eden 启动未完成")
        .setMessage(ApiClient.message(e))
        .setPositiveButton("知道了", null)
        .show();
  }
}
