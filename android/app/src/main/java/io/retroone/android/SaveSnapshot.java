package io.retroone.android;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import org.json.*;

/** Content comparison ignores mtimes and array order; a newer clock never wins automatically. */
final class SaveSnapshot {
  static String hash(JSONArray files) throws Exception {
    SaveBackup.validate(files);
    TreeMap<String, byte[]> sorted = new TreeMap<>();
    for (int i = 0; i < files.length(); i++) {
      JSONObject f = files.getJSONObject(i);
      sorted.put(f.getString("path"), Base64.getDecoder().decode(f.getString("data")));
    }
    MessageDigest hash = MessageDigest.getInstance("SHA-256");
    for (Map.Entry<String, byte[]> e : sorted.entrySet()) {
      hash.update(e.getKey().getBytes(StandardCharsets.UTF_8));
      hash.update((byte) 0);
      hash.update(MessageDigest.getInstance("SHA-256").digest(e.getValue()));
    }
    StringBuilder result = new StringBuilder();
    for (byte b : hash.digest()) result.append(String.format(Locale.ROOT, "%02x", b & 255));
    return result.toString();
  }

  static String prefix(JSONArray files) throws Exception {
    SaveBackup.validate(files);
    String prefix = "";
    for (int i = 0; i < files.length(); i++) {
      String folder = files.getJSONObject(i).getString("path").split("/")[0];
      if (!folder.matches("[A-Z]{4}[0-9]{5}.*"))
        throw new IOException("存档目录无法识别为 PSP 游戏编号，暂不自动写入模拟器");
      String next = folder.substring(0, 9);
      if (!prefix.isEmpty() && !prefix.equals(next))
        throw new IOException("这份存档包含多个游戏编号，暂不合并，请分别导出检查");
      prefix = next;
    }
    return prefix;
  }

  static void unchanged(JSONObject preview, JSONObject current, boolean cloud) throws Exception {
    if (!hash(preview.getJSONArray("files")).equals(hash(current.getJSONArray("files")))
        || (cloud && preview.getLong("revision") != current.getLong("revision")))
      throw new IOException((cloud ? "云端" : "本机") + "存档在选择期间发生变化；未覆盖，请重新比较");
  }
}
