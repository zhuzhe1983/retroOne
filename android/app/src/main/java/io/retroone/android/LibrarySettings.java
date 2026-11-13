package io.retroone.android;

import java.io.IOException;
import java.util.*;
import org.json.*;

/** Same per-player preferences as the web library. Read before editing unrelated fields. */
final class LibrarySettings {
  final List<String> pins;
  final String sort, direction;

  LibrarySettings(JSONObject value) throws Exception {
    JSONArray ids = value.getJSONArray("pinned_platforms");
    List<String> parsed = new ArrayList<>();
    if (ids.length() > 128) throw new IOException("平台设置格式不正确");
    for (int i = 0; i < ids.length(); i++) {
      String id = ids.getString(i);
      if (!id.matches("[a-z0-9_-]{1,60}") || parsed.contains(id))
        throw new IOException("平台设置格式不正确");
      parsed.add(id);
    }
    pins = Collections.unmodifiableList(parsed);
    sort = value.getString("sort");
    direction = value.getString("direction");
    if (!Arrays.asList(LibraryQuery.SORTS).contains(sort)
        || !("asc".equals(direction) || "desc".equals(direction)))
      throw new IOException("排序设置格式不正确");
  }

  static LibrarySettings read(ServerConfig source) throws Exception {
    return new LibrarySettings(
        new ApiClient(source).json(source.api("settings/library")).getJSONObject("settings"));
  }

  JSONObject json() throws Exception {
    return new JSONObject()
        .put("pinned_platforms", new JSONArray(pins))
        .put("sort", sort)
        .put("direction", direction);
  }

  static LibrarySettings update(
      ServerConfig source, String pin, Boolean follow, String sort, String direction)
      throws Exception {
    LibrarySettings current = read(source);
    JSONObject next = current.json();
    if (pin != null) {
      List<String> pins = new ArrayList<>(current.pins);
      if (Boolean.TRUE.equals(follow) && !pins.contains(pin)) pins.add(pin);
      if (Boolean.FALSE.equals(follow)) pins.remove(pin);
      next.put("pinned_platforms", new JSONArray(pins));
    }
    if (sort != null) next.put("sort", sort).put("direction", direction);
    new LibrarySettings(next); // Never replace malformed/missing remote preferences with defaults.
    return new LibrarySettings(
        new ApiClient(source)
            .put(source.api("settings/library"), next, 16384)
            .getJSONObject("settings"));
  }

  static List<JSONObject> visible(List<JSONObject> all, List<String> pins, String active) {
    Map<String, JSONObject> byId = new LinkedHashMap<>();
    for (JSONObject p : all) byId.put(p.optString("id"), p);
    List<JSONObject> result = new ArrayList<>();
    for (String id : pins) if (byId.containsKey(id) && result.size() < 6) result.add(byId.get(id));
    List<JSONObject> recent = new ArrayList<>(all);
    recent.sort(
        (a, b) ->
            Double.compare(b.optDouble("last_played_at", 0), a.optDouble("last_played_at", 0)));
    for (JSONObject p : recent)
      if (result.size() < 6 && p.optDouble("last_played_at", 0) > 0 && !result.contains(p))
        result.add(p);
    if (result.isEmpty())
      for (String id : List.of("nes", "snes", "gba", "psp", "switch", "fbneo"))
        if (byId.containsKey(id)) result.add(byId.get(id));
    if (!active.isEmpty() && byId.containsKey(active) && !result.contains(byId.get(active)))
      result.add(byId.get(active));
    return result;
  }
}
