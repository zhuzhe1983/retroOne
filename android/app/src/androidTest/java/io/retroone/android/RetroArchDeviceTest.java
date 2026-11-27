package io.retroone.android;

import static org.junit.Assert.*;

import android.content.ClipData;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import androidx.core.content.FileProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Explicit opt-in diagnostic only; never installs cores, changes settings, or stops an emulator.
 */
@RunWith(AndroidJUnit4.class)
public class RetroArchDeviceTest {
  @Test
  public void originalProbeAssetsAndNarrowGrant() throws Exception {
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    RetroArch adapter = new RetroArch(context);
    File file = adapter.probeFile();
    assertEquals(24592, file.length());
    byte[] bytes;
    try (java.io.InputStream in = new java.io.FileInputStream(file)) {
      bytes = ApiClient.read(in, 65536);
    }
    java.security.MessageDigest digest = java.security.MessageDigest.getInstance("SHA-256");
    StringBuilder hash = new StringBuilder();
    for (byte b : digest.digest(bytes))
      hash.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
    assertEquals(
        "a45cd77002fa7d91283fe2802b87cb304c4fafc1ee9d322a46ce8d3930d68e90", hash.toString());
    Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".games", file);
    assertEquals("content", uri.getScheme());
    try {
      FileProvider.getUriForFile(
          context, context.getPackageName() + ".games", new File(context.getCacheDir(), "private"));
      fail("Provider exposed other cache entries");
    } catch (IllegalArgumentException expected) {
    }
  }

  @Test
  public void diagnosticContentUriLaunch() throws Exception {
    org.junit.Assume.assumeTrue(
        "Opt in and inspect RetroArch screenshot/log; launch is not proof of playback",
        "true".equals(InstrumentationRegistry.getArguments().getString("probeRetroArchUri")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    RetroArch adapter = new RetroArch(context);
    assertTrue(adapter.installed());
    Uri uri =
        FileProvider.getUriForFile(
            context, context.getPackageName() + ".games", adapter.probeFile());
    Intent launch =
        new Intent(Intent.ACTION_MAIN)
            .setComponent(new ComponentName(RetroArchPolicy.PACKAGE, RetroArchPolicy.ACTIVITY))
            .putExtra("ROM", uri.toString())
            .putExtra("LIBRETRO", adapter.corePath())
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
    launch.setClipData(ClipData.newRawUri("Original NES probe", uri));
    adapter.withEnvironment(launch);
    context.startActivity(launch);
    android.util.Log.i(
        "RetroOneTest", "Content URI dispatched; inspect emulator for actual success/failure.");
  }

  @Test
  public void authorizedSharedFolderRoundTrip() throws Exception {
    org.junit.Assume.assumeTrue(
        "Requires an explicitly selected shared folder in the app",
        "true".equals(InstrumentationRegistry.getArguments().getString("probeRetroArchFolder")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    RetroArch adapter = new RetroArch(context);
    assertTrue(adapter.installed());
    assertTrue(adapter.configured());
    String path = adapter.stageProbe();
    assertEquals(
        path, adapter.stageProbe()); // reuse verified bytes, same basename for save continuity
    Intent launch = adapter.intent(path);
    assertEquals(path, launch.getStringExtra("ROM"));
    assertEquals(adapter.corePath(), launch.getStringExtra("LIBRETRO"));
    assertFalse(launch.hasExtra("QUITFOCUS"));
    android.content.pm.ApplicationInfo target =
        context.getPackageManager().getApplicationInfo(RetroArchPolicy.PACKAGE, 0);
    String storage = android.os.Environment.getExternalStorageDirectory().getAbsolutePath();
    String external = storage + "/Android/data/" + RetroArchPolicy.PACKAGE + "/files";
    assertEquals(external + "/retroarch.cfg", launch.getStringExtra("CONFIGFILE"));
    assertEquals(external, launch.getStringExtra("EXTERNAL"));
    assertEquals(storage, launch.getStringExtra("SDCARD"));
    assertEquals(target.dataDir, launch.getStringExtra("DATADIR"));
    assertEquals(target.sourceDir, launch.getStringExtra("APK"));
    assertNotEquals(context.getApplicationInfo().dataDir, launch.getStringExtra("DATADIR"));
    String ime =
        android.provider.Settings.Secure.getString(
            context.getContentResolver(), android.provider.Settings.Secure.DEFAULT_INPUT_METHOD);
    assertEquals(ime == null ? "" : ime, launch.getStringExtra("IME"));
    try {
      adapter.intent("/storage/emulated/0/elsewhere.nes");
      fail();
    } catch (java.io.IOException expected) {
    }
    if ("true".equals(InstrumentationRegistry.getArguments().getString("launchRetroArch"))) {
      context.startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      android.util.Log.i(
          "RetroOneTest", "Managed NES copy dispatched; inspect framebuffer and input separately.");
    }
  }

  @Test
  public void sharedProbeTamperingIsRejectedWithoutOverwrite() throws Exception {
    org.junit.Assume.assumeTrue(
        "Opt in only after exiting the original probe; mutates and restores its managed copy",
        "true".equals(InstrumentationRegistry.getArguments().getString("probeRetroArchFaults")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    RetroArch adapter = new RetroArch(context);
    assertTrue(adapter.configured());
    String path = adapter.stageProbe();
    assertTrue(
        path.endsWith("ro-a45cd77002fa7d91283fe2802b87cb304c4fafc1ee9d322a46ce8d3930d68e90.nes"));
    Uri parent = Uri.parse(context.getSharedPreferences("retroarch", 0).getString("folderUri", ""));
    Uri copy =
        android.provider.DocumentsContract.buildDocumentUriUsingTree(
            parent,
            android.provider.DocumentsContract.getDocumentId(parent)
                + "/"
                + new File(path).getName());
    byte[] original;
    try (java.io.InputStream in = context.getContentResolver().openInputStream(copy)) {
      original = ApiClient.read(in, 65536);
    }
    byte[] altered = original.clone();
    altered[1024] ^= 1;
    try {
      try (java.io.OutputStream out = context.getContentResolver().openOutputStream(copy, "wt")) {
        assertNotNull(out);
        out.write(altered);
      }
      try {
        adapter.stageProbe();
        fail("Accepted a changed shared copy");
      } catch (java.io.IOException expected) {
      }
      try (java.io.InputStream in = context.getContentResolver().openInputStream(copy)) {
        assertArrayEquals(
            "Changed copy must not be silently replaced", altered, ApiClient.read(in, 65536));
      }
    } finally {
      try (java.io.OutputStream out = context.getContentResolver().openOutputStream(copy, "wt")) {
        assertNotNull(out);
        out.write(original);
      }
    }
    assertEquals(path, adapter.stageProbe());
  }

  @Test
  public void fixtureDownloadStagesWithoutChangingConnection() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    org.junit.Assume.assumeTrue(
        "Explicit isolated fixture and confirmed NES setup required",
        !base.isEmpty()
            && "true"
                .equals(
                    InstrumentationRegistry.getArguments().getString("probeRetroArchDownloads")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    RetroApplication app = RetroApplication.from(context);
    RetroArch adapter = new RetroArch(context);
    assertTrue("Confirm real probe frame and input in the app first", adapter.verified());
    for (DownloadStore.Item item : app.downloads.all())
      org.junit.Assume.assumeFalse(
          "Do not run beside someone else's active downloads",
          java.util.List.of(DownloadStore.QUEUED, DownloadStore.RUNNING, DownloadStore.PREPARING)
              .contains(item.state));
    String oldBase = app.credentials.base();
    boolean oldHttp = app.credentials.http();
    android.app.Instrumentation inst = InstrumentationRegistry.getInstrumentation();
    inst.startActivitySync(
        new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    ServerConfig fixture = new ServerConfig(base, true, "");
    ApiClient api = new ApiClient(fixture);
    String lastPath = null;
    String[] names =
        "true".equals(InstrumentationRegistry.getArguments().getString("nesZip"))
            ? new String[] {
              "RetroOne-NES-Self-Test", "RetroOne-NES-Alternate", "RetroOne-NES-Zip-Test"
            }
            : new String[] {"RetroOne-NES-Self-Test", "RetroOne-NES-Alternate"};
    for (String name : names) {
      org.json.JSONArray games =
          api.json(fixture.api("games?q=" + ServerConfig.encode(name))).getJSONArray("games");
      assertEquals(1, games.length());
      org.json.JSONObject game = games.getJSONObject(0);
      assertEquals("nes", game.getString("platform"));
      assertTrue(game.getString("title").contains("RetroOne-NES"));
      String id = app.downloads.enqueue(fixture, game);
      DownloadService.command(context, DownloadService.RESUME, id);
      long until = clockMillis() + 60000;
      DownloadStore.Item item;
      do {
        Thread.sleep(200);
        item = app.downloads.get(id);
        assertNotEquals(item.message, DownloadStore.FAILED, item.state);
      } while (!DownloadStore.READY.equals(item.state) && clockMillis() < until);
      assertEquals(item.message, DownloadStore.READY, item.state);
      assertEquals(LaunchPolicy.State.PLAYABLE, Emulators.status(context, item, false));
      String path = adapter.stageGame(item);
      assertEquals(path, adapter.stageGame(item));
      assertNotEquals(lastPath, path);
      lastPath = path;
      assertEquals(path, adapter.intent(path).getStringExtra("ROM"));
    }
    assertEquals(oldBase, app.credentials.base());
    assertEquals(oldHttp, app.credentials.http());
  }

  private static long clockMillis() {
    return android.os.SystemClock.elapsedRealtime();
  }
}
