package io.retroone.android;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.URI;
import java.security.MessageDigest;
import java.util.function.BooleanSupplier;
import java.util.function.LongConsumer;

/** Bounded memory, verified resume, exact range semantics. Never appends unchecked bytes. */
public final class Transfer {
  private static void check(BooleanSupplier canceled) throws InterruptedIOException {
    if (canceled.getAsBoolean()) throw new InterruptedIOException("已暂停，保留已校验分片");
  }

  public static long verifiedPrefix(
      RandomAccessFile file, DownloadManifest manifest, BooleanSupplier canceled) throws Exception {
    byte[] block = new byte[65536];
    long verified = 0;
    for (int i = 0; i < manifest.count(); i++) {
      check(canceled);
      int length = manifest.length(i);
      if (file.length() < verified + length) break;
      file.seek(verified);
      MessageDigest hash = MessageDigest.getInstance("SHA-1");
      int left = length;
      while (left > 0) {
        check(canceled);
        int n = file.read(block, 0, Math.min(block.length, left));
        if (n < 0) throw new IOException("本机文件读取中断");
        hash.update(block, 0, n);
        left -= n;
      }
      if (!manifest.matches(i, hash.digest())) break;
      verified += length;
    }
    return verified;
  }

  public static void download(
      ApiClient api,
      DownloadManifest manifest,
      File partial,
      BooleanSupplier canceled,
      LongConsumer progress)
      throws Exception {
    try (RandomAccessFile file = new RandomAccessFile(partial, "rw")) {
      long verified = verifiedPrefix(file, manifest, canceled);
      file.setLength(verified);
      progress.accept(verified);
      if (partial.getParentFile().getUsableSpace() < manifest.size - verified + 16L * 1024 * 1024)
        throw new IOException("本机空间不足，请释放空间后继续；已校验分片已保留");
      byte[] block = new byte[65536];
      for (int i = (int) (verified / manifest.pieceLength); verified < manifest.size; i++) {
        check(canceled);
        int length = manifest.length(i);
        long end = verified + length - 1;
        HttpURLConnection connection =
            api.open(URI.create(manifest.url), "bytes=" + verified + "-" + end);
        try {
          int status = connection.getResponseCode();
          if (status != 206) {
            if (status == 200) throw new IOException("服务器或代理忽略了 Range；为避免错误续传，下载已停止");
            throw ApiClient.status(status);
          }
          String expected = "bytes " + verified + "-" + end + "/" + manifest.size;
          if (!expected.equals(connection.getHeaderField("Content-Range"))
              || (connection.getContentLengthLong() != -1
                  && connection.getContentLengthLong() != length))
            throw new IOException("服务器返回的续传范围不匹配");
          String encoding = connection.getContentEncoding();
          if (encoding != null && !encoding.equalsIgnoreCase("identity"))
            throw new IOException("服务器压缩了 ROM 响应，无法安全续传");
          MessageDigest hash = MessageDigest.getInstance("SHA-1");
          file.seek(verified);
          try (InputStream input = connection.getInputStream()) {
            int left = length;
            while (left > 0) {
              check(canceled);
              int n = input.read(block, 0, Math.min(block.length, left));
              if (n < 0) throw new IOException("连接中断，文件尚未完整；请重试");
              file.write(block, 0, n);
              hash.update(block, 0, n);
              left -= n;
            }
            if (input.read() != -1) throw new IOException("服务器返回了超出范围的数据");
          }
          if (!manifest.matches(i, hash.digest())) {
            file.setLength(verified);
            throw new IOException("分片校验失败，未接受损坏内容；可重试");
          }
          verified += length;
          file.getFD().sync();
          progress.accept(verified);
        } finally {
          connection.disconnect();
        }
      }
    }
  }
}
