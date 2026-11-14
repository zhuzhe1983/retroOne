package io.retroone.android;

import android.content.Context;
import android.net.Uri;
import androidx.documentfile.provider.DocumentFile;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.json.*;

/**
 * User-granted SAVEDATA only. No all-files permission and no shared-folder wholesale replacement.
 */
final class PspSaveDirectory {
  final Context context;
  final DocumentFile root;
  private final File journal;

  PspSaveDirectory(Context context, Uri uri) throws Exception {
    this.context = context;
    DocumentFile selected = DocumentFile.fromTreeUri(context, uri);
    if (selected == null || !selected.isDirectory()) throw new IOException("目录授权已失效，请重新选择");
    if ("PSP".equalsIgnoreCase(selected.getName())) selected = selected.findFile("SAVEDATA");
    if (selected == null
        || !"SAVEDATA".equalsIgnoreCase(selected.getName())
        || !selected.isDirectory())
      throw new IOException("请选择 PPSSPP 正在使用的 PSP/SAVEDATA 目录；没有存档时请先在游戏内保存一次");
    root = selected;
    if (!root.canRead() || !root.canWrite()) throw new IOException("存档目录需要读写授权");
    journal =
        new File(
            context.getFilesDir(),
            "save-write-" + ServerConfig.hash(root.getUri().toString()) + ".json");
    if (journal.exists())
      throw new IOException("上次存档替换未完整结束，已暂停写入。替换前备份保留在「本地内容 → 存档」，请先导出恢复；没有继续覆盖");
  }

  Map<String, String> games() throws Exception {
    Map<String, String> groups = new TreeMap<>();
    for (DocumentFile dir : children(root)) {
      String name = dir.getName();
      if (!dir.isDirectory() || name == null || !name.matches("[A-Z]{4}[0-9]{5}.*")) continue;
      String prefix = name.substring(0, 9), title = sfoTitle(dir.findFile("PARAM.SFO"));
      if (!groups.containsKey(prefix) || !title.isEmpty())
        groups.put(prefix, title.isEmpty() ? prefix : title + " · " + prefix);
    }
    return groups;
  }

  private String sfoTitle(DocumentFile file) {
    if (file == null || file.length() > 256 * 1024) return "";
    try (InputStream in = context.getContentResolver().openInputStream(file.getUri())) {
      byte[] data = ApiClient.read(in, 256 * 1024);
      ByteBuffer b = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
      if (b.getInt(0) != 0x46535000) return "";
      int keys = b.getInt(8), values = b.getInt(12), count = b.getInt(16);
      if (count < 0 || count > 512) return "";
      for (int i = 0; i < count; i++) {
        int at = 20 + i * 16, key = keys + Short.toUnsignedInt(b.getShort(at));
        int end = key;
        while (end < data.length && data[end] != 0) end++;
        if (!new String(data, key, end - key, StandardCharsets.UTF_8).equals("TITLE")) continue;
        int start = values + b.getInt(at + 12), length = b.getInt(at + 4);
        if (length < 1 || length > 2048 || start < 0 || start + length > data.length) return "";
        return new String(data, start, length, StandardCharsets.UTF_8).replace("\0", "").trim();
      }
    } catch (Exception ignored) {
    }
    return "";
  }

  JSONObject read(String prefix) throws Exception {
    if (!prefix.matches("[A-Z]{4}[0-9]{5}")) throw new IOException("PSP 游戏编号无效");
    JSONArray files = new JSONArray();
    long[] stats = {0, 0};
    for (DocumentFile dir : children(root)) {
      String name = dir.getName();
      if (name != null && name.startsWith(prefix) && dir.isDirectory())
        walk(dir, name, files, stats);
    }
    SaveBackup.validate(files);
    return new JSONObject()
        .put("files", files)
        .put("revision", 0)
        .put("updated_at", stats[1] / 1000.0)
        .put("file_count", files.length())
        .put("size_bytes", stats[0]);
  }

  JSONObject stableRead(String prefix) throws Exception {
    JSONObject first = read(prefix);
    Thread.sleep(200);
    SaveSnapshot.unchanged(first, read(prefix), false);
    return first;
  }

  private void walk(DocumentFile dir, String path, JSONArray files, long[] stats) throws Exception {
    if (path.split("/").length > 3) throw new IOException("存档目录层级过深");
    for (DocumentFile child : children(dir)) {
      String name = child.getName();
      if (name == null || !name.matches("[A-Za-z0-9_][A-Za-z0-9_. -]{0,95}"))
        throw new IOException("存档文件名无效");
      String next = path + "/" + name;
      if (child.isDirectory()) walk(child, next, files, stats);
      else if (child.isFile()) {
        if (files.length() >= 256 || child.length() > 32 * 1024 * 1024 - stats[0])
          throw new IOException("存档超过同步限制");
        long before = child.lastModified(), size = child.length();
        byte[] data;
        try (InputStream in = context.getContentResolver().openInputStream(child.getUri())) {
          if (in == null) throw new IOException("无法读取存档");
          data = ApiClient.read(in, (int) (32 * 1024 * 1024 - stats[0]));
        }
        if (before != child.lastModified() || size != child.length())
          throw new IOException("模拟器仍在写入存档，请退出后重试");
        stats[0] += data.length;
        stats[1] = Math.max(stats[1], before);
        files.put(
            new JSONObject()
                .put("path", next)
                .put("data", Base64.getEncoder().encodeToString(data)));
      } else throw new IOException("存档包含不可读取的内容");
    }
  }

  /**
   * Stage every file, verify bytes, then swap only this game's directories. Backups precede this
   * call.
   */
  interface LeaseCheck {
    void verify() throws Exception;
  }

