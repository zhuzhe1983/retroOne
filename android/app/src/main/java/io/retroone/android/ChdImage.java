package io.retroone.android;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Read-only, bounded screening of standalone v5 CD/GD CHDs, not a CHD decoder or verifier.
 *
 * <p>Header/map/metadata layouts follow MAME's src/lib/util/{chd.h,chd.cpp,cdrom.cpp}:
 * https://github.com/mamedev/mame/tree/master/src/lib/util . Passing does not identify a console,
 * validate compressed map codes, decompress hunks, verify their checksums, or prove playability.
 * The 2 GiB physical/logical and metadata limits are this client's scope, not format limits.
 */
final class ChdImage {
  static final long MAX_FILE_BYTES = 2L * 1024 * 1024 * 1024;
  static final long MAX_LOGICAL_BYTES = MAX_FILE_BYTES;
  static final int MAX_HUNK_BYTES = 512 * 1024;
  static final int MAX_MAP_BYTES = 16 * 1024 * 1024;
  static final int MAX_METADATA_ENTRIES = 256;
  static final int MAX_METADATA_BYTES = 1024 * 1024;
  private static final int HEADER_BYTES = 124;
  private static final int FRAME_BYTES = 2352 + 96;
  private static final int OLD_CD_BYTES = (1 + 99 * 6) * 4;
  private static final Set<String> OTHER_MEDIA =
      Set.of("GDDD", "IDNT", "KEY ", "CIS ", "DVD ", "AVAV", "AVLD");
  private static final Set<String> TRACK_TYPES =
      Set.of(
          "MODE1", "MODE1/2048", "MODE1_RAW", "MODE1/2352", "MODE2", "MODE2/2336",
          "MODE2_FORM1", "MODE2/2048", "MODE2_FORM2", "MODE2/2324", "MODE2_FORM_MIX",
          "MODE2_RAW", "MODE2/2352", "CDI/2352", "AUDIO", "CDG");

  private ChdImage() {}

