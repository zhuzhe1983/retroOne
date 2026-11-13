package io.retroone.android;

/** Reuses the web library's platform silhouettes, with short labels for a handheld screen. */
final class PlatformArt {
  static String shortName(String id, String name) {
    return switch (id) {
      case "nes" -> "FC";
      case "snes" -> "SFC";
      case "gb" -> "Game Boy";
      case "gbc" -> "GBC";
      case "gba" -> "GBA";
      case "nds" -> "NDS";
      case "3ds" -> "3DS";
      case "psp" -> "PSP";
      case "psx", "ps1" -> "PlayStation";
      case "ps2" -> "PS2";
      case "n64" -> "N64";
      case "switch" -> "Switch";
      case "fbneo" -> "街机 FBNeo";
      case "megadrive" -> "MD";
      case "dreamcast" -> "Dreamcast";
      default -> name;
    };
  }

  static int resource(String id) {
    if (id.equals("switch")) return R.drawable.platform_switch;
    if (id.equals("nds") || id.equals("3ds")) return R.drawable.platform_ds;
    if (id.matches("gb|gbc|gameboy|virtualboy|ngp|ngpc|pokemini|supervision"))
      return R.drawable.platform_gameboy;
    if (id.matches("gba|psp|psvita|gamegear|lynx|wonderswan|wonderswancolor|ws|wsc"))
      return R.drawable.platform_psp;
    if (id.matches(".*(arcade|mame|fbneo|fba|cps|lightgun|teknoparrot|model2|model3|naomi).*"))
      return R.drawable.platform_arcade;
    if (id.matches(".*(dos|windows|pc98|scumm|amiga|msx|pc|c64|zx|amstrad).*"))
      return R.drawable.platform_pc;
    if (id.equals("nes") || id.equals("fds")) return R.drawable.platform_nes;
    if (id.equals("snes")) return R.drawable.platform_snes;
    if (id.equals("n64")) return R.drawable.platform_n64;
    if (id.equals("gamecube")) return R.drawable.platform_cube;
    if (id.startsWith("wii")) return R.drawable.platform_wii;
    if (id.matches("psx|ps1|playstation")) return R.drawable.platform_ps;
    if (id.matches("ps[2345].*")) return R.drawable.platform_ps2;
    if (id.contains("xbox")) return R.drawable.platform_xbox;
    if (id.matches("dreamcast|saturn|3do|segacd|pcenginecd|pcecd|pcfx|neogeocd"))
      return R.drawable.platform_disc;
    return R.drawable.platform_controller;
  }
}
