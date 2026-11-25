package io.retroone.android;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Environment;
import android.provider.DocumentsContract;
import android.provider.Settings;
import androidx.documentfile.provider.DocumentFile;
import java.io.*;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.UUID;
import org.json.JSONObject;

/** NES only. A granted shared directory bridges RetroArch's native-path launch API. */
final class RetroArch {
  private final Context context;
  private final SharedPreferences preferences;

  RetroArch(Context context) {
    this.context = context.getApplicationContext();
    preferences = context.getSharedPreferences("retroarch", Context.MODE_PRIVATE);
  }

  boolean installed() {
    if (!Arrays.asList(android.os.Build.SUPPORTED_ABIS).contains("arm64-v8a")) return false;
    try {
      android.content.pm.ActivityInfo info =
          context
              .getPackageManager()
              .getActivityInfo(
                  new ComponentName(RetroArchPolicy.PACKAGE, RetroArchPolicy.ACTIVITY), 0);
      return info.enabled && info.exported && info.applicationInfo.enabled;
    } catch (PackageManager.NameNotFoundException e) {
      return false;
    }
  }

  String corePath() throws PackageManager.NameNotFoundException {
    return corePath("nes");
  }

  String corePath(String platform) throws PackageManager.NameNotFoundException {
    if (!platform.equals("nes") && !platform.equals("fbneo"))
      throw new IllegalArgumentException("Unsupported core");
    return context.getPackageManager().getApplicationInfo(RetroArchPolicy.PACKAGE, 0).dataDir
        + "/cores/"
        + (platform.equals("fbneo") ? "fbneo_libretro_android.so" : RetroArchPolicy.CORE);
  }

  String externalFilesPath() {
    return new File(
            Environment.getExternalStorageDirectory(),
            "Android/data/" + RetroArchPolicy.PACKAGE + "/files")
        .getAbsolutePath();
  }

  String configPath() {
    // This adapter supports the official sideloaded build's default global configuration.
    // Scoped storage prevents inspecting RetroArch's private preferences/config selection.
    return new File(externalFilesPath(), "retroarch.cfg").getAbsolutePath();
  }

  Intent withEnvironment(Intent intent) throws Exception {
    if (!Environment.MEDIA_MOUNTED.equals(Environment.getExternalStorageState()))
      throw new IOException("共享存储不可用，无法读取 RetroArch 默认配置");
    android.content.pm.ApplicationInfo target =
        context.getPackageManager().getApplicationInfo(RetroArchPolicy.PACKAGE, 0);
    String ime =
        Settings.Secure.getString(
            context.getContentResolver(), Settings.Secure.DEFAULT_INPUT_METHOD);
    // Match v1.22.2 MainMenuActivity.startRetroActivity, using the target app's paths.
    // Passing only ROM/core skips these defaults in that version. Do not write the cfg.
    return intent
        .putExtra("CONFIGFILE", configPath())
        .putExtra("IME", ime == null ? "" : ime)
        .putExtra("DATADIR", target.dataDir)
        .putExtra("APK", target.sourceDir)
        .putExtra("SDCARD", Environment.getExternalStorageDirectory().getAbsolutePath())
        .putExtra("EXTERNAL", externalFilesPath());
  }

  String directoryLabel() {
    return preferences.getString("folderPath", "尚未选择共享目录");
  }

  boolean configured() {
    String tree = preferences.getString("tree", "");
    if (tree.isEmpty() || preferences.getString("folderUri", "").isEmpty()) return false;
    for (android.content.UriPermission grant :
        context.getContentResolver().getPersistedUriPermissions())
      if (grant.getUri().toString().equals(tree)
          && grant.isReadPermission()
          && grant.isWritePermission()) return true;
    return false;
  }

  String fingerprint() {
    if (!installed() || !configured()) return "";
    try {
      PackageInfo info = context.getPackageManager().getPackageInfo(RetroArchPolicy.PACKAGE, 0);
      return ServerConfig.hash(
          (android.os.Build.VERSION.SDK_INT >= 28 ? info.getLongVersionCode() : info.versionCode)
              + ":"
              + info.lastUpdateTime
              + ":"
              + corePath()
              + ":"
              + preferences.getString("folderUri", "")
              + ":nes-probe-v2:launch-env-v1:"
              + configPath());
    } catch (PackageManager.NameNotFoundException e) {
      return "";
    }
  }