  static void validate(File file, boolean allowGd) throws IOException {
    interrupted();
    if (file == null || !file.isFile() || !file.canRead()) throw invalid("本地文件不存在或不可读");
    long size = file.length();
    long modified = file.lastModified();
    if (size < HEADER_BYTES || size > MAX_FILE_BYTES)
      throw invalid("文件长度不在本次 124 字节至 2 GiB 检查范围内");
    try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
      byte[] header = new byte[HEADER_BYTES];
      in.readFully(header);
      if (!"MComprHD".equals(new String(header, 0, 8, StandardCharsets.US_ASCII))
          || u32(header, 8) != HEADER_BYTES
          || u32(header, 12) != 5) throw invalid("仅接入具有 124 字节头部的 CHD v5");
      for (int i = 104; i < HEADER_BYTES; i++) {
        if (header[i] != 0) throw invalid("镜像依赖父 CHD，尚不支持这种多文件内容");
      }
      long logical = positive64(header, 32, "逻辑长度");
      long hunk = u32(header, 56), unit = u32(header, 60);
      if (logical == 0 || logical > MAX_LOGICAL_BYTES || logical % FRAME_BYTES != 0)
        throw invalid("光盘逻辑长度必须为完整帧且不超过 2 GiB");
      if (unit != FRAME_BYTES || hunk == 0 || hunk > MAX_HUNK_BYTES || hunk % unit != 0)
        throw invalid("光盘 hunk / unit 大小不在支持范围内");

      boolean compressed = u32(header, 16) != 0;
      boolean emptySlot = false;
      for (int i = 16; i < 32; i += 4) {
        if (u32(header, i) == 0) emptySlot = true;
        else if (emptySlot) throw invalid("压缩器列表在空项后仍有内容");
      }
      long count = (logical - 1) / hunk + 1;
      long mapOffset = positive64(header, 40, "map 偏移");
      // MAME create_common writes 124 here for uncompressed CHDs. A compressed
      // zero offset denotes an unfinished map, not an implicit uncompressed map.
      if (mapOffset == 0) throw invalid("map 尚未写入，不能作为完成的镜像启动");
      long firstData = 0;
      Range map;
      if (compressed) {
        range(mapOffset, 16, size, "map 头部");
        byte[] mapHeader = read(in, mapOffset, 16);
        long bytes = u32(mapHeader, 0);
        if (bytes == 0 || bytes > MAX_MAP_BYTES) throw invalid("压缩 map 长度超出检查范围");
        map = range(mapOffset, 16 + bytes, size, "压缩 map");
        firstData = unsigned(mapHeader, 4, 6);
        range(firstData, 1, size, "首个数据块");
        if (map.overlaps(firstData, firstData + 1)) throw invalid("首个数据块与 map 重叠");
        // These widths feed native bitstream readers. Zero widths are valid when
        // that class of reference does not occur; values above 32 are not.
        for (int i = 12; i <= 14; i++) {
          if ((mapHeader[i] & 255) > 32) throw invalid("压缩 map 位宽超出支持范围");
        }
      } else {
        // The table has one unsigned 32-bit block index per hunk, not 12-byte entries.
        map = range(mapOffset, count * 4, size, "未压缩 map");
      }

      List<Range> metadata =
          metadata(in, positive64(header, 48, "元数据偏移"), size, logical / unit, map, allowGd);
      if (compressed) {
        rejectOverlap(metadata, firstData, firstData + 1, "首个数据块与元数据重叠");
      } else {
        validatePlainMap(in, map, count, hunk, size, metadata);
      }
      interrupted();
      if (in.length() != size || file.length() != size || file.lastModified() != modified)
        throw invalid("检查期间文件发生变化，请等待下载完成后重试");
    }
  }

  private static List<Range> metadata(
      RandomAccessFile in, long offset, long size, long frames, Range map, boolean allowGd)
      throws IOException {
    Set<Long> visited = new HashSet<>();
    List<Range> ranges = new ArrayList<>();
    long total = 0;
    boolean disc = false;
    while (offset != 0) {
      interrupted();
      if (!visited.add(offset)) throw invalid("元数据链存在循环");
      if (visited.size() > MAX_METADATA_ENTRIES) throw invalid("元数据条目数量超出检查范围");
      range(offset, 16, size, "元数据头部");
      byte[] header = read(in, offset, 16);
      String tag = new String(header, 0, 4, StandardCharsets.US_ASCII);
      int bytes = (int) (u32(header, 4) & 0x00ffffffL);
      if (bytes > MAX_METADATA_BYTES - total)
        throw invalid("元数据总长度超出 1 MiB 检查范围");
      total += bytes;
      Range record = range(offset, 16L + bytes, size, "元数据内容");
      if (map.overlaps(record.start, record.end)) throw invalid("元数据与 map 重叠");
      rejectOverlap(ranges, record.start, record.end, "元数据条目互相重叠");
      ranges.add(record);
      if (OTHER_MEDIA.contains(tag)) throw invalid("含硬盘、DVD 或其他非 CD / GD 媒体元数据");
      if (tag.equals("CHGD") || tag.equals("CHGT")) {
        if (!allowGd) throw invalid("当前平台不接受 GD-ROM 元数据");
        validateTrack(readTrack(in, offset + 16, bytes), tag, frames);
        disc = true;
      } else if (tag.equals("CHTR") || tag.equals("CHT2")) {
        validateTrack(readTrack(in, offset + 16, bytes), tag, frames);
        disc = true;
      } else if (tag.equals("CHCD")) {
        // Old CD metadata is 1 + 99 * 6 uint32 words, with either historical endian.
        if (bytes < OLD_CD_BYTES) throw invalid("旧 CD 元数据不完整");
        validateOldCd(read(in, offset + 16, OLD_CD_BYTES), frames);
        disc = true;
      }
      // Preserve bounded unknown auxiliary metadata, including newer session tags.
      // It cannot itself establish that the container describes a supported disc.
      offset = positive64(header, 8, "元数据 next 偏移");
    }
    if (!disc) throw invalid("未找到受支持的 CD / GD 光盘元数据");
    ranges.sort(Comparator.comparingLong(value -> value.start));
    return ranges;
  }

  private static String readTrack(RandomAccessFile in, long offset, int bytes) throws IOException {
    if (bytes == 0 || bytes > 1024) throw invalid("光盘轨道元数据长度无效");
    byte[] data = read(in, offset, bytes);
    int end = data.length;
    while (end > 0 && data[end - 1] == 0) end--;
    for (int i = 0; i < end; i++) {
      int b = data[i] & 255;
      if ((b < 32 && b != '\t' && b != '\n' && b != '\r') || b > 126)
        throw invalid("光盘轨道元数据不是受支持的文本");
    }
    return new String(data, 0, end, StandardCharsets.US_ASCII).trim();
  }

  private static void validateTrack(String text, String tag, long frames) throws IOException {
    boolean gd = tag.equals("CHGD") || tag.equals("CHGT");
    String[] fields = text.split("\\s+");
    if (fields.length != (gd ? 9 : tag.equals("CHT2") ? 8 : 4))
      throw invalid("光盘轨道元数据字段不完整");
    long track = number(fields[0], "TRACK", 99);
    if (track == 0 || number(fields[3], "FRAMES", frames) == 0)
      throw invalid("光盘轨道编号或帧数无效");
    if (!TRACK_TYPES.contains(field(fields[1], "TYPE"))) throw invalid("光盘轨道类型不受支持");
    subtype(field(fields[2], "SUBTYPE"));
    int i = 4;
    if (gd) number(fields[i++], "PAD", frames);
    if (fields.length > 4) {
      // Virtual gaps need not occupy bytes in the image (and can exceed the
      // content length of a tiny homebrew disc). Bound them by our disc scope.
      number(fields[i++], "PREGAP", MAX_LOGICAL_BYTES / FRAME_BYTES);
      String type = field(fields[i++], "PGTYPE");
      if (type.startsWith("V")) type = type.substring(1);
      if (!TRACK_TYPES.contains(type)) throw invalid("光盘 pregap 类型不受支持");
      subtype(field(fields[i++], "PGSUB"));
      number(fields[i], "POSTGAP", MAX_LOGICAL_BYTES / FRAME_BYTES);
    }
  }

  private static void subtype(String value) throws IOException {
    if (!value.equals("NONE") && !value.equals("RW") && !value.equals("RW_RAW"))
      throw invalid("光盘子通道类型不受支持");
  }

  private static String field(String value, String key) throws IOException {
    if (!value.startsWith(key + ":") || value.length() == key.length() + 1)
      throw invalid("光盘轨道元数据字段无效");
    return value.substring(key.length() + 1);
  }

  private static long number(String value, String key, long max) throws IOException {
    String digits = field(value, key);
    if (digits.length() > 10) throw invalid("光盘轨道数值超出支持范围");
    long result = 0;
    for (int i = 0; i < digits.length(); i++) {
      int n = digits.charAt(i) - '0';
      if (n < 0 || n > 9) throw invalid("光盘轨道数值无效");
      result = result * 10 + n;
    }
    if (result > max) throw invalid("光盘轨道数值超出逻辑长度或支持范围");
    return result;
  }

  private static void validateOldCd(byte[] data, long frames) throws IOException {
    boolean little = u32(data, 0) > 99;
    long count = oldWord(data, 0, little), total = 0;
    if (count == 0 || count > 99) throw invalid("旧 CD 元数据轨道数量无效");
    for (int i = 0; i < count; i++) {
      int offset = 4 + i * 24;
      long type = oldWord(data, offset, little), subtype = oldWord(data, offset + 4, little);
      long dataBytes = oldWord(data, offset + 8, little);
      long subBytes = oldWord(data, offset + 12, little);
      long trackFrames = oldWord(data, offset + 16, little);
      long extraFrames = oldWord(data, offset + 20, little);
      if (type > 7 || subtype > 2 || dataBytes == 0 || dataBytes > 2352 || subBytes > 96)
        throw invalid("旧 CD 元数据轨道类型或帧大小无效");
      if (trackFrames == 0 || trackFrames > frames || extraFrames > frames)
        throw invalid("旧 CD 元数据帧数无效");
      total += trackFrames + extraFrames;
      if (total > frames) throw invalid("旧 CD 元数据帧数超过逻辑长度");
    }
  }

  private static long oldWord(byte[] data, int offset, boolean little) {
    if (!little) return u32(data, offset);
    long result = 0;
    for (int i = 3; i >= 0; i--) result = (result << 8) | (data[offset + i] & 255L);
    return result;
  }

  private static void validatePlainMap(
      RandomAccessFile in, Range map, long count, long hunk, long size, List<Range> metadata)
      throws IOException {
    byte[] buffer = new byte[65536];
    in.seek(map.start);
    while (count > 0) {
      interrupted();
      int entries = (int) Math.min(count, buffer.length / 4);
      in.readFully(buffer, 0, entries * 4);
      for (int i = 0; i < entries; i++) {
        long block = u32(buffer, i * 4);
        // Zero is a sparse all-zero hunk when no parent exists. It is not an offset
        // into the header and does not require logicalbytes bytes in the file.
        if (block == 0) continue;
        long offset = block * hunk; // uint32 * <= 512 KiB fits signed long.
        Range data = range(offset, hunk, size, "未压缩数据块");
        if (map.overlaps(data.start, data.end)) throw invalid("未压缩数据块与 map 重叠");
        rejectSortedOverlap(metadata, data.start, data.end);
      }
      count -= entries;
    }
  }

  private static void rejectSortedOverlap(List<Range> ranges, long start, long end)
      throws IOException {
    int low = 0, high = ranges.size();
    while (low < high) {
      int middle = (low + high) >>> 1;
      if (ranges.get(middle).end <= start) low = middle + 1;
      else high = middle;
    }
    if (low < ranges.size() && ranges.get(low).start < end)
      throw invalid("未压缩数据块与元数据重叠");
  }

  private static void rejectOverlap(List<Range> ranges, long start, long end, String detail)
      throws IOException {
    for (Range range : ranges) if (range.overlaps(start, end)) throw invalid(detail);
  }

  private static Range range(long offset, long bytes, long size, String label) throws IOException {
    if (offset < HEADER_BYTES || bytes < 0 || bytes > size || offset > size - bytes)
      throw invalid(label + "越界或内容不完整");
    return new Range(offset, offset + bytes);
  }

  private static byte[] read(RandomAccessFile in, long offset, int bytes) throws IOException {
    byte[] result = new byte[bytes];
    in.seek(offset);
    in.readFully(result);
    return result;
  }

  private static long positive64(byte[] data, int offset, String field) throws IOException {
    if (data[offset] < 0) throw invalid(field + "超过有符号 64 位检查范围");
    return unsigned(data, offset, 8);
  }

  private static long u32(byte[] data, int offset) {
    return unsigned(data, offset, 4);
  }

  private static long unsigned(byte[] data, int offset, int count) {
    long result = 0;
    for (int i = 0; i < count; i++) result = (result << 8) | (data[offset + i] & 255L);
    return result;
  }

  private static void interrupted() throws IOException {
    if (Thread.currentThread().isInterrupted()) throw invalid("检查已取消");
  }

  private static IOException invalid(String detail) {
    return new IOException("CHD：" + detail + "；请核对原文件或手动导入，客户端不会修改镜像");
  }

  private static final class Range {
    final long start;
    final long end;

    Range(long start, long end) {
      this.start = start;
      this.end = end;
    }

    boolean overlaps(long otherStart, long otherEnd) {
      return start < otherEnd && otherStart < end;
    }
  }
}
