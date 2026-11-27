package io.retroone.android;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;

/**
 * Read-only format screening for the additional RetroArch runtimes. Passing these checks
 * establishes only the supported container/structure, not game compatibility, BIOS availability or
 * playability.
 *
 * <p>N64 signatures follow mupen64plus-core/src/main/rom.c; DS fields and bounds follow melonDS
 * src/NDS_Header.h and src/NDSCart.cpp. DS capacity, proprietary logo and header CRC are
 * deliberately not compatibility gates: trimmed images and homebrew need not have retail headers.
 */
final class RuntimePolicy {
  private static final long MIB = 1024L * 1024;

  static final class Spec {
    final String id;
    final String label;
    final String core;
    final long maxBytes;
    final long maxArchiveBytes;
    final List<String> extensions;
    final String notes;

    private Spec(
        String id,
        String label,
        String core,
        long maxBytes,
        long maxArchiveBytes,
        String notes,
        String... extensions) {
      this.id = id;
      this.label = label;
      this.core = core + "_libretro_android.so";
      this.maxBytes = maxBytes;
      this.maxArchiveBytes = maxArchiveBytes;
      this.extensions = List.of(extensions);
      this.notes = notes;
    }
  }

  private static final List<Spec> SPECS =
      List.of(
          new Spec(
              "n64",
              "N64",
              "mupen64plus_next_gles3",
              64 * MIB,
              128 * MIB,
              "普通 N64 卡带无需额外 BIOS；仅接通 GLES3 核心。64DD、Transfer Pak 和多文件子系统请手动导入。",
              ".z64",
              ".n64",
              ".v64"),
          new Spec(
              "nds",
              "NDS",
              "melondsds",
              512 * MIB,
              576 * MIB,
              "仅接通 DS 模式的独立卡带；DS BIOS 和固件为可选项，保留现有核心设置。DSi 专用内容、Slot-2 和外部 SD"
                  + " 依赖请手动导入。双屏、触控与运行速度需实际验证。",
              ".nds"),
          new Spec(
              "psx",
              "PlayStation",
              "pcsx_rearmed",
              2048 * MIB,
              0,
              "仅接通独立 CHDv5 光盘。建议在 RetroArch 的 system 目录提供 scph5500/5501/5502 中对应区域的 BIOS；缺少时核心可能使用"
                  + " HLE，兼容性需实际确认。BIN/CUE、多碟 M3U、外部补丁及父文件依赖请手动导入。",
              ".chd"),
          new Spec(
              "dreamcast",
              "Dreamcast",
              "flycast",
              2048 * MIB,
              0,
              "仅接通独立、无父依赖的 CHDv5 光盘；保留 Flycast 原有 BIOS/HLE 和 VMU 设置。GDI/CUE、多碟、NAOMI ROM"
                  + " 集及依赖包请手动导入。容器检查不代表平台识别或游戏兼容。",
              ".chd"),
          new Spec(
              "saturn",
              "SEGA Saturn",
              "mednafen_saturn",
              2048 * MIB,
              0,
              "仅接通独立 CHDv5 光盘。日本/亚洲 NTSC 游戏需要 system/sega_101.bin，其他区域需要 system/mpr-17933.bin；特殊"
                  + " ROM 卡仍需另配。BIN/CUE、多碟 M3U 和父文件依赖请手动导入，运行速度需逐款确认。",
              ".chd"));

  static List<Spec> all() {
    return SPECS;
  }

  static Spec spec(String platform) {
    if (platform == null) return null;
    String id = platform.trim().toLowerCase(Locale.ROOT);
    id =
        switch (id) {
          case "nintendo - nintendo 64" -> "n64";
          case "nintendo - nintendo ds", "nintendo ds" -> "nds";
          case "ps1", "playstation", "sony - playstation" -> "psx";
          case "dc", "sega - dreamcast" -> "dreamcast";
          case "sega - saturn", "sega saturn" -> "saturn";
          default -> id;
        };
    for (Spec value : SPECS) if (value.id.equals(id)) return value;
    return null;
  }

  static boolean supports(String platform) {
    return spec(platform) != null;
  }

