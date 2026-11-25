package io.retroone.android;

import android.content.ClipData;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import androidx.core.content.FileProvider;
import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.util.Locale;

/** Exact, stable single-file grants; a Home/Back transition is not proof that Eden exited. */
final class Eden {
  static final String PACKAGE = "dev.eden.eden_emulator";
  static final String ACTIVITY = "org.yuzu.yuzu_emu.activities.EmulationActivity";
  private final Context context;
  private final SharedPreferences prefs;

  Eden(Context context) {
    this.context = context.getApplicationContext();
    prefs = context.getSharedPreferences("eden", Context.MODE_PRIVATE);
  }

  static String formatReason(String name) {
    String lower = name.toLowerCase(Locale.ROOT);
    return lower.endsWith(".nsp") || lower.endsWith(".xci")
        ? ""
        : "Eden 当前只接入独立 NSP / XCI；压缩包、NSZ 与 XCZ 请在模拟器中手动导入。";
  }

  boolean installed() {
    try {
      android.content.pm.ActivityInfo info =
          context.getPackageManager().getActivityInfo(new ComponentName(PACKAGE, ACTIVITY), 0);
      return info.enabled && info.exported && info.applicationInfo.enabled;
    } catch (PackageManager.NameNotFoundException e) {
      return false;
    }
  }

  LaunchPolicy.State status(DownloadStore.Item item, boolean present) {
    if (!DownloadStore.READY.equals(item.state)) return LaunchPolicy.State.INCOMPLETE;
    if (!present) return LaunchPolicy.State.MISSING;
    if (!formatReason(item.name).isEmpty()) return LaunchPolicy.State.MANUAL;
    return installed() ? LaunchPolicy.State.OPEN_EDEN : LaunchPolicy.State.INSTALL_EDEN;
  }

  String activeId() {
    return prefs.getString("task", "");
  }

  String activeTitle() {
    return prefs.getString("title", "Switch 游戏");
  }

  Uri uri(DownloadStore.Item item) throws Exception {
    if (item == null
        || !"switch".equals(item.platform)
        || !DownloadStore.READY.equals(item.state)
        || !formatReason(item.name).isEmpty()) throw new IOException("请选择已下载完成的 Switch NSP / XCI");
    File file = RetroApplication.from(context).downloads.complete(item);
    if (!file.isFile() || file.length() != item.size) throw new IOException("游戏文件不存在或大小已变化");
    // DownloadService verified every piece. Check container identity without hashing many GB on
    // launch.
    try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
      boolean xci = item.name.toLowerCase(Locale.ROOT).endsWith(".xci");
      in.seek(xci ? 0x100 : 0);
      byte[] magic = new byte[4];
      in.readFully(magic);
      String expected = xci ? "HEAD" : "PFS0";
      if (!java.util.Arrays.equals(
          magic, expected.getBytes(java.nio.charset.StandardCharsets.US_ASCII)))
        throw new IOException("镜像头与 NSP / XCI 格式不符，请检查原文件；不会自动改名或解包");
    }
    Uri result = FileProvider.getUriForFile(context, context.getPackageName() + ".games", file);
    try (android.os.ParcelFileDescriptor fd =
        context.getContentResolver().openFileDescriptor(result, "r")) {
      if (fd == null || fd.getStatSize() != item.size) throw new IOException("无法读取完整游戏文件");
    }
    return result;
  }

  Intent intent(Uri uri) {
    Intent intent =
        new Intent(Intent.ACTION_VIEW)
            .setComponent(new ComponentName(PACKAGE, ACTIVITY))
            .setDataAndType(uri, "application/octet-stream")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
    intent.setClipData(ClipData.newRawUri("RetroOne Switch game", uri));
    return intent;
  }

  Intent resumeIntent() {
    // Eden 0.2.1 treats any Intent.data as a ROM swap, even for the same file.
    return new Intent(Intent.ACTION_MAIN)
        .setComponent(new ComponentName(PACKAGE, ACTIVITY))
        .addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                | Intent.FLAG_ACTIVITY_SINGLE_TOP);
  }

  interface Start {
    void run(Intent intent) throws Exception;
  }

  void launch(DownloadStore.Item item, Start start) throws Exception {
    if (item == null) throw new IOException("下载记录已移除，请刷新后重试");
    if (!installed()) throw new IOException("未找到可用的 Eden，请先安装并在其中完成固件与密钥设置");
    if (!activeId().isEmpty() && !activeId().equals(item.id))
      throw new IOException("请先确认已在 Eden 保存并退出上一局，再切换游戏");
    Uri uri = uri(item);
    String old = prefs.getString("uri", "");
    boolean existing = old.equals(uri.toString());
    context.grantUriPermission(PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
    try {
      int uid = context.getPackageManager().getApplicationInfo(PACKAGE, 0).uid;
      if (context.checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION)
          != PackageManager.PERMISSION_GRANTED) throw new IOException("未能授予 Eden 文件读取权限");
      if (!prefs
          .edit()
          .putString("uri", uri.toString())
          .putString("task", item.id)
          .putString("title", item.title)
          .commit()) throw new IOException("未能保存启动授权状态");
      start.run(existing ? resumeIntent() : intent(uri));
    } catch (Exception e) {
      // Never revoke a pre-existing grant: the same game may still be reading in another task.
      if (!existing) {
        context.revokeUriPermission(PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        prefs.edit().remove("uri").remove("task").remove("title").commit();
      }
      throw e;
    }
  }

  void releaseAfterConfirmedExit() throws IOException {
    String value = prefs.getString("uri", "");
    if (!value.isEmpty()) {
      Uri uri = Uri.parse(value);
      // Do not turn a preferences value into a broad or foreign URI revocation.
      if (!"content".equals(uri.getScheme())
          || !(context.getPackageName() + ".games").equals(uri.getAuthority())
          || uri.getQuery() != null) throw new IOException("启动授权记录无效，请保留现场并检查");
      context.revokeUriPermission(PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
    }
    if (!prefs.edit().remove("uri").remove("task").remove("title").commit())
      throw new IOException("授权已回收，但启动记录未能清理，请重试");
  }
}
