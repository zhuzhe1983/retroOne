package io.retroone.android;

import static org.junit.Assert.*;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;

/** All real-file tests require explicit task IDs; no discovery, save writes or process stops. */
@RunWith(AndroidJUnit4.class)
public class NativeAdapterDeviceTest {
  private Context context() {
    return InstrumentationRegistry.getInstrumentation().getTargetContext();
  }

  private DownloadStore.Item task(String argument) {
    String id = InstrumentationRegistry.getArguments().getString(argument, "");
    assertTrue("Explicit task UUID required", id.matches("[a-f0-9-]{36}"));
    DownloadStore.Item item = RetroApplication.from(context()).downloads.get(id);
    assertNotNull(item);
    assertEquals(DownloadStore.READY, item.state);
    return item;
  }

  private boolean opt(String key) {
    return "true".equals(InstrumentationRegistry.getArguments().getString(key));
  }

  @Test
  public void edenResumeCannotRequestRomSwap() {
    Intent resume = new Eden(context()).resumeIntent();
    assertEquals(Intent.ACTION_MAIN, resume.getAction());
    assertNull(resume.getData());
    assertNull(resume.getExtras());
    assertNull(resume.getClipData());
    assertEquals(0, resume.getFlags() & Intent.FLAG_GRANT_READ_URI_PERMISSION);
    assertTrue((resume.getFlags() & Intent.FLAG_ACTIVITY_REORDER_TO_FRONT) != 0);
  }

  @Test
  public void explicitFbneoArchiveAndEnvironment() throws Exception {
    org.junit.Assume.assumeTrue(opt("fbneoAdapterProbe"));
    DownloadStore.Item item = task("fbneoTaskId");
    Fbneo adapter = new Fbneo(context());
    String expected = InstrumentationRegistry.getArguments().getString("expectedSet", "");
    assertTrue(expected.matches("[A-Za-z0-9_-]{1,64}"));
    String path = adapter.prepare(item);
    assertTrue(path.endsWith("/" + expected + ".zip"));
    assertEquals(path, adapter.prepare(item));
    assertNull("This check is only for official FBNeo sets", adapter.compatibility(item));
    Intent intent = adapter.retroarch.intent(path, "fbneo");
    assertTrue(intent.getStringExtra("LIBRETRO").endsWith("/cores/fbneo_libretro_android.so"));
    assertEquals(adapter.retroarch.configPath(), intent.getStringExtra("CONFIGFILE"));
    assertFalse(intent.hasExtra("QUITFOCUS"));
    try {
      adapter.retroarch.intent(adapter.retroarch.directoryLabel() + "/../outside.zip", "fbneo");
      fail();
    } catch (java.io.IOException expectedFailure) {
    }
    if (opt("launchFbneo")) context().startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
  }

  @Test
  public void explicitCompatibilityArchiveAndEnvironment() throws Exception {
    org.junit.Assume.assumeTrue(opt("arcadeCompatProbe"));
    DownloadStore.Item item = task("fbneoTaskId");
    java.io.File source = RetroApplication.from(context()).downloads.complete(item);
    long bytes = source.length(), modified = source.lastModified();
    ArcadeCompatProfile.Profile profile = ArcadeCompatProfile.match(source);
    assertNotNull("Only the exact reviewed MOD archive may be used", profile);
    Fbneo adapter = new Fbneo(context());
    String path = adapter.prepare(item);
    assertEquals(path, adapter.prepare(item));
    assertEquals("mame2003plus", adapter.launchPlatform(item));
    assertEquals(profile.id, adapter.compatibility(item).id);
    assertEquals(LaunchPolicy.State.OPEN_ARCADE_COMPAT, Emulators.status(context(), item, false));
    Intent intent = adapter.retroarch.intent(path, adapter.launchPlatform(item));
    assertTrue(intent.getStringExtra("LIBRETRO").endsWith("/cores/" + profile.core));
    assertEquals(adapter.retroarch.configPath(), intent.getStringExtra("CONFIGFILE"));
    assertFalse(intent.hasExtra("QUITFOCUS"));
    assertEquals(bytes, source.length());
    assertEquals(modified, source.lastModified());
    assertEquals(profile.id, ArcadeCompatProfile.match(source).id);
    assertEquals(
        DownloadStore.READY, RetroApplication.from(context()).downloads.get(item.id).state);
    if (opt("launchArcadeCompat"))
      context().startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
  }

