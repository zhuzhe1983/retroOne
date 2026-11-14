package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.widget.*;
import java.io.IOException;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.*;

/** Explicit, quit-before-sync PSP flow. Preview holds no lease; commit rechecks both sides. */
final class SaveSyncFlow {
  static final int PICK_DIRECTORY = 42;
  private static final AtomicBoolean WRITING = new AtomicBoolean();
  private final Activity activity;
  private final ExecutorService io;
  private final ServerConfig source;
  private final JSONObject game;
  private final String player;
  private final android.content.SharedPreferences prefs;
  private final String binding;
  private PspSaveDirectory directory;
  private boolean quitConfirmed;

  SaveSyncFlow(
      Activity activity, ExecutorService io, ServerConfig source, JSONObject game, String player) {
    this.activity = activity;
    this.io = io;
    this.source = source;
    this.game = game;
    this.player = player;
    prefs = activity.getSharedPreferences("psp-save-sync", 0);
    binding = source.key() + "." + game.optString("id");
  }

  void begin() {
    new AlertDialog.Builder(activity)
        .setTitle("同步 PSP 存档 · " + player)
        .setMessage(
            "请先在 PPSSPP 中结束游戏并退出，再比较进度。\n\n本机进度将按「"
                + player
                + "」处理。切换 RetroOne 玩家不会自动切换 PPSSPP 存档，请确认没有选用其他家人的进度。")
        .setNegativeButton("暂不处理", null)
        .setNeutralButton("选择存档目录", (d, w) -> choose())
        .setPositiveButton(
            "已退出，比较存档",
            (d, w) -> {
              quitConfirmed = true;
              String saved = prefs.getString("tree", "");
              if (saved.isEmpty()) choose();
              else prepare(Uri.parse(saved));
            })
        .show();
  }

