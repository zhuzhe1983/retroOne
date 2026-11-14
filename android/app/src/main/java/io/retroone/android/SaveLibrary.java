package io.retroone.android;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import org.json.*;

/** Immutable, player-scoped backup files owned by RetroOne. Never traverses emulator storage. */
final class SaveLibrary {
  final File root;

  SaveLibrary(File appFiles) {
    try {
      root = new File(appFiles.getCanonicalFile(), "save-backups");
    } catch (IOException e) {
      throw new IllegalStateException("无法定位本机存档目录", e);
    }
  }

  static final class Entry {
    final File file;
    final JSONObject info;

    Entry(File file, JSONObject info) {
      this.file = file;
      this.info = info;
    }

    String title() {
      return info.optString("title", "旧版存档备份");
    }

    String player() {
      return info.optString("player", "所属玩家待确认");
    }

    long created() {
      return info.optLong("created", file.lastModified());
    }
  }

  synchronized Entry create(
      ServerConfig source, String player, JSONObject game, JSONObject snapshot) throws Exception {
    return create(source, player, game, snapshot, "cloud");
  }

  synchronized Entry create(
      ServerConfig source, String player, JSONObject game, JSONObject snapshot, String origin)
      throws Exception {
    File dir = new File(root, source.key() + "/" + ServerConfig.hash(game.getString("id")));
    if (!dir.isDirectory() && !dir.mkdirs()) throw new IOException("无法创建存档备份目录");
    if (!dir.getCanonicalPath().startsWith(root.getCanonicalPath() + File.separator))
      throw new IOException("备份路径无效");
    File file =
        new File(
            dir, "revision-" + snapshot.getLong("revision") + "-" + UUID.randomUUID() + ".zip");
    JSONObject info =
        new JSONObject()
            .put("title", game.getString("title"))
            .put("game", game.getString("id"))
            .put("base", source.base)
            .put("player", player)
            .put("revision", snapshot.getLong("revision"))
            .put("created", System.currentTimeMillis())
            .put("digest", snapshot.optString("digest"))
            .put("origin", origin);
    SaveBackup.zip(snapshot, file);
    try {
      Files.write(
          new File(file.getPath() + ".json").toPath(),
          info.toString().getBytes(StandardCharsets.UTF_8));
    } catch (Exception e) {
      file.delete();
      throw e;
    }
    return new Entry(file, info);
  }

  synchronized List<Entry> all() throws Exception {
    List<Entry> entries = new ArrayList<>();
    for (File server : children(root)) {
      if (!server.getName().matches("[a-f0-9]{64}") || Files.isSymbolicLink(server.toPath()))
        continue;
      for (File game : children(server)) {
        if (!game.getName().matches("[a-f0-9]{64}") || Files.isSymbolicLink(game.toPath()))
          continue;
        for (File file : children(game)) {
          if (!file.getName().matches("revision-[0-9]+-[a-f0-9-]{36}\\.zip") || !file.isFile())
            continue;
          if (!safe(file)) continue;
          JSONObject info = new JSONObject();
          File meta = new File(file.getPath() + ".json");
          if (meta.isFile() && !Files.isSymbolicLink(meta.toPath()) && meta.length() <= 16384) {
            try {
              info =
                  new JSONObject(
                      new String(Files.readAllBytes(meta.toPath()), StandardCharsets.UTF_8));
            } catch (Exception ignored) {
              /* A legacy or interrupted backup remains recoverable. */
            }
          }
          entries.add(new Entry(file, info));
        }
      }
    }
    entries.sort((a, b) -> Long.compare(b.created(), a.created()));
    return entries;
  }

  private static File[] children(File parent) {
    File[] files = parent.listFiles();
    return files == null ? new File[0] : files;
  }

  boolean safe(File file) throws IOException {
    File absolute = file.getAbsoluteFile();
    if (!absolute.equals(file.getCanonicalFile()) || !file.isFile()) return false;
    String relative =
        root.toPath()
            .toAbsolutePath()
            .relativize(absolute.toPath())
            .toString()
            .replace(File.separatorChar, '/');
    return relative.matches("[a-f0-9]{64}/[a-f0-9]{64}/revision-[0-9]+-[a-f0-9-]{36}\\.zip");
  }

  synchronized void delete(Entry entry) throws IOException {
    if (!safe(entry.file)) throw new IOException("备份不存在或路径无效");
    if (!entry.file.delete()) throw new IOException("无法移除存档备份");
    File meta = new File(entry.file.getPath() + ".json");
    if (!Files.isSymbolicLink(meta.toPath())) meta.delete();
  }
}
