package io.retroone.android;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import org.json.JSONObject;

/** Parses the existing HTTP manifest's private single-file torrent, without P2P. */
public final class DownloadManifest {
  public final String name, revision, url;
  public final long size;
  public final int pieceLength;
  private final byte[] hashes;

  public DownloadManifest(JSONObject json, ServerConfig config) throws Exception {
    name = json.getString("name");
    revision = json.getString("revision");
    size = json.getLong("size");
    url = config.resolve(json.getString("download_url")).toString();
    pieceLength = json.getInt("piece_length");
    if (!name.matches("[A-Za-z0-9_-]{1,128}\\.[A-Za-z0-9]{1,8}")
        || !revision.matches("[a-f0-9]{64}")
        || size <= 0
        || size > 16L * 1024 * 1024 * 1024 * 1024
        || pieceLength < 16384
        || pieceLength > 64 * 1024 * 1024) throw new IOException("下载清单的文件名、大小或版本无效");
    byte[] torrent = Base64.getDecoder().decode(json.getString("torrent_base64"));
    if (torrent.length > 2 * 1024 * 1024) throw new IOException("分片清单过大");
    Bencode parser = new Bencode(torrent);
    Object result = parser.read(0);
    if (parser.pos != torrent.length || !(result instanceof Map)) throw new IOException("分片清单格式无效");
    Object infoValue = ((Map<?, ?>) result).get("info");
    if (!(infoValue instanceof Map)) throw new IOException("缺少分片信息");
    Map<?, ?> info = (Map<?, ?>) infoValue;
    if (!Long.valueOf(size).equals(info.get("length"))
        || !Long.valueOf(pieceLength).equals(info.get("piece length"))
        || !(info.get("name") instanceof byte[])
        || !Arrays.equals(name.getBytes(StandardCharsets.UTF_8), (byte[]) info.get("name"))
        || info.containsKey("files")
        || !(info.get("pieces") instanceof byte[])) throw new IOException("分片清单与下载文件不一致；不支持多文件下载");
    hashes = (byte[]) info.get("pieces");
    if (hashes.length != ((size - 1) / pieceLength + 1) * 20) throw new IOException("分片校验数量无效");
  }

  public boolean matches(int piece, byte[] digest) {
    return MessageDigest.isEqual(Arrays.copyOfRange(hashes, piece * 20, piece * 20 + 20), digest);
  }

  public int count() {
    return hashes.length / 20;
  }

  public int length(int index) {
    return (int) Math.min(pieceLength, size - (long) index * pieceLength);
  }

  static final class Bencode {
    final byte[] bytes;
    int pos;

    Bencode(byte[] bytes) {
      this.bytes = bytes;
    }

    Object read(int depth) throws IOException {
      if (depth > 8 || pos >= bytes.length) throw new IOException("分片编码不完整");
      int c = bytes[pos++];
      if (c == 'd') {
        Map<String, Object> map = new LinkedHashMap<>();
        while (pos < bytes.length && bytes[pos] != 'e') {
          Object key = read(depth + 1);
          if (!(key instanceof byte[]) || ((byte[]) key).length > 128)
            throw new IOException("分片键无效");
          String name = new String((byte[]) key, StandardCharsets.UTF_8);
          if (map.containsKey(name) || map.size() > 32) throw new IOException("分片字段重复或过多");
          map.put(name, read(depth + 1));
        }
        if (pos >= bytes.length) throw new IOException("分片编码不完整");
        pos++;
        return map;
      }
      if (c == 'i') {
        int start = pos;
        while (pos < bytes.length && bytes[pos] != 'e' && pos - start <= 20) pos++;
        if (pos >= bytes.length || bytes[pos] != 'e') throw new IOException("分片整数无效");
        try {
          return Long.parseLong(new String(bytes, start, pos++ - start, StandardCharsets.US_ASCII));
        } catch (NumberFormatException e) {
          throw new IOException("分片整数溢出");
        }
      }
      if (c < '0' || c > '9') throw new IOException("不支持的分片编码");
      long length = c - '0';
      while (pos < bytes.length && bytes[pos] != ':') {
        int digit = bytes[pos++] - '0';
        if (digit < 0 || digit > 9 || length > bytes.length) throw new IOException("分片长度无效");
        length = length * 10 + digit;
      }
      pos++;
      if (length > bytes.length - pos || pos > bytes.length) throw new IOException("分片长度越界");
      byte[] out = Arrays.copyOfRange(bytes, pos, pos + (int) length);
      pos += (int) length;
      return out;
    }
  }
}
