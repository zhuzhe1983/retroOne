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
    if (!"psp".equals(platform)) return "此平台尚未接入经过验证的 Android 模拟器；可保存服务允许下载的独立文件";
    String lower = name.toLowerCase(Locale.ROOT);
    if (lower.endsWith(".pbp"))
      return "PBP 暂不支持直接启动：PPSSPP 1.20.4 对本应用的 content URI 存在目录解析问题。可下载保留，不会假装启动成功";
    if (!(lower.endsWith(".iso") || lower.endsWith(".cso")))
      return "当前直启仅开放已验证传递方式的 PSP ISO / CSO；CHD、PBP 和压缩包可下载，但尚未接入可靠直启";
    return "";
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
