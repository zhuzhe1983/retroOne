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
      if (find(text, null) != null) return;
      Thread.sleep(200);
    }
    fail("Expected library status not displayed");
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
      AccessibilityNodeInfo search = find(null, "android.widget.EditText");
      assertNotNull(search);
      Bundle value = new Bundle();
      value.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "Mario");
      assertTrue(search.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, value));
      waitText("共 " + filtered + " 款 · 第 1 页");
      Bundle report = new Bundle();
      report.putInt("catalog_total", all);
      report.putInt("search_total", filtered);
      inst.sendStatus(2, report);
      value.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "");
      search = find(null, "android.widget.EditText");
      assertNotNull(search);
      assertTrue(search.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, value));
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
    } finally {
      inst.runOnMainSync(
          () -> activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    }
  }
}
