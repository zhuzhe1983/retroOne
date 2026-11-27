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
import java.io.InputStream;
import java.util.concurrent.ExecutorService;

final class FbneoFlow {
  static final int PICK_BIOS = 44;
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
          .setMessage(
              "RetroArch 切换内容时可能先退出旧实例。可再启动一次；若仍返回，请检查 " + adapter.launchLabel(item) + " 核心和归档版本。")
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
    if (activity.isDestroyed() || activity.isFinishing()) return;
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    int pad = (int) (20 * activity.getResources().getDisplayMetrics().density);
    content.setPadding(pad, pad / 2, pad, pad / 2);
    TextView status = new TextView(activity);
    status.setText(
        "普通街机使用 FinalBurn Neo；特定已验证 MOD 使用 MAME2003-Plus。请先安装对应核心并保存默认配置。\n\n共享目录："
            + adapter.retroarch.directoryLabel()
            + "\n\n"
            + adapter.bios.label());
    content.addView(status);
    AlertDialog dialog =
        new AlertDialog.Builder(activity).setTitle("街机设置").setNegativeButton("返回", null).create();
    button(
        content,
        "选择共享目录",
        () -> {
          dialog.dismiss();
          nesFlow.chooseSharedDirectory(this::show);
        });
    button(
        content,
        "导入 NeoGeo BIOS",
        () -> {
          dialog.dismiss();
          chooseBios();
        });
    button(
        content,
        "打开核心设置",
        () -> {
          try {
            Intent intent =
                activity.getPackageManager().getLaunchIntentForPackage(RetroArchPolicy.PACKAGE);
            dialog.dismiss();
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
        });
    TextView note = new TextView(activity);
    note.setText("ROM 和 BIOS 按内容核验，首次启动仍需确认真实画面和输入。原文件保持不变，存档由 RetroArch 管理，暂不支持街机云同步或玩家隔离。");
    note.setPadding(0, pad / 2, 0, 0);
    content.addView(note);
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

  private void chooseBios() {
    if (busy) return;
    try {
      Intent intent =
          new Intent(Intent.ACTION_OPEN_DOCUMENT)
              .addCategory(Intent.CATEGORY_OPENABLE)
              .setType("*/*")
              .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
      activity.startActivityForResult(intent, PICK_BIOS);
    } catch (Exception e) {
      error(e);
    }
  }

  void selectedBios(int result, Intent data) {
    if (result != Activity.RESULT_OK || data == null || data.getData() == null || busy) return;
    android.net.Uri uri = data.getData();
    if (!"content".equals(uri.getScheme())) {
      error(new IOException("请通过文件选择器选择 BIOS ZIP"));
      return;
    }
    busy = true;
    FbneoBios.Cancellation cancelled = new FbneoBios.Cancellation();
    AlertDialog progress =
        new AlertDialog.Builder(activity)
            .setMessage("正在校验 NeoGeo BIOS…")
            .setCancelable(false)
            .setNegativeButton("取消", (d, w) -> cancelled.cancel())
            .show();
    io.execute(
        () -> {
          Exception problem = null;
          try (InputStream in = activity.getContentResolver().openInputStream(uri)) {
            if (in == null) throw new IOException("无法读取所选 BIOS 文件");
            adapter.bios.importFile(in, adapter.index(), cancelled);
          } catch (Exception e) {
            problem = e;
          }
          Exception failure = problem;
          handler.post(
              () -> {
                busy = false;
                if (activity.isDestroyed() || activity.isFinishing()) return;
                progress.dismiss();
                changed.run();
                if (cancelled.isCancelled()) return;
                if (failure != null) {
                  error(failure);
                  return;
                }
                new AlertDialog.Builder(activity)
                    .setTitle("BIOS 已导入")
                    .setMessage("必需文件已校验。现在可以重新启动 NeoGeo 游戏；原 ZIP 和 RetroArch 全局设置保持不变。")
                    .setPositiveButton("知道了", null)
                    .show();
              });
        });
  }

  private void missingBios(String message) {
    new AlertDialog.Builder(activity)
        .setTitle("需要 NeoGeo BIOS")
        .setMessage(message + "\n\n选择你自己已有的 neogeo.zip；只读校验并保存启动副本。")
        .setPositiveButton("选择 BIOS ZIP", (d, w) -> chooseBios())
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
      if (adapter.needsBios(item)) {
        missingBios(issue);
        return;
      }
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
                  if (failure instanceof FbneoRomSet.MissingBiosException)
                    missingBios(ApiClient.message(failure));
                  else error(failure);
                  return;
                }
                if (!resumed) return;
                changed.run();
                String last =
                    activity.getSharedPreferences("retroarch", 0).getString("lastLaunch", "");
                if (!adapter.verified(item) || (!last.isEmpty() && !last.equals(result))) {
                  new AlertDialog.Builder(activity)
                      .setTitle("启动 " + adapter.launchLabel(item))
                      .setMessage(
                          (adapter.compatibility(item) == null
                                  ? adapter.launchDetails(item)
                                  : "这是已核验摘要的 MOD，使用 MAME2003-Plus 兼容模式。仅副本内部文件名作兼容调整，ROM 字节不变。")
                              + "\n\n"
                              + "请确认核心已安装，并在 RetroArch 保存上一局。切换时若先返回，可再次启动。\n\n"
                              + "存档暂不与云端同步，也不按玩家隔离。")
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
      activity.startActivity(adapter.retroarch.intent(path, adapter.launchPlatform(item)));
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
