package io.retroone.android;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;

/**
 * Bounded, read-only screening of single-file GameCube ISO/GCM and RVZ containers.
 *
 * <p>Layouts follow Dolphin 2609b (3a5b3743b18da3e8f04cbc44f20dc50fbd3a9ef3),
 * Source/Core/DiscIO/{DiscUtils.h,Volume.cpp,VolumeDisc.cpp,WIABlob.h,WIABlob.cpp} and
 * docs/WiaAndRvz.md. This is not a disc verifier or decompressor: passing does not validate RVZ
 * table contents, compressed data, the filesystem, bootability, or game compatibility. RVZ's
 * embedded header covers only 0x80 logical bytes, so it cannot exclude an internal NKit marker at
 * 0x200. Explicit NKit filenames and the marker in raw images are rejected.
 */
final class DolphinImage {
  static final long MAX_BYTES = 2L * 1024 * 1024 * 1024;
  static final int MIN_GC_BYTES = 0x2460; // boot header, BI2, and apploader header
  static final int MAX_HEADER_BYTES = 4096;
  static final int MAX_TABLE_BYTES = 16 * 1024 * 1024;
  static final int MAX_RAW_ENTRIES = 4096;
  static final int MAX_GROUP_ENTRIES = 1024 * 1024;
  static final int MAX_CHUNK_BYTES = 2 * 1024 * 1024;
  static final int MAX_DICTIONARY_BYTES = 16 * 1024 * 1024;
  private static final int HEADER_1_BYTES = 0x48;
  private static final int HEADER_2_MIN_BYTES = 0xd5;
  private static final long RVZ_MAGIC = 0x52565a01L;
  private static final long GC_MAGIC = 0xc2339f3dL;
  private static final long WII_MAGIC = 0x5d1c9ea3L;
  private static final long RVZ_VERSION = 0x01000000L;
  private static final long RVZ_READ_COMPATIBLE = 0x00030000L;

  static final class Result {
    final String format;
    final String platform;
    final long logicalSize;

    private Result(String format, long logicalSize) {
      this.format = format;
      this.platform = "gc";
      this.logicalSize = logicalSize;
    }
  }

  private DolphinImage() {}

  static boolean supports(String platform) {
    if (platform == null) return false;
    String value = platform.trim().toLowerCase(Locale.ROOT);
    return value.equals("gc") || value.equals("gamecube");
  }

  /** Empty means that the platform and basename are candidates; inspect is still required. */
  static String formatReason(String platform, String name) {
    if (!supports(platform)) return "本轮 Dolphin 仅接通 GameCube；Wii 及其他平台尚未开放。";
    if (!safeBasename(name)) return "需要单一游戏文件名，不能使用路径、空文件名或控制字符。";
    String lower = name.toLowerCase(Locale.ROOT);
    if (lower.contains(".nkit.") || lower.startsWith("nkit."))
      return "本轮不接入显式 NKit 文件，请在 Dolphin 中手动核对。";
    if (lower.matches(".*\\.part[0-9]+(?:\\..*)?"))
      return "本轮不接入分卷镜像；一个文件的读取授权不包含其他分卷。";
    if (extension(lower) != null) return "";
    return "GameCube 目前仅接通单文件 .iso / .gcm / .rvz；WIA、WBFS、WAD、压缩包和多文件内容请手动处理。";
  }