  private void choose() {
    new AlertDialog.Builder(activity)
        .setTitle("授权 PPSSPP 存档目录")
        .setMessage("选择 PPSSPP 正在使用的 PSP/SAVEDATA 文件夹。只处理接下来指定的游戏，不复制其他游戏的进度。")
        .setNegativeButton("返回", null)
        .setPositiveButton(
            "选择目录",
            (d, w) -> {
              try {
                Intent pick =
                    new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                        .addFlags(
                            Intent.FLAG_GRANT_READ_URI_PERMISSION
                                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
                activity.startActivityForResult(pick, PICK_DIRECTORY);
              } catch (Exception e) {
                error(new IOException("设备未提供目录授权界面"));
              }
            })
        .show();
  }

  void selected(int result, Intent data) {
    if (result != Activity.RESULT_OK || data == null || data.getData() == null) return;
    try {
      int flags =
          data.getFlags()
              & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
      if (flags != (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION))
        throw new IOException("需要存档目录的读写授权");
      activity
          .getContentResolver()
          .takePersistableUriPermission(
              data.getData(),
              Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
      if (quitConfirmed) prepare(data.getData());
      else
        new AlertDialog.Builder(activity)
            .setMessage("目录已授权。请先在 PPSSPP 中结束游戏并退出，再比较存档。")
            .setNegativeButton("暂不处理", null)
            .setPositiveButton(
                "已退出，比较存档",
                (d, w) -> {
                  quitConfirmed = true;
                  prepare(data.getData());
                })
            .show();
    } catch (Exception e) {
      error(e);
    }
  }

  private void prepare(Uri uri) {
    work(
        "正在读取存档目录…",
        () -> {
          directory = new PspSaveDirectory(activity, uri);
          Map<String, String> groups = directory.games();
          prefs.edit().putString("tree", uri.toString()).apply();
          JSONObject cloud = SaveBackup.fetch(source, game.getString("id"));
          String cloudPrefix = SaveSnapshot.prefix(cloud.getJSONArray("files"));
          String savedPrefix = prefs.getString(binding, "");
          if (!cloudPrefix.isEmpty()) {
            if (!savedPrefix.isEmpty() && !cloudPrefix.equals(savedPrefix))
              throw new IOException("云端游戏编号与先前绑定不同，请核对游戏版本后再同步");
            preview(cloudPrefix, cloud);
          } else if (groups.containsKey(savedPrefix)) preview(savedPrefix, cloud);
          else
            activity.runOnUiThread(
                () -> {
                  if (groups.isEmpty()) {
                    error(new IOException("没有找到可识别的游戏内存档，请先在游戏内保存一次"));
                    return;
                  }
                  String[] ids = groups.keySet().toArray(new String[0]);
                  new AlertDialog.Builder(activity)
                      .setTitle("为「" + game.optString("title") + "」选择本机存档")
                      .setItems(
                          groups.values().toArray(new String[0]),
                          (d, w) ->
                              work(
                                  "正在比较存档…",
                                  () ->
                                      preview(
                                          ids[w], SaveBackup.fetch(source, game.getString("id")))))
                      .setNegativeButton("返回", null)
                      .show();
                });
        });
  }

  private void preview(String prefix, JSONObject cloud) throws Exception {
    String remotePrefix = SaveSnapshot.prefix(cloud.getJSONArray("files"));
    if (!remotePrefix.isEmpty() && !prefix.equals(remotePrefix))
      throw new IOException("本机与云端的游戏编号不同，未覆盖；请核对地区和版本");
    JSONObject local = directory.stableRead(prefix);
    boolean equal =
        SaveSnapshot.hash(local.getJSONArray("files"))
            .equals(SaveSnapshot.hash(cloud.getJSONArray("files")));
    if (equal) {
      prefs.edit().putString(binding, prefix).apply();
      activity.runOnUiThread(() -> message("本机与云端存档一致，无须替换。"));
      return;
    }
    SaveLibrary backups = new SaveLibrary(activity.getFilesDir());
    if (local.getJSONArray("files").length() > 0)
      backups.create(source, player, game, local, "local");
    if (cloud.getJSONArray("files").length() > 0) backups.create(source, player, game, cloud);
    activity.runOnUiThread(() -> showChoice(prefix, local, cloud));
  }

  private void showChoice(String prefix, JSONObject local, JSONObject cloud) {
    if (activity.isFinishing() || activity.isDestroyed()) return;
    LinearLayout panel = new LinearLayout(activity);
    panel.setOrientation(LinearLayout.VERTICAL);
    int pad = Math.round(20 * activity.getResources().getDisplayMetrics().density);
    panel.setPadding(pad, pad, pad, pad);
    TextView info = new TextView(activity);
    info.setTextColor(MainActivity.TEXT);
    info.setTextSize(16);
    info.setText(
        "为「"
            + player
            + "」选择要继续的进度\n\n本机 · "
            + describe(local, false)
            + "\n\n云端 · "
            + describe(cloud, true)
            + "\n\n替换前的两份进度已备份，可在「本地内容 → 存档」导出。");
    panel.addView(info);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(panel);
    AlertDialog dialog =
        new AlertDialog.Builder(activity)
            .setTitle("存档不同 · " + game.optString("title"))
            .setView(scroll)
            .setNegativeButton("暂不同步", null)
            .create();
    for (boolean useLocal : new boolean[] {true, false}) {
      if ((useLocal ? local : cloud).optJSONArray("files").length() == 0) continue;
      Button choose = new Button(activity);
      choose.setAllCaps(false);
      choose.setText(useLocal ? "使用本机进度 → 云端" : "使用云端进度 → 本机");
      choose.setTextColor(MainActivity.TEXT);
      android.graphics.drawable.GradientDrawable background =
          new android.graphics.drawable.GradientDrawable();
      background.setColor(MainActivity.PANEL);
      background.setCornerRadius(pad / 2f);
      background.setStroke(2, MainActivity.ACCENT);
      choose.setBackground(background);
      choose.setMinHeight(Math.round(pad * 2.4f));
      LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(-1, -2);
      layout.topMargin = pad / 2;
      choose.setLayoutParams(layout);
      choose.setOnClickListener(
          v -> {
            dialog.dismiss();
            commit(prefix, local, cloud, useLocal);
          });
      panel.addView(choose);
    }
    dialog.show();
  }

  static String describe(JSONObject snapshot, boolean cloud) {
    long bytes = snapshot.optLong("size_bytes");
    String date =
        snapshot.optDouble("updated_at") > 0
            ? java.text.DateFormat.getDateTimeInstance()
                .format(new Date((long) (snapshot.optDouble("updated_at") * 1000)))
            : "时间未知";
    if (snapshot.optJSONArray("files").length() == 0) return "暂无存档";
    return (cloud ? "版本 " + snapshot.optLong("revision") + " · " : "")
        + date
        + "\n"
        + snapshot.optJSONArray("files").length()
        + " 个文件 · "
        + String.format(Locale.ROOT, "%.1f KiB", bytes / 1024.0);
  }

  private void commit(String prefix, JSONObject local, JSONObject cloud, boolean useLocal) {
    work(
        "正在同步，请保持应用前台…",
        () -> {
          if (!WRITING.compareAndSet(false, true)) throw new IOException("另一份存档正在同步，请稍后重试");
          String path = "games/" + ServerConfig.encode(game.getString("id")) + "/cloud-save";
          ApiClient api = new ApiClient(source);
          JSONObject leased = null;
          java.util.concurrent.ScheduledExecutorService heartbeats =
              java.util.concurrent.Executors.newSingleThreadScheduledExecutor();
          java.util.concurrent.atomic.AtomicReference<Exception> heartbeatFailure =
              new java.util.concurrent.atomic.AtomicReference<>();
          try {
            leased =
                api.post(
                    source.api(path + "/lease"),
                    new JSONObject().put("client_id", UUID.randomUUID().toString()),
                    45 * 1024 * 1024);
            String leaseId = leased.getString("lease_id");
            PspSaveDirectory.LeaseCheck check =
                () -> {
                  if (heartbeatFailure.get() != null)
                    throw new IOException("云端占用已失效，停止替换，请重新比较", heartbeatFailure.get());
                  api.post(
                      source.api(path + "/heartbeat"),
                      new JSONObject().put("lease_id", leaseId),
                      16384);
                };
            heartbeats.scheduleWithFixedDelay(
                () -> {
                  try {
                    check.verify();
                  } catch (Exception e) {
                    heartbeatFailure.compareAndSet(null, e);
                  }
                },
                20,
                20,
                java.util.concurrent.TimeUnit.SECONDS);
            SaveBackup.validate(leased.getJSONArray("files"));
            SaveSnapshot.unchanged(cloud, leased, true);
            SaveSnapshot.unchanged(local, directory.stableRead(prefix), false);
            if (useLocal) {
              api.put(
                  source.api(path),
                  new JSONObject()
                      .put("lease_id", leased.getString("lease_id"))
                      .put("base_revision", leased.getLong("revision"))
                      .put("files", local.getJSONArray("files")),
                  65536);
            } else {
              directory.replace(prefix, local, leased.getJSONArray("files"), check);
            }
            prefs.edit().putString(binding, prefix).apply();
          } finally {
            heartbeats.shutdownNow();
            try {
              if (leased != null)
                api.post(
                    source.api(path + "/release"),
                    new JSONObject().put("lease_id", leased.getString("lease_id")),
                    16384);
            } finally {
              WRITING.set(false);
            }
          }
          activity.runOnUiThread(() -> message("已同步本机与云端进度。原版本备份仍保留。"));
        });
  }

  private interface Job {
    void run() throws Exception;
  }

  private void work(String label, Job job) {
    if (activity.isFinishing() || activity.isDestroyed()) return;
    AlertDialog progress =
        new AlertDialog.Builder(activity).setMessage(label).setCancelable(false).create();
    progress.show();
    io.execute(
        () -> {
          try {
            job.run();
          } catch (Exception e) {
            activity.runOnUiThread(() -> error(e));
          } finally {
            activity.runOnUiThread(progress::dismiss);
          }
        });
  }

  private void error(Exception e) {
    message(ApiClient.message(e));
  }

  private void message(String message) {
    if (!activity.isFinishing() && !activity.isDestroyed())
      new AlertDialog.Builder(activity).setMessage(message).setPositiveButton("知道了", null).show();
  }
}
