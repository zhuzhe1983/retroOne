package io.retroone.android;

import static org.junit.Assert.*;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityNodeInfo;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Opt-in read-only UI test using the configured server; no launch or ROM requests. */
@RunWith(AndroidJUnit4.class)
public class LibraryUiTest {
  private Instrumentation inst;

  private AccessibilityNodeInfo find(AccessibilityNodeInfo node, String text, String clazz) {
    if (node == null) return null;
    if ((text == null || text.contentEquals(node.getText() == null ? "" : node.getText()))
        && (clazz == null || clazz.contentEquals(node.getClassName()))) return node;
    for (int i = 0; i < node.getChildCount(); i++) {
      AccessibilityNodeInfo match = find(node.getChild(i), text, clazz);
      if (match != null) return match;
    }
    return null;
  }

  private AccessibilityNodeInfo find(String text, String clazz) {
    return find(inst.getUiAutomation().getRootInActiveWindow(), text, clazz);
  }

  private void waitText(String text) throws Exception {
    long until = System.currentTimeMillis() + 25000;
    while (System.currentTimeMillis() < until) {
      AccessibilityNodeInfo root = inst.getUiAutomation().getRootInActiveWindow();
      if (root != null
          && root.findAccessibilityNodeInfosByText(text).stream()
              .anyMatch(n -> n.getText() != null && n.getText().toString().startsWith(text)))
        return;
      Thread.sleep(200);
    }
    fail("Expected library status not displayed");
  }

  private AccessibilityNodeInfo description(AccessibilityNodeInfo node, String label) {
    if (node == null) return null;
    if (label.contentEquals(
        node.getContentDescription() == null ? "" : node.getContentDescription())) return node;
    for (int i = 0; i < node.getChildCount(); i++) {
      AccessibilityNodeInfo match = description(node.getChild(i), label);
      if (match != null) return match;
    }
    return null;
  }

  private void clickDescription(String label) throws Exception {
    long deadline = System.currentTimeMillis() + 10000;
    AccessibilityNodeInfo node;
    do {
      node = description(inst.getUiAutomation().getRootInActiveWindow(), label);
      if (node != null) break;
      Thread.sleep(100);
    } while (System.currentTimeMillis() < deadline);
    assertNotNull("Missing action: " + label, node);
    assertTrue(node.performAction(AccessibilityNodeInfo.ACTION_CLICK));
  }

  private void screenshot(Context context, String name) throws Exception {
    Thread.sleep(1200);
    android.graphics.Bitmap image = inst.getUiAutomation().takeScreenshot();
    try (java.io.FileOutputStream out =
        new java.io.FileOutputStream(new java.io.File(context.getExternalFilesDir(null), name))) {
      assertNotNull(image);
      image.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out);
    }
  }

  @Test
  public void searchAndPaginationOnConfiguredLibrary() throws Exception {
    org.junit.Assume.assumeTrue(
        "true".equals(InstrumentationRegistry.getArguments().getString("readOnlyLibraryUi")));
    inst = InstrumentationRegistry.getInstrumentation();
    Context context = inst.getTargetContext();
    ServerConfig config = RetroApplication.from(context).credentials.current();
    ApiClient api = new ApiClient(config);
    int all = api.json(config.api("games?limit=1")).getInt("total");
    int filtered = api.json(config.api("games?q=Mario&limit=1")).getInt("total");
    assertTrue(filtered > 0 && filtered < all);
    Activity activity =
        inst.startActivitySync(
            new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    inst.runOnMainSync(
        () -> activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    try {
      waitText("共 " + all + " 款 · 第 1 页");
      org.json.JSONObject first =
          api.json(config.api(LibraryQuery.route("", "", "all", "popularity", "desc", 0)))
              .getJSONArray("games")
              .getJSONObject(0);
      AccessibilityNodeInfo card =
          description(
              inst.getUiAutomation().getRootInActiveWindow(), first.getString("title") + "，查看详情");
      assertNotNull(card);
      android.graphics.Rect bounds = new android.graphics.Rect();
      card.getBoundsInScreen(bounds);
      assertTrue(
          "Library chrome must use less than 32% of this landscape screen",
          bounds.top < context.getResources().getDisplayMetrics().heightPixels * .32);
      screenshot(context, "library-compact.png");
      clickDescription("搜索游戏");
      waitText("搜索游戏");
      AccessibilityNodeInfo search = find(null, "android.widget.EditText");
      assertNotNull(search);
      Bundle value = new Bundle();
      value.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "Mario");
      assertTrue(search.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, value));
      assertTrue(
          find("搜索", "android.widget.Button").performAction(AccessibilityNodeInfo.ACTION_CLICK));
      waitText("共 " + filtered + " 款 · 第 1 页");
      Bundle report = new Bundle();
      report.putInt("catalog_total", all);
      report.putInt("search_total", filtered);
      inst.sendStatus(2, report);
      clickDescription("搜索游戏");
      waitText("清除搜索");
      assertTrue(
          find("清除搜索", "android.widget.Button").performAction(AccessibilityNodeInfo.ACTION_CLICK));
      waitText("共 " + all + " 款 · 第 1 页");
      for (int i = 0; i < 25 && find("下一页", null) == null; i++) {
        AccessibilityNodeInfo scroll = find(null, "android.widget.ScrollView");
        assertNotNull(scroll);
        scroll.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD);
        Thread.sleep(120);
      }
      AccessibilityNodeInfo next = find("下一页", null);
      assertNotNull(next);
      assertTrue(next.performAction(AccessibilityNodeInfo.ACTION_CLICK));
      waitText("共 " + all + " 款 · 第 2 页");
      // A real downloadable PSP entry must retain its action even on this native client.
      clickDescription("搜索游戏");
      waitText("搜索游戏");
      search = find(null, "android.widget.EditText");
      value.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "N+");
      assertTrue(search.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, value));
      assertTrue(
          find("搜索", "android.widget.Button").performAction(AccessibilityNodeInfo.ACTION_CLICK));
      clickDescription("N+，查看详情");
      waitText("PSP · 使用已安装的 PPSSPP");
      assertTrue(
          find("↓ 下载到掌机", null) != null
              || find("▶ 游玩", null) != null
              || find("已暂停 · 继续", null) != null);
      screenshot(context, "psp-download.png");
      assertTrue(
          find("返回", "android.widget.Button").performAction(AccessibilityNodeInfo.ACTION_CLICK));
      clickDescription("切换玩家");
      waitText("选择玩家");
      screenshot(context, "player-picker.png");
      // Dismiss without switching a family member or changing server state.
      inst.sendKeyDownUpSync(android.view.KeyEvent.KEYCODE_BACK);
    } finally {
      inst.runOnMainSync(
          () -> activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    }
  }
}
