package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.DocumentsContract;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.concurrent.ExecutorService;

/** One-time NES setup and explicit recovery for RetroArch 1.22.2's switch-and-exit behavior. */
final class RetroArchFlow {
  static final int PICK_DIRECTORY = 43;
  private final Activity activity;
  private final ExecutorService io;
  private final RetroArch adapter;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private final Runnable changed;
  private AlertDialog dialog;
  private Runnable directoryDone;
  private boolean busy, resumed, launchPaused, awaitingReturn, launchedProbe;
  private String launchedId;
  private long launchedAt;

  RetroArchFlow(Activity activity, ExecutorService io, Runnable changed) {
    this.activity = activity;
    this.io = io;
    this.changed = changed;
    adapter = new RetroArch(activity);
  }

  void onPause() {
    resumed = false;
    if (awaitingReturn) launchPaused = true;
  }

  void onResume() {
    resumed = true;
    if (!awaitingReturn || !launchPaused) return;
    awaitingReturn = false;
    if (launchedProbe) {
      handler.post(this::show);
    } else if (SystemClock.elapsedRealtime() - launchedAt < 6000) {
      handler.post(
          () ->
              new AlertDialog.Builder(activity)
                  .setTitle("继续启动 NES 游戏")
                  .setMessage(
                      "模拟器很快返回了。部分 RetroArch 版本在切换游戏时先退出上一局，需要再启动一次。若仍未显示游戏，请检查 FCEUmm 核心和文件访问权限。")
                  .setPositiveButton(
                      "再次启动",
                      (d, w) -> {
                        DownloadStore.Item item =
                            RetroApplication.from(activity).downloads.get(launchedId);
                        if (item == null) error(new java.io.IOException("下载记录已移除，请重新选择游戏"));
                        else play(item);
                      })
                  .setNeutralButton("检查设置", (d, w) -> show())
                  .setNegativeButton("取消", null)
                  .show());
    }
  }

  void show() {
    if (activity.isFinishing() || activity.isDestroyed()) return;
    if (dialog != null) dialog.dismiss();
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    int pad = (int) (20 * activity.getResources().getDisplayMetrics().density);
    content.setPadding(pad, pad / 2, pad, pad / 2);
    text(content, adapter.installed() ? "RetroArch ARM64 · 已安装" : "RetroArch ARM64 · 未安装或不可用");
    if (!adapter.installed()) {
      text(content, "请安装官方 ARM64 版本。此适配器只开放 NES，其他平台需分别验证。");
      button(
          content,
          "打开官方安装页面",
          () -> {
            try {
              activity.startActivity(
                  new Intent(
                      Intent.ACTION_VIEW, Uri.parse("https://www.retroarch.com/?page=platforms")));
            } catch (Exception e) {
              error(e);
            }
          });
    } else {
      text(content, "1 · 在 RetroArch 安装 FCEUmm 核心，允许读取共享存储，并保存默认全局配置。测试前从其菜单退出应用。");
      button(
          content,
          "打开 RetroArch",
          () -> {
            try {
              Intent intent =
                  activity.getPackageManager().getLaunchIntentForPackage(RetroArchPolicy.PACKAGE);
              if (intent == null) throw new IllegalStateException("模拟器入口不可用");
              activity.startActivity(intent);
            } catch (Exception e) {
              error(e);
            }
          });
      text(content, "2 · 选择共享目录（只需一次）\n" + adapter.directoryLabel());
      button(content, adapter.configured() ? "更换共享目录" : "选择共享目录", this::pick);
      text(content, "仅在目录内创建 RetroOne 专用启动副本；下载原件保持不变。请在内部存储选择 ROMs 等文件夹。");
      text(content, "3 · " + (adapter.verified() ? "已确认测试画面与按键正常" : "核心和读取权限尚待运行验证"));
      if (adapter.configured()) {
        button(content, "运行 NES 测试", () -> prepare(null));
        if (adapter.probePending()) {
          text(content, "看到 RETROONE 和蓝色条、按 A 可以变红后，才确认通过。若刚切换程序便返回本页，请再运行一次测试。");
          button(
              content,
              "我已确认画面与按键正常",
              () -> {
                try {
                  adapter.confirmProbe();
                  changed.run();
                  show();
                } catch (Exception e) {
                  error(e);
                }
              });
        }
      }
      button(
          content,
          "核心路径与说明 ⓘ",
          () -> {
            try {
              new AlertDialog.Builder(activity)
                  .setTitle("NES 启动依赖")
                  .setMessage(
                      "核心路径：\n"
                          + adapter.corePath()
                          + "\n\n默认全局配置：\n"
                          + adapter.configPath()
                          + "\n\n由 RetroArch 读取这个文件里的按键、画面和存档目录；本应用不改写配置。当前未适配另存位置或按核心独立配置。"
                          + "\n\n"
                          + "Android 不允许跨应用检查私有核心文件，安装应用并不代表核心已就绪。测试由你确认，具体游戏兼容性仍取决于核心。更新 RetroArch"
                          + " 或更换目录后需重新测试。\n\n"
                          + "NES 存档由 RetroArch 管理，当前未接入云存档，也不按 RetroOne"
                          + " 玩家隔离。共享副本会保留在选定目录，可退出模拟器后用系统文件管理器清理。")
                  .setPositiveButton("知道了", null)
                  .show();
            } catch (Exception e) {
              error(e);
            }
          });
    }
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(content);
    dialog =
        new AlertDialog.Builder(activity)
            .setTitle("NES 模拟器设置")
            .setView(scroll)
            .setPositiveButton("完成", (d, w) -> changed.run())
            .create();
    dialog.show();
  }