  boolean verified() {
    String current = fingerprint();
    return !current.isEmpty() && current.equals(preferences.getString("verified", ""));
  }

  boolean probePending() {
    String current = fingerprint();
    return !current.isEmpty() && current.equals(preferences.getString("probe", ""));
  }

  void probeLaunched() {
    preferences.edit().putString("probe", fingerprint()).remove("verified").apply();
  }

  void confirmProbe() throws IOException {
    if (!probePending()) throw new IOException("配置已变化，请重新运行 NES 测试");
    preferences.edit().putString("verified", fingerprint()).remove("probe").apply();
  }

  void invalidateProbe() {
    preferences.edit().remove("verified").remove("probe").apply();
  }

  void selectDirectory(Uri tree, int resultFlags) throws Exception {
    if (!"content".equals(tree.getScheme())
        || !"com.android.externalstorage.documents".equals(tree.getAuthority()))
      throw new IOException("请选择系统文件选择器中的内部存储文件夹，暂不支持其他文件提供方");
    String relative = RetroArchPolicy.relativeDirectory(DocumentsContract.getTreeDocumentId(tree));
    int flags =
        resultFlags
            & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
    if (flags != (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION))
      throw new IOException("共享目录需要读写授权");
    context.getContentResolver().takePersistableUriPermission(tree, flags);
    DocumentFile parent = DocumentFile.fromTreeUri(context, tree);
    if (parent == null || !parent.isDirectory() || !parent.canWrite() || !parent.canRead())
      throw new IOException("目录不可读写，请重新选择");
    // Re-selecting the same directory renews permission without orphaning its managed copies.
    if (tree.toString().equals(preferences.getString("tree", ""))
        && !preferences.getString("folderUri", "").isEmpty()) {
      DocumentFile old =
          DocumentFile.fromSingleUri(context, Uri.parse(preferences.getString("folderUri", "")));
      if (old != null && old.exists()) {
        folder();
        invalidateProbe();
        return;
      }
      // An explicitly reselected parent can replace a removed managed folder.
    }
    String name = "RetroOne-Launch-" + UUID.randomUUID().toString().substring(0, 8);
    if (parent.findFile(name) != null) throw new IOException("目录名冲突，请重试");
    DocumentFile child = parent.createDirectory(name);
    if (child == null || !child.isDirectory()) throw new IOException("无法创建启动副本目录");
    String path = nativePath(child.getUri());
    String expected =
        new File(Environment.getExternalStorageDirectory(), relative + "/" + name)
            .getAbsolutePath();
    if (!path.equals(expected)) throw new IOException("文件提供方返回了意外路径，未启用此目录");
    boolean saved =
        preferences
            .edit()
            .putString("tree", tree.toString())
            .putString("folderUri", child.getUri().toString())
            .putString("folderPath", path)
            .remove("verified")
            .remove("probe")
            .commit();
    if (!saved) throw new IOException("目录设置未能保存，请释放应用存储空间后重试");
  }

  private String nativePath(Uri document) throws IOException {
    if (!"com.android.externalstorage.documents".equals(document.getAuthority()))
      throw new IOException("共享文件来源无效");
    String relative = RetroArchPolicy.relativeDirectory(DocumentsContract.getDocumentId(document));
    return new File(Environment.getExternalStorageDirectory(), relative).getAbsolutePath();
  }

  private DocumentFile folder() throws IOException {
    if (!configured()) throw new IOException("共享目录授权已失效，请重新选择目录并测试启动");
    Uri uri = Uri.parse(preferences.getString("folderUri", ""));
    DocumentFile result = DocumentFile.fromSingleUri(context, uri);
    if (result == null
        || !result.isDirectory()
        || !result.canRead()
        || !result.canWrite()
        || !nativePath(uri).equals(directoryLabel()))
      throw new IOException("启动副本目录已移走或不可读写，请重新设置共享目录");
    return result;
  }

