package io.retroone.android;

import static org.junit.Assert.*;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ProviderInfo;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.InputStream;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Opt-in real API + foreground service + FileProvider + package visibility test. Requires
 * tools/fixture_server.py and an explicit adb USB reverse; no commercial ROM.
 */
@RunWith(AndroidJUnit4.class)
public class DeviceIntegrationTest {
  @Test
  public void realApiDownloadAndPpssppGrant() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    org.junit.Assume.assumeTrue(
        "Pass fixtureBase pointing to our generated fixture server", !base.isEmpty());
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    android.app.Instrumentation instrumentation = InstrumentationRegistry.getInstrumentation();
    android.app.Activity activity =
        instrumentation.startActivitySync(
            new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra("downloads", true));
    instrumentation.runOnMainSync(
        () ->
            activity
                .getWindow()
                .addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    stage("activity-ready");
    ServerConfig old = null;
    try {
      old = app.credentials.current();
    } catch (Exception ignored) {
    }
    ServerConfig config = new ServerConfig(base, true, "");
    try {
      // Exercise Android Keystore storage without logging the synthetic credential.
      ServerConfig secret = new ServerConfig(base, true, "Bearer original-fixture-only");
      app.credentials.save(secret);
      assertEquals(secret.authorization, app.credentials.current().authorization);
      app.credentials.save(config);
      stage("keystore-roundtrip-ok");
      ApiClient api = new ApiClient(config);
      String fixture =
          InstrumentationRegistry.getArguments()
              .getString("fixtureQuery", "RetroOne-ISO-Self-Test");
      JSONArray games =
          api.json(config.api("games?q=" + ServerConfig.encode(fixture))).getJSONArray("games");
      assertEquals(1, games.length());
      JSONObject game = games.getJSONObject(0);
      String id = app.downloads.enqueue(config, game);
      stage("catalog-and-queue-ok");
      Thread.sleep(1000);
      DownloadService.command(context, DownloadService.RESUME, id);
      long deadline = System.currentTimeMillis() + 90000;
      DownloadStore.Item item;
      do {
        Thread.sleep(250);
        item = app.downloads.get(id);
        assertNotEquals(item.message, DownloadStore.FAILED, item.state);
      } while (!DownloadStore.READY.equals(item.state) && System.currentTimeMillis() < deadline);
      assertEquals(item.message, DownloadStore.READY, item.state);
      stage("download-verified");
      assertEquals(item.size, item.bytes);
      assertTrue(item.size > 1000);
      assertTrue(
          "Installed PPSSPP must be visible on Android 13",
          Emulators.installed(context).contains(0));
      Intent launch = Emulators.intent(context, item, 0);
      assertEquals("content", launch.getData().getScheme());
      assertNotNull(launch.getClipData());
      assertEquals(Intent.FLAG_GRANT_READ_URI_PERMISSION, launch.getFlags());
      ProviderInfo provider =
          context
              .getPackageManager()
              .resolveContentProvider(context.getPackageName() + ".games", 0);
      assertNotNull(provider);
      assertFalse(provider.exported);
      assertTrue(provider.grantUriPermissions);
      try (InputStream input = context.getContentResolver().openInputStream(launch.getData())) {
        assertNotNull(input);
        assertTrue(input.read() >= 0);
      }
      DownloadManifest manifest = new DownloadManifest(new JSONObject(item.manifest), config);
      try (java.io.RandomAccessFile file =
          new java.io.RandomAccessFile(app.downloads.complete(item), "r")) {
        assertEquals(item.size, Transfer.verifiedPrefix(file, manifest, () -> false));
      }
      try {
        app.downloads.directory("../../external");
        fail("Traversal accepted");
      } catch (java.io.IOException expected) {
      }
      File secretFile = new File(context.getFilesDir(), "private-test.txt");
      try {
        androidx.core.content.FileProvider.getUriForFile(
            context, context.getPackageName() + ".games", secretFile);
        fail("Provider shared outside game directory");
      } catch (IllegalArgumentException expected) {
      }
      JSONArray multi = api.json(config.api("games?platform=psx")).getJSONArray("games");
      assertTrue(multi.length() > 0);
      JSONObject rejected =
          api.json(
              config.api("games/" + multi.getJSONObject(0).getString("id") + "/download-options"));
      assertFalse(rejected.getBoolean("available"));
      assertFalse(rejected.optString("reason").isEmpty());
      // Explicitly opt in: caller visually inspects the generated self-test in PPSSPP.
      if ("true".equals(InstrumentationRegistry.getArguments().getString("launchPpsspp"))) {
        context.startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        Thread.sleep(4000);
        android.app.UiAutomation automation = instrumentation.getUiAutomation();
        android.graphics.Bitmap before = automation.takeScreenshot();
        assertNotNull(before);
        int x = before.getWidth() / 2, y = before.getHeight() * 232 / 272;
        assertEquals(
            "Original fixture must have drawn its teal band", 0xff66d8c0, before.getPixel(x, y));
        File evidence = new File(context.getFilesDir(), "qa");
        assertTrue(evidence.isDirectory() || evidence.mkdir());
        try (java.io.FileOutputStream out =
            new java.io.FileOutputStream(new File(evidence, fixture + "-idle.png"))) {
          before.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out);
        }
        stage("ppsspp-original-framebuffer-ok");
        if ("true".equals(InstrumentationRegistry.getArguments().getString("verifyInput"))) {
          long now = android.os.SystemClock.uptimeMillis();
          int key = android.view.KeyEvent.KEYCODE_Z,
              source = android.view.InputDevice.SOURCE_KEYBOARD;
          automation.injectInputEvent(
              new android.view.KeyEvent(
                  now, now, android.view.KeyEvent.ACTION_DOWN, key, 0, 0, -1, 0, 0, source),
              true);
          android.graphics.Bitmap pressed;
          try {
            Thread.sleep(500);
            pressed = automation.takeScreenshot();
          } finally {
            automation.injectInputEvent(
                new android.view.KeyEvent(
                    now,
                    android.os.SystemClock.uptimeMillis(),
                    android.view.KeyEvent.ACTION_UP,
                    key,
                    0,
                    0,
                    -1,
                    0,
                    0,
                    source),
                true);
          }
          assertNotNull(pressed);
          try (java.io.FileOutputStream out =
              new java.io.FileOutputStream(new File(evidence, fixture + "-pressed.png"))) {
            pressed.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out);
          }
          assertEquals(
              "CROSS must turn the original program's band orange",
              0xffffa066,
              pressed.getPixel(x, y));
          stage("ppsspp-cross-input-ok");
        }
      }
    } finally {
      if (old != null) app.credentials.save(old);
      instrumentation.runOnMainSync(
          () ->
              activity
                  .getWindow()
                  .clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON));
    }
  }

  private void stage(String value) {
    android.os.Bundle data = new android.os.Bundle();
    data.putString("stage", value);
    InstrumentationRegistry.getInstrumentation().sendStatus(2, data);
  }
}
