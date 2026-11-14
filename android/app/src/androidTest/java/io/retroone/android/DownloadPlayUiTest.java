package io.retroone.android;

import static org.junit.Assert.*;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.view.accessibility.AccessibilityNodeInfo;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.FileOutputStream;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Opt-in UI acceptance using only our generated fixture content. Never run over a live game. */
@RunWith(AndroidJUnit4.class)
public class DownloadPlayUiTest {
  private Instrumentation inst;
  private Context context;

  private AccessibilityNodeInfo find(AccessibilityNodeInfo node, String label, boolean desc) {
    if (node == null) return null;
    CharSequence value = desc ? node.getContentDescription() : node.getText();
    if (value != null && value.toString().startsWith(label)) return node;
    for (int i = 0; i < node.getChildCount(); i++) {
      AccessibilityNodeInfo match = find(node.getChild(i), label, desc);
      if (match != null) return match;
    }
    return null;
  }

  private AccessibilityNodeInfo waitNode(String label, boolean desc) throws Exception {
    long end = System.currentTimeMillis() + 15000;
    do {
      AccessibilityNodeInfo node =
          find(inst.getUiAutomation().getRootInActiveWindow(), label, desc);
      if (node != null) return node;
      Thread.sleep(150);
    } while (System.currentTimeMillis() < end);
    throw new AssertionError("Missing action: " + label);
  }

  private void click(String label, boolean desc) throws Exception {
    assertTrue(waitNode(label, desc).performAction(AccessibilityNodeInfo.ACTION_CLICK));
    inst.waitForIdleSync();
    Thread.sleep(250);
  }

  private void shot(String name) throws Exception {
    Thread.sleep(400);
    Bitmap image = inst.getUiAutomation().takeScreenshot();
    assertNotNull(image);
    File directory = new File(context.getFilesDir(), "qa");
    assertTrue(directory.isDirectory() || directory.mkdir());
    try (FileOutputStream out = new FileOutputStream(new File(directory, name + ".png"))) {
      image.compress(Bitmap.CompressFormat.PNG, 100, out);
    }
  }

  private void state(RetroApplication app, String id, String expected) throws Exception {
    long end = System.currentTimeMillis() + 60000;
    do {
      DownloadStore.Item item = app.downloads.get(id);
      assertNotEquals(item.message, DownloadStore.FAILED, item.state);
      if (expected.equals(item.state)) return;
      Thread.sleep(150);
    } while (System.currentTimeMillis() < end);
    fail("Download did not reach " + expected);
  }

  @Test
  public void completionPlayAndPersistentProgressBubble() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    org.junit.Assume.assumeTrue(!base.isEmpty());
    inst = InstrumentationRegistry.getInstrumentation();
    context = inst.getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    ServerConfig old = null;
    try {
      old = app.credentials.current();
    } catch (Exception ignored) {
    }
    ServerConfig config = new ServerConfig(base, true, "");
    ApiClient api = new ApiClient(config);
    JSONObject game =
        api.json(config.api("games?q=RetroOne-ISO-Self-Test"))
            .getJSONArray("games")
            .getJSONObject(0);
    assertEquals("RetroOne-ISO-Self-Test", game.getString("title"));
    JSONObject transfer =
        api.json(config.api("games?q=Transfer-Only-Not-A-Game"))
            .getJSONArray("games")
            .getJSONObject(0);
    String playableId = null, transferId = null;
    Activity activity = null;
    try {
      app.credentials.save(config);
      for (DownloadStore.Item item : app.downloads.all())
        if (item.base.equals(config.base)
            && (item.game.equals(game.getString("id"))
                || item.game.equals(transfer.getString("id")))) app.downloads.cancel(item.id);
      activity =
          inst.startActivitySync(
              new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      Activity screen = activity;
      inst.runOnMainSync(
          () ->
              screen
                  .getWindow()
                  .addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
      click("RetroOne-ISO-Self-Test，查看详情", true);
      click("下载 RetroOne-ISO-Self-Test", true);
      waitNode("查看全部下载", false);
      waitNode("游玩 RetroOne-ISO-Self-Test", true);
      assertEquals(
          "Completion must not auto-launch an emulator",
          context.getPackageName(),
          inst.getUiAutomation().getRootInActiveWindow().getPackageName().toString());
      for (DownloadStore.Item item : app.downloads.all())
        if (item.base.equals(config.base)
            && item.game.equals(game.getString("id"))
            && DownloadStore.READY.equals(item.state)) playableId = item.id;
      assertNotNull(playableId);
      shot("download-complete-popup");
      click("收起下载浮层", true);
      // Same catalog, no reload: the completed card gains a Play button.
      waitNode("游玩 RetroOne-ISO-Self-Test", true);
      shot("catalog-play");
      transferId = app.downloads.enqueue(config, transfer);
      DownloadService.command(context, DownloadService.RESUME, transferId);
      state(app, transferId, DownloadStore.RUNNING);
      waitNode("下载进度：", true);
      click("下载进度：", true);
      click("暂停", false);
      state(app, transferId, DownloadStore.PAUSED);
      click("继续 / 重试", false);
      waitNode("暂停", false);
      shot("download-progress-popup");
      click("收起下载浮层", true);
      // Badge survives page changes and the service continues independently.
      click("更多功能", true);
      click("应用", false);
      waitNode("下载进度：", true);
      shot("apps-download-progress");
      state(app, transferId, DownloadStore.READY);
      click("下载完成：", true);
      waitNode("游玩 RetroOne-ISO-Self-Test", true);
      if ("true".equals(InstrumentationRegistry.getArguments().getString("launchPpsspp"))) {
        click("游玩 RetroOne-ISO-Self-Test", true);
        long end = System.currentTimeMillis() + 20000;
        boolean rendered = false;
        do {
          Thread.sleep(250);
          Bitmap image = inst.getUiAutomation().takeScreenshot();
          if (image != null) {
            // Find the original program's exact teal band even with Android system insets.
            for (int y = 0; y < image.getHeight(); y++)
              if (image.getPixel(image.getWidth() / 2, y) == 0xff66d8c0) {
                rendered = true;
                break;
              }
          }
        } while (!rendered && System.currentTimeMillis() < end);
        assertTrue("Play must run the original PSP program", rendered);
        shot("play-ppsspp-original-program");
      }
    } finally {
      if (transferId != null) app.downloads.cancel(transferId);
      // Keep the small original program: PPSSPP may still be reading its granted URI.
      if (old != null) app.credentials.save(old);
      if (activity != null) {
        Activity screen = activity;
        inst.runOnMainSync(
            () ->
                screen
                    .getWindow()
                    .clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
      }
    }
  }
}
