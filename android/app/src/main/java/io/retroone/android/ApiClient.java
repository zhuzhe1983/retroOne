package io.retroone.android;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

public final class ApiClient {
  public final ServerConfig config;

  public ApiClient(ServerConfig config) {
    this.config = config;
  }

  public HttpURLConnection open(URI initial, String range) throws IOException {
    URI uri = config.resolve(initial.toString());
    for (int redirects = 0; redirects < 4; redirects++) {
      HttpURLConnection connection = (HttpURLConnection) uri.toURL().openConnection();
      connection.setConnectTimeout(12000);
      connection.setReadTimeout(20000);
      connection.setInstanceFollowRedirects(false);
      connection.setRequestProperty("Accept-Encoding", "identity");
      connection.setRequestProperty("Accept-Language", "zh-CN");
      if (!config.authorization.isEmpty())
        connection.setRequestProperty("Authorization", config.authorization);
      if (range != null) connection.setRequestProperty("Range", range);
      int code;
      try {
        code = connection.getResponseCode();
      } catch (IOException e) {
        connection.disconnect();
        throw e;
      }
      if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308) {
        String location = connection.getHeaderField("Location");
        connection.disconnect();
        if (location == null) throw new IOException("服务跳转缺少目标地址");
        uri = config.resolve(uri.resolve(location).toString());
        continue;
      }
      return connection;
    }
    throw new IOException("服务重定向过多，请填写最终 API 入口地址");
  }

  public JSONObject json(URI uri) throws Exception {
    HttpURLConnection connection = open(uri, null);
    try {
      int code = connection.getResponseCode();
      if (code != 200 && code != 202) throw status(code);
      byte[] body;
      try (InputStream input = connection.getInputStream()) {
        body = read(input, 4 * 1024 * 1024);
      }
      try {
        return new JSONObject(new String(body, StandardCharsets.UTF_8));
      } catch (Exception e) {
        throw new IOException("服务未返回 RetroOne JSON；请检查地址、反向代理和认证方式");
      }
    } finally {
      connection.disconnect();
    }
  }

  /** Mutations never follow redirects: method, body and player scope must remain explicit. */
  public JSONObject post(URI target, JSONObject payload, int limit) throws Exception {
    return mutate("POST", target, payload, limit);
  }

  public JSONObject put(URI target, JSONObject payload, int limit) throws Exception {
    return mutate("PUT", target, payload, limit);
  }

  private JSONObject mutate(String method, URI target, JSONObject payload, int limit)
      throws Exception {
    URI uri = config.resolve(target.toString());
    HttpURLConnection c = (HttpURLConnection) uri.toURL().openConnection();
    c.setConnectTimeout(12000);
    c.setReadTimeout(30000);
    c.setInstanceFollowRedirects(false);
    c.setRequestMethod(method);
    c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
    if (!config.authorization.isEmpty())
      c.setRequestProperty("Authorization", config.authorization);
    byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
    c.setFixedLengthStreamingMode(body.length);
    c.setDoOutput(true);
    try {
      try (java.io.OutputStream out = c.getOutputStream()) {
        out.write(body);
      }
      int code = c.getResponseCode();
      if (code != 200 && code != 202) {
        if (code == 409) throw new IOException("该游戏正在其他设备运行、存档被占用或版本有冲突，请退出游戏后重试；未覆盖任何存档");
        throw status(code);
      }
      try (InputStream in = c.getInputStream()) {
        return new JSONObject(new String(read(in, limit), StandardCharsets.UTF_8));
      }
    } finally {
      c.disconnect();
    }
  }

  static byte[] read(InputStream input, int limit) throws IOException {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    byte[] block = new byte[16384];
    int n;
    while ((n = input.read(block)) != -1) {
      if (out.size() + n > limit) throw new IOException("响应超出安全大小限制");
      out.write(block, 0, n);
    }
    return out.toByteArray();
  }

  static IOException status(int code) {
    return new IOException(
        switch (code) {
          case 401, 403 -> "HTTP " + code + "：认证失败或资源不允许访问，请检查连接设置";
          case 404 -> "HTTP 404：接口或资源不存在；请检查服务版本与玩家路径";
          case 409 -> "HTTP 409：服务端文件或配置已变化，请取消此任务后重新下载";
          case 416 -> "HTTP 416：续传范围不匹配，请重新下载";
          default -> "服务请求失败（HTTP " + code + "），可稍后重试";
        });
  }

  static String message(Throwable error) {
    if (error instanceof javax.net.ssl.SSLException)
      return "HTTPS 证书校验失败；请使用可信证书或在系统安装自己的 CA，不能跳过校验";
    if (error instanceof java.net.SocketTimeoutException) return "连接超时；请检查网络后重试，已校验分片会保留";
    if (error instanceof java.net.UnknownHostException
        || error instanceof java.net.ConnectException) return "无法连接服务器；请检查地址、网络和服务状态";
    if (error instanceof IOException || error instanceof IllegalArgumentException)
      return error.getMessage() == null ? "操作失败，请重试" : error.getMessage();
    return "操作失败，请检查连接与资源后重试";
  }
}
