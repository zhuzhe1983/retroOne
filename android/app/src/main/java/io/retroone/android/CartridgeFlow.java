package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.IOException;
import java.util.List;
import java.util.concurrent.ExecutorService;

/** Reuses the NES path, with independent evidence for every cartridge platform/core pair. */
final class CartridgeFlow {
  private final Activity activity;
  private final ExecutorService io;
  private final Runnable changed;
  private final RetroArchFlow sharedDirectory;
  private final CartridgeAdapter adapter;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog;
  private boolean resumed, busy, awaiting, paused, launchedProbe;
  private String launchedPlatform, launchedId;
  private long started;

  CartridgeFlow(Activity activity, ExecutorService io, Runnable changed, RetroArchFlow directory) {
    this.activity = activity;
    this.io = io;
    this.changed = changed;
    sharedDirectory = directory;
    adapter = new CartridgeAdapter(activity);
  }

  void onPause() {
    resumed = false;
    if (awaiting) paused = true;
  }

  void onResume() {
    resumed = true;
    if (!awaiting || !paused) return;
    awaiting = false;
    String platform = launchedPlatform;
    CartridgePolicy.Spec spec = CartridgePolicy.spec(platform);
    if (spec == null) return;
    if (launchedProbe) {
      handler.post(() -> show(platform));
    } else if (SystemClock.elapsedRealtime() - started < 6000) {
      String id = launchedId;
      handler.post(
          () ->
              new AlertDialog.Builder(activity)
                  .setTitle("继续启动 " + spec.label)
                  .setMessage("RetroArch 切换内容时可能先退出旧实例，可再启动一次。若仍返回，请检查本平台核心与文件格式；测试程序通过不代表所有游戏兼容。")
                  .setPositiveButton(
                      "再次启动",
                      (d, w) -> {
                        DownloadStore.Item item = RetroApplication.from(activity).downloads.get(id);
                        if (item == null) error(new IOException("下载记录已移除"));
                        else play(item);
                      })
                  .setNeutralButton("检查设置", (d, w) -> show(platform))
                  .setNegativeButton("取消", null)
                  .show());
    }
  }

  void show() {
    List<CartridgePolicy.Spec> specs = CartridgePolicy.all();
    String[] labels = new String[specs.size()];
    for (int i = 0; i < specs.size(); i++) {
      CartridgePolicy.Spec spec = specs.get(i);
      labels[i] = spec.label + (adapter.verified(spec) ? " · 已确认" : " · 待测试");
    }
    new AlertDialog.Builder(activity)
        .setTitle("经典卡带 · 平台设置")
        .setItems(labels, (d, which) -> show(specs.get(which).id))
        .setNegativeButton("返回", null)
        .show();
  }