  private DocumentFile findChild(DocumentFile parent, String name) throws IOException {
    Uri children =
        DocumentsContract.buildChildDocumentsUriUsingTree(
            parent.getUri(), DocumentsContract.getDocumentId(parent.getUri()));
    try (android.database.Cursor cursor =
        context
            .getContentResolver()
            .query(
                children,
                new String[] {
                  DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                  DocumentsContract.Document.COLUMN_DISPLAY_NAME
                },
                null,
                null,
                null)) {
      if (cursor == null) throw new IOException("无法读取共享目录");
      while (cursor.moveToNext())
        if (name.equals(cursor.getString(1)))
          return DocumentFile.fromSingleUri(
              context,
              DocumentsContract.buildDocumentUriUsingTree(parent.getUri(), cursor.getString(0)));
      return null;
    }
  }

  File probeFile() throws Exception {
    File directory = new File(context.getCacheDir(), "emulator-probe");
    if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法准备测试程序");
    File file = new File(directory, "retroone-nes-probe.nes");
    try (InputStream in = context.getAssets().open("retroone-nes-probe.nes");
        OutputStream out = new FileOutputStream(file)) {
      copy(in, out);
    }
    return file;
  }

  String stageProbe() throws Exception {
    return stage(probeFile());
  }

  String stageGame(DownloadStore.Item item) throws Exception {
    if (item == null
        || !RetroArchPolicy.supports(item.platform)
        || !RetroArchPolicy.formatReason(item.name).isEmpty())
      throw new IOException("此格式尚未适配 NES 直启");
    if (!verified()) throw new IOException("请先完成 NES 启动测试");
    return stage(verifiedDownload(item));
  }

  File verifiedDownload(DownloadStore.Item item) throws Exception {
    if (!DownloadStore.READY.equals(item.state)) throw new IOException("请等待下载完成");
    File file = RetroApplication.from(context).downloads.complete(item);
    if (!file.isFile() || file.length() != item.size) throw new IOException("下载文件不存在或大小已变化");
    DownloadManifest manifest =
        new DownloadManifest(
            new JSONObject(item.manifest), new ServerConfig(item.base, item.http, ""));
    try (RandomAccessFile input = new RandomAccessFile(file, "r")) {
      if (Transfer.verifiedPrefix(input, manifest, () -> Thread.currentThread().isInterrupted())
          != item.size) throw new IOException("下载文件校验失败，请重新下载");
    }
    return file;
  }

  String stageArchive(File source, String set) throws Exception {
    if (!set.matches("[A-Za-z0-9_-]{1,64}")) throw new IOException("ROM 集名称无效");
    String hash;
    try (InputStream in = new FileInputStream(source)) {
      hash = digest(in, source.length());
    }
    DocumentFile parent = folder();
    String childName = "fb-" + hash;
    DocumentFile child = findChild(parent, childName);
    if (child == null) {
      Uri uri =
          DocumentsContract.createDocument(
              context.getContentResolver(),
              parent.getUri(),
              DocumentsContract.Document.MIME_TYPE_DIR,
              childName);
      child = uri == null ? null : DocumentFile.fromSingleUri(context, uri);
    }
    String path = directoryLabel() + "/" + childName;
    if (child == null || !child.isDirectory() || !nativePath(child.getUri()).equals(path))
      throw new IOException("无法准备街机启动目录");
    return stageDocument(source, child, set + ".zip", path, hash);
  }

  private String stage(File source) throws Exception {
    byte[] header = new byte[16];
    try (DataInputStream in = new DataInputStream(new FileInputStream(source))) {
      in.readFully(header);
    }
    RetroArchPolicy.validateHeader(header, source.length());
    String hash;
    try (InputStream in = new FileInputStream(source)) {
      hash = digest(in, source.length());
    }
    return stageDocument(source, folder(), RetroArchPolicy.fileName(hash), directoryLabel(), hash);
  }

