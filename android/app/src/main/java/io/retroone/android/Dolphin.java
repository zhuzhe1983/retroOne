package io.retroone.android;

import android.content.ClipData;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import androidx.core.content.FileProvider;
import java.io.File;
import java.io.IOException;
import java.util.List;

/** GC-only external adapter. File access lasts until an explicit native-game exit confirmation. */
final class Dolphin {
  static final String PACKAGE = "org.dolphinemu.dolphinemu";
  static final String ACTIVITY = PACKAGE + ".ui.main.MainActivity";
  private static final Object LOCK = new Object();
  private final Context context;
  private final SharedPreferences prefs;

  Dolphin(Context context) {
    this.context = context.getApplicationContext();
    prefs = this.context.getSharedPreferences("dolphin", Context.MODE_PRIVATE);
  }

  boolean installed() {
    try {
      android.content.pm.ActivityInfo info = context.getPackageManager().getActivityInfo(
          new ComponentName(PACKAGE, ACTIVITY), 0);
      return info.exported && info.enabled && info.applicationInfo.enabled;
    } catch (PackageManager.NameNotFoundException e) {
      return false;
    }
  }

  LaunchPolicy.State status(DownloadStore.Item item, boolean present) {
    if (!DownloadStore.READY.equals(item.state)) return LaunchPolicy.State.INCOMPLETE;
    if (!present) return LaunchPolicy.State.MISSING;
    if (!DolphinImage.formatReason(item.platform, item.name).isEmpty())
      return LaunchPolicy.State.MANUAL;
    if (!installed()) return LaunchPolicy.State.INSTALL_DOLPHIN;
    if (item.id.equals(activeId())) return LaunchPolicy.State.RETURN_DOLPHIN;
    return verified(item) ? LaunchPolicy.State.OPEN_DOLPHIN : LaunchPolicy.State.CHECK_DOLPHIN;
  }

  String activeId() { return prefs.getString("task", ""); }
  String activeTitle() { return prefs.getString("title", "GameCube 游戏"); }
  String activeUri() { return prefs.getString("uri", ""); }
  String trialFingerprint() { return prefs.getString("trial", ""); }

  private File source(DownloadStore.Item item) throws IOException {
    if (item == null || !DolphinImage.supports(item.platform)
        || !DownloadStore.READY.equals(item.state) || item.size <= 0)
      throw new IOException("请选择已完整下载的 GameCube 游戏");
    String reason = DolphinImage.formatReason(item.platform, item.name);
    if (!reason.isEmpty()) throw new IOException(reason);
    if (!item.id.matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"))
      throw new IOException("下载任务标识无效");
    DownloadStore store = RetroApplication.from(context).downloads;
    DownloadStore.Item current = store.get(item.id);
    if (current == null || !DownloadStore.READY.equals(current.state)
        || !current.manifest.equals(item.manifest) || !current.name.equals(item.name)
        || current.size != item.size || !current.platform.equals(item.platform))
      throw new IOException("下载记录已变化，请刷新后重试");
    File directory = new File(store.root, item.id);
    if (!directory.isDirectory()
        || !directory.getCanonicalFile().equals(new File(store.root.getCanonicalFile(), item.id))
        || !directory.getCanonicalFile().getParentFile().equals(store.root.getCanonicalFile()))
      throw new IOException("游戏目录不存在或路径无效");
    File file = store.complete(item);
    if (!file.getCanonicalFile().equals(new File(directory.getCanonicalFile(), item.name))
        || !directory.getCanonicalFile().equals(file.getCanonicalFile().getParentFile())
        || !file.isFile() || file.length() != item.size)
      throw new IOException("游戏文件不存在、路径无效或大小已变化");
    return file;
  }

  String fingerprint(DownloadStore.Item item) {
    try {
      if (!installed()) return "";
      File file = source(item);
      PackageInfo info = context.getPackageManager().getPackageInfo(PACKAGE, 0);
      long version = android.os.Build.VERSION.SDK_INT >= 28
          ? info.getLongVersionCode() : info.versionCode;
      return ServerConfig.hash("dolphin-gc-v1:" + version + ":" + info.lastUpdateTime + ":"
          + item.id + ":" + item.manifest + ":" + item.name + ":" + item.platform + ":"
          + file.length() + ":" + file.lastModified());
    } catch (IOException | PackageManager.NameNotFoundException e) { return ""; }
  }

  boolean verified(DownloadStore.Item item) {
    String value = fingerprint(item);
    return !value.isEmpty() && value.equals(prefs.getString("verified." + item.id, ""));
  }

  void confirm(DownloadStore.Item item, String trial) throws IOException {
    if (item == null || !item.id.equals(activeId()) || trial == null || trial.isEmpty()
        || !trial.equals(trialFingerprint()) || !trial.equals(fingerprint(item)))
      throw new IOException("文件或模拟器已变化，请重新试运行；不会自动确认兼容性");
    if (!prefs.edit().putString("verified." + item.id, trial).commit())
      throw new IOException("未能保存本款游戏的确认结果");
  }

  static final class Prepared {
    final Uri uri;
    final String fingerprint;
    private Prepared(Uri uri, String fingerprint) {
      this.uri = uri;
      this.fingerprint = fingerprint;
    }
  }

  Prepared prepare(DownloadStore.Item item) throws Exception {
    if (Thread.currentThread().isInterrupted()) throw new IOException("准备已取消");
    String before = fingerprint(item);
    if (before.isEmpty()) throw new IOException("请先安装 Dolphin 并保留完整下载文件");
    File file = source(item);
    DolphinImage.inspect(file, item.platform, item.name);
    Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".games", file);
    validateUri(uri);
    try (android.os.ParcelFileDescriptor fd = context.getContentResolver().openFileDescriptor(uri, "r")) {
      if (fd == null || fd.getStatSize() != item.size) throw new IOException("无法读取完整镜像");
    }
    if (Thread.currentThread().isInterrupted()) throw new IOException("准备已取消");
    if (!before.equals(fingerprint(item))) throw new IOException("文件或模拟器在检查时发生变化，请重试");
    return new Prepared(uri, before);
  }

