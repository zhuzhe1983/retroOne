package io.retroone.android;

import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** A connection is immutable: queued downloads never silently switch servers. */
public final class ServerConfig {
  public final String base;
  public final boolean allowHttp;
  public final String authorization;

  public ServerConfig(String input, boolean allowHttp, String authorization) {
    try {
      URI uri = new URI(input.trim());
      String scheme = uri.getScheme();
      if (!("http".equals(scheme) || "https".equals(scheme))
          || uri.getHost() == null
          || uri.getUserInfo() != null
          || uri.getQuery() != null
          || uri.getFragment() != null
          || uri.getRawPath().contains("%")
          || uri.getPath().contains("..")) throw new IllegalArgumentException();
      if ("http".equals(scheme) && !allowHttp)
        throw new IllegalArgumentException("HTTP 未启用；请选择 HTTPS，或允许可信网络的明文连接");
      base = uri.toASCIIString().replaceAll("/+$", "");
      this.allowHttp = allowHttp;
      this.authorization = authorization == null ? "" : authorization;
      if (this.authorization.contains("\r") || this.authorization.contains("\n"))
        throw new IllegalArgumentException();
    } catch (Exception e) {
      throw new IllegalArgumentException(
          e.getMessage() != null && e.getMessage().startsWith("HTTP")
              ? e.getMessage()
              : "地址须为 http(s)://主机[:端口][/p/playerN]，不能包含密码、查询或片段");
    }
  }

  public URI api(String path) {
    return URI.create(base + "/api/" + path);
  }

  /** Never forward credentials to a CDN, another origin, or a downgrade redirect. */
  public URI resolve(String value) {
    URI origin = URI.create(base + "/");
    URI target = origin.resolve(value);
    if (!origin.getScheme().equalsIgnoreCase(target.getScheme())
        || !origin.getHost().equalsIgnoreCase(target.getHost() == null ? "" : target.getHost())
        || port(origin) != port(target)
        || target.getUserInfo() != null
        || target.getFragment() != null)
      throw new IllegalArgumentException("服务返回了不同来源的地址，已拒绝传递凭据或下载文件");
    return target;
  }

  private static int port(URI uri) {
    return uri.getPort() == -1 ? ("https".equals(uri.getScheme()) ? 443 : 80) : uri.getPort();
  }

  public String key() {
    return hash(base);
  }

  public static String encode(String value) {
    try {
      return URLEncoder.encode(value, "UTF-8");
    } catch (java.io.UnsupportedEncodingException e) {
      throw new AssertionError(e);
    }
  }

  public static String hash(String value) {
    try {
      byte[] bytes =
          MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
      StringBuilder out = new StringBuilder();
      for (byte b : bytes) out.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
      return out.toString();
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