  void show(String platform) {
    CartridgePolicy.Spec spec = CartridgePolicy.spec(platform);
    if (spec == null || activity.isFinishing() || activity.isDestroyed()) return;
    if (dialog != null) dialog.dismiss();
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    int pad = (int) (20 * activity.getResources().getDisplayMetrics().density);
    content.setPadding(pad, pad / 2, pad, pad / 2);
    text(
        content,
        adapter.retroarch.installed() ? "RetroArch ARM64 · 已安装" : "RetroArch ARM64 · 未安装或不可用");
    if (!adapter.retroarch.installed()) {
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
      text(
          content, "1 · 安装 " + coreLabel(spec) + " 核心，允许共享存储访问并保存默认全局配置。测试前请保存并退出 RetroArch 当前游戏。");
      button(
          content,
          "打开核心设置（更新后需重测）",
          () -> {
            try {
              Intent intent =
                  activity.getPackageManager().getLaunchIntentForPackage(RetroArchPolicy.PACKAGE);
              if (intent == null) throw new IOException("RetroArch 入口不可用");
              adapter.resetCore(spec);
              changed.run();
              activity.startActivity(intent);
            } catch (Exception e) {
              error(e);
            }
          });
      text(content, "2 · 共享目录\n" + adapter.retroarch.directoryLabel());
      button(
          content,
          adapter.retroarch.configured() ? "更换共享目录" : "选择共享目录",
          () -> sharedDirectory.chooseSharedDirectory(() -> show(spec.id)));
      text(content, "3 · " + (adapter.verified(spec) ? "本平台测试画面与按键已确认" : "核心和读取权限尚待运行验证"));
      if (adapter.retroarch.configured()) {
        text(content, probeInput(spec));
        if (java.util.List.of("gb", "gbc", "gba").contains(spec.id))
          text(content, "原创测试程序不含厂商启动图案，使用核心内置启动／HLE。若已自行启用外部 BIOS 且测试停在启动画面，请在核心设置中核对；本应用不会修改配置。");
        button(content, "运行 " + spec.label + " 测试", () -> prepare(spec, null));
        if (adapter.pending(spec)) {
          text(content, "看到原创测试画面后，按画面提示验证动作键变色、松开恢复，再确认。只进入模拟器菜单或黑屏不算通过；切换时若先返回，可重新运行测试。");
          button(
              content,
              "我已确认画面与按键正常",
              () -> {
                try {
                  adapter.confirm(spec);
                  changed.run();
                  show(spec.id);
                } catch (Exception e) {
                  error(e);
                }
              });
        }
      }
      button(
          content,
          "格式、核心与存档说明 ⓘ",
          () -> {
            try {
              new AlertDialog.Builder(activity)
                  .setTitle(spec.label + " 启动说明")
                  .setMessage(
                      "核心：\n"
                          + adapter.retroarch.corePath(spec.id)
                          + "\n\n配置：\n"
                          + adapter.retroarch.configPath()
                          + "\n\n"
                          + spec.biosNote
                          + "\n\n"
                          + "支持独立卡带及只含一份对应 ROM 的 ZIP；先校验后提取缓存，原 ZIP 不变。多游戏混装、嵌套压缩及依赖包不自动处理。Android"
                          + " 不允许跨应用检查私有核心，测试需本人确认；更新核心后请重新测试使用它的各个平台。\n\n"
                          + "启动副本与原下载分别保留，原件不改动。按键、画面和存档目录由 RetroArch 默认配置决定；尚无这些平台的云存档或 RetroOne"
                          + " 玩家隔离。")
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
            .setTitle(spec.label + " 模拟器设置")
            .setView(scroll)
            .setPositiveButton("完成", (d, w) -> changed.run())
            .create();
    dialog.show();
  }

  static String coreLabel(CartridgePolicy.Spec spec) {
    return spec.core.replace("_libretro_android.so", "");
  }

  static String probeInput(CartridgePolicy.Spec spec) {
    return switch (spec.id) {
      case "snes" -> "测试操作：按住 RetroArch B，蓝条变红；松开恢复。";
      case "megadrive" -> "测试操作：MD 原生 A 对应 RetroArch Y，按住变红；松开恢复。";
      case "mastersystem", "gamegear" -> "测试操作：原生 II 对应 RetroArch A，按住变红；松开恢复。";
      case "pce" -> "测试操作：PCE 原生 I 对应 RetroArch A，按住变红；松开恢复。";
      default -> "测试操作：按住 RetroArch A，色条变化；松开恢复。";
    };
  }

  void play(DownloadStore.Item item) {
    if (item == null) {
      error(new IOException("下载记录已移除"));
      return;
    }
    CartridgePolicy.Spec spec = CartridgePolicy.spec(item.platform);
    if (spec == null) {
      error(new IOException("此平台尚未适配"));
      return;
    }
    if (!adapter.verified(spec)) {
      show(spec.id);
      return;
    }
    prepare(spec, item);
  }

  private void prepare(CartridgePolicy.Spec spec, DownloadStore.Item item) {
    if (busy) return;
    if (dialog != null) dialog.dismiss();
    String fingerprint = adapter.fingerprint(spec);
    if (fingerprint.isEmpty()) {
      show(spec.id);
      return;
    }
    busy = true;
    AlertDialog progress =
        new AlertDialog.Builder(activity)
            .setMessage("正在校验并准备 " + spec.label + " 启动副本…")
            .setCancelable(false)
            .show();
    io.execute(
        () -> {
          String path = null;
          Exception problem = null;
          try {
            path = item == null ? adapter.stageProbe(spec) : adapter.stageGame(item);
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
                  error(failure);
                  return;
                }
                if (!resumed) return;
                String previous =
                    activity.getSharedPreferences("retroarch", 0).getString("lastLaunch", "");
                Runnable launch = () -> open(spec, result, item, fingerprint);
                if (!previous.isEmpty() && !previous.equals(result)) {
                  new AlertDialog.Builder(activity)
                      .setTitle("切换 RetroArch 内容")
                      .setMessage("请先保存上一局。切换会结束原游戏；若模拟器先返回，可再启动一次。")
                      .setPositiveButton("已保存，继续", (d, w) -> launch.run())
                      .setNegativeButton("取消", null)
                      .show();
                } else launch.run();
              });
        });
  }

  private void open(
      CartridgePolicy.Spec spec, String path, DownloadStore.Item item, String fingerprint) {
    if (!resumed) return;
    try {
      if (!fingerprint.equals(adapter.fingerprint(spec))
          || (item != null && !adapter.verified(spec))) throw new IOException("配置已变化，请重新运行平台测试");
      Intent intent = adapter.retroarch.intent(path, spec.id);
      launchedPlatform = spec.id;
      launchedId = item == null ? null : item.id;
      launchedProbe = item == null;
      started = SystemClock.elapsedRealtime();
      awaiting = true;
      paused = false;
      activity.startActivity(intent);
      if (item == null) adapter.probeLaunched(spec, fingerprint);
      else RetroApplication.from(activity).downloads.played(item.id);
      activity.getSharedPreferences("retroarch", 0).edit().putString("lastLaunch", path).apply();
    } catch (Exception e) {
      awaiting = false;
      error(e);
    }
  }

  private void error(Exception e) {
    new AlertDialog.Builder(activity)
        .setTitle("卡带启动未完成")
        .setMessage(ApiClient.message(e))
        .setPositiveButton("知道了", null)
        .show();
  }

  private void text(LinearLayout target, String value) {
    TextView view = new TextView(activity);
    view.setText(value);
    view.setTextSize(14);
    view.setTextColor(MainActivity.TEXT);
    view.setPadding(0, 12, 0, 12);
    target.addView(view);
  }

  private void button(LinearLayout target, String label, Runnable action) {
    Button view = new Button(activity);
    view.setText(label);
    view.setAllCaps(false);
    view.setOnClickListener(v -> action.run());
    target.addView(view);
  }
}