  private String stageDocument(
      File source, DocumentFile directory, String name, String expectedDirectory, String hash)
      throws Exception {
    DocumentFile existing = findChild(directory, name);
    if (existing != null) {
      try (InputStream in = context.getContentResolver().openInputStream(existing.getUri())) {
        if (existing.isFile() && hash.equals(digest(in, source.length())))
          return validatedPath(existing, name, expectedDirectory);
      }
      // Never replace an unexpected file, even inside our own managed directory.
      throw new IOException("共享副本已被修改，请移走此副本后重试：" + name);
    }
    Uri created =
        DocumentsContract.createDocument(
            context.getContentResolver(),
            directory.getUri(),
            "application/octet-stream",
            "pending-" + UUID.randomUUID() + ".tmp");
    DocumentFile staged = created == null ? null : DocumentFile.fromSingleUri(context, created);
    if (staged == null) throw new IOException("共享目录无法写入");
    Uri cleanupUri = created;
    boolean complete = false;
    Exception failure = null;
    try {
      try (InputStream in = new FileInputStream(source);
          OutputStream out = context.getContentResolver().openOutputStream(staged.getUri(), "wt")) {
        if (out == null) throw new IOException("无法写入启动副本");
        copy(in, out);
      }
      try (InputStream in = context.getContentResolver().openInputStream(staged.getUri())) {
        if (!hash.equals(digest(in, source.length()))) throw new IOException("启动副本校验失败");
      }
      // SingleDocumentFile deliberately does not implement renameTo(). The provider may
      // return a different document ID, so all further checks and cleanup use that URI.
      Uri renamed =
          DocumentsContract.renameDocument(context.getContentResolver(), staged.getUri(), name);
      if (renamed == null) throw new IOException("无法启用启动副本");
      cleanupUri = renamed;
      DocumentFile ready = DocumentFile.fromSingleUri(context, renamed);
      if (ready == null) throw new IOException("无法读取重命名后的启动副本");
      String path = validatedPath(ready, name, expectedDirectory);
      complete = true;
      return path;
    } catch (Exception e) {
      failure = e;
      throw e;
    } finally {
      if (!complete) {
        try {
          if (!DocumentsContract.deleteDocument(context.getContentResolver(), cleanupUri))
            throw new IOException("未能清理本次启动临时副本");
        } catch (Exception cleanupFailure) {
          if (failure != null) failure.addSuppressed(cleanupFailure);
          else throw cleanupFailure;
        }
      }
    }
  }

  private String validatedPath(DocumentFile file, String name, String expectedDirectory)
      throws IOException {
    String path = nativePath(file.getUri());
    if (!name.equals(file.getName()) || !path.equals(expectedDirectory + "/" + name))
      throw new IOException("启动副本的实际路径与预期不符");
    return path;
  }

  Intent intent(String path) throws Exception {
    return intent(path, "nes");
  }

  Intent intent(String path, String platform) throws Exception {
    if (!installed()) throw new IOException("未找到可用的 RetroArch ARM64");
    if (!configured()
        || !path.startsWith(directoryLabel() + "/")
        || !path.substring(directoryLabel().length() + 1)
            .matches(
                platform.equals("fbneo")
                    ? "fb-[a-f0-9]{64}/[A-Za-z0-9_-]{1,64}\\.zip"
                    : "ro-[a-f0-9]{64}\\.nes")) throw new IOException("启动路径必须是已授权目录中的校验副本");
    // Load RetroArch's default global config; save/input paths come from that config.
    return withEnvironment(
        new Intent(Intent.ACTION_MAIN)
            .setComponent(new ComponentName(RetroArchPolicy.PACKAGE, RetroArchPolicy.ACTIVITY))
            .putExtra("ROM", path)
            .putExtra("LIBRETRO", corePath(platform)));
  }

  private static void copy(InputStream in, OutputStream out) throws IOException {
    byte[] buffer = new byte[65536];
    int n;
    while ((n = in.read(buffer)) != -1) {
      if (Thread.currentThread().isInterrupted()) throw new IOException("准备已取消");
      out.write(buffer, 0, n);
    }
  }

  private static String digest(InputStream in, long size) throws Exception {
    if (in == null) throw new IOException("无法读取启动副本");
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    byte[] buffer = new byte[65536];
    int n;
    long total = 0;
    while ((n = in.read(buffer)) != -1) {
      if (Thread.currentThread().isInterrupted()) throw new IOException("校验已取消");
      total += n;
      if (total > size) throw new IOException("启动副本大小不符");
      digest.update(buffer, 0, n);
    }
    if (total != size) throw new IOException("启动副本不完整");
    StringBuilder result = new StringBuilder();
    for (byte b : digest.digest())
      result.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
    return result.toString();
  }
}
