package io.retroone.android;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;

/**
 * Bounded, read-only screening for standalone cartridge images. Passing this check is not proof of
 * compatibility or a complete dump; the selected core, real rendering and input still need testing.
 *
 * <p>Core/firmware scope: docs.libretro.com/library/{snes9x,gambatte,mgba,genesis_plus_gx,
 * beetle_pce_fast}/. Header locations follow the respective upstream core loaders. Deliberately no
 * archive extraction, byte swapping, header repair, firmware download or CD/subsystem support.
 */
final class CartridgePolicy {
  private static final long MIB = 1024L * 1024;

  static final class Spec {
    final String id;
    final String label;
    final String core;
    final String probeAsset;
    final List<String> extensions;
    final long maxBytes;
    final String biosNote;

    private Spec(
        String id,
        String label,
        String core,
        String probeExtension,
        long maxBytes,
        String biosNote,
        String... extensions) {
      this.id = id;
      this.label = label;
      this.core = core + "_libretro_android.so";
      this.probeAsset = "retroone-" + id + "-probe" + probeExtension;
      this.extensions = List.of(extensions);
      this.maxBytes = maxBytes;
      this.biosNote = biosNote;
    }
  }

  private static final List<Spec> SPECS =
      List.of(
          new Spec(
              "snes",
              "SFC",
              "snes9x",
              ".sfc",
              16 * MIB + 512,
              "普通 SFC 卡带无需额外 BIOS；BS-X、Sufami Turbo 和 MSU-1 多文件内容请手动导入。",
              ".sfc",
              ".smc"),
          new Spec(
              "gb",
              "GB",
              "gambatte",
              ".gb",
              8 * MIB,
              "GB 启动画面 BIOS 为可选项；保留 RetroArch 现有核心设置。",
              ".gb"),
          new Spec(
              "gbc",
              "GBC",
              "gambatte",
              ".gbc",
              8 * MIB,
              "GBC 启动画面 BIOS 为可选项；保留 RetroArch 现有核心设置。",
              ".gbc",
              ".gb"),
          new Spec(
              "gba",
              "GBA",
              "mgba",
              ".gba",
              32 * MIB,
              "mGBA 的 GBA BIOS 为可选项；部分游戏与现有核心设置仍需实际验证。",
              ".gba"),
          new Spec(
              "megadrive",
              "MD",
              "genesis_plus_gx",
              ".md",
              16 * MIB + 512,
              "MD 启动 BIOS 为可选项；不含 Mega CD、32X、PICO 或外部锁定卡带。",
              ".md",
              ".gen",
              ".bin",
              ".smd"),
          new Spec(
              "mastersystem",
              "SMS",
              "genesis_plus_gx",
              ".sms",
              8 * MIB + 512,
              "SMS 启动 BIOS 为可选项；保留 RetroArch 现有核心设置。",
              ".sms"),
          new Spec(
              "gamegear",
              "GG",
              "genesis_plus_gx",
              ".gg",
              8 * MIB + 512,
              "GG 启动 BIOS 为可选项；保留 RetroArch 现有核心设置。",
              ".gg"),
          new Spec(
              "pce",
              "PCE",
              "mednafen_pce_fast",
              ".pce",
              4 * MIB + 512,
              "仅支持无需 CD BIOS 的 HuCard 卡带；PCE CD 和 SuperGrafx 请使用对应核心手动导入。",
              ".pce"));

  static List<Spec> all() {
    return SPECS;
  }

