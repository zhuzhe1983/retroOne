package io.retroone.android;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.*;
import org.json.JSONObject;

final class Fbneo {
  final RetroArch retroarch;
  final FbneoBios bios;
  private final Context context;
  private final SharedPreferences prefs;
  private static JSONObject index;

  Fbneo(Context context) {
    this.context = context.getApplicationContext();
    retroarch = new RetroArch(context);
    bios = new FbneoBios(context);
    prefs = context.getSharedPreferences("fbneo", Context.MODE_PRIVATE);
  }

  LaunchPolicy.State status(DownloadStore.Item item, boolean present) {
    if (!DownloadStore.READY.equals(item.state)) return LaunchPolicy.State.INCOMPLETE;
    if (!present) return LaunchPolicy.State.MISSING;
    if (!LaunchPolicy.formatReason("fbneo", item.name).isEmpty()) return LaunchPolicy.State.MANUAL;
    if (!retroarch.installed()) return LaunchPolicy.State.INSTALL_RETROARCH;
    if (!issue(item).isEmpty())
      return needsBios(item)
          ? LaunchPolicy.State.CONFIGURE_NEOGEO
          : LaunchPolicy.State.REVIEW_FBNEO;
    if (compatibility(item) != null) return LaunchPolicy.State.OPEN_ARCADE_COMPAT;
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
              + FbneoRomSet.VERSION
              + ":"
              + bios.fingerprint()
              + ":"
              + ArcadeCompatProfile.VERSION
              + ":"
              + retroarch.corePath("mame2003plus")
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

  ArcadeCompatProfile.Profile compatibility(DownloadStore.Item item) {
    String current = fingerprint(item);
    return !current.isEmpty() && current.equals(prefs.getString("compatFp." + item.id, ""))
        ? ArcadeCompatProfile.profile(prefs.getString("compat." + item.id, ""))
        : null;
  }

  String launchPlatform(DownloadStore.Item item) {
    return compatibility(item) == null ? "fbneo" : "mame2003plus";
  }

  String launchLabel(DownloadStore.Item item) {
    ArcadeCompatProfile.Profile profile = compatibility(item);
    return profile == null ? "FinalBurn Neo" : profile.label;
  }

  String launchDetails(DownloadStore.Item item) {
    if (!fingerprint(item).equals(prefs.getString("matchFp." + item.id, ""))) return "";
    String set = prefs.getString("set." + item.id, "");
    return (prefs.getBoolean("merged." + item.id, false)
            ? "合并归档：启动基础集 " + set + "，子目录中的衍生版本仍保留在原包中。"
            : "游戏 ROM 集已精确匹配：" + set + "。")
        + (prefs.getBoolean("neoGeo." + item.id, false)
            ? "\nNeoGeo 必需 BIOS 已校验。若在 RetroArch 自行选择了其他 BIOS，请确认对应文件也已提供；本应用不更改该设置。"
            : "");
  }

  boolean needsBios(DownloadStore.Item item) {
    return !issue(item).isEmpty() && prefs.getBoolean("needsBios." + item.id, false);
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
    prefs
        .edit()
        .remove("issueFp." + item.id)
        .remove("issue." + item.id)
        .remove("needsBios." + item.id)
        .apply();
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
    FbneoRomSet.Match match;
    File externalBios = null;
    try {
      match = FbneoRomSet.inspect(file, index());
      if (!match.missingBios.isEmpty()) externalBios = bios.file();
      match = FbneoRomSet.resolve(match, externalBios, index());
    } catch (FbneoArchive.MatchException e) {
      if (e instanceof FbneoRomSet.MissingBiosException) {
        recordIssue(item, e, true);
        throw e;
      }
      ArcadeCompatProfile.Profile compatible = ArcadeCompatProfile.match(file);
      if (compatible != null) {
        File converted = ArcadeCompatProfile.prepare(file, context.getCacheDir());
        String staged = retroarch.stageArchive(converted, compatible.set);
        prefs
            .edit()
            .putString("compat." + item.id, compatible.id)
            .putString("compatFp." + item.id, fingerprint(item))
            .apply();
        clearIssue(item);
        return staged;
      }
      recordIssue(item, e, false);
      throw e;
    }
    prefs.edit().remove("compat." + item.id).remove("compatFp." + item.id).apply();
    clearIssue(item);
    String path =
        retroarch.stageArchive(file, match.set, match.biosSha256.isEmpty() ? null : externalBios);
    prefs
        .edit()
        .putString("matchFp." + item.id, fingerprint(item))
        .putString("set." + item.id, match.set)
        .putBoolean("merged." + item.id, match.merged)
        .putBoolean("neoGeo." + item.id, match.neoGeo)
        .apply();
    return path;
  }

  private void recordIssue(DownloadStore.Item item, Exception error, boolean needsBios) {
    prefs
        .edit()
        .putString("issueFp." + item.id, fingerprint(item))
        .putString("issue." + item.id, ApiClient.message(error))
        .putBoolean("needsBios." + item.id, needsBios)
        .apply();
  }

  synchronized JSONObject index() throws Exception {
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
