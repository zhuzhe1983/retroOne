package io.retroone.android;

/** Query the entire server library, not just sort the current page. */
final class LibraryQuery {
  static final String[] SORTS = {
    "popularity", "name", "release_date", "added_at", "play_count", "last_played", "default"
  };
  static final String[] LABELS = {"大众热度", "游戏名称", "发行日期", "加入时间", "游玩次数", "上次游玩", "默认顺序"};

  static String route(
      String query, String platform, String scope, String sort, String direction, int offset) {
    if (!java.util.Arrays.asList(SORTS).contains(sort)
        || !("asc".equals(direction) || "desc".equals(direction))
        || offset < 0) throw new IllegalArgumentException("请选择有效的排序方式");
    return "games?limit=60&offset="
        + offset
        + "&q="
        + ServerConfig.encode(query)
        + "&platform="
        + ServerConfig.encode(platform)
        + "&sort="
        + sort
        + "&direction="
        + direction
        + ("favorites".equals(scope)
            ? "&favorite=true"
            : "recent".equals(scope) ? "&recent=true" : "");
  }
}
