package io.retroone.android;

import java.util.*;
import org.json.JSONObject;

/** Device files are queried locally, not by filtering one page from the server. */
final class LocalGames {
  static final String[] SORTS = {"downloaded", "played", "name", "size"};
  static final String[] LABELS = {"下载时间", "最近游玩", "游戏名称", "文件大小"};

  static List<DownloadStore.Item> select(
      DownloadStore store, String platform, String query, String order) {
    List<DownloadStore.Item> found = new ArrayList<>();
    Set<String> seen = new HashSet<>();
    for (DownloadStore.Item item : store.all()) {
      if (!store.present(item)
          || (!platform.isEmpty() && !platform.equals(item.platform))
          || !item.title.toLowerCase(Locale.ROOT).contains(query.toLowerCase(Locale.ROOT)))
        continue;
      // The same catalog game downloaded through another player is one playable title in this view.
      String source = item.base.replaceFirst("/p/[^/]+$", "");
      String revision = item.id;
      try {
        revision = new JSONObject(item.manifest).getString("revision");
      } catch (Exception ignored) {
      }
      if (seen.add(source + "/" + item.game + "/" + revision)) found.add(item);
    }
    found.sort(
        switch (order) {
          case "name" -> Comparator.comparing(a -> a.title.toLowerCase(Locale.ROOT));
          case "size" -> Comparator.comparingLong((DownloadStore.Item a) -> a.size).reversed();
          case "played" ->
              Comparator.comparingLong((DownloadStore.Item a) -> a.lastPlayed).reversed();
          default -> Comparator.comparingLong((DownloadStore.Item a) -> a.created).reversed();
        });
    return found;
  }

  static JSONObject view(DownloadStore.Item item) {
    JSONObject value;
    try {
      value = new JSONObject(item.metadata);
    } catch (Exception e) {
      value = new JSONObject();
    }
    try {
      return value
          .put("id", item.game)
          .put("title", item.title)
          .put("platform", item.platform)
          .put("size", item.size)
          .put("_local_id", item.id);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
