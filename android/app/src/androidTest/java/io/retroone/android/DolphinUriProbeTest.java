package io.retroone.android;

import static org.junit.Assert.*;

import android.content.ClipData;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import androidx.core.content.FileProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.util.Locale;
import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Test APK only. Opt in with dolphinUriProbe=true and one dolphinTaskId UUID after saving/exiting
 * Dolphin. Inspect the frame during/after the 15-second observation window; dispatch is not proof
 * of playable emulation. After exiting, use dolphinUriRevoke=true and dolphinExited=true to revoke.
 */
@RunWith(AndroidJUnit4.class)
public class DolphinUriProbeTest {
  private static final String PACKAGE = "org.dolphinemu.dolphinemu";
  private static final ComponentName MAIN =
      new ComponentName(PACKAGE, PACKAGE + ".ui.main.MainActivity");

  @Test
  public void explicitReadyTaskReadOnlyUri() throws Exception {
    Bundle arguments = InstrumentationRegistry.getArguments();
    Assume.assumeTrue(
        "Opt in only after saving and exiting any current Dolphin game",
        "true".equals(arguments.getString("dolphinUriProbe")));
    assertFalse(
        "Dispatch and post-exit revocation are separate runs",
        "true".equals(arguments.getString("dolphinUriRevoke")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    Uri uri = taskUri(context, arguments);
    ActivityInfo activity = context.getPackageManager().getActivityInfo(MAIN, 0);
    assertTrue(
        "Use Dolphin's exported MainActivity, not private EmulationActivity", activity.exported);
    assertTrue(activity.enabled && activity.applicationInfo.enabled);
    int uid = activity.applicationInfo.uid;
    assertEquals(
        "Exit the earlier probe and revoke its grant before another dispatch",
        PackageManager.PERMISSION_DENIED,
        context.checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION));
    assertEquals(
        PackageManager.PERMISSION_DENIED,
        context.checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_WRITE_URI_PERMISSION));
    Intent launch =
        new Intent(Intent.ACTION_VIEW)
            .setComponent(MAIN)
            .setDataAndType(uri, context.getContentResolver().getType(uri))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
    launch.setClipData(ClipData.newRawUri("RetroOne explicit Dolphin probe", uri));
    assertEquals(uri, launch.getData());
    assertEquals(1, launch.getClipData().getItemCount());
    assertEquals(uri, launch.getClipData().getItemAt(0).getUri());
    assertEquals(
        Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION, launch.getFlags());
    boolean retainForLazyReads = false;
    try {
      // Explicit, package-specific grant of this one stable URI; never a tree or a write grant.
      context.grantUriPermission(PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
      assertEquals(
          PackageManager.PERMISSION_GRANTED,
          context.checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION));
      assertEquals(
          PackageManager.PERMISSION_DENIED,
          context.checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_WRITE_URI_PERMISSION));
      context.startActivity(launch);
      Thread.sleep(15000);
      retainForLazyReads = true;
    } finally {
      // Also revoke if an assertion or the observation wait fails after startActivity returned.
      if (!retainForLazyReads)
        context.revokeUriPermission(PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
    }
    android.util.Log.i(
        "RetroOneTest",
        "Dolphin readonly URI dispatched; visually verify game frame/input separately. Exit before"
            + " revoking.");
    // No downloads, configuration edits, emulator stops, or save-directory access by this probe.
    // Dolphin itself may maintain its normal recent-game metadata and game saves while running.
  }

  @Test
  public void revokeExplicitTaskGrantAfterExit() throws Exception {
    Bundle arguments = InstrumentationRegistry.getArguments();
    Assume.assumeTrue(
        "Explicit post-exit opt-in required",
        "true".equals(arguments.getString("dolphinUriRevoke")));
    assertEquals(
        "Confirm the probe game has been saved and exited",
        "true",
        arguments.getString("dolphinExited"));
    assertFalse(
        "Do not launch and revoke in the same run",
        "true".equals(arguments.getString("dolphinUriProbe")));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    Uri uri = taskUri(context, arguments);
    context.revokeUriPermission(PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
    int uid = context.getPackageManager().getApplicationInfo(PACKAGE, 0).uid;
    assertEquals(
        PackageManager.PERMISSION_DENIED,
        context.checkUriPermission(uri, -1, uid, Intent.FLAG_GRANT_READ_URI_PERMISSION));
  }

  private static Uri taskUri(Context context, Bundle arguments) throws Exception {
    String id = arguments.getString("dolphinTaskId", "");
    assertTrue(
        "Pass one exact ready task UUID; never auto-select by title or platform",
        id.matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"));
    DownloadStore store = RetroApplication.from(context).downloads;
    DownloadStore.Item item = store.get(id);
    assertNotNull("Task not found; this probe never downloads", item);
    assertTrue(
        "Only gamecube/gc/wii tasks are eligible",
        "gamecube".equals(item.platform)
            || "gc".equals(item.platform)
            || "wii".equals(item.platform));
    assertEquals(
        "The file must already be downloaded and verified", DownloadStore.READY, item.state);
    String name = item.name.toLowerCase(Locale.ROOT);
    assertTrue(
        "Only standalone ISO/GCM/RVZ",
        name.endsWith(".iso") || name.endsWith(".gcm") || name.endsWith(".rvz"));
    assertTrue(item.size > 0);
    // Check the existing task directory before complete(), which may otherwise create a directory.
    File directory = new File(store.root, item.id).getCanonicalFile();
    assertTrue(directory.isDirectory());
    assertEquals(store.root.getCanonicalFile(), directory.getParentFile());
    File file = store.complete(item);
    assertEquals(directory, file.getCanonicalFile().getParentFile());
    assertTrue(file.isFile());
    assertEquals(item.size, file.length());
    Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".games", file);
    assertNull(uri.getQuery());
    String[] projection = {DocumentsContract.Document.COLUMN_MIME_TYPE, OpenableColumns.SIZE};
    try (Cursor cursor = context.getContentResolver().query(uri, projection, null, null, null)) {
      assertNotNull(cursor);
      assertArrayEquals(projection, cursor.getColumnNames());
      assertTrue(cursor.moveToFirst());
      assertEquals(context.getContentResolver().getType(uri), cursor.getString(0));
      assertEquals(item.size, cursor.getLong(1));
    }
    try (ParcelFileDescriptor descriptor =
        context.getContentResolver().openFileDescriptor(uri, "r")) {
      assertNotNull(descriptor);
      assertEquals(item.size, descriptor.getStatSize());
    }
    return uri;
  }
}