  static Spec spec(String platform) {
    if (platform == null) return null;
    String id = platform.trim().toLowerCase(Locale.ROOT);
    id =
        switch (id) {
          case "sfc", "nintendo - super nintendo entertainment system" -> "snes";
          case "nintendo - game boy" -> "gb";
          case "nintendo - game boy color" -> "gbc";
          case "nintendo - game boy advance" -> "gba";
          case "md", "genesis", "sega - mega drive - genesis" -> "megadrive";
          case "sms", "sega - master system - mark iii" -> "mastersystem";
          case "gg", "sega - game gear" -> "gamegear";
          case "pcengine", "nec - pc engine - turbografx 16" -> "pce";
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
    if (value == null) return "此平台尚未接入独立卡带启动，请手动导入对应模拟器。";
    if (extension(value, name) != null) return "";
    return value.label
        + " 目前仅支持独立 "
        + String.join(" / ", value.extensions)
        + " 文件；压缩包、光盘及多文件内容请手动导入，不会自动解包或改写 ROM。";
  }

  static String stagedName(Spec value, String sha256, String name) throws IOException {
    requireSpec(value);
    if (sha256 == null || !sha256.matches("[a-f0-9]{64}")) throw new IOException("文件摘要无效");
    String extension = extension(value, name);
    if (extension == null) throw new IOException(formatReason(value.id, name));
    return "ro-" + value.id + "-" + sha256 + extension;
  }

  /**
   * Only generated basenames, never paths or externally supplied core names, may reach an Intent.
   */
  static boolean isStagedName(Spec value, String name) {
    if (value == null || !SPECS.contains(value) || name == null) return false;
    for (String extension : value.extensions) {
      if (name.matches("ro-" + value.id + "-[a-f0-9]{64}\\" + extension)) return true;
    }
    return false;
  }

  private static String extension(Spec value, String name) {
    if (name == null
        || name.isEmpty()
        || name.codePoints()
            .anyMatch(c -> c == '/' || c == '\\' || c == ':' || Character.isISOControl(c)))
      return null;
    String lower = name.toLowerCase(Locale.ROOT);
    for (String extension : value.extensions) {
      if (lower.endsWith(extension) && lower.length() > extension.length()) return extension;
    }
    return null;
  }

  private static void requireSpec(Spec value) throws IOException {
    if (value == null || !SPECS.contains(value)) throw new IOException("卡带平台无效");
  }

  /** The caller separately checks the download's original filename via formatReason/stagedName. */
  static void validateFile(Spec value, File file) throws IOException {
    requireSpec(value);
    if (file == null || !file.isFile() || !file.canRead()) throw new IOException("本地卡带文件不存在或不可读");
    long size = file.length();
    if (size < 192 || size > value.maxBytes)
      throw new IOException(value.label + " 文件大小超出本次独立卡带检查范围，请手动核对原文件");
    try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
      byte[] head = read(in, 0, (int) Math.min(512, size));
      rejectForeignContent(value, head);
      rejectKnownSegaContent(value, in, head, size);
      switch (value.id) {
        case "gb", "gbc" -> validateGameBoy(value, head, size);
        case "gba" -> validateGba(value, head, size);
        case "megadrive" -> {
          long payloadSize = size;
          // Match Genesis Plus GX's 512-byte copier-header / 16 KiB SMD block layout.
          // Its loader does not require AA BB 06; inspect only the logical header, never rewrite.
          if (!ascii(head, 0x100, "SEGA") && size >= 16896 && size % 16384 == 512) {
            head = smdHeader(in);
            payloadSize -= 512;
            rejectForeignContent(value, head);
          }
          validateMd(value, head, payloadSize);
        }
        case "snes" -> validateSnes(value, in, size);
        case "mastersystem", "gamegear" -> validateSega8(value, in, size);
        case "pce" -> validatePce(value, in, size);
        default -> throw new IOException("卡带平台无效");
      }
      if (in.length() != size || file.length() != size)
        throw new IOException("检查期间文件大小发生变化，请等待下载完成后重试");
    }
  }

  private static void rejectForeignContent(Spec value, byte[] head) throws IOException {
    if (ascii(head, 0, "PK\003\004")
        || ascii(head, 0, "PK\005\006")
        || ascii(head, 0, "7z\274\257\047\034")
        || ascii(head, 0, "Rar!")
        || ascii(head, 0, "MComprHD")
        || ascii(head, 0, "NES\032")
        || ascii(head, 0, "FDS\032")
        || ascii(head, 0, "\177ELF")
        || ascii(head, 0, "SEGADISCSYSTEM")
        || ascii(head, 16, "SEGADISCSYSTEM")) throw checkError(value, "内容属于压缩包、其他平台或光盘格式");
    String text =
        new String(head, 0, Math.min(head.length, 96), StandardCharsets.US_ASCII)
            .trim()
            .toLowerCase(Locale.ROOT);
    if (text.startsWith("<!doctype html") || text.startsWith("<html"))
      throw checkError(value, "内容是网页而非卡带镜像");
    if (!value.id.equals("megadrive") && ascii(head, 0x100, "SEGA"))
      throw checkError(value, "内容含 MD 平台标识");
    if (!value.id.equals("gba") && gbaHeader(head)) throw checkError(value, "内容含 GBA 平台头部");
    if (!value.id.equals("gb") && !value.id.equals("gbc") && recognizableGameBoyHeader(head))
      throw checkError(value, "内容含 GB / GBC 平台头部");
  }

  private static void rejectKnownSegaContent(
      Spec value, RandomAccessFile in, byte[] head, long size) throws IOException {
    // These are explicit signatures only. A bank-aligned, headerless HuCard or homebrew
    // must not be assigned to another console merely because its vectors look plausible.
    if (!value.id.equals("mastersystem") && !value.id.equals("gamegear")) {
      long prefix = size % 8192 == 512 ? 512 : 0;
      for (long position : new long[] {0x1ff0, 0x3ff0, 0x7ff0}) {
        if (prefix + position + 16 <= size && ascii(read(in, prefix + position, 16), 0, "TMR SEGA"))
          throw checkError(value, "内容含 SMS / GG 平台标识");
      }
    }
    if (!value.id.equals("megadrive")
        && !ascii(head, 0x100, "SEGA")
        && size >= 16896
        && size % 16384 == 512
        && ascii(smdHeader(in), 0x100, "SEGA")) throw checkError(value, "内容含 SMD 交织布局的 MD 平台标识");
  }