  static String formatReason(String platform, String name) {
    Spec value = spec(platform);
    if (value == null) return "此平台尚未接入本次运行核心，请手动导入对应模拟器。";
    if (isRomName(value, name) || (value.maxArchiveBytes > 0 && isZipName(name))) return "";
    if (value.maxArchiveBytes > 0)
      return value.label
          + " 支持独立 "
          + String.join(" / ", value.extensions)
          + " 或只含一份对应卡带的 ZIP；多游戏包、嵌套压缩及多文件依赖请手动导入。";
    return value.label + " 目前仅支持独立、无父依赖的 .chd（CHDv5）；ZIP、CUE/GDI、轨道文件和多碟 M3U 请手动导入。";
  }

  static boolean isRomName(Spec value, String name) {
    return extension(value, name) != null;
  }

  static String stagedName(Spec value, String sha256, String name) throws IOException {
    requireSpec(value);
    if (sha256 == null || !sha256.matches("[a-f0-9]{64}")) throw new IOException("文件摘要无效");
    String extension = extension(value, name);
    if (extension == null) throw new IOException("启动副本需要已校验的独立 ROM 文件，不能使用压缩包或路径名");
    return "ro-" + value.id + "-" + sha256 + extension;
  }

  /** Only generated basenames from this fixed core/platform registry may reach an Intent. */
  static boolean validStagedName(String name) {
    if (!safeBasename(name)) return false;
    for (Spec value : SPECS) {
      for (String extension : value.extensions) {
        if (name.matches("ro-" + value.id + "-[a-f0-9]{64}\\" + extension)) return true;
      }
    }
    return false;
  }

