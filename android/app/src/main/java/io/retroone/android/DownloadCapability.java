package io.retroone.android;

import java.io.IOException;
import org.json.JSONObject;

/** File availability is independent of browser WASM support and Android emulator adapters. */
final class DownloadCapability {
  final boolean available;
  final String name, reason;
  final long size;

  DownloadCapability(JSONObject data) throws Exception {
    if (!(data.opt("available") instanceof Boolean)) throw new IOException("服务器未返回完整下载信息，请更新服务后重试");
    available = data.getBoolean("available");
    name = data.isNull("name") ? "" : data.optString("name", "");
    size = data.optLong("size_bytes", 0);
    reason = data.isNull("reason") ? "" : data.optString("reason", "");
    if (available && (name.isEmpty() || size <= 0)) throw new IOException("服务器返回的文件信息不完整，请重新读取");
  }

  String explanation() {
    if (available) return "";
    if (reason.contains("多个文件") || reason.contains("分盘")) return "这是多文件或多碟游戏，服务器尚未提供完整打包下载。";
    if (reason.contains("不是独立文件") || reason.contains("不在游戏库"))
      return "此条目没有可下载的独立 ROM；可能是目录游戏或文件缺失，需要在服务器游戏库中处理。";
    return reason.isEmpty() ? "服务器暂未提供此游戏的下载文件。" : reason;
  }
}
