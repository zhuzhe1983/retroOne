package io.retroone.android;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.zip.*;
import org.json.*;

/**
 * Matches complete ZIP member bytes against a pinned official DAT, never titles or guessed names.
 */
final class FbneoArchive {
  static class MatchException extends IOException {
    MatchException(String message) {
      super(message);
    }
  }

  static final String DAT = "0031b6557d493bc52d601e25b640e8a2d830bd6c78d4f3fff227463d2c62ae88";

  static final long MAX_ARCHIVE_BYTES = 512L * 1024 * 1024;
  static final int MAX_ENTRIES = 4096;
  static final long MAX_DIRECTORY_BYTES = 4L * 1024 * 1024;

  /** Bound memory before ZipFile parses the central directory. No archive bytes are rewritten. */
  static int checkLayout(File archive) throws IOException {
    return checkLayout(archive, MAX_ARCHIVE_BYTES);
  }

  // The DS cartridge adapter needs room for a 512 MiB payload and ZIP overhead.
  // Existing arcade callers retain their original 512 MiB bound.
  static int checkLayout(File archive, long physicalLimit) throws IOException {
    if (physicalLimit <= 0 || physicalLimit > 576L * 1024 * 1024)
      throw new IOException("ZIP 物理大小限制无效");
    try (RandomAccessFile file = new RandomAccessFile(archive, "r")) {
      long size = file.length();
      if (size > physicalLimit)
        throw new IOException("ZIP 超过自动校验的 " + physicalLimit / (1024 * 1024) + " MiB 物理大小上限，请手动导入");
      if (size < 22 || file.readInt() != 0x504b0304)
        throw new IOException("仅接入标准 ZIP；带前缀、自解压或空归档请手动导入");
      byte[] tail = new byte[(int) Math.min(size, 65557)];
      long tailOffset = size - tail.length;
      file.seek(tailOffset);
      file.readFully(tail);
      int end = -1;
      for (int i = tail.length - 22; i >= 0; i--) {
        if (u32(tail, i) == 0x06054b50L && i + 22 + u16(tail, i + 20) == tail.length) {
          end = i;
          break;
        }
      }
      if (end < 0) throw new IOException("ZIP 结束记录无效或不完整");
      int count = u16(tail, end + 10);
      long length = u32(tail, end + 12), offset = u32(tail, end + 16);
      if (u16(tail, end + 4) != 0
          || u16(tail, end + 6) != 0
          || count == 0xffff
          || length == 0xffffffffL
          || offset == 0xffffffffL) throw new IOException("ZIP64 或多卷 ZIP 暂需手动导入");
      if (count == 0 || count > MAX_ENTRIES || count != u16(tail, end + 8))
        throw new IOException("ZIP 条目数量超出当前校验范围或计数不一致（包括目录）");
      long directoryEnd = offset + length;
      if (length > MAX_DIRECTORY_BYTES
          || offset < 4
          || length < count * 46L
          || directoryEnd != tailOffset + end)
        throw new IOException("ZIP 中央目录过大或范围不一致；ZIP64 等扩展暂需手动导入");
      file.seek(offset);
      byte[] header = new byte[46];
      for (int i = 0; i < count; i++) {
        if (Thread.currentThread().isInterrupted()) throw new IOException("校验已取消");
        if (file.getFilePointer() + header.length > directoryEnd)
          throw new IOException("ZIP 中央目录条目不完整");
        file.readFully(header);
        if (u32(header, 0) != 0x02014b50L
            || u16(header, 34) != 0
            || u32(header, 20) == 0xffffffffL
            || u32(header, 24) == 0xffffffffL
            || u32(header, 42) >= offset
            || (u16(header, 8) & 1) != 0) throw new IOException("ZIP 中央目录无效，或含 ZIP64／加密／多卷条目");
        long next = file.getFilePointer() + u16(header, 28) + u16(header, 30) + u16(header, 32);
        if (next > directoryEnd || u16(header, 28) == 0) throw new IOException("ZIP 中央目录条目长度无效");
        file.seek(next);
      }
      if (file.getFilePointer() != directoryEnd) throw new IOException("ZIP 中央目录实际条目与声明数量不一致");
      return count;
    }
  }