  private static byte[] smdHeader(RandomAccessFile in) throws IOException {
    byte[] odd = read(in, 512, 256);
    byte[] even = read(in, 512 + 8192, 256);
    byte[] logical = new byte[512];
    for (int i = 0; i < 256; i++) {
      logical[i * 2] = even[i];
      logical[i * 2 + 1] = odd[i];
    }
    return logical;
  }

  private static void validateGameBoy(Spec value, byte[] head, long size) throws IOException {
    if (size < 32768 || size % 16384 != 0 || !gameBoyHeader(head))
      throw checkError(value, "卡带头部或 16 KiB 分区长度未通过检查");
    int code = head[0x148] & 255;
    long declared =
        switch (code) {
          case 0, 1, 2, 3, 4, 5, 6, 7, 8 -> 32768L << code;
          case 0x52 -> 72L * 16384;
          case 0x53 -> 80L * 16384;
          case 0x54 -> 96L * 16384;
          default -> 0;
        };
    if (declared == 0 || size < declared) throw checkError(value, "卡带头部声明的容量与文件长度不符，可能不完整");
    if (value.id.equals("gb") && (head[0x143] & 0xc0) == 0xc0)
      throw checkError(value, "这是仅适用于 GBC 的卡带，请核对收藏平台");
  }

  private static boolean gameBoyHeader(byte[] head) {
    if (head.length < 0x150) return false;
    int checksum = 0;
    for (int i = 0x134; i <= 0x14c; i++) checksum = (checksum - (head[i] & 255) - 1) & 255;
    return checksum == (head[0x14d] & 255);
  }

  private static boolean recognizableGameBoyHeader(byte[] head) {
    if (!gameBoyHeader(head)) return false;
    int sizeCode = head[0x148] & 255;
    if (sizeCode > 8 && sizeCode != 0x52 && sizeCode != 0x53 && sizeCode != 0x54) return false;
    // A checksum alone has 1/256 accidental matches in arbitrary PCE/SNES program data.
    return (head[0x100] & 255) == 0xc3
        || (head[0x100] & 255) == 0x18
        || (head[0x100] == 0 && (head[0x101] & 255) == 0xc3)
        || ((head[0x104] & 255) == 0xce
            && (head[0x105] & 255) == 0xed
            && (head[0x106] & 255) == 0x66
            && (head[0x107] & 255) == 0x66);
  }

  private static void validateGba(Spec value, byte[] head, long size) throws IOException {
    if (size <= 192 || size % 4 != 0 || !gbaHeader(head))
      throw checkError(value, "GBA 固定字节、头部校验或长度未通过检查");
  }

  private static boolean gbaHeader(byte[] head) {
    if (head.length < 192 || (head[0xb2] & 255) != 0x96) return false;
    int checksum = 0x19;
    for (int i = 0xa0; i <= 0xbc; i++) checksum += head[i] & 255;
    return ((checksum + (head[0xbd] & 255)) & 255) == 0;
  }

  private static void validateMd(Spec value, byte[] head, long size) throws IOException {
    if (size < 512 || (size & 1) != 0) throw checkError(value, "MD 卡带长度未通过检查");
    String console = new String(head, 0x100, 16, StandardCharsets.US_ASCII);
    if (console.contains("32X") || console.contains("MARS") || console.contains("PICO"))
      throw checkError(value, "32X / PICO 不属于此 MD 核心入口");
    // Original homebrew need not carry SEGA's console string or a retail checksum.
    long stack = big32(head, 0), reset = big32(head, 4);
    if (stack < 0xff0000L
        || stack > 0x1000000L
        || (stack & 1) != 0
        || reset < 8
        || reset >= size
        || (reset & 1) != 0) throw checkError(value, "未识别到可检查的 MD 复位向量或受支持的 SMD 交织布局");
    if (ascii(head, 0x100, "SEGA") && big32(head, 0x1a0) == 0) {
      long end = big32(head, 0x1a4);
      if (end >= 511 && end < value.maxBytes && end + 1 > size)
        throw checkError(value, "MD 头部声明的 ROM 范围超出文件长度，可能不完整");
    }
  }

