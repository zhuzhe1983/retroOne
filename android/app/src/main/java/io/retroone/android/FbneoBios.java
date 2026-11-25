package io.retroone.android;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.*;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.security.MessageDigest;
import org.json.JSONObject;

/** User-selected BIOS, copied privately only after validation. Never changes RetroArch globals. */
final class FbneoBios {
  static final long MAX_BYTES = 16L * 1024 * 1024;
  private static final Object LOCK = new Object();
  private final File directory;
  private final SharedPreferences prefs;

  static final class Cancellation {
    private volatile boolean cancelled;
    private boolean committed;

    synchronized boolean cancel() {
      if (committed) return false;
      cancelled = true;
      return true;
    }

    boolean isCancelled() {
      return cancelled;
    }
  }

  FbneoBios(Context context) {
    directory = new File(context.getFilesDir(), "fbneo-bios");
    prefs = context.getSharedPreferences("fbneo-bios", Context.MODE_PRIVATE);
  }

  String fingerprint() {
    String hash = prefs.getString("active", "");
    if (!hash.matches("[a-f0-9]{64}")) return "none";
    File file = new File(directory, "neogeo-" + hash + ".zip");
    return hash + ":" + file.length() + ":" + file.lastModified();
  }

  String label() {
    String hash = prefs.getString("active", "");
    File file = new File(directory, "neogeo-" + hash + ".zip");
    return hash.matches("[a-f0-9]{64}") && file.isFile()
        ? "NeoGeo BIOS · 已导入（启动时复核）"
        : "NeoGeo BIOS · 尚未导入";
  }

  File file() throws Exception {
    synchronized (LOCK) {
      String hash = prefs.getString("active", "");
      if (hash.isEmpty()) return null;
      if (!hash.matches("[a-f0-9]{64}")) throw new IOException("BIOS 配置无效，请重新导入");
      checkDirectory();
      File file = new File(directory, "neogeo-" + hash + ".zip");
      if (!Files.isRegularFile(file.toPath(), LinkOption.NOFOLLOW_LINKS)
          || !hash.equals(hash(file))) throw new IOException("BIOS 副本已变化，请重新导入");
      return file;
    }
  }

  FbneoRomSet.Bios importFile(InputStream source, JSONObject index, Cancellation cancelled)
      throws Exception {
    synchronized (LOCK) {
      checkDirectory();
      File pending = File.createTempFile("pending-", ".tmp", directory);
      File published = null;
      boolean selected = false;
      try {
        try (FileOutputStream out = new FileOutputStream(pending)) {
          byte[] block = new byte[32768];
          long count = 0;
          int n;
          while ((n = source.read(block)) != -1) {
            checkCancelled(cancelled);
            count += n;
            if (count > MAX_BYTES) throw new IOException("BIOS ZIP 超过 16 MiB，请核对选择的文件");
            out.write(block, 0, n);
          }
          out.getFD().sync();
        }
        checkCancelled(cancelled);
        FbneoRomSet.Bios result = FbneoRomSet.verifyBios(pending, index);
        File target = new File(directory, "neogeo-" + result.sha256 + ".zip");
        checkCancelled(cancelled);
        if (Files.exists(target.toPath(), LinkOption.NOFOLLOW_LINKS)) {
          if (!Files.isRegularFile(target.toPath(), LinkOption.NOFOLLOW_LINKS)
              || !result.sha256.equals(hash(target)))
            throw new IOException("已有 BIOS 副本内容不符，不会覆盖；请检查应用本地内容");
        } else {
          // Same private directory, serialized above; no overwrite or SELinux-forbidden links.
          Files.move(pending.toPath(), target.toPath());
          published = target;
        }
        synchronized (cancelled) {
          checkCancelled(cancelled);
          String old = prefs.getString("active", "");
          if (!prefs.edit().putString("active", result.sha256).commit()) {
            prefs.edit().putString("active", old).commit();
            throw new IOException("未能保存 BIOS 设置；原设置保持可用");
          }
          selected = true;
          cancelled.committed = true;
        }
        return result;
      } finally {
        Files.deleteIfExists(pending.toPath());
        if (!selected && published != null) Files.deleteIfExists(published.toPath());
      }
    }
  }

  private void checkDirectory() throws IOException {
    if (Files.isSymbolicLink(directory.toPath())) throw new IOException("BIOS 目录不能是符号链接");
    if (!directory.isDirectory() && !directory.mkdir()) throw new IOException("无法创建 BIOS 私有目录");
  }

  private static void checkCancelled(Cancellation cancelled) throws IOException {
    if (cancelled.isCancelled() || Thread.currentThread().isInterrupted())
      throw new IOException("已取消导入，原 BIOS 设置保持不变");
  }

  private static String hash(File file) throws Exception {
    if (file.length() > MAX_BYTES) throw new IOException("BIOS 副本大小异常");
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (InputStream in = new FileInputStream(file)) {
      byte[] block = new byte[32768];
      long count = 0;
      int n;
      while ((n = in.read(block)) != -1) {
        if ((count += n) > MAX_BYTES) throw new IOException("BIOS 副本大小异常");
        digest.update(block, 0, n);
      }
    }
    return FbneoArchive.hex(digest.digest());
  }
}
