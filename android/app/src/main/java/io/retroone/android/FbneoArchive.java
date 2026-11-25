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
  static final class MatchException extends IOException {
    MatchException(String message) {
      super(message);
    }
  }

  static final String DAT = "0031b6557d493bc52d601e25b640e8a2d830bd6c78d4f3fff227463d2c62ae88";

  static String identify(File archive, JSONObject index) throws Exception {
    if (!DAT.equals(index.getString("source_sha256"))) throw new IOException("ROM 集索引版本无效");
    List<String> signatures = new ArrayList<>();
    Set<String> names = new HashSet<>();
    long total = 0;
    byte[] buffer = new byte[65536];
    try (ZipFile zip = new ZipFile(archive)) {
      Enumeration<? extends ZipEntry> entries = zip.entries();
      while (entries.hasMoreElements()) {
        ZipEntry entry = entries.nextElement();
        if (entry.isDirectory()) continue;
        String name = entry.getName();
        if (!names.add(name)
            || names.size() > 4096
            || name.contains("/")
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