  private static int u16(byte[] bytes, int at) {
    return (bytes[at] & 255) | ((bytes[at + 1] & 255) << 8);
  }

  private static long u32(byte[] bytes, int at) {
    return (long) u16(bytes, at) | ((long) u16(bytes, at + 2) << 16);
  }

  static String identify(File archive, JSONObject index) throws Exception {
    if (index.optInt("schema_version") == 2) return FbneoRomSet.resolve(archive, null, index).set;
    if (!DAT.equals(index.getString("source_sha256"))) throw new IOException("ROM 集索引版本无效");
    int expectedEntries = checkLayout(archive);
    List<String> signatures = new ArrayList<>();
    Set<String> names = new HashSet<>();
    long total = 0;
    byte[] buffer = new byte[65536];
    try (ZipFile zip = new ZipFile(archive)) {
      if (zip.size() != expectedEntries) throw new IOException("ZIP 条目数量已变化");
      int entryCount = 0;
      Enumeration<? extends ZipEntry> entries = zip.entries();
      while (entries.hasMoreElements()) {
        ZipEntry entry = entries.nextElement();
        String name = entry.getName();
        if (++entryCount > MAX_ENTRIES || !names.add(name))
          throw new IOException("ZIP 条目过多或重复（包括目录）");
        if (entry.isDirectory()) continue;
        if (name.contains("/")
            || name.contains("\\")
            || name.equals("..")
            || entry.getSize() < 0
            || entry.getSize() > 512L * 1024 * 1024)
          throw new IOException("ZIP 不是可直接启动的独立 ROM 集；不自动解包嵌套目录");
        total += entry.getSize();
        if (total > 2L * 1024 * 1024 * 1024) throw new IOException("归档展开量超出当前街机校验范围，请手动导入");
        CRC32 crc = new CRC32();
        long count = 0;
        try (InputStream in = zip.getInputStream(entry)) {
          int n;
          while ((n = in.read(buffer)) != -1) {
            if (Thread.currentThread().isInterrupted()) throw new IOException("校验已取消");
            count += n;
            if (count > entry.getSize()) throw new IOException("ZIP 内容大小异常");
            crc.update(buffer, 0, n);
          }
        }
        if (count != entry.getSize() || crc.getValue() != entry.getCrc())
          throw new IOException("ZIP 分片内容损坏，CRC 不符");
        signatures.add(String.format(Locale.ROOT, "%08x:%d", crc.getValue(), count));
      }
    }
    Collections.sort(signatures);
    String signature =
        hex(
            MessageDigest.getInstance("SHA-256")
                .digest((String.join("\n", signatures) + "\n").getBytes(StandardCharsets.UTF_8)));
    JSONArray matches = index.getJSONObject("sets").optJSONArray(signature);
    if (matches == null || matches.length() == 0)
      throw new MatchException(
          "归档未匹配当前核心／索引的完整 ROM 集，需核对版本或依赖。可能是 MOD／改版、缺少父集或 BIOS，或 ROM"
              + " 集版本不符；已保留原文件，请手动导入匹配的核心，不会改成原版。");
    if (matches.length() != 1) throw new MatchException("归档对应多个 ROM 集，无法唯一确定启动名称；请手动导入");
    JSONArray match = matches.getJSONArray(0);
    String name = match.getString(0), dependency = match.getString(1);
    if (!dependency.isEmpty())
      throw new MatchException(
          "此 ROM 集关联父集或 BIOS：" + dependency + "。当前尚未开放克隆／BIOS 关联集直启，请手动导入；此提示不表示归档实际缺少文件。");
    if (!name.matches("[A-Za-z0-9_-]{1,64}")) throw new IOException("ROM 集标识无效");
    return name;
  }

  static String hex(byte[] bytes) {
    StringBuilder text = new StringBuilder();
    for (byte b : bytes) text.append(String.format(Locale.ROOT, "%02x", b & 255));
    return text.toString();
  }
}
