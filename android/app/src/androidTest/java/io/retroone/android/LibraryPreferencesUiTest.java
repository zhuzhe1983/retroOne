package io.retroone.android;

import static org.junit.Assert.*;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityNodeInfo;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.*;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Mutating UI checks only against the opt-in, generated fixture server. */
@RunWith(AndroidJUnit4.class)
public class LibraryPreferencesUiTest {
  Instrumentation inst;

  AccessibilityNodeInfo find(AccessibilityNodeInfo node, String label, boolean description) {
    if (node == null) return null;
    CharSequence value = description ? node.getContentDescription() : node.getText();
    if (value != null && value.toString().equals(label)) return node;
    for (int i = 0; i < node.getChildCount(); i++) {
      AccessibilityNodeInfo found = find(node.getChild(i), label, description);
      if (found != null) return found;
    }
    return null;
  }

  AccessibilityNodeInfo waitFor(String label, boolean description) throws Exception {
    long until = System.currentTimeMillis() + 15000;
    do {
      AccessibilityNodeInfo node =
          find(inst.getUiAutomation().getRootInActiveWindow(), label, description);
      if (node != null) return node;
      Thread.sleep(150);
    } while (System.currentTimeMillis() < until);
    throw new AssertionError("Missing UI: " + label);
  }

  void click(String label, boolean description) throws Exception {
    assertTrue(waitFor(label, description).performAction(AccessibilityNodeInfo.ACTION_CLICK));
  }

  @Test
  public void pinsSortAndFavoritesRoundTripThroughUi() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase");
    org.junit.Assume.assumeTrue(base != null && !base.isEmpty());
    inst = InstrumentationRegistry.getInstrumentation();
    Context context = inst.getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    ServerConfig original = app.credentials.current();
    ServerConfig fixture = new ServerConfig(base, true, "");
    ApiClient api = new ApiClient(fixture);
    // This test is only meaningful with the supplied original fixture, never production ROMs.
    JSONArray games = api.json(fixture.api("games?q=RetroOne-ISO-Self-Test")).getJSONArray("games");
    assertEquals(1, games.length());
    JSONObject game = games.getJSONObject(0);
    assertEquals("RetroOne-ISO-Self-Test", game.getString("title"));
    JSONObject originalSettings = LibrarySettings.read(fixture).json();
    boolean wasFavorite = game.optBoolean("favorite");
    Activity activity = null;
    android.content.SharedPreferences prefs = context.getSharedPreferences("MainActivity", 0);
    String oldScope = prefs.getString(fixture.key() + ".scope", "all");
    try {
      prefs.edit().putString(fixture.key() + ".scope", "all").commit();
      api.put(
          fixture.api("settings/library"),
          new JSONObject(
              "{\"pinned_platforms\":[],\"sort\":\"popularity\",\"direction\":\"desc\"}"),
          16384);
      api.post(
          fixture.api("games/" + game.getString("id") + "/favorite"),
          new JSONObject().put("favorite", false),
          16384);
      app.credentials.save(fixture);
      activity =
          inst.startActivitySync(
              new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      Activity running = activity;
      inst.runOnMainSync(
          () -> running.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
      waitFor("RetroOne-ISO-Self-Test，查看详情", true);
      click("更多平台", true);
      click("关注 PSP", true);
      waitFor("取消关注 PSP", true);
      assertTrue(LibrarySettings.read(fixture).pins.contains("psp"));
      click("完成", false);
      click("游戏排序", true);
      click("游戏名称", false);
      waitFor("游戏名称 ↑", false);
      LibrarySettings updated = LibrarySettings.read(fixture);
      assertEquals("name", updated.sort);
      assertTrue(updated.pins.contains("psp"));
      click("RetroOne-ISO-Self-Test，查看详情", true);
      click("☆ 收藏", false);
      waitFor("★ 已收藏", false);
      assertTrue(api.json(fixture.api("games/" + game.getString("id"))).getBoolean("favorite"));
      click("返回", false);
      click("★ 收藏", false);
      waitFor("共 1 款 · 第 1 页", false);
      click("更多平台", true);
      click("取消关注 PSP", true);
      waitFor("关注 PSP", true);
      assertFalse(LibrarySettings.read(fixture).pins.contains("psp"));
    } finally {
      prefs.edit().putString(fixture.key() + ".scope", oldScope).commit();
      api.put(fixture.api("settings/library"), originalSettings, 16384);
      api.post(
          fixture.api("games/" + game.getString("id") + "/favorite"),
          new JSONObject().put("favorite", wasFavorite),
          16384);
      app.credentials.save(original);
      if (activity != null) {
        Activity running = activity;
        inst.runOnMainSync(
            () -> {
              running.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
              running.finish();
            });
      }
    }
  }
}
