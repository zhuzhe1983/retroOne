package io.retroone.android;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.json.JSONObject;

final class DownloadStore extends SQLiteOpenHelper {
  static final String QUEUED = "queued",
      PREPARING = "preparing",
      RUNNING = "running",
      PAUSED = "paused",
      FAILED = "failed",
      READY = "ready",
      CANCELED = "canceled";
  final File root;

  DownloadStore(Context context) {
    super(context, "downloads.db", null, 2);
    root = new File(context.getFilesDir(), "games");
    if (!root.exists() && !root.mkdirs()) throw new IllegalStateException("无法创建本机游戏目录");
  }

  @Override
  public void onCreate(SQLiteDatabase db) {
    db.execSQL(
        "CREATE TABLE downloads (id TEXT PRIMARY KEY, base TEXT NOT NULL, http INTEGER NOT NULL,"
            + " game TEXT NOT NULL, title TEXT NOT NULL, platform TEXT NOT NULL, state TEXT NOT"
            + " NULL, bytes INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0, manifest"
            + " TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', message TEXT NOT NULL"
            + " DEFAULT '', created INTEGER NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',"
            + " last_played INTEGER NOT NULL DEFAULT 0)");
  }

  @Override
  public void onUpgrade(SQLiteDatabase db, int old, int next) {
    if (old < 2) {
      db.execSQL("ALTER TABLE downloads ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'");
      db.execSQL("ALTER TABLE downloads ADD COLUMN last_played INTEGER NOT NULL DEFAULT 0");
    }
  }

  synchronized void recover() {
    ContentValues values = new ContentValues();
    values.put("state", PAUSED);
    values.put("message", "上次下载已中断，点继续会重新校验后续传");
    getWritableDatabase()
        .update("downloads", values, "state IN (?,?,?)", new String[] {QUEUED, PREPARING, RUNNING});
  }

  synchronized String enqueue(ServerConfig config, JSONObject game) throws Exception {
    for (Item item : all())
      if (item.base.equals(config.base)
          && item.game.equals(game.getString("id"))
          && !CANCELED.equals(item.state)) return item.id;
    String id = UUID.randomUUID().toString();
    ContentValues values = new ContentValues();
    values.put("id", id);
    values.put("base", config.base);
    values.put("http", config.allowHttp ? 1 : 0);
    values.put("game", game.getString("id"));
    values.put("title", game.getString("title"));
    values.put("platform", game.getString("platform"));
    values.put("state", QUEUED);
    values.put("created", System.currentTimeMillis());
    JSONObject metadata = new JSONObject();
    for (String key : new String[] {"cover_url", "platform_name"})
      if (game.has(key)) metadata.put(key, game.optString(key));
    values.put("metadata", metadata.toString());
    getWritableDatabase().insertOrThrow("downloads", null, values);
    return id;
  }

  synchronized List<Item> all() {
    List<Item> items = new ArrayList<>();
    try (Cursor c =
        getReadableDatabase().query("downloads", null, null, null, null, null, "created DESC")) {
      while (c.moveToNext()) items.add(new Item(c));
    }
    return items;
  }

  synchronized Item get(String id) {
    try (Cursor c =
        getReadableDatabase()
            .query("downloads", null, "id=?", new String[] {id}, null, null, null)) {
      return c.moveToFirst() ? new Item(c) : null;
    }
  }

  synchronized void state(String id, String state, String message) {
    ContentValues values = new ContentValues();
    values.put("state", state);
    values.put("message", message);
    getWritableDatabase().update("downloads", values, "id=?", new String[] {id});
  }

  synchronized void progress(String id, long bytes) {
    ContentValues values = new ContentValues();
    values.put("bytes", bytes);
    getWritableDatabase().update("downloads", values, "id=?", new String[] {id});
  }

  synchronized void played(String id) {
    ContentValues values = new ContentValues();
    values.put("last_played", System.currentTimeMillis());
    getWritableDatabase().update("downloads", values, "id=?", new String[] {id});
  }

  synchronized void remember(ServerConfig source, List<JSONObject> games) {
    for (JSONObject game : games) {
      try {
        JSONObject metadata = new JSONObject();
        for (String key : new String[] {"cover_url", "platform_name"})
          if (game.has(key)) metadata.put(key, game.optString(key));
        ContentValues values = new ContentValues();
        values.put("metadata", metadata.toString());
        getWritableDatabase()
            .update(
                "downloads",
                values,
                "base=? AND game=?",
                new String[] {source.base, game.getString("id")});
      } catch (Exception ignored) {
        /* Catalog browsing must not lose a valid local file. */
      }
    }
  }

  boolean present(Item item) {
    if (!READY.equals(item.state)) return false;
    try {
      File file = complete(item);
      return file.isFile() && file.length() == item.size;
    } catch (IOException ignored) {
      return false;
    }
  }

  synchronized void manifest(String id, JSONObject json, DownloadManifest manifest) {
    ContentValues values = new ContentValues();
    values.put("manifest", json.toString());
    values.put("size", manifest.size);
    values.put("name", manifest.name);
    getWritableDatabase().update("downloads", values, "id=?", new String[] {id});
  }

  File directory(String id) throws IOException {
    if (id == null || !id.matches("[a-f0-9-]{36}")) throw new IOException("本机任务标识无效");
    File dir = new File(root, id);
    if (!dir.getCanonicalFile().getParentFile().equals(root.getCanonicalFile()))
      throw new IOException("文件路径越界");
    if (!dir.exists() && !dir.mkdir()) throw new IOException("无法创建本机任务目录");
    return dir;
  }

  File partial(Item item) throws IOException {
    return new File(directory(item.id), "download.part");
  }

  File complete(Item item) throws IOException {
    if (!item.name.matches("[A-Za-z0-9_-]{1,128}\\.[A-Za-z0-9]{1,8}"))
      throw new IOException("本机文件名无效");
    return new File(directory(item.id), item.name);
  }

  synchronized void cancel(String id) throws IOException {
    Item item = get(id);
    if (item == null) return;
    if (QUEUED.equals(item.state) || RUNNING.equals(item.state) || PREPARING.equals(item.state))
      throw new IOException("请先暂停并等待下载停止");
    // Only our task-owned partial and completed ROM; never emulator saves or shared folders.
    for (File file :
        new File[] {partial(item), item.name.isEmpty() ? partial(item) : complete(item)}) {
      if (file.exists() && !file.delete()) throw new IOException("文件正在使用或无法移除");
    }
    state(id, CANCELED, "本机下载已移除；模拟器存档未改动");
    progress(id, 0);
  }

  static final class Item {
    final String id, base, game, title, platform, state, manifest, name, message, metadata;
    final boolean http;
    final long bytes, size, created, lastPlayed;

    Item(Cursor c) {
      id = s(c, "id");
      base = s(c, "base");
      game = s(c, "game");
      title = s(c, "title");
      platform = s(c, "platform");
      state = s(c, "state");
      manifest = s(c, "manifest");
      name = s(c, "name");
      message = s(c, "message");
      http = c.getInt(c.getColumnIndexOrThrow("http")) == 1;
      bytes = c.getLong(c.getColumnIndexOrThrow("bytes"));
      size = c.getLong(c.getColumnIndexOrThrow("size"));
      created = c.getLong(c.getColumnIndexOrThrow("created"));
      lastPlayed = c.getLong(c.getColumnIndexOrThrow("last_played"));
      metadata = s(c, "metadata");
    }

    private static String s(Cursor c, String key) {
      return c.getString(c.getColumnIndexOrThrow(key));
    }
  }
}