  synchronized void replace(String prefix, JSONObject expected, JSONArray files, LeaseCheck lease)
      throws Exception {
    if (files.length() == 0 || !prefix.equals(SaveSnapshot.prefix(files)))
      throw new IOException("云端存档为空或游戏编号不一致，未写入");
    SaveSnapshot.unchanged(expected, stableRead(prefix), false);
    TreeSet<String> names = new TreeSet<>();
    for (DocumentFile d : children(root))
      if (d.getName() != null && d.getName().startsWith(prefix) && d.isDirectory())
        names.add(d.getName());
    for (int i = 0; i < files.length(); i++)
      names.add(files.getJSONObject(i).getString("path").split("/")[0]);
    String id = UUID.randomUUID().toString().replace("-", "");
    List<Swap> swaps = new ArrayList<>();
    boolean committed = false, rollbackOk = true;
    try {
      int n = 0;
      for (String name : names) {
        Swap swap = new Swap(name, "RO_STAGE_" + id + "_" + n, "RO_OLD_" + id + "_" + n++);
        swaps.add(swap);
        swap.original = root.findFile(name);
        for (int i = 0; i < files.length(); i++) {
          JSONObject f = files.getJSONObject(i);
          String[] parts = f.getString("path").split("/");
          if (!parts[0].equals(name)) continue;
          if (swap.staged == null) swap.staged = require(root.createDirectory(swap.stageName));
          DocumentFile parent = swap.staged;
          for (int p = 1; p < parts.length - 1; p++) {
            DocumentFile child = parent.findFile(parts[p]);
            parent = child == null ? require(parent.createDirectory(parts[p])) : child;
          }
          DocumentFile target =
              require(parent.createFile("application/octet-stream", parts[parts.length - 1]));
          byte[] bytes = Base64.getDecoder().decode(f.getString("data"));
          try (OutputStream out =
              context.getContentResolver().openOutputStream(target.getUri(), "wt")) {
            if (out == null) throw new IOException("无法写入暂存文件");
            out.write(bytes);
          }
          try (InputStream in = context.getContentResolver().openInputStream(target.getUri())) {
            if (in == null || !Arrays.equals(bytes, ApiClient.read(in, 32 * 1024 * 1024)))
              throw new IOException("存档写入校验失败");
          }
        }
      }
      SaveSnapshot.unchanged(expected, stableRead(prefix), false);
      lease.verify();
      JSONArray plan = new JSONArray();
      for (Swap s : swaps)
        plan.put(
            new JSONObject().put("name", s.name).put("stage", s.stageName).put("old", s.oldName));
      try (FileOutputStream out = new FileOutputStream(journal)) {
        out.write(
            new JSONObject()
                .put("root", root.getUri().toString())
                .put("plan", plan)
                .toString()
                .getBytes(StandardCharsets.UTF_8));
        out.getFD().sync();
      }
      for (Swap s : swaps) {
        lease.verify();
        if (s.original != null) {
          if (!s.original.renameTo(s.oldName)) throw new IOException("此目录不支持安全替换，原存档已保留");
          s.movedOld = true;
        }
        if (s.staged != null) {
          if (!s.staged.renameTo(s.name)) throw new IOException("无法启用新存档");
          s.movedNew = true;
        }
      }
      if (!SaveSnapshot.hash(files).equals(SaveSnapshot.hash(read(prefix).getJSONArray("files"))))
        throw new IOException("替换后校验失败");
      lease.verify();
      committed = true;
    } finally {
      if (!committed) {
        Collections.reverse(swaps);
        for (Swap s : swaps) {
          if (s.movedNew && !s.staged.delete()) rollbackOk = false;
          if (s.movedOld && !s.original.renameTo(s.name)) rollbackOk = false;
        }
      }
      for (Swap s : swaps) {
        if (!s.movedNew && s.staged != null && s.staged.exists()) s.staged.delete();
        if (committed && s.movedOld && !s.original.delete()) rollbackOk = false;
      }
      if (rollbackOk) journal.delete();
      else throw new IOException("替换未能完整收尾，已暂停后续写入。请从「本地内容 → 存档」导出原备份恢复");
    }
  }

  private static DocumentFile require(DocumentFile f) throws IOException {
    if (f == null) throw new IOException("存档目录不可写入");
    return f;
  }

  private DocumentFile[] children(DocumentFile parent) throws IOException {
    // DocumentFile.listFiles() alone silently turns provider failures into an empty directory.
    // A denied/interrupted read must never be interpreted as an empty save to replace.
    Uri query =
        android.provider.DocumentsContract.buildChildDocumentsUriUsingTree(
            parent.getUri(), android.provider.DocumentsContract.getDocumentId(parent.getUri()));
    int count;
    try (android.database.Cursor cursor =
        context
            .getContentResolver()
            .query(
                query,
                new String[] {android.provider.DocumentsContract.Document.COLUMN_DOCUMENT_ID},
                null,
                null,
                null)) {
      if (cursor == null) throw new IOException("无法读取存档目录，未将其当作空存档");
      count = cursor.getCount();
    } catch (SecurityException e) {
      throw new IOException("存档目录授权已失效，请重新选择", e);
    }
    DocumentFile[] files = parent.listFiles();
    if (files.length != count) throw new IOException("存档目录在读取期间变化，请退出模拟器后重试");
    return files;
  }

  private static final class Swap {
    final String name, stageName, oldName;
    DocumentFile original, staged;
    boolean movedOld, movedNew;

    Swap(String name, String stageName, String oldName) {
      this.name = name;
      this.stageName = stageName;
      this.oldName = oldName;
    }
  }
}