  private static void validateSnes(Spec value, RandomAccessFile in, long size) throws IOException {
    long prefix = size % 8192 == 512 ? 512 : 0;
    if (size - prefix < 32768 || (size - prefix) % 8192 != 0)
      throw checkError(value, "SFC 卡带长度未通过检查");
    if (ascii(read(in, prefix, 16), 0, "BANDAI SFC-ADX"))
      throw checkError(value, "Sufami Turbo 多卡带内容需手动导入");
    // No mandatory title, maker bytes or checksum: original homebrew commonly omits these.
    for (long position : new long[] {0x7fc0, 0xffc0, 0x407fc0, 0x40ffc0}) {
      if (prefix + position + 64 > size) continue;
      byte[] header = read(in, prefix + position, 64);
      int mode = header[0x15] & 255;
      int reset = little16(header, 0x3c);
      if ((mode & 0xe0) == 0x20 && reset >= 0x8000 && reset != 0xffff) return;
      // A headerless homebrew with executable reset target can also be checked conservatively.
      long target =
          prefix + (position & ~0xffffL) + (position % 0x10000 == 0x7fc0 ? reset & 0x7fff : reset);
      if (mode == 0 && reset >= 0x8000 && reset != 0xffff && target < size) {
        int opcode = read(in, target, 1)[0] & 255;
        if (opcode != 0 && opcode != 255) return;
      }
    }
    throw checkError(value, "未识别到可检查的 SFC 映射头部与复位向量");
  }

  private static void validateSega8(Spec value, RandomAccessFile in, long size) throws IOException {
    long prefix = size % 8192 == 512 ? 512 : 0;
    if (size - prefix < 8192 || (size - prefix) % 8192 != 0)
      throw checkError(value, "8 KiB 卡带分区长度未通过检查");
    for (long position : new long[] {0x1ff0, 0x3ff0, 0x7ff0}) {
      if (prefix + position + 16 > size) continue;
      byte[] header = read(in, prefix + position, 16);
      if (!ascii(header, 0, "TMR SEGA")) continue;
      int region = (header[15] & 255) >>> 4;
      if ((value.id.equals("mastersystem") && region >= 5 && region <= 7)
          || (value.id.equals("gamegear") && (region == 3 || region == 4)))
        throw checkError(value, "SMS / GG 平台标识与收藏平台不符");
      int code = header[15] & 15;
      long declared =
          switch (code) {
            case 0 -> 256 * 1024;
            case 1 -> 512 * 1024;
            case 2 -> 1024 * 1024;
            case 0xa -> 8192;
            case 0xb -> 16384;
            case 0xc -> 32768;
            case 0xd -> 49152;
            case 0xe -> 65536;
            case 0xf -> 131072;
            default -> 0;
          };
      if (declared > size - prefix) throw checkError(value, "卡带头部声明的容量超出文件长度，可能不完整");
      return;
    }
    // Unlicensed and homebrew cartridges may have no TMR header. Leave execution to the core.
    rejectBlankStart(value, read(in, prefix, 512));
  }

  private static void validatePce(Spec value, RandomAccessFile in, long size) throws IOException {
    long prefix = size % 8192 == 512 ? 512 : 0;
    if (size - prefix < 8192 || (size - prefix) % 8192 != 0)
      throw checkError(value, "HuCard 的 8 KiB 分区长度未通过检查");
    // HuCards have no universal magic/header. This only rules out empty/truncated basic images.
    byte[] start = read(in, prefix, 512);
    rejectBlankStart(value, start);
    int reset = little16(read(in, prefix + 0x1ffe, 2), 0);
    if (reset == 0 || reset == 0xffff) throw checkError(value, "未识别到可检查的 HuCard 复位向量");
  }

  private static void rejectBlankStart(Spec value, byte[] bytes) throws IOException {
    int first = bytes[0] & 255;
    if (first != 0 && first != 255) return;
    for (byte b : bytes) if ((b & 255) != first) return;
    throw checkError(value, "卡带起始区域为空白，需核对文件格式");
  }

  private static IOException checkError(Spec value, String detail) {
    return new IOException(value.label + "：" + detail + "。这不代表下载损坏；请核对版本或在模拟器中手动检查，客户端不会修补原文件。");
  }

  private static byte[] read(RandomAccessFile in, long position, int length) throws IOException {
    byte[] bytes = new byte[length];
    in.seek(position);
    in.readFully(bytes);
    return bytes;
  }

  private static boolean ascii(byte[] bytes, int offset, String text) {
    if (offset + text.length() > bytes.length) return false;
    for (int i = 0; i < text.length(); i++)
      if ((bytes[offset + i] & 255) != text.charAt(i)) return false;
    return true;
  }

  private static int little16(byte[] bytes, int offset) {
    return (bytes[offset] & 255) | (bytes[offset + 1] & 255) << 8;
  }

  private static long big32(byte[] bytes, int offset) {
    return (long) (bytes[offset] & 255) << 24
        | (long) (bytes[offset + 1] & 255) << 16
        | (long) (bytes[offset + 2] & 255) << 8
        | bytes[offset + 3] & 255L;
  }
}
