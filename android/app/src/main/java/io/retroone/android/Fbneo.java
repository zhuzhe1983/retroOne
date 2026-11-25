package io.retroone.android;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.*;
import org.json.JSONObject;

final class Fbneo {
  final RetroArch retroarch;
  private final Context context;
  private final SharedPreferences prefs;
  private static JSONObject index;

  Fbneo(Context context) {
    this.context = context.getApplicationContext();
    retroarch = new RetroArch(context);
    prefs = context.getSharedPreferences("fbneo", Context.MODE_PRIVATE);
  }

  LaunchPolicy.State status(DownloadStore.Item item, boolean present) {
    if (!DownloadStore.READY.equals(item.state)) return LaunchPolicy.State.INCOMPLETE;
    if (!present) return LaunchPolicy.State.MISSING;
    if (!LaunchPolicy.formatReason("fbneo", item.name).isEmpty()) return LaunchPolicy.State.MANUAL;
    if (!retroarch.installed()) return LaunchPolicy.State.INSTALL_RETROARCH;
    if (!issue(item).isEmpty()) return LaunchPolicy.State.REVIEW_FBNEO;
    return verified(item) ? LaunchPolicy.State.PLAYABLE : LaunchPolicy.State.CHECK_FBNEO;
  }

  String fingerprint(DownloadStore.Item item) {
    try {
      File file = RetroApplication.from(context).downloads.complete(item);
      if (!retroarch.configured() || !file.isFile()) return "";
      return ServerConfig.hash(
          retroarch.fingerprint()
              + ":"
              + retroarch.corePath("fbneo")
              + ":"
              + FbneoArchive.DAT
              + ":"
              + item.id
              + ":"
              + item.manifest
              + ":"
              + file.length()
              + ":"
              + file.lastModified());
    } catch (Exception e) {
      return "";
    }
  }

  boolean verified(DownloadStore.Item item) {
    String current = fingerprint(item);
    return !current.isEmpty() && current.equals(prefs.getString("verified." + item.id, ""));
  }

  void confirm(DownloadStore.Item item, String fingerprint) throws IOException {
    if (fingerprint.isEmpty() || !fingerprint.equals(fingerprint(item)))
      throw new IOException("文件或配置已变化，请重新测试");
    if (!prefs.edit().putString("verified." + item.id, fingerprint).commit())
      throw new IOException("未能保存验证结果");
  }

  String issue(DownloadStore.Item item) {
    return fingerprint(item).equals(prefs.getString("issueFp." + item.id, "-"))
        ? prefs.getString("issue." + item.id, "")
        : "";
  }

  void clearIssue(DownloadStore.Item item) {
    prefs.edit().remove("issueFp." + item.id).remove("issue." + item.id).apply();
  }

  void reset() {
    prefs.edit().clear().apply();
  }

  String prepare(DownloadStore.Item item) throws Exception {
    if (item == null
        || !"fbneo".equals(item.platform)
        || !LaunchPolicy.formatReason(item.platform, item.name).isEmpty())
      throw new IOException("请选择已下载的 FBNeo 独立 ZIP");
    File file = retroarch.verifiedDownload(item);
    String set;
    try {
      set = FbneoArchive.identify(file, index());
    } catch (FbneoArchive.MatchException e) {
      prefs
          .edit()
          .putString("issueFp." + item.id, fingerprint(item))
          .putString("issue." + item.id, e.getMessage())
          .apply();
      throw e;
    }
    clearIssue(item);
    return retroarch.stageArchive(file, set);
  }

  private synchronized JSONObject index() throws Exception {
    if (index == null) {
      try (InputStream in = context.getAssets().open("fbneo-sets.json")) {
        index =
            new JSONObject(
                new String(
                    ApiClient.read(in, 4 * 1024 * 1024), java.nio.charset.StandardCharsets.UTF_8));
      }
    }
    return index;
  }
}