  @Test
  public void explicitNeoGeoBiosAndArchive() throws Exception {
    org.junit.Assume.assumeTrue(opt("neoGeoAdapterProbe"));
    DownloadStore.Item item = task("fbneoTaskId");
    Context context = context();
    Fbneo adapter = new Fbneo(context);
    java.io.File file = RetroApplication.from(context).downloads.complete(item);
    FbneoRomSet.Match inspected = FbneoRomSet.inspect(file, adapter.index());
    assertTrue(inspected.neoGeo);
    String expected = InstrumentationRegistry.getArguments().getString("expectedSet", "");
    assertEquals(expected, inspected.set);
    assertEquals(opt("expectedMerged"), inspected.merged);
    java.io.File bios = adapter.bios.file();
    assertNotNull("Import your own BIOS using the official app UI first", bios);
    String before = adapter.bios.fingerprint();
    FbneoRomSet.Bios valid = FbneoRomSet.verifyBios(bios, adapter.index());
    assertEquals(valid.sha256, FbneoRomSet.resolve(inspected, bios, adapter.index()).biosSha256);
    // Failed and cancelled imports must preserve the selected BIOS and file.
    try (java.io.InputStream invalid = new java.io.ByteArrayInputStream(new byte[] {1, 2, 3})) {
      try {
        adapter.bios.importFile(invalid, adapter.index(), new FbneoBios.Cancellation());
        fail();
      } catch (java.io.IOException expectedFailure) {
      }
    }
    try (java.io.InputStream original = new java.io.FileInputStream(bios)) {
      FbneoBios.Cancellation cancelled = new FbneoBios.Cancellation();
      cancelled.cancel();
      try {
        adapter.bios.importFile(original, adapter.index(), cancelled);
        fail();
      } catch (java.io.IOException expectedFailure) {
        assertTrue(expectedFailure.getMessage().contains("取消"));
      }
    }
    assertEquals(before, adapter.bios.fingerprint());
    String base = RetroApplication.from(context).credentials.base();
    String path = adapter.prepare(item);
    assertEquals(path, adapter.prepare(item));
    assertTrue(path.endsWith("/" + expected + ".zip"));
    assertEquals(
        adapter.retroarch.configPath(),
        adapter.retroarch.intent(path, "fbneo").getStringExtra("CONFIGFILE"));
    assertEquals(inspected.archiveSha256, FbneoRomSet.inspect(file, adapter.index()).archiveSha256);
    assertEquals(valid.sha256, FbneoRomSet.verifyBios(adapter.bios.file(), adapter.index()).sha256);
    assertEquals(base, RetroApplication.from(context).credentials.base());
    assertEquals(DownloadStore.READY, RetroApplication.from(context).downloads.get(item.id).state);
    assertEquals(before, adapter.bios.fingerprint());
    // This checks preparation only; use the formal Play UI to assess frame, sound and input.
  }

  @Test
  public void explicitUnmatchedArchivePreserved() throws Exception {
    org.junit.Assume.assumeTrue(opt("fbneoMismatchProbe"));
    DownloadStore.Item item = task("fbneoTaskId");
    java.io.File file = RetroApplication.from(context()).downloads.complete(item);
    long size = file.length(), modified = file.lastModified();
    try {
      new Fbneo(context()).prepare(item);
      fail("Unknown modified set was accepted");
    } catch (java.io.IOException expected) {
      assertTrue(expected.getMessage().contains("未匹配"));
    }
    assertEquals(size, file.length());
    assertEquals(modified, file.lastModified());
    assertEquals(
        DownloadStore.READY, RetroApplication.from(context()).downloads.get(item.id).state);
  }

  @Test
  public void edenDispatchFailureRevokesOnlyNewReadGrant() throws Exception {
    org.junit.Assume.assumeTrue(opt("edenFailureProbe"));
    Eden adapter = new Eden(context());
    assertTrue("Save/exit Eden and release its previous grant first", adapter.activeId().isEmpty());
    DownloadStore.Item item = task("edenTaskId");
    Uri uri = adapter.uri(item);
    int uid = context().getPackageManager().getApplicationInfo(Eden.PACKAGE, 0).uid;
    assertEquals(
        "Release earlier diagnostic grants before running this test",
        PackageManager.PERMISSION_DENIED,
        context().checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION));
    try {
      adapter.launch(
          item,
          intent -> {
            assertEquals(uri, intent.getData());
            assertTrue((intent.getFlags() & Intent.FLAG_ACTIVITY_NEW_TASK) != 0);
            assertNull(intent.getData().getQuery());
            assertEquals(
                0,
                intent.getFlags()
                    & (Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                        | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION));
            assertEquals(
                PackageManager.PERMISSION_GRANTED,
                context().checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION));
            throw new android.content.ActivityNotFoundException("Injected launch failure");
          });
      fail();
    } catch (android.content.ActivityNotFoundException expected) {
    }
    assertTrue(adapter.activeId().isEmpty());
    assertEquals(
        PackageManager.PERMISSION_DENIED,
        context().checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION));
  }
}