  void chooseSharedDirectory(Runnable done) {
    directoryDone = done;
    pick();
  }

  private void pick() {
    try {
      Intent pick =
          new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
              .addFlags(
                  Intent.FLAG_GRANT_READ_URI_PERMISSION
                      | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                      | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                      | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
      pick.putExtra(
          DocumentsContract.EXTRA_INITIAL_URI,
          DocumentsContract.buildDocumentUri(
              "com.android.externalstorage.documents", "primary:ROMs"));
      activity.startActivityForResult(pick, PICK_DIRECTORY);
    } catch (Exception e) {
      error(e);
    }
  }

  void selected(int result, Intent data) {
    if (result != Activity.RESULT_OK || data == null || data.getData() == null) {
      directoryDone = null;
      return;
    }
    run(
        "正在授权共享目录…",
        () -> {
          adapter.selectDirectory(data.getData(), data.getFlags());
          return "";
        },
        ignored -> {
          changed.run();
          Runnable done = directoryDone;
          directoryDone = null;
          if (done == null) show();
          else done.run();
        });
  }

  void play(DownloadStore.Item item) {
    if (!adapter.verified()) {
      show();
      return;
    }
    prepare(item);
  }

  private void prepare(DownloadStore.Item item) {
    if (busy) return;
    if (dialog != null) dialog.dismiss();
    run(
        "正在校验并准备 NES 启动副本…",
        () -> item == null ? adapter.stageProbe() : adapter.stageGame(item),
        path -> {
          String previous =
              activity.getSharedPreferences("retroarch", 0).getString("lastLaunch", "");
          Runnable launch = () -> open(path, item == null, item == null ? null : item.id);
          if (!previous.isEmpty() && !previous.equals(path)) {
            new AlertDialog.Builder(activity)
                .setTitle("切换 NES 游戏")
                .setMessage("请先在 RetroArch 保存上一局。切换会结束原游戏；若模拟器先返回本页，可再点一次启动。")
                .setPositiveButton("已保存，继续", (d, w) -> launch.run())
                .setNegativeButton("取消", null)
                .show();
          } else launch.run();
        });
  }

  private void open(String path, boolean probe, String id) {
    try {
      if (!resumed) return;
      if (!probe && !adapter.verified()) throw new java.io.IOException("配置已变化，请重新完成 NES 启动测试");
      Intent intent = adapter.intent(path);
      // No kill, QUITFOCUS, config rewrite, or blind delayed restart of someone else's session.
      launchedProbe = probe;
      launchedId = id;
      launchedAt = SystemClock.elapsedRealtime();
      launchPaused = false;
      awaitingReturn = true;
      activity.startActivity(intent);
      if (probe) adapter.probeLaunched();
      if (id != null) RetroApplication.from(activity).downloads.played(id);
      activity.getSharedPreferences("retroarch", 0).edit().putString("lastLaunch", path).apply();
    } catch (Exception e) {
      awaitingReturn = false;
      error(e);
    }
  }

  private interface Work {
    String run() throws Exception;
  }

  private interface Done {
    void run(String value);
  }

  private void run(String label, Work work, Done done) {
    if (busy) return;
    busy = true;
    AlertDialog progress =
        new AlertDialog.Builder(activity).setMessage(label).setCancelable(false).show();
    io.execute(
        () -> {
          String value = null;
          Exception problem = null;
          try {
            value = work.run();
          } catch (Exception e) {
            problem = e;
          }
          String result = value;
          Exception failure = problem;
          handler.post(
              () -> {
                busy = false;
                if (activity.isDestroyed() || activity.isFinishing()) return;
                progress.dismiss();
                if (failure != null) error(failure);
                else if (resumed) done.run(result);
                // Do not launch another app while RetroOne is in the background.
              });
        });
  }

  private void error(Exception e) {
    new AlertDialog.Builder(activity)
        .setTitle("NES 启动未完成")
        .setMessage(ApiClient.message(e))
        .setPositiveButton("知道了", null)
        .show();
  }

  private void text(LinearLayout target, String value) {
    TextView text = new TextView(activity);
    text.setText(value);
    text.setTextSize(14);
    text.setTextColor(MainActivity.TEXT);
    text.setPadding(0, 12, 0, 12);
    target.addView(text);
  }

  private void button(LinearLayout target, String label, Runnable action) {
    Button button = new Button(activity);
    button.setText(label);
    button.setAllCaps(false);
    button.setOnClickListener(v -> action.run());
    target.addView(button);
  }
}
