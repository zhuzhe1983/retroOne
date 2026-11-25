package io.retroone.android;

import java.util.Locale;

/**
 * A platform/format adapter, shared by every Play entry point. Never infer support from an
 * extension alone.
 */
final class LaunchPolicy {
  enum State {
    PLAYABLE("▶ 游玩"),
    INSTALL("安装 PPSSPP"),
    INSTALL_EDEN("安装 Eden"),
    OPEN_EDEN("用 Eden 启动"),
    CHECK_FBNEO("检查街机启动"),
    REVIEW_FBNEO("ROM 版本待核对"),
    INSTALL_RETROARCH("安装 RetroArch"),
    CONFIGURE_RETROARCH("设置 NES 模拟器"),
    VERIFY_RETROARCH("测试 NES 启动"),
    MANUAL("手动导入"),
    UNSUPPORTED("待适配"),
    MISSING("文件不可用"),
    INCOMPLETE("下载未完成");
    final String label;

    State(String label) {
      this.label = label;
    }
  }

  static String formatReason(String platform, String name) {
    if ("switch".equals(platform)) return Eden.formatReason(name);
    if ("fbneo".equals(platform))
      return name.toLowerCase(Locale.ROOT).endsWith(".zip")
          ? ""
          : "FBNeo 目前仅接入独立 ZIP ROM 集，不自动解压或重命名其他格式。";
    if (RetroArchPolicy.supports(platform)) return RetroArchPolicy.formatReason(name);
    if (!"psp".equals(platform)) return "此平台尚未适配 Android 模拟器直接启动。文件可以保留或导出，暂不能从 RetroOne 直接游玩。";
    String lower = name.toLowerCase(Locale.ROOT);
    if (lower.endsWith(".pbp")) return "PBP 暂不支持直接启动：PPSSPP 对本应用文件地址存在目录解析问题，请导出后手动打开。";
    if (!(lower.endsWith(".iso") || lower.endsWith(".cso")))
      return "PSP 当前支持 ISO / CSO 直接启动；此格式需导出后手动导入模拟器。";
    return "";
  }

  static State assess(
      String platform,
      String name,
      long size,
      boolean ready,
      boolean filePresent,
      boolean installed) {
    if (!ready) return State.INCOMPLETE;
    if (!filePresent) return State.MISSING;
    if (!"psp".equals(platform)) return State.UNSUPPORTED;
    if (!formatReason(platform, name).isEmpty()
        || (name.toLowerCase(Locale.ROOT).endsWith(".cso") && size < 0x8800)) return State.MANUAL;
    return installed ? State.PLAYABLE : State.INSTALL;
  }
}
