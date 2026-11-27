package io.retroone.android;

import android.content.ClipData;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import androidx.core.content.FileProvider;
import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

final class Emulators {
  static final String[] PACKAGES = {
    "org.ppsspp.ppsspp", "org.ppsspp.ppssppgold", "org.ppsspp.ppsspplegacy"
  };
  static final String[] LABELS = {"PPSSPP", "PPSSPP Gold（未实机验证）", "PPSSPP Legacy（未实机验证）"};

  static String support(String platform, String name) {
    return LaunchPolicy.formatReason(platform, name);
  }

  static LaunchPolicy.State status(Context context, DownloadStore.Item item, boolean installed) {
    if (item == null) return LaunchPolicy.State.MISSING;
    boolean present = false;
    if (DownloadStore.READY.equals(item.state)) {
      try {
        File file = RetroApplication.from(context).downloads.complete(item);
        present = file.isFile() && file.length() == item.size;
      } catch (IOException ignored) {
      }
    }
    if ("switch".equals(item.platform)) return new Eden(context).status(item, present);
    if ("fbneo".equals(item.platform)) return new Fbneo(context).status(item, present);
    if (CartridgePolicy.supports(item.platform))
      return new CartridgeAdapter(context).status(item, present);
    if (RuntimePolicy.supports(item.platform))
      return new RuntimeAdapter(context).status(item, present);
    if (RetroArchPolicy.supports(item.platform)) {
      RetroArch adapter = new RetroArch(context);
      return RetroArchPolicy.assess(
          item.name,
          DownloadStore.READY.equals(item.state),
          present,
          adapter.installed(),
          adapter.configured(),
          adapter.verified());
    }
    return LaunchPolicy.assess(
        item.platform,
        item.name,
        item.size,
        DownloadStore.READY.equals(item.state),
        present,
        installed);
  }

  static List<Integer> installed(Context context) {
    List<Integer> result = new ArrayList<>();
    for (int i = 0; i < PACKAGES.length; i++)
      try {
        android.content.pm.ActivityInfo info =
            context
                .getPackageManager()
                .getActivityInfo(
                    new ComponentName(PACKAGES[i], "org.ppsspp.ppsspp.PpssppActivity"), 0);
        if (info.enabled && info.exported && info.applicationInfo.enabled) result.add(i);
      } catch (PackageManager.NameNotFoundException ignored) {
      }
    return result;
  }

  static Intent intent(Context context, DownloadStore.Item item, int emulator) throws IOException {
    if (item == null) throw new IOException("本机文件记录不存在，请刷新后重试");
    if (!"psp".equals(item.platform)) throw new IOException("此入口仅用于 PSP");
    if (emulator < 0 || emulator >= PACKAGES.length) throw new IOException("模拟器选择无效");
    if (!DownloadStore.READY.equals(item.state)) throw new IOException("文件尚未完整下载并校验");
    String reason = support(item.platform, item.name);
    if (!reason.isEmpty()) throw new IOException(reason);
    File file = RetroApplication.from(context).downloads.complete(item);
    if (!file.isFile() || file.length() != item.size) throw new IOException("本机文件不存在或大小变化，请重新下载");
    if (item.name.toLowerCase(Locale.ROOT).endsWith(".cso") && item.size < 0x8800)
      throw new IOException("此 CSO 小于 34 KiB，PPSSPP 对此类 content URI 无法可靠识别；请导出后在模拟器中手动打开");
    // PPSSPP caches failed lookups after a revoked grant. A fresh URI keeps a later
    // launch (e.g. after process death/update) independent of the old cached entry.
    Uri uri =
        FileProvider.getUriForFile(context, context.getPackageName() + ".games", file)
            .buildUpon()
            .appendQueryParameter("launch", java.util.UUID.randomUUID().toString())
            .build();
    Intent intent =
        new Intent(Intent.ACTION_VIEW)
            .setComponent(new ComponentName(PACKAGES[emulator], "org.ppsspp.ppsspp.PpssppActivity"))
            .setDataAndType(uri, "application/octet-stream")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
    intent.setClipData(ClipData.newRawUri("RetroOne game", uri));
    return intent;
  }
}
