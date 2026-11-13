package io.retroone.android;

import static org.junit.Assert.*;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.view.WindowManager;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Run phase=prepare, then a separate instrumentation invocation phase=recover. Android terminates
 * the instrumented process between runs; data is never cleared.
 */
@RunWith(AndroidJUnit4.class)
public class RecoveryTest {
  @Test
  public void interruptedDownloadSurvivesProcessDeath() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    String phase = InstrumentationRegistry.getArguments().getString("phase", "");
    org.junit.Assume.assumeTrue(!base.isEmpty() && !phase.isEmpty());
    android.app.Instrumentation inst = InstrumentationRegistry.getInstrumentation();
    Context context = inst.getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    Activity activity =
        inst.startActivitySync(
            new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra("downloads", true));
    inst.runOnMainSync(
        () -> activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    ServerConfig config = new ServerConfig(base, true, "");
    ServerConfig old = null;
    try {
      old = app.credentials.current();
    } catch (Exception ignored) {
    }
    try {
      app.credentials.save(config);
      String id;
      if ("prepare".equals(phase)) {
        JSONObject game =
            new ApiClient(config)
                .json(config.api("games?q=Transfer-Only-Not-A-Game"))
                .getJSONArray("games")
                .getJSONObject(0);
        id = app.downloads.enqueue(config, game);
        DownloadService.command(context, DownloadService.RESUME, id);
        long until = System.currentTimeMillis() + 20000;
        while (app.downloads.get(id).bytes < 2 * 1024 * 1024 && System.currentTimeMillis() < until)
          Thread.sleep(100);
        assertEquals(DownloadStore.RUNNING, app.downloads.get(id).state);
        assertTrue(
            app.downloads.get(id).bytes > 0 && app.downloads.get(id).bytes < 32L * 1024 * 1024);
      } else {
        DownloadStore.Item found = null;
        for (DownloadStore.Item item : app.downloads.all())
          if (item.base.equals(base)
              && item.title.contains("Transfer-Only")
              && !DownloadStore.CANCELED.equals(item.state)) {
            found = item;
            break;
          }
        assertNotNull(found);
        id = found.id;
        assertEquals(DownloadStore.PAUSED, found.state);
        assertTrue(found.bytes > 0);
        DownloadService.command(context, DownloadService.RESUME, id);
        long until = System.currentTimeMillis() + 90000;
        while (!DownloadStore.READY.equals(app.downloads.get(id).state)
            && System.currentTimeMillis() < until) {
          assertNotEquals(DownloadStore.FAILED, app.downloads.get(id).state);
          Thread.sleep(250);
        }
        assertEquals(DownloadStore.READY, app.downloads.get(id).state);
        assertEquals(32L * 1024 * 1024, app.downloads.get(id).bytes);
        app.downloads.cancel(id);
      }
    } finally {
      if (old != null) app.credentials.save(old);
      inst.runOnMainSync(
          () -> activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    }
  }
}
