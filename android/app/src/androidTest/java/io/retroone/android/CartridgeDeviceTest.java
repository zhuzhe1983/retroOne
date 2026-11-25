package io.retroone.android;

import static org.junit.Assert.*;

import android.content.Context;
import android.content.Intent;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.IOException;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Only opt-in tests use a shared folder or start an emulator. A launch is not playback proof. */
@RunWith(AndroidJUnit4.class)
public class CartridgeDeviceTest {
  @Test
  public void originalCartridgeAssets() throws Exception {
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    CartridgeAdapter adapter = new CartridgeAdapter(context);
    for (CartridgePolicy.Spec spec : CartridgePolicy.all()) {
      File file = adapter.probeFile(spec);
      assertTrue(spec.id, file.isFile());
      assertTrue(spec.id, file.length() > 0 && file.length() <= spec.maxBytes);
      CartridgePolicy.validateFile(spec, file);
    }
  }

  @Test
  public void authorizedCartridgeRoundTrip() throws Exception {
    org.junit.Assume.assumeTrue(
        "Explicit shared-folder opt-in required",
        "true".equals(InstrumentationRegistry.getArguments().getString("cartridgeFolder")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    CartridgeAdapter adapter = new CartridgeAdapter(context);
    CartridgePolicy.Spec spec = selected();
    assertTrue(adapter.retroarch.installed());
    assertTrue(adapter.retroarch.configured());
    boolean wasVerified = adapter.verified(spec);
    boolean nesVerified = adapter.retroarch.verified();
    String path = adapter.stageProbe(spec);
    assertEquals(path, adapter.stageProbe(spec));
    Intent launch = adapter.retroarch.intent(path, spec.id);
    assertEquals(path, launch.getStringExtra("ROM"));
    assertTrue(launch.getStringExtra("LIBRETRO").endsWith("/cores/" + spec.core));
    assertEquals(adapter.retroarch.configPath(), launch.getStringExtra("CONFIGFILE"));
    assertEquals(adapter.retroarch.externalFilesPath(), launch.getStringExtra("EXTERNAL"));
    assertEquals(
        context.getPackageManager().getApplicationInfo(RetroArchPolicy.PACKAGE, 0).dataDir,
        launch.getStringExtra("DATADIR"));
    assertFalse(launch.hasExtra("QUITFOCUS"));
    assertEquals(wasVerified, adapter.verified(spec));
    assertEquals(nesVerified, adapter.retroarch.verified());
    for (CartridgePolicy.Spec other : CartridgePolicy.all()) {
      if (other.id.equals(spec.id)) continue;
      assertNotEquals(adapter.fingerprint(spec), adapter.fingerprint(other));
      try {
        adapter.retroarch.intent(path, other.id);
        fail("Accepted another platform's path");
      } catch (IOException expected) {
      }
    }
    try {
      adapter.retroarch.intent(path + "/../other", spec.id);
      fail("Accepted path traversal");
    } catch (IOException expected) {
    }
    if ("true".equals(InstrumentationRegistry.getArguments().getString("launchCartridge"))) {
      context.startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      android.util.Log.i(
          "RetroOneTest",
          "Cartridge probe dispatched; inspect frame/input separately. No proof was recorded.");
    }
  }

  @Test
  public void fixtureDownloadAndStage() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    org.junit.Assume.assumeTrue(
        "Explicit isolated fixture opt-in required",
        !base.isEmpty()
            && "true"
                .equals(InstrumentationRegistry.getArguments().getString("cartridgeDownload")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    CartridgeAdapter adapter = new CartridgeAdapter(context);
    CartridgePolicy.Spec spec = selected();
    assertTrue("First verify this platform's real frame/input in the UI", adapter.verified(spec));
    for (DownloadStore.Item item : app.downloads.all()) {
      assertFalse(
          "Other downloads are active; run after they finish",
          java.util.List.of(DownloadStore.QUEUED, DownloadStore.RUNNING, DownloadStore.PREPARING)
              .contains(item.state));
    }
    String oldBase = app.credentials.base();
    boolean oldHttp = app.credentials.http();
    InstrumentationRegistry.getInstrumentation()
        .startActivitySync(
            new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    ServerConfig fixture = new ServerConfig(base, true, "");
    boolean zipped =
        "true".equals(InstrumentationRegistry.getArguments().getString("cartridgeZip"));
    String title = "RetroOne-Cart-" + spec.id + (zipped ? "-Zip-Test" : "-Self-Test");
    org.json.JSONArray games =
        new ApiClient(fixture)
            .json(fixture.api("games?q=" + ServerConfig.encode(title)))
            .getJSONArray("games");
    assertEquals(1, games.length());
    org.json.JSONObject game = games.getJSONObject(0);
    assertEquals(spec.id, game.getString("platform"));
    assertEquals(title, game.getString("title"));
    String id = app.downloads.enqueue(fixture, game);
    DownloadService.command(context, DownloadService.RESUME, id);
    long until = android.os.SystemClock.elapsedRealtime() + 60000;
    DownloadStore.Item item;
    do {
      Thread.sleep(200);
      item = app.downloads.get(id);
      assertNotEquals(item.message, DownloadStore.FAILED, item.state);
    } while (!DownloadStore.READY.equals(item.state)
        && android.os.SystemClock.elapsedRealtime() < until);
    assertEquals(item.message, DownloadStore.READY, item.state);
    assertEquals(LaunchPolicy.State.PLAYABLE, Emulators.status(context, item, false));
    String path = adapter.stageGame(item);
    assertEquals(path, adapter.stageGame(item));
    assertEquals(path, adapter.retroarch.intent(path, spec.id).getStringExtra("ROM"));
    assertEquals(oldBase, app.credentials.base());
    assertEquals(oldHttp, app.credentials.http());
  }

  @Test
  public void zipExtractionPublishesAndReusesOnAndroid() throws Exception {
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    File scratch =
        java.nio.file.Files.createTempDirectory(context.getCacheDir().toPath(), "zip-publish-test-")
            .toFile();
    try {
      java.util.List<String> platforms = new java.util.ArrayList<>();
      platforms.add("nes");
      for (CartridgePolicy.Spec spec : CartridgePolicy.all()) platforms.add(spec.id);
      for (String platform : platforms) {
        String asset =
            platform.equals("nes")
                ? "retroone-nes-probe.nes"
                : CartridgePolicy.spec(platform).probeAsset;
        byte[] bytes;
        try (java.io.InputStream in = context.getAssets().open(asset)) {
          bytes = ApiClient.read(in, 4 * 1024 * 1024);
        }
        File zip = new File(scratch, platform + ".zip");
        try (java.util.zip.ZipOutputStream out =
            new java.util.zip.ZipOutputStream(new java.io.FileOutputStream(zip))) {
          out.putNextEntry(new java.util.zip.ZipEntry("fixture/" + asset));
          out.write(bytes);
          out.closeEntry();
        }
        byte[] original = java.nio.file.Files.readAllBytes(zip.toPath());
        CartridgeArchive.Extracted extracted =
            CartridgeArchive.prepare(zip, zip.getName(), platform, scratch);
        assertTrue(extracted.fromArchive);
        assertArrayEquals(bytes, java.nio.file.Files.readAllBytes(extracted.file.toPath()));
        assertEquals(
            extracted.file, CartridgeArchive.prepare(zip, zip.getName(), platform, scratch).file);
        assertArrayEquals(original, java.nio.file.Files.readAllBytes(zip.toPath()));
        byte[] wrong = bytes.clone();
        wrong[wrong.length - 1] ^= 1;
        java.nio.file.Files.write(extracted.file.toPath(), wrong);
        try {
          CartridgeArchive.prepare(zip, zip.getName(), platform, scratch);
          fail("Changed cache overwritten");
        } catch (IOException expected) {
          assertTrue(expected.getMessage(), expected.getMessage().contains("不会覆盖"));
        }
        assertArrayEquals(wrong, java.nio.file.Files.readAllBytes(extracted.file.toPath()));
      }
      String[] pending =
          new File(scratch, "cartridge-roms").list((d, n) -> n.startsWith("pending-"));
      assertNotNull(pending);
      assertEquals(0, pending.length);
    } finally {
      // Only this test's unique private scratch tree is removed; no real downloads or probes.
      try (java.util.stream.Stream<java.nio.file.Path> paths =
          java.nio.file.Files.walk(scratch.toPath())) {
        for (java.nio.file.Path path :
            paths.sorted(java.util.Comparator.reverseOrder()).toArray(java.nio.file.Path[]::new))
          java.nio.file.Files.deleteIfExists(path);
      }
    }
  }

  private CartridgePolicy.Spec selected() {
    String platform = InstrumentationRegistry.getArguments().getString("cartridgePlatform", "");
    CartridgePolicy.Spec spec = CartridgePolicy.spec(platform);
    assertNotNull("Explicit cartridgePlatform required", spec);
    return spec;
  }
}
