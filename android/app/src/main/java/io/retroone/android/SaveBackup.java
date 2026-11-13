package io.retroone.android;

import java.io.*;
import java.util.*;
import java.util.zip.*;
import org.json.*;

/**
 * Read a revision under the server's existing lease, then release it even on failure. Backups never
 * write into a running emulator's shared SAVEDATA folder.
 */
final class SaveBackup {
  static JSONObject fetch(ServerConfig config, String game) throws Exception {
    String path = "games/" + ServerConfig.encode(game) + "/cloud-save";
    ApiClient api = new ApiClient(config);
    JSONObject snapshot =
        api.post(
            config.api(path + "/lease"),
            new JSONObject().put("client_id", UUID.randomUUID().toString()),
            45 * 1024 * 1024);
    try {
      validate(snapshot.getJSONArray("files"));
      return snapshot;
    } finally {
      api.post(
          config.api(path + "/release"),
          new JSONObject().put("lease_id", snapshot.getString("lease_id")),
          16384);
    }
  }

  static void validate(JSONArray files) throws Exception {
    if (files.length() > 256) throw new IOException("存档文件过多");
    Set<String> paths = new HashSet<>();
    long total = 0;
    for (int i = 0; i < files.length(); i++) {
      JSONObject entry = files.getJSONObject(i);
      String path = entry.getString("path");
      String[] parts = path.split("/", -1);
      if (parts.length < 2 || parts.length > 4) throw new IOException("存档路径无效");
      for (String part : parts)
        if (!part.matches("[A-Za-z0-9_][A-Za-z0-9_. -]{0,95}")
            || part.equals(".")
            || part.equals("..")) throw new IOException("存档路径无效");
      String lower = path.toLowerCase(Locale.ROOT);
      if (lower.endsWith(".state")
          || lower.endsWith(".key")
          || lower.endsWith(".keys")
          || !paths.add(lower)) throw new IOException("存档路径重复或类型无效");
      String data = entry.getString("data");
      if (data.length() > 44739244) throw new IOException("存档超过 32 MiB");
      total += Base64.getDecoder().decode(data).length;
      if (total > 32 * 1024 * 1024) throw new IOException("存档超过 32 MiB");
    }
    for (String path : paths) {
      int at = path.indexOf('/');
      while (at >= 0) {
        if (paths.contains(path.substring(0, at))) throw new IOException("存档目录与文件冲突");
        at = path.indexOf('/', at + 1);
      }
    }
  }

  static void zip(JSONObject snapshot, File target) throws Exception {
    JSONArray files = snapshot.getJSONArray("files");
    validate(files);
    try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(target))) {
      for (int i = 0; i < files.length(); i++) {
        JSONObject f = files.getJSONObject(i);
        out.putNextEntry(new ZipEntry("SAVEDATA/" + f.getString("path")));
        out.write(Base64.getDecoder().decode(f.getString("data")));
        out.closeEntry();
      }
    } catch (Exception e) {
      target.delete();
      throw e;
    }
  }
}
