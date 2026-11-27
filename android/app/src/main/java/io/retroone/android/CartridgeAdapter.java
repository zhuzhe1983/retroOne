package io.retroone.android;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;

/** Per-platform setup proofs; shares storage and launch mechanics with the verified NES adapter. */
final class CartridgeAdapter {
  private final Context context;
  private final SharedPreferences preferences;
  final RetroArch retroarch;

  CartridgeAdapter(Context context) {
    this.context = context.getApplicationContext();
    preferences = this.context.getSharedPreferences("cartridge", Context.MODE_PRIVATE);
    retroarch = new RetroArch(this.context);
  }

  String fingerprint(CartridgePolicy.Spec spec) {
    if (spec == null) return "";
    String environment = retroarch.fingerprint();
    if (environment.isEmpty()) return "";
    return ServerConfig.hash(
        environment
            + ":"
            + spec.id
            + ":"
            + spec.core
            + ":"
            + spec.probeAsset
            + ":cartridge-probe-v1:"
            + preferences.getLong("generation." + spec.core, 0));
  }

  boolean verified(CartridgePolicy.Spec spec) {
    String current = fingerprint(spec);
    return !current.isEmpty() && current.equals(preferences.getString("verified." + spec.id, ""));
  }

  boolean pending(CartridgePolicy.Spec spec) {
    String current = fingerprint(spec);
    return !current.isEmpty() && current.equals(preferences.getString("pending." + spec.id, ""));
  }

  void probeLaunched(CartridgePolicy.Spec spec, String preparedFingerprint) throws IOException {
    if (preparedFingerprint.isEmpty() || !preparedFingerprint.equals(fingerprint(spec)))
      throw new IOException("平台设置已变化，请重新运行测试");
    preferences
        .edit()
        .putString("pending." + spec.id, preparedFingerprint)
        .remove("verified." + spec.id)
        .apply();
  }

  void confirm(CartridgePolicy.Spec spec) throws IOException {
    if (!pending(spec)) throw new IOException("请先运行当前平台的测试并确认画面与按键");
    preferences
        .edit()
        .putString("verified." + spec.id, fingerprint(spec))
        .remove("pending." + spec.id)
        .apply();
  }

  void resetCore(CartridgePolicy.Spec spec) {
    // Several platforms share one binary. A manually changed core invalidates them together.
    SharedPreferences.Editor editor = preferences.edit();
    editor.putLong(
        "generation." + spec.core, preferences.getLong("generation." + spec.core, 0) + 1);
    for (CartridgePolicy.Spec other : CartridgePolicy.all()) {
      if (other.core.equals(spec.core))
        editor.remove("verified." + other.id).remove("pending." + other.id);
    }
    editor.apply();
  }

  LaunchPolicy.State status(DownloadStore.Item item, boolean present) {
    if (!DownloadStore.READY.equals(item.state)) return LaunchPolicy.State.INCOMPLETE;
    if (!present) return LaunchPolicy.State.MISSING;
    CartridgePolicy.Spec spec = CartridgePolicy.spec(item.platform);
    if (spec == null) return LaunchPolicy.State.UNSUPPORTED;
    if (!LaunchPolicy.formatReason(spec.id, item.name).isEmpty()) return LaunchPolicy.State.MANUAL;
    if (!retroarch.installed()) return LaunchPolicy.State.INSTALL_RETROARCH;
    if (!retroarch.configured()) return LaunchPolicy.State.CONFIGURE_CARTRIDGE;
    return verified(spec) ? LaunchPolicy.State.PLAYABLE : LaunchPolicy.State.VERIFY_CARTRIDGE;
  }

  File probeFile(CartridgePolicy.Spec spec) throws IOException {
    if (spec == null) throw new IOException("平台不可用");
    File directory = new File(context.getCacheDir(), "emulator-probe");
    if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法准备原创测试程序");
    File target = new File(directory, spec.probeAsset);
    try (InputStream in = context.getAssets().open(spec.probeAsset);
        FileOutputStream out = new FileOutputStream(target)) {
      byte[] buffer = new byte[65536];
      int n;
      while ((n = in.read(buffer)) != -1) {
        if (Thread.currentThread().isInterrupted()) throw new IOException("准备已取消");
        out.write(buffer, 0, n);
      }
    }
    CartridgePolicy.validateFile(spec, target);
    return target;
  }

  String stageProbe(CartridgePolicy.Spec spec) throws Exception {
    return retroarch.stageCartridge(probeFile(spec), spec, spec.probeAsset);
  }

  String stageGame(DownloadStore.Item item) throws Exception {
    if (item == null) throw new IOException("下载记录已移除");
    CartridgePolicy.Spec spec = CartridgePolicy.spec(item.platform);
    if (spec == null) throw new IOException("此平台尚未适配");
    if (!verified(spec)) throw new IOException("请先完成 " + spec.label + " 平台测试");
    String reason = LaunchPolicy.formatReason(spec.id, item.name);
    if (!reason.isEmpty()) throw new IOException(reason);
    long maximum = CartridgeArchive.isZipName(item.name) ? 128L * 1024 * 1024 : spec.maxBytes;
    if (item.size <= 0 || item.size > maximum) throw new IOException("文件大小超出当前卡带直启范围，请核对格式");
    CartridgeArchive.Extracted extracted =
        CartridgeArchive.prepare(
            retroarch.verifiedDownload(item), item.name, spec.id, context.getCacheDir());
    return retroarch.stageCartridge(extracted.file, spec, extracted.name);
  }
}
