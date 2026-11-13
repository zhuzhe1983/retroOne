package io.retroone.android;

import static org.junit.Assert.*;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.view.WindowManager;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class DownloadLifecycleTest {
  @Test
  public void pauseResumeAndBackgroundCompletion() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    org.junit.Assume.assumeTrue(!base.isEmpty());
    Instrumentation instrumentation = InstrumentationRegistry.getInstrumentation();
    Context context = instrumentation.getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    ServerConfig old = null;
    try {
      old = app.credentials.current();
    } catch (Exception ignored) {
    }
    ServerConfig config = new ServerConfig(base, true, "");
    Activity activity =
        instrumentation.startActivitySync(
            new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra("downloads", true));
    instrumentation.runOnMainSync(
        () -> activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    String id = null;
    try {
      app.credentials.save(config);
      JSONObject game =
          new ApiClient(config)
              .json(config.api("games?q=Transfer-Only-Not-A-Game"))
              .getJSONArray("games")
              .getJSONObject(0);
      id = app.downloads.enqueue(config, game);
      DownloadStore.Item previous = app.downloads.get(id);
      if (DownloadStore.READY.equals(previous.state)) {
        app.downloads.cancel(id);
        id = app.downloads.enqueue(config, game);
      }
      DownloadService.command(context, DownloadService.RESUME, id);
      long until = System.currentTimeMillis() + 45000;
      while (app.downloads.get(id).bytes == 0 && System.currentTimeMillis() < until)
        Thread.sleep(100);
      assertTrue(app.downloads.get(id).bytes > 0);
      DownloadService.command(context, DownloadService.PAUSE, id);
      waitState(app, id, DownloadStore.PAUSED, 15000);
      long paused = app.downloads.get(id).bytes;
      Thread.sleep(1200);
      assertEquals(paused, app.downloads.get(id).bytes);
      DownloadService.command(context, DownloadService.RESUME, id);
      Thread.sleep(400);
      context.startActivity(
          new Intent(Intent.ACTION_MAIN)
              .addCategory(Intent.CATEGORY_HOME)
              .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      waitState(app, id, DownloadStore.READY, 90000);
      assertEquals(32L * 1024 * 1024, app.downloads.get(id).bytes);
      try (java.io.RandomAccessFile file =
          new java.io.RandomAccessFile(app.downloads.complete(app.downloads.get(id)), "r")) {
        file.seek(20L * 1024 * 1024 + 127);
        assertEquals(127, file.read());
      }
      // Remove only this generated, non-playable transfer fixture after the test.
      app.downloads.cancel(id);
      assertEquals(DownloadStore.CANCELED, app.downloads.get(id).state);
    } finally {
      if (old != null) app.credentials.save(old);
      instrumentation.runOnMainSync(
          () -> activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    }
  }

  private void waitState(RetroApplication app, String id, String expected, long timeout)
      throws Exception {
    long until = System.currentTimeMillis() + timeout;
    while (System.currentTimeMillis() < until) {
      DownloadStore.Item item = app.downloads.get(id);
      assertNotEquals(item.message, DownloadStore.FAILED, item.state);
      if (expected.equals(item.state)) return;
      InstrumentationRegistry.getInstrumentation()
          .getUiAutomation()
          .executeShellCommand("input keyevent KEYCODE_WAKEUP")
          .close();
      Thread.sleep(500);
    }
    assertEquals(app.downloads.get(id).message, expected, app.downloads.get(id).state);
  }
}