  private void validateUri(Uri uri) throws IOException {
    if (uri == null || !"content".equals(uri.getScheme())
        || !(context.getPackageName() + ".games").equals(uri.getAuthority())
        || uri.getQuery() != null || uri.getFragment() != null)
      throw new IOException("游戏文件授权地址无效");
    List<String> p = uri.getPathSegments();
    if (p.size() != 3 || !"games".equals(p.get(0))
        || !p.get(1).matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
        || !p.get(2).matches("[A-Za-z0-9_-]{1,128}\\.[A-Za-z0-9]{1,8}")
        || !uri.getEncodedPath().equals("/games/" + p.get(1) + "/" + p.get(2)))
      throw new IOException("只允许授权当前下载任务的单个文件");
  }

  Intent coldIntent(Uri uri) throws IOException {
    validateUri(uri);
    Intent intent = new Intent(Intent.ACTION_VIEW)
        .setComponent(new ComponentName(PACKAGE, ACTIVITY))
        .setDataAndType(uri, "application/octet-stream")
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP
            | Intent.FLAG_GRANT_READ_URI_PERMISSION);
    intent.setClipData(ClipData.newRawUri("RetroOne GameCube game", uri));
    return intent;
  }

  Intent openIntent() {
    // This opens Main over a retained Emu on 2609b. The UI explicitly asks for system Back.
    return new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        .setComponent(new ComponentName(PACKAGE, ACTIVITY))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
  }

  interface Start { void run(Intent intent) throws Exception; }

  void launch(DownloadStore.Item item, Prepared prepared, Start start) throws Exception {
    DownloadStore store = RetroApplication.from(context).downloads;
    // Serialize with removal: no file can be deleted between the last identity check and grant.
    synchronized (store) {
      synchronized (LOCK) {
        if (!activeId().isEmpty() || !activeUri().isEmpty())
          throw new IOException("请先在 Dolphin 保存并退出上一局，再确认回收授权");
        if (!installed() || prepared == null || prepared.fingerprint.isEmpty()
            || !prepared.fingerprint.equals(fingerprint(item)))
          throw new IOException("文件或模拟器已变化，请重新准备");
        Uri expected = FileProvider.getUriForFile(context, context.getPackageName() + ".games", source(item));
        if (!expected.equals(prepared.uri)) throw new IOException("游戏文件地址已变化");
        Intent intent = coldIntent(prepared.uri);
        int uid = context.getPackageManager().getApplicationInfo(PACKAGE, 0).uid;
        if (context.checkUriPermission(prepared.uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            == PackageManager.PERMISSION_GRANTED
            || context.checkUriPermission(prepared.uri, -1, uid, Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                == PackageManager.PERMISSION_GRANTED)
          throw new IOException("此文件还有未回收的 Dolphin 授权，请先结束此前测试或游戏会话");
        try {
          if (!prefs.edit().putString("task", item.id).putString("uri", prepared.uri.toString())
              .putString("title", item.title).putString("trial", prepared.fingerprint).commit())
            throw new IOException("未能保存启动状态，尚未启动游戏");
          context.grantUriPermission(PACKAGE, prepared.uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
          if (context.checkUriPermission(prepared.uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION)
              != PackageManager.PERMISSION_GRANTED) throw new IOException("未能授予 Dolphin 读取权限");
          start.run(intent);
        } catch (Exception e) {
          context.revokeUriPermission(PACKAGE, prepared.uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
          clearActive();
          throw e;
        }
      }
    }
  }

  void open(Start start) throws Exception {
    if (!installed()) throw new IOException("未找到可用的 Dolphin");
    // Never regrant, revoke, or replay a ROM URI to simulate resuming.
    start.run(openIntent());
  }

  void releaseAfterConfirmedExit() throws IOException {
    synchronized (LOCK) {
      String value = activeUri();
      if (!value.isEmpty()) {
        Uri uri = Uri.parse(value);
        validateUri(uri);
        if (!uri.getPathSegments().get(1).equals(activeId()))
          throw new IOException("启动授权记录不一致，请保留现场检查");
        context.revokeUriPermission(PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
      }
      clearActive();
    }
  }

  private void clearActive() throws IOException {
    if (!prefs.edit().remove("task").remove("uri").remove("title").remove("trial").commit())
      throw new IOException("读取授权已回收，但会话记录未能清理，请重试");
  }
}
