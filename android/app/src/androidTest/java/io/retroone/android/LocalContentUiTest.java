package io.retroone.android;

import static org.junit.Assert.*;

import android.app.*;
import android.content.*;
import android.graphics.Bitmap;
import android.view.accessibility.AccessibilityNodeInfo;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.*;
import java.util.*;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;

/**
 * Run after DeviceIntegrationTest on 0.2.2 then upgrade, proving old downloads survive migration.
 */
@RunWith(AndroidJUnit4.class)
public class LocalContentUiTest {
  private Instrumentation inst;

  private AccessibilityNodeInfo find(AccessibilityNodeInfo n, String label, boolean description) {
    if (n == null) return null;
    CharSequence value = description ? n.getContentDescription() : n.getText();
    if (value != null && value.toString().equals(label)) return n;
    for (int i = 0; i < n.getChildCount(); i++) {
      AccessibilityNodeInfo found = find(n.getChild(i), label, description);
      if (found != null) return found;
    }
    return null;
  }

  private AccessibilityNodeInfo waitFor(String label, boolean description) throws Exception {
    long end = System.currentTimeMillis() + 15000;
    do {
      AccessibilityNodeInfo n =
          find(inst.getUiAutomation().getRootInActiveWindow(), label, description);
      if (n != null) return n;
      Thread.sleep(150);
    } while (System.currentTimeMillis() < end);
    throw new AssertionError("Missing UI: " + label);
  }

  private void click(String label, boolean description) throws Exception {
    assertTrue(waitFor(label, description).performAction(AccessibilityNodeInfo.ACTION_CLICK));
    inst.waitForIdleSync();
    Thread.sleep(300);
  }

  private void screenshot(Context context, String name) throws Exception {
    Thread.sleep(500);
    Bitmap bitmap = inst.getUiAutomation().takeScreenshot();
    assertNotNull(bitmap);
    File dir = new File(context.getFilesDir(), "qa");
    assertTrue(dir.isDirectory() || dir.mkdir());
    try (FileOutputStream out = new FileOutputStream(new File(dir, name + ".png"))) {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, out);
    }
  }

  @Test
  public void offlineDownloadedFilterAndIndependentSaveRemoval() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    org.junit.Assume.assumeTrue(!base.isEmpty());
    inst = InstrumentationRegistry.getInstrumentation();
    Context context = inst.getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    ServerConfig old = app.credentials.current(), source = new ServerConfig(base, true, "");
    JSONObject game =
        new ApiClient(source)
            .json(source.api("games?q=RetroOne-ISO-Self-Test"))
            .getJSONArray("games")
            .getJSONObject(0);
    DownloadStore.Item playable = null;
    for (DownloadStore.Item item : app.downloads.all())
      if (item.base.equals(base)
          && item.game.equals(game.getString("id"))
          && app.downloads.present(item)) playable = item;
    assertNotNull("A download from 0.2.2 must survive database upgrade", playable);
    assertNotNull(playable.metadata);
    assertTrue(playable.created > 0);
    DownloadStore.Item selected = playable;
    JSONObject partialGame =
        new JSONObject()
            .put("id", "111111111111111111111111")
            .put("title", "Incomplete fixture")
            .put("platform", "psp");
    String partial = app.downloads.enqueue(source, partialGame);
    app.downloads.state(partial, DownloadStore.PAUSED, "test");
    SaveLibrary library = new SaveLibrary(context.getFilesDir());
    for (SaveLibrary.Entry entry : library.all())
      if (entry.info.optString("game").equals(game.getString("id"))
          && entry.info.optString("base").startsWith(base.replace("/p/player1", "/p/")))
        library.delete(entry);
    JSONObject snapshot =
        new JSONObject()
            .put("revision", 3)
            .put("digest", "fixture")
            .put(
                "files",
                new JSONArray()
                    .put(new JSONObject().put("path", "TESTSAVE/DATA.BIN").put("data", "AQID")));
    SaveLibrary.Entry a = library.create(source, "测试甲", game, snapshot);
    SaveLibrary.Entry b =
        library.create(
            new ServerConfig(base.replace("player1", "player2"), true, ""), "测试乙", game, snapshot);
    ServerConfig offline = new ServerConfig("http://127.0.0.1:1/p/player1", true, "");
    Activity activity = null;
    try {
      app.credentials.save(offline);
      context
          .getSharedPreferences("MainActivity", 0)
          .edit()
          .putString(offline.key() + ".scope", "downloaded")
          .commit();
      activity =
          inst.startActivitySync(
              new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      waitFor("已下载 1 款 · 第 1 页", false);
      waitFor("游玩 RetroOne-ISO-Self-Test", true);
      assertNull(
          find(inst.getUiAutomation().getRootInActiveWindow(), "Incomplete fixture，查看详情", true));
      screenshot(context, "downloaded-offline");
      click("游戏排序", true);
      click("文件大小", false);
      waitFor("已下载 1 款 · 第 1 页", false);
      click("更多功能", true);
      click("本地内容", false);
      waitFor("移除游戏 RetroOne-ISO-Self-Test", true);
      screenshot(context, "local-games");
      click("移除游戏 RetroOne-ISO-Self-Test", true);
      click("保留", false);
      assertTrue(app.downloads.present(selected));
      click("移除游戏 RetroOne-ISO-Self-Test", true);
      click("移除游戏文件", false);
      long end = System.currentTimeMillis() + 10000;
      while (app.downloads.present(selected) && System.currentTimeMillis() < end) Thread.sleep(100);
      assertFalse(app.downloads.present(selected));
      assertTrue(a.file.isFile());
      assertTrue(b.file.isFile());
      click("存档", false);
      waitFor("移除存档备份 RetroOne-ISO-Self-Test 测试甲", true);
      waitFor("移除存档备份 RetroOne-ISO-Self-Test 测试乙", true);
      screenshot(context, "local-save-backups");
      click("移除存档备份 RetroOne-ISO-Self-Test 测试甲", true);
      click("保留", false);
      assertTrue(a.file.isFile());
      click("移除存档备份 RetroOne-ISO-Self-Test 测试甲", true);
      click("仅移除这份备份", false);
      end = System.currentTimeMillis() + 10000;
      while (a.file.exists() && System.currentTimeMillis() < end) Thread.sleep(100);
      assertFalse(a.file.exists());
      assertTrue(b.file.isFile());
    } finally {
      if (a.file.exists()) library.delete(a);
      if (b.file.exists()) library.delete(b);
      app.downloads.cancel(partial);
      app.credentials.save(old);
      if (activity != null) {
        Activity screen = activity;
        inst.runOnMainSync(screen::finish);
      }
    }
  }
}
