package io.retroone.android;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.File;
import java.io.IOException;

/** Single-image core adapters. A format check never grants a gameplay confirmation. */
final class RuntimeAdapter {
  private final Context context;
  private final SharedPreferences preferences;
  final RetroArch retroarch;

  RuntimeAdapter(Context context) {
    this.context = context.getApplicationContext();
    preferences = this.context.getSharedPreferences("runtime-adapters", Context.MODE_PRIVATE);
    retroarch = new RetroArch(this.context);
  }

  LaunchPolicy.State status(DownloadStore.Item item, boolean present) {
    if (!DownloadStore.READY.equals(item.state)) return LaunchPolicy.State.INCOMPLETE;
    if (!present) return LaunchPolicy.State.MISSING;
    if (!RuntimePolicy.formatReason(item.platform, item.name).isEmpty())
      return LaunchPolicy.State.MANUAL;
    if (!retroarch.installed()) return LaunchPolicy.State.INSTALL_RETROARCH;
    if (!retroarch.configured()) return LaunchPolicy.State.CONFIGURE_RUNTIME;
    return verified(item) ? LaunchPolicy.State.PLAYABLE : LaunchPolicy.State.CHECK_RUNTIME;
  }

  String fingerprint(DownloadStore.Item item) {
    if (item == null) return "";
    RuntimePolicy.Spec spec = RuntimePolicy.spec(item.platform);
    String environment = retroarch.fingerprint();
    if (spec == null || environment.isEmpty()) return "";
    try {
      File file = RetroApplication.from(context).downloads.complete(item);
      if (!file.isFile() || file.length() != item.size) return "";
      return ServerConfig.hash(
          environment
              + ":standalone-v1:"
              + spec.id
              + ":"
              + spec.core
              + ":"
              + preferences.getLong("generation." + spec.core, 0)
              + ":"
              + item.id
              + ":"
              + item.manifest
              + ":"
              + file.length()
              + ":"
              + file.lastModified());
    } catch (IOException e) {
      return "";
    }
  }

  boolean verified(DownloadStore.Item item) {
    String current = fingerprint(item);
    return !current.isEmpty() && current.equals(preferences.getString("verified." + item.id, ""));
  }

  void confirm(DownloadStore.Item item, String prepared) throws IOException {
    if (prepared == null || prepared.isEmpty() || !prepared.equals(fingerprint(item)))
      throw new IOException("文件或启动环境已变化，请重新测试");
    if (!preferences.edit().putString("verified." + item.id, prepared).commit())
      throw new IOException("无法保存本款游戏的确认结果");
  }

  void reset(RuntimePolicy.Spec spec) {
    if (spec == null) return;
    preferences
        .edit()
        .putLong("generation." + spec.core, preferences.getLong("generation." + spec.core, 0) + 1)
        .apply();
  }

  String prepare(DownloadStore.Item item) throws Exception {
    if (item == null) throw new IOException("下载记录不存在");
    RuntimePolicy.Spec spec = RuntimePolicy.spec(item.platform);
    String reason = RuntimePolicy.formatReason(item.platform, item.name);
    if (spec == null || !reason.isEmpty())
      throw new IOException(reason.isEmpty() ? "此平台尚未接入" : reason);
    if (!retroarch.installed() || !retroarch.configured())
      throw new IOException("请先安装 RetroArch ARM64 并选择共享目录");
    File source = RetroApplication.from(context).downloads.complete(item);
    long maximum = CartridgeArchive.isZipName(item.name) ? spec.maxArchiveBytes : spec.maxBytes;
    if (source.length() > maximum) throw new IOException("镜像超过本次适配的大小范围，请手动导入模拟器");
    source = retroarch.verifiedDownload(item);
    String name = item.name;
    if (CartridgeArchive.isZipName(name)) {
      CartridgeArchive.Extracted extracted =
          CartridgeArchive.prepareRuntime(source, name, spec, context.getCacheDir());
      source = extracted.file;
      name = extracted.name;
    }
    return retroarch.stageRuntime(source, spec, name);
  }
}