  static Result inspect(File file, String platform, String name) throws IOException {
    interrupted();
    String reason = formatReason(platform, name);
    if (!reason.isEmpty()) throw invalid(reason);
    if (file == null || !file.isFile() || !file.canRead()) throw invalid("本地文件不存在或不可读");
    long size = file.length();
    long modified = file.lastModified();
    interrupted();
    if (size < HEADER_1_BYTES || size > MAX_BYTES)
      throw invalid("文件大小超出本次 2 GiB 以内的单文件检查范围");
    String format = extension(name.toLowerCase(Locale.ROOT));
    long logical;
    try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
      if (format.equals("rvz")) {
        logical = inspectRvz(in, size);
      } else {
        if (size < MIN_GC_BYTES) throw invalid("原始 GC 镜像不足最小启动头部长度");
        byte[] header = read(in, 0, MIN_GC_BYTES);
        long magic = be32(header, 0);
        if (magic == RVZ_MAGIC || magic == 0x57494101L || magic == 0x57424653L)
          throw invalid("容器签名与原始 ISO / GCM 扩展名不一致");
        requireGameCube(header, 0);
        if (be32(header, 0x200) == 0x4e4b4954L)
          throw invalid("原始镜像包含 NKIT 标记，本轮不接入 NKit");
        logical = size;
      }
      interrupted();
      if (in.length() != size || file.length() != size || file.lastModified() != modified)
        throw invalid("检查期间文件发生变化，请等待下载完成后重试");
    }
    interrupted();
    return new Result(format, logical);
  }

  private static long inspectRvz(RandomAccessFile in, long size) throws IOException {
    byte[] first = read(in, 0, HEADER_1_BYTES);
    if (be32(first, 0) != RVZ_MAGIC) throw invalid("文件未包含 RVZ 签名，仅改扩展名不能转换格式");
    long version = be32(first, 4), compatible = be32(first, 8);
    if (version < RVZ_READ_COMPATIBLE || compatible > RVZ_VERSION || compatible > version)
      throw invalid("RVZ 版本不在 Dolphin 2609b 的读取兼容范围内");
    checkHash(first, 0x34, first, 0, 0x34, "RVZ Header 1");
    if (positive64(first, 0x2c) != size) throw invalid("RVZ 声明的物理大小与实际文件不一致");
    long logical = positive64(first, 0x24);
    long headerSize = be32(first, 0x0c);
    interrupted();
    if (headerSize < HEADER_2_MIN_BYTES || headerSize > MAX_HEADER_BYTES)
      throw invalid("RVZ Header 2 长度超出有界检查范围");
    range(HEADER_1_BYTES, headerSize, size, 0, "RVZ Header 2");
    byte[] second = read(in, HEADER_1_BYTES, (int) headerSize);
    checkHash(first, 0x10, second, 0, second.length, "RVZ Header 2");
    requireGameCube(second, 0x10);
    long discType = be32(second, 0);
    if (discType == 2) throw invalid("RVZ 声明为 Wii，本轮仅接通 GameCube");
    if (discType != 0 && discType != 1) throw invalid("RVZ 光盘类型不在本轮 GC 检查范围内");
    interrupted();
    if (logical < MIN_GC_BYTES || logical > MAX_BYTES)
      throw invalid("RVZ 逻辑大小超出本次 GC 最小启动头部至 2 GiB 的范围");

    long chunk = be32(second, 0x0c);
    interrupted();
    if (chunk < 0x8000 || chunk > MAX_CHUNK_BYTES || (chunk & (chunk - 1)) != 0)
      throw invalid("RVZ chunk 必须为 32 KiB 至 2 MiB 范围内的 2 次幂");
    long compression = be32(second, 4);
    compressionParameters(second, compression);

    long partitionCount = be32(second, 0x90);
    interrupted();
    if (partitionCount != 0) throw invalid("RVZ 含 Wii 分区条目，本轮仅接通 GC 镜像");
    // GC legitimately has no partition entries. An unused entry size may be zero, and an empty
    // table may have offset zero. There is no reason to allocate or multiply its declared size.
    range(positive64(second, 0x98), 0, size, 0, "RVZ 空分区表");
    checkHash(second, 0xa0, new byte[0], 0, 0, "RVZ 空分区表");

    long headerEnd = HEADER_1_BYTES + headerSize;
    List<Range> tables = new ArrayList<>(2);
    checkTable(
        second, 0xb4, 0xb8, 0xc0, 0x18, MAX_RAW_ENTRIES, compression, size, headerEnd,
        tables, "RVZ raw 表");
    checkTable(
        second, 0xc4, 0xc8, 0xd0, 0x0c, MAX_GROUP_ENTRIES, compression, size, headerEnd,
        tables, "RVZ group 表");
    // No table decompression, group-content reading, or logical 0x200/NKit claim here.
    return logical;
  }

  private static void checkTable(
      byte[] header, int countField, int offsetField, int sizeField, int entryBytes, int maxEntries,
      long compression, long fileSize, long headerEnd, List<Range> tables, String label)
      throws IOException {
    interrupted();
    long count = be32(header, countField), bytes = be32(header, sizeField);
    // The multiplication is in long and is bounded before it can influence an allocation.
    long expanded = count * entryBytes;
    if (count > maxEntries || expanded > MAX_TABLE_BYTES || bytes > MAX_TABLE_BYTES)
      throw invalid(label + "的条目或长度超出检查预算");
    if (count != 0 && bytes == 0) throw invalid(label + "声明非空但未包含表数据");
    if (compression == 0 && bytes != expanded) throw invalid(label + "的未压缩长度与条目数量不一致");
    Range table =
        range(positive64(header, offsetField), bytes, fileSize, bytes == 0 ? 0 : headerEnd, label);
    if (bytes == 0) return;
    for (Range previous : tables) {
      if (table.start < previous.end && previous.start < table.end)
        throw invalid("RVZ 外层表的物理范围互相重叠");
    }
    tables.add(table);
  }

  private static void compressionParameters(byte[] header, long compression) throws IOException {
    interrupted();
    if (compression != 0 && compression != 2 && compression != 3 && compression != 4 && compression != 5)
      throw invalid("RVZ 压缩算法未识别，RVZ 不支持 PURGE");
    int count = header[0xd4] & 255;
    if (count > 7 || HEADER_2_MIN_BYTES + count > header.length)
      throw invalid("RVZ 压缩参数长度无效");
    if (compression == 3) {
      if (count != 5 || (header[0xd5] & 255) >= 225)
        throw invalid("RVZ LZMA 参数无效");
      long dictionary = 0;
      for (int i = 3; i >= 0; i--) dictionary = (dictionary << 8) | (header[0xd6 + i] & 255L);
      if (dictionary > MAX_DICTIONARY_BYTES) throw invalid("RVZ LZMA 字典超出 16 MiB 预算");
    } else if (compression == 4) {
      if (count != 1 || (header[0xd5] & 255) > 40)
        throw invalid("RVZ LZMA2 参数无效");
      int property = header[0xd5] & 255;
      long dictionary = property == 40 ? 0xffffffffL : (2L | (property & 1)) << (property / 2 + 11);
      if (dictionary > MAX_DICTIONARY_BYTES) throw invalid("RVZ LZMA2 字典超出 16 MiB 预算");
    } else if (count != 0) {
      throw invalid("RVZ 当前压缩算法不应包含额外参数");
    }
    // Zstd frame windows and actual decoder memory are not inspected without decoding payloads.
    interrupted();
  }

  private static void requireGameCube(byte[] header, int offset) throws IOException {
    interrupted();
    if (be32(header, offset + 0x18) == WII_MAGIC)
      throw invalid("文件包含 Wii 光盘标识，本轮仅接通 GameCube");
    if (be32(header, offset + 0x1c) != GC_MAGIC)
      throw invalid("未识别到 GameCube 光盘头，不能仅凭文件扩展名启动");
  }

  private static Range range(long offset, long bytes, long fileSize, long minimum, String label)
      throws IOException {
    interrupted();
    if (offset < minimum || bytes > fileSize || offset > fileSize - bytes)
      throw invalid(label + "的偏移或长度超出实际文件");
    return new Range(offset, offset + bytes);
  }

  private static byte[] read(RandomAccessFile in, long offset, int length) throws IOException {
    interrupted();
    byte[] result = new byte[length];
    in.seek(offset);
    in.readFully(result);
    interrupted();
    return result;
  }

  private static void checkHash(
      byte[] expected, int expectedOffset, byte[] data, int offset, int length, String label)
      throws IOException {
    interrupted();
    try {
      MessageDigest digest = MessageDigest.getInstance("SHA-1");
      digest.update(data, offset, length);
      byte[] wanted = Arrays.copyOfRange(expected, expectedOffset, expectedOffset + 20);
      if (!MessageDigest.isEqual(wanted, digest.digest())) throw invalid(label + " SHA-1 不匹配");
    } catch (NoSuchAlgorithmException e) {
      throw new IOException("当前运行环境缺少 SHA-1 检查能力", e);
    }
    interrupted();
  }

  private static long be32(byte[] bytes, int offset) {
    long value = 0;
    for (int i = 0; i < 4; i++) value = (value << 8) | (bytes[offset + i] & 255L);
    return value;
  }

  private static long positive64(byte[] bytes, int offset) throws IOException {
    if ((bytes[offset] & 0x80) != 0) throw invalid("RVZ 无符号 64 位字段超出检查范围");
    long value = 0;
    for (int i = 0; i < 8; i++) value = (value << 8) | (bytes[offset + i] & 255L);
    return value;
  }

  private static String extension(String lower) {
    for (String extension : new String[] {"iso", "gcm", "rvz"}) {
      if (lower.length() > extension.length() + 1 && lower.endsWith("." + extension)) return extension;
    }
    return null;
  }

  private static boolean safeBasename(String name) {
    if (name == null || name.isEmpty() || name.length() > 1024) return false;
    for (int i = 0; i < name.length(); i++) {
      char c = name.charAt(i);
      if (c == '/' || c == '\\' || c == ':' || Character.isISOControl(c)) return false;
    }
    return true;
  }

  private static void interrupted() throws IOException {
    if (Thread.currentThread().isInterrupted()) throw invalid("文件检查已取消");
  }

  private static IOException invalid(String message) {
    return new IOException("Dolphin：" + message + "；客户端只读检查，不会转换或修改原镜像");
  }

  private static final class Range {
    final long start;
    final long end;

    Range(long start, long end) {
      this.start = start;
      this.end = end;
    }
  }
}
