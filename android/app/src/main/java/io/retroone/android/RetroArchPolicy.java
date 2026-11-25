package io.retroone.android;

import java.io.IOException;
import java.util.Locale;

/** Platform and path rules without Android dependencies, also exercised on the JVM. */
final class RetroArchPolicy {
  static final String PACKAGE = "com.retroarch.aarch64";
  static final String ACTIVITY = "com.retroarch.browser.retroactivity.RetroActivityFuture";
  static final String CORE = "fceumm_libretro_android.so";

  static boolean supports(String platform) {
    return "nes".equals(platform);
  }

  static String formatReason(String name) {
    return name.toLowerCase(Locale.ROOT).endsWith(".nes")
        ? ""
        : "NES 目前仅支持独立 .nes 文件；压缩包、FDS 和其他格式请手动导入对应模拟器。";
  }

  static LaunchPolicy.State assess(
      String name,
      boolean ready,
      boolean present,
      boolean installed,
      boolean configured,
      boolean verified) {
    if (!ready) return LaunchPolicy.State.INCOMPLETE;
    if (!present) return LaunchPolicy.State.MISSING;
    if (!formatReason(name).isEmpty()) return LaunchPolicy.State.MANUAL;
    if (!installed) return LaunchPolicy.State.INSTALL_RETROARCH;
    if (!configured) return LaunchPolicy.State.CONFIGURE_RETROARCH;
    return verified ? LaunchPolicy.State.PLAYABLE : LaunchPolicy.State.VERIFY_RETROARCH;
  }

  static String relativeDirectory(String documentId) throws IOException {
    if (!documentId.startsWith("primary:"))
      throw new IOException("请选择本机内部共享存储中的文件夹，暂不支持 SD 卡或网络目录");
    String path = documentId.substring(8);
    if (path.isEmpty() || path.startsWith("/") || path.endsWith("/"))
      throw new IOException("请选择一个文件夹，例如 ROMs；不要选择存储根目录");
    for (String segment : path.split("/", -1)) {
      if (segment.isEmpty()
          || segment.equals(".")
          || segment.equals("..")
          || segment.matches(".*[\\\\:\\p{Cntrl}].*")) throw new IOException("目录路径无效");
    }
    if (path.split("/", 2)[0].equalsIgnoreCase("Android"))
      throw new IOException("请选择 Android 应用数据目录以外的文件夹，例如 ROMs");
    return path;
  }

  static String fileName(String sha256) throws IOException {
    if (!sha256.matches("[a-f0-9]{64}")) throw new IOException("文件摘要无效");
    return "ro-" + sha256 + ".nes";
  }

  static void validateHeader(byte[] header, long size) throws IOException {
    if (header.length < 16
        || size < 16
        || size > 64L * 1024 * 1024
        || header[0] != 'N'
        || header[1] != 'E'
        || header[2] != 'S'
        || header[3] != 0x1a) throw new IOException("文件不是完整的 NES 镜像；扩展名正确并不代表内容可运行");
    // NES 2.0 has separate size encoding; the emulator checks its mapper and payload.
    if ((header[7] & 0x0c) != 8) {
      long required =
          16L
              + ((header[6] & 4) == 0 ? 0 : 512)
              + (header[4] & 255) * 16384L
              + (header[5] & 255) * 8192L;
      if ((header[4] & 255) == 0 || size < required) throw new IOException("NES 镜像内容不完整，请重新检查原文件");
    }
  }
}