  /** Does not extract archives or change bytes. Original filenames are checked separately. */
  static void validateFile(Spec value, File file) throws IOException {
    requireSpec(value);
    interrupted();
    if (file == null || !file.isFile() || !file.canRead()) throw new IOException("本地文件不存在或不可读");
    long size = file.length();
    long modified = file.lastModified();
    long minimum = value.maxArchiveBytes > 0 ? 0x1000 : 124;
    if (size < minimum || size > value.maxBytes)
      throw new IOException(value.label + " 文件大小超出本次独立格式检查范围，请核对原文件");
    if (value.maxArchiveBytes == 0) {
      ChdImage.validate(file, value.id.equals("dreamcast"));
    } else {
      try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
        byte[] header = new byte[512];
        in.readFully(header);
        if (value.id.equals("n64")) validateN64(header, size);
        else {
          rejectForeignNds(in, header, size);
          validateNds(header, size);
        }
        if (in.length() != size) throw new IOException("检查期间文件大小发生变化，请等待下载完成后重试");
      }
    }
    interrupted();
    if (file.length() != size || file.lastModified() != modified)
      throw new IOException("检查期间文件发生变化，请等待下载完成后重试");
  }

  private static void validateN64(byte[] header, long size) throws IOException {
    long magic = be32(header, 0);
    if (magic != 0x80371240L && magic != 0x37804012L && magic != 0x40123780L)
      throw new IOException("未识别到 N64 卡带头部；仅更改扩展名不能转换文件格式");
    if ((magic == 0x37804012L && size % 2 != 0) || (magic == 0x40123780L && size % 4 != 0))
      throw new IOException("N64 卡带长度不符合头部字节序的对齐要求，请核对原文件");
  }

  private static void validateNds(byte[] header, long size) throws IOException {
    int unit = header[0x12] & 255;
    if (unit == 3) throw new IOException("此文件为 DSi 专用内容，需要独立 BIOS、固件和 NAND，请手动导入");
    if (unit != 0 && unit != 2) throw new IOException("NDS 头部硬件类型未识别；本次仅接通 DS 与 DS/DSi 双模式卡带");
    checkNdsRange(header, 0x20, 0x2c, size, "ARM9", true);
    checkNdsRange(header, 0x30, 0x3c, size, "ARM7", true);
    checkNdsRange(header, 0x40, 0x44, size, "文件名表", false);
    checkNdsRange(header, 0x48, 0x4c, size, "文件分配表", false);
    checkNdsRange(header, 0x50, 0x54, size, "ARM9 overlay 表", false);
    checkNdsRange(header, 0x58, 0x5c, size, "ARM7 overlay 表", false);
  }

  private static void checkNdsRange(
      byte[] header, int offsetField, int lengthField, long size, String label, boolean required)
      throws IOException {
    long offset = le32(header, offsetField), length = le32(header, lengthField);
    if (length == 0 && !required) return;
    if (length == 0 || offset < 0x200 || offset > size || length > size - offset)
      throw new IOException("NDS " + label + " 段范围超出实际文件或为空，请核对原文件");
  }

  private static void rejectForeignNds(RandomAccessFile in, byte[] header, long size)
      throws IOException {
    long magic = be32(header, 0);
    if (magic == 0x80371240L || magic == 0x37804012L || magic == 0x40123780L)
      throw new IOException("文件包含 N64 平台标识，不能按 NDS 卡带启动");
    if (ascii(header, 0, "PK\003\004")
        || ascii(header, 0, "PK\005\006")
        || ascii(header, 0, "PK\007\010")
        || ascii(header, 0, "7z\274\257\047\034")
        || ascii(header, 0, "Rar!")
        || ascii(header, 0, "MComprHD")
        || ascii(header, 0, "NES\032")
        || ascii(header, 0, "FDS\032")
        || ascii(header, 0, "\177ELF")
        || ascii(header, 0, "SEGADISCSYSTEM")
        || ascii(header, 16, "SEGADISCSYSTEM")
        || ascii(header, 0, "SEGA SEGASATURN")
        || ascii(header, 0, "SEGA SEGAKATANA")
        || ascii(header, 0x100, "SEGA")) throw new IOException("文件包含压缩包、光盘或其他平台标识，不能按 NDS 卡带启动");
    String text =
        new String(header, 0, 96, StandardCharsets.US_ASCII).trim().toLowerCase(Locale.ROOT);
    if (text.startsWith("<!doctype html") || text.startsWith("<html") || text.startsWith("<?xml"))
      throw new IOException("文件是网页或 XML 响应，不能按 NDS 卡带启动");
    for (int prefix : new int[] {0, 512}) {
      for (int offset : new int[] {0x1ff0, 0x3ff0, 0x7ff0}) {
        long position = (long) prefix + offset;
        if (position + 8 > size) continue;
        byte[] sega = new byte[8];
        in.seek(position);
        in.readFully(sega);
        if (ascii(sega, 0, "TMR SEGA")) throw new IOException("文件包含 SMS / GG 平台标识，不能按 NDS 卡带启动");
      }
    }
    // Genesis Plus GX's SMD layout: 512-byte copier header, then interleaved 16 KiB blocks.
    if (size >= 16896 && size % 16384 == 512) {
      byte[] logical = new byte[4];
      for (int i = 0; i < logical.length; i++) {
        int offset = 0x100 + i;
        in.seek(512L + (offset % 2 == 0 ? 8192 : 0) + offset / 2);
        logical[i] = in.readByte();
      }
      if (ascii(logical, 0, "SEGA")) throw new IOException("文件包含交织 MD / SMD 平台标识，不能按 NDS 卡带启动");
    }
  }

  private static boolean ascii(byte[] bytes, int offset, String value) {
    if (offset < 0 || offset > bytes.length - value.length()) return false;
    for (int i = 0; i < value.length(); i++)
      if ((bytes[offset + i] & 255) != value.charAt(i)) return false;
    return true;
  }

  private static long le32(byte[] bytes, int offset) {
    return (bytes[offset] & 255L)
        | ((bytes[offset + 1] & 255L) << 8)
        | ((bytes[offset + 2] & 255L) << 16)
        | ((bytes[offset + 3] & 255L) << 24);
  }

  private static long be32(byte[] bytes, int offset) {
    return ((bytes[offset] & 255L) << 24)
        | ((bytes[offset + 1] & 255L) << 16)
        | ((bytes[offset + 2] & 255L) << 8)
        | (bytes[offset + 3] & 255L);
  }

  private static String extension(Spec value, String name) {
    if (value == null || !SPECS.contains(value) || !safeBasename(name)) return null;
    String lower = name.toLowerCase(Locale.ROOT);
    for (String extension : value.extensions)
      if (lower.endsWith(extension) && lower.length() > extension.length()) return extension;
    return null;
  }

  private static boolean isZipName(String name) {
    return safeBasename(name)
        && name.length() > 4
        && name.toLowerCase(Locale.ROOT).endsWith(".zip");
  }

  private static boolean safeBasename(String name) {
    return name != null
        && !name.isEmpty()
        && !name.codePoints()
            .anyMatch(c -> c == '/' || c == '\\' || c == ':' || Character.isISOControl(c));
  }

  private static void requireSpec(Spec value) throws IOException {
    if (value == null || !SPECS.contains(value)) throw new IOException("运行平台无效");
  }

  private static void interrupted() throws IOException {
    if (Thread.currentThread().isInterrupted()) throw new IOException("文件检查已取消");
  }
}
