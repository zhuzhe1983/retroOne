package io.retroone.android;

import static org.junit.Assert.*;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import android.util.Log;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Default tests never dispatch an emulator. Real-file checks require one exact ready GC task UUID
 * and one of dolphinAdapterProbe/Failure/Launch/Revoke/Lifecycle=true. Launch, Revoke and Lifecycle require
 * dolphinExited=true. No game is automatically confirmed compatible and no old grant is pre-cleared.
 */
@RunWith(AndroidJUnit4.class)
public class DolphinAdapterDeviceTest {
  private static final String[] MODES = {
    "dolphinAdapterProbe", "dolphinAdapterFailure", "dolphinAdapterLaunch", "dolphinAdapterRevoke",
    "dolphinAdapterLifecycle"
  };
  private static final String EXIT_MARKER = "retroone-dolphin-exit-ack.txt";
  private static final long EXIT_TIMEOUT_MS = 180000;
  private static final int READ = Intent.FLAG_GRANT_READ_URI_PERMISSION;
  private static final int WRITE = Intent.FLAG_GRANT_WRITE_URI_PERMISSION;
  private static final String UUID_PATTERN =
      "[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}";

  @Test
  public void openIntentHasNoRomOrUriGrant() {
    Dolphin adapter = new Dolphin(context());
    Map<String, ?> before = dolphinState();
    assertOpenIntent(adapter.openIntent());
    assertTrue("Intent construction changed adapter state", before.equals(dolphinState()));
  }

  @Test
  public void coldIntentIsBoundedToOneCanonicalFileUri() throws Exception {
    Dolphin adapter = new Dolphin(context());
    Map<String, ?> before = dolphinState();
    String authority = context().getPackageName() + ".games";
    String suffix = "/games/11111111-2222-4333-8444-555555555555/fixture.rvz";
    Uri synthetic = Uri.parse("content://" + authority + suffix);
    // Intent construction only: this URI is not resolved, granted, or launched.
    assertColdIntent(adapter.coldIntent(synthetic), synthetic);
    Uri[] rejected = {
      null,
      Uri.parse("file:///games/fixture.rvz"),
      Uri.parse("https://example.invalid/fixture.rvz"),
      Uri.parse("content://another.provider" + suffix),
      Uri.parse("content://user@" + authority + suffix),
      Uri.parse("content://" + authority + "/games"),
      Uri.parse("content://" + authority + "/games/11111111-2222-4333-8444-555555555555"),
      Uri.parse("content://" + authority + "/emulator-probe/fixture.rvz"),
      Uri.parse("content://" + authority + suffix + "/child"),
      Uri.parse("content://" + authority + suffix + "?displayName=other.rvz"),
      Uri.parse("content://" + authority + suffix + "#fragment"),
      Uri.parse("content://" + authority + suffix.replace("fixture.rvz", "../fixture.rvz")),
      Uri.parse("content://" + authority + suffix.replace("fixture.rvz", "%2e%2e")),
      Uri.parse("content://" + authority + suffix.replace("fixture.rvz", "%66ixture.rvz")),
      Uri.parse("content://" + authority + suffix.replace("fixture.rvz", "nested%2Ffixture.rvz"))
    };
    for (Uri uri : rejected) {
      try {
        adapter.coldIntent(uri);
        fail("An unbounded or noncanonical URI was accepted");
      } catch (IOException expected) {
        // No external provider, directory grant, alternate encoding, query, or fragment.
      }
    }
    assertTrue("Rejected URI construction changed adapter state", before.equals(dolphinState()));
  }

  @Test
  public void invalidConfirmationCannotChangeExistingState() throws Exception {
    Dolphin adapter = new Dolphin(context());
    Map<String, ?> before = dolphinState();
    rejectConfirmation(adapter, null);
    assertTrue("Invalid confirmation changed adapter state", before.equals(dolphinState()));
  }

  @Test
  public void explicitReadyTaskPreflightPreservesState() throws Exception {
    optIn("dolphinAdapterProbe", false);
    DownloadStore.Item item = task();
    Dolphin adapter = new Dolphin(context());
    assertIdle(adapter);
    Preserved before = new Preserved(item);
    Map<String, ?> preferences = dolphinState();
    Dolphin.Prepared prepared = adapter.prepare(item);
    int uid = targetUid(adapter);
    assertNoPriorGrant(prepared.uri, uid);
    evidence("preflight", prepared.uri, uid);
    assertPrepared(adapter, item, prepared);
    assertColdIntent(adapter.coldIntent(prepared.uri), prepared.uri);
    rejectConfirmation(adapter, item);
    final boolean[] opened = {false};
    adapter.open(intent -> {
      assertOpenIntent(intent);
      opened[0] = true;
      // The injected callback deliberately does not start an Activity.
    });
    assertTrue(opened[0]);
    assertNoPriorGrant(prepared.uri, uid);
    assertTrue("Preflight must not create a trial or confirmation", preferences.equals(dolphinState()));
    before.assertUnchanged(adapter, item);
  }

  @Test
  public void explicitInjectedStartFailureRollsBackOnlyItsNewReadGrant() throws Exception {
    optIn("dolphinAdapterFailure", false);
    DownloadStore.Item item = task();
    Dolphin adapter = new Dolphin(context());
    assertIdle(adapter);
    Preserved before = new Preserved(item);
    Map<String, ?> preferences = dolphinState();
    Dolphin.Prepared prepared = adapter.prepare(item);
    int uid = targetUid(adapter);
    assertNoPriorGrant(prepared.uri, uid);
    evidence("failure-before", prepared.uri, uid);
    IOException injected = new IOException("Injected Start failure; no Activity was dispatched");
    final boolean[] entered = {false};
    final AssertionError[] callbackFailure = {null};
    try {
      adapter.launch(item, prepared, intent -> {
        entered[0] = true;
        try {
          assertColdIntent(intent, prepared.uri);
          assertActive(adapter, item, prepared);
          evidence("failure-in-callback", intent.getData(), uid);
          assertReadOnlyGrant(prepared.uri, uid);
          assertEquals(
              "Dispatch must not create compatibility confirmation",
              before.verified,
              adapter.verified(item));
        } catch (AssertionError error) {
          // Let the adapter's Exception rollback execute even if a test assertion failed.
          callbackFailure[0] = error;
        }
        throw injected;
      });
      fail("Injected Start failure was swallowed");
    } catch (IOException expected) {
      assertSame("Launch failed before the injected callback", injected, expected);
    } finally {
      evidence("failure-after", prepared.uri, uid);
    }
    assertTrue("Start callback was not reached", entered[0]);
    assertIdle(adapter);
    assertNoPriorGrant(prepared.uri, uid);
    assertTrue("Failure must preserve previous confirmations", preferences.equals(dolphinState()));
    before.assertUnchanged(adapter, item);
    if (callbackFailure[0] != null)
      throw new AssertionError("Intent/grant assertions failed before rollback", callbackFailure[0]);
    // No fallback cleanup: unexpected retained state remains visible for diagnosis.
  }

  @Test
  public void explicitLaunchRetainsReadGrantWithoutConfirmingCompatibility() throws Exception {
    optIn("dolphinAdapterLaunch", true);
    DownloadStore.Item item = task();
    Dolphin adapter = new Dolphin(context());
    assertIdle(adapter);
    Preserved before = new Preserved(item);
    Map<String, ?> confirmations = confirmations();
    Dolphin.Prepared prepared = adapter.prepare(item);
    int uid = targetUid(adapter);
    assertNoPriorGrant(prepared.uri, uid);
    assertPrepared(adapter, item, prepared);
    evidence("launch-before", prepared.uri, uid);
    final boolean[] dispatched = {false};
    try {
      adapter.launch(item, prepared, intent -> {
        try {
          assertColdIntent(intent, prepared.uri);
          assertActive(adapter, item, prepared);
          evidence("launch-in-callback", intent.getData(), uid);
          assertReadOnlyGrant(prepared.uri, uid);
        } catch (AssertionError error) {
          // Before dispatch, force the adapter's normal failure rollback, not AssertionError bypass.
          throw new IOException("Pre-dispatch intent/grant assertion failed", error);
        }
        context().startActivity(intent);
        dispatched[0] = true;
      });
      assertTrue(dispatched[0]);
      evidence("launch-dispatched", prepared.uri, uid);
      Thread.sleep(15000);
      assertActive(adapter, item, prepared);
      assertReadOnlyGrant(prepared.uri, uid);
      rejectConfirmation(adapter, item);
      assertTrue("A launch cannot confirm gameplay compatibility", confirmations.equals(confirmations()));
      before.assertUnchanged(adapter, item);
      Log.i("RetroOneTest", "Dolphin dispatch only: inspect real frame/input/audio separately;"
          + " no compatibility confirmation saved. Save and exit before the separate revoke test.");
    } finally {
      evidence(dispatched[0] ? "launch-retained-after-test" : "launch-not-dispatched", prepared.uri, uid);
      // After actual dispatch, even a failed observation must not revoke a game's lazy-read grant.
      // Only explicit post-exit confirmation in the separate test releases it.
    }
  }

  @Test
  public void explicitPostExitRevokesOnlyTheMatchingActiveTask() throws Exception {
    optIn("dolphinAdapterRevoke", true);
    DownloadStore.Item item = task();
    Dolphin adapter = new Dolphin(context());
    Preserved before = new Preserved(item);
    Map<String, ?> confirmations = confirmations();
    assertEquals("Do not release another task's active grant", item.id, adapter.activeId());
    Dolphin.Prepared prepared = adapter.prepare(item);
    assertEquals("The saved grant must match this exact file", prepared.uri.toString(), adapter.activeUri());
    int uid = targetUid(adapter);
    evidence("revoke-before-confirmed-exit", prepared.uri, uid);
    // Starting a separate instrumentation force-stops its target and can clear its URI grants.
    // Do not turn already-denied -> denied into evidence of an observed READ revocation.
    int beforeRead = permission(prepared.uri, uid, READ);
    assertEquals(PackageManager.PERMISSION_DENIED, permission(prepared.uri, uid, WRITE));
    if (beforeRead == PackageManager.PERMISSION_GRANTED) assertReadOnlyGrant(prepared.uri, uid);
    else assertNoPriorGrant(prepared.uri, uid);
    adapter.releaseAfterConfirmedExit();
    evidence("revoke-after-confirmed-exit", prepared.uri, uid);
    assertIdle(adapter);
    assertNoPriorGrant(prepared.uri, uid);
    assertTrue("Releasing a grant must preserve confirmations", confirmations.equals(confirmations()));
    before.assertUnchanged(adapter, item);
    Log.i("RetroOneTest", beforeRead == PackageManager.PERMISSION_GRANTED
        ? "Dolphin cleanup observed READ granted -> denied in this invocation."
        : "Dolphin cleanup found READ already denied: verified stale-record cleanup only;"
            + " this is NOT evidence of a granted -> denied transition.");
  }

  @Test
  public void explicitLifecycleWaitsForNativeExitBeforeRevokingInSameInvocation() throws Exception {
    optIn("dolphinAdapterLifecycle", true);
    DownloadStore.Item item = task();
    Dolphin adapter = new Dolphin(context());
    assertIdle(adapter);
    File marker = new File(context().getCacheDir().getCanonicalFile(), EXIT_MARKER);
    assertTrue("Exit marker must be directly inside the app cache",
        marker.getAbsoluteFile().equals(marker.getCanonicalFile()));
    assertFalse("Pre-existing exit marker is never reused or automatically removed",
        Files.exists(marker.toPath(), LinkOption.NOFOLLOW_LINKS));
    Preserved before = new Preserved(item);
    Map<String, ?> confirmations = confirmations();
    Dolphin.Prepared prepared = adapter.prepare(item);
    int uid = targetUid(adapter);
    assertNoPriorGrant(prepared.uri, uid);
    assertPrepared(adapter, item, prepared);
    String nonce = UUID.randomUUID().toString();
    final boolean[] dispatched = {false};
    evidence("lifecycle-before", prepared.uri, uid);
    try {
      adapter.launch(item, prepared, intent -> {
        try {
          assertFalse("Exit marker appeared before dispatch",
              Files.exists(marker.toPath(), LinkOption.NOFOLLOW_LINKS));
          assertColdIntent(intent, prepared.uri);
          assertActive(adapter, item, prepared);
          assertReadOnlyGrant(prepared.uri, uid);
        } catch (AssertionError error) {
          throw new IOException("Lifecycle pre-dispatch assertions failed", error);
        }
        context().startActivity(intent);
        dispatched[0] = true;
      });
      assertTrue(dispatched[0]);
      assertReadOnlyGrant(prepared.uri, uid);
      evidence("lifecycle-dispatched", prepared.uri, uid);
      Log.i("RetroOneTest", "DolphinLifecycle waiting for explicit native-exit acknowledgement:"
          + " task=" + item.id + " marker=cache/" + EXIT_MARKER + " nonce=" + nonce
          + " timeoutSeconds=180. Owner must save/Exit Emulation and verify no EmulationActivity"
          + " remains before writing this nonce. No second instrumentation may be started.");
      awaitExitAcknowledgement(marker, nonce);
      // The marker is the device owner's explicit observation, not an automatic exit detector.
      assertActive(adapter, item, prepared);
      assertReadOnlyGrant(prepared.uri, uid);
      evidence("lifecycle-acknowledged-before-release", prepared.uri, uid);
      adapter.releaseAfterConfirmedExit();
      evidence("lifecycle-after-release", prepared.uri, uid);
      assertIdle(adapter);
      assertNoPriorGrant(prepared.uri, uid);
      assertTrue("Lifecycle must not mark gameplay compatibility", confirmations.equals(confirmations()));
      before.assertUnchanged(adapter, item);
      assertTrue("Exit acknowledgement changed before exact cleanup", exitAcknowledged(marker, nonce));
      Files.delete(marker.toPath());
      Log.i("RetroOneTest", "DolphinLifecycle PASS: observed READ granted before owner-confirmed"
          + " native exit and granted -> denied at explicit release within one invocation."
          + " No gameplay confirmation was written.");
    } finally {
      evidence(dispatched[0] ? "lifecycle-final-observation" : "lifecycle-not-dispatched",
          prepared.uri, uid);
      // No timeout/failure fallback revocation or marker removal. Preserve the active record and
      // evidence. Android may nevertheless clear grants when default instrumentation force-stops
      // the target after the run; this cannot promise OS-grant persistence after test termination.
    }
  }

  private void awaitExitAcknowledgement(File marker, String nonce) throws Exception {
    long deadline = SystemClock.elapsedRealtime() + EXIT_TIMEOUT_MS;
    while (SystemClock.elapsedRealtime() < deadline) {
      if (Thread.currentThread().isInterrupted()) throw new IOException("Lifecycle observation interrupted");
      if (exitAcknowledged(marker, nonce)) return;
      Thread.sleep(250);
    }
    throw new IOException("Timed out waiting for explicit native exit; no adapter release was called."
        + " Active state/marker retained. Framework test shutdown may clear transient URI grants.");
  }

  private boolean exitAcknowledged(File marker, String nonce) throws IOException {
    if (!Files.exists(marker.toPath(), LinkOption.NOFOLLOW_LINKS)) return false;
    if (Files.isSymbolicLink(marker.toPath()) || !marker.isFile()
        || !marker.getAbsoluteFile().equals(marker.getCanonicalFile()) || marker.length() > 128)
      throw new IOException("Exit acknowledgement must be one bounded ordinary cache file");
    // An empty file can be the writer's just-opened destination; retry without accepting it.
    byte[] bytes;
    try (java.io.InputStream in = Files.newInputStream(marker.toPath())) {
      bytes = new byte[129];
      int count = 0;
      while (count < bytes.length) {
        int n = in.read(bytes, count, bytes.length - count);
        if (n < 0) break;
        count += n;
      }
      if (count > 128) throw new IOException("Exit acknowledgement exceeds 128 bytes");
      if (count == 0) return false;
      String value = new String(bytes, 0, count, StandardCharsets.UTF_8).trim();
      if (!nonce.equals(value)) throw new IOException("Exit acknowledgement nonce does not match this run");
    }
    return true;
  }

  private Context context() {
    return InstrumentationRegistry.getInstrumentation().getTargetContext();
  }

  private void optIn(String selected, boolean exited) {
    Bundle arguments = InstrumentationRegistry.getArguments();
    Assume.assumeTrue(
        "Explicit opt-in required: " + selected, "true".equals(arguments.getString(selected)));
    for (String mode : MODES)
      if (!mode.equals(selected))
        assertFalse(
            "Run preparation, failure, launch and revoke separately",
            "true".equals(arguments.getString(mode)));
    assertFalse(
        "Do not mix the legacy URI probe with this adapter test",
        "true".equals(arguments.getString("dolphinUriProbe")));
    assertFalse(
        "Do not mix the legacy URI revocation with this adapter test",
        "true".equals(arguments.getString("dolphinUriRevoke")));
    if (exited)
      assertEquals(
          "Explicitly confirm the earlier native game was saved and exited",
          "true",
          arguments.getString("dolphinExited"));
  }

  private DownloadStore.Item task() throws Exception {
    String id = InstrumentationRegistry.getArguments().getString("dolphinTaskId", "");
    assertTrue("One exact task UUID is required; no automatic game selection", id.matches(UUID_PATTERN));
    DownloadStore store = RetroApplication.from(context()).downloads;
    DownloadStore.Item item = store.get(id);
    assertNotNull("The probe never downloads a missing task", item);
    assertEquals(DownloadStore.READY, item.state);
    assertTrue(
        "This adapter test is GC-only",
        "gamecube".equals(item.platform) || "gc".equals(item.platform));
    File directory = new File(store.root, item.id);
    assertTrue("The existing task directory must be present", directory.isDirectory());
    assertTrue("Task directory must remain under the games root",
        directory.getCanonicalFile().equals(new File(store.root.getCanonicalFile(), item.id)));
    return item;
  }

  private void assertIdle(Dolphin adapter) {
    assertTrue(
        "Save/exit Dolphin and explicitly release its old grant first; this test never pre-clears",
        adapter.activeId().isEmpty());
    assertTrue("Do not overwrite an existing URI grant record", adapter.activeUri().isEmpty());
    assertTrue("Do not overwrite an existing trial record", adapter.trialFingerprint().isEmpty());
  }

  private int targetUid(Dolphin adapter) throws Exception {
    assertTrue("Install the official exported Dolphin MainActivity before opting in", adapter.installed());
    int uid = context().getPackageManager().getApplicationInfo(Dolphin.PACKAGE, 0).uid;
    assertNotEquals(context().getApplicationInfo().uid, uid);
    return uid;
  }

  private void assertPrepared(Dolphin adapter, DownloadStore.Item item, Dolphin.Prepared prepared)
      throws Exception {
    assertNotNull(prepared);
    assertTrue(prepared.fingerprint.matches("[a-f0-9]{64}"));
    assertEquals(adapter.fingerprint(item), prepared.fingerprint);
    assertEquals("content", prepared.uri.getScheme());
    assertEquals(context().getPackageName() + ".games", prepared.uri.getAuthority());
    assertEquals("/games/" + item.id + "/" + item.name, prepared.uri.getPath());
    assertNull(prepared.uri.getQuery());
    assertNull(prepared.uri.getFragment());
    String[] projection = {DocumentsContract.Document.COLUMN_MIME_TYPE, OpenableColumns.SIZE};
    try (Cursor cursor = context().getContentResolver().query(prepared.uri, projection, null, null, null)) {
      assertNotNull(cursor);
      assertArrayEquals(projection, cursor.getColumnNames());
      assertTrue(cursor.moveToFirst());
      assertEquals(context().getContentResolver().getType(prepared.uri), cursor.getString(0));
      assertNotEquals(DocumentsContract.Document.MIME_TYPE_DIR, cursor.getString(0));
      assertEquals(item.size, cursor.getLong(1));
    }
    try (ParcelFileDescriptor descriptor =
        context().getContentResolver().openFileDescriptor(prepared.uri, "r")) {
      assertNotNull(descriptor);
      assertEquals(item.size, descriptor.getStatSize());
    }
  }

  private void assertColdIntent(Intent intent, Uri uri) {
    assertEquals(Intent.ACTION_VIEW, intent.getAction());
    assertEquals(new ComponentName(Dolphin.PACKAGE, Dolphin.ACTIVITY), intent.getComponent());
    assertEquals(uri, intent.getData());
    assertEquals("application/octet-stream", intent.getType());
    assertNull(intent.getExtras());
    assertNotNull(intent.getClipData());
    assertEquals(1, intent.getClipData().getItemCount());
    assertEquals(uri, intent.getClipData().getItemAt(0).getUri());
    assertNull(intent.getClipData().getItemAt(0).getIntent());
    assertEquals(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | READ, intent.getFlags());
  }

  private void assertOpenIntent(Intent intent) {
    assertEquals(Intent.ACTION_MAIN, intent.getAction());
    assertEquals(new ComponentName(Dolphin.PACKAGE, Dolphin.ACTIVITY), intent.getComponent());
    assertNotNull(intent.getCategories());
    assertEquals(1, intent.getCategories().size());
    assertTrue(intent.hasCategory(Intent.CATEGORY_LAUNCHER));
    assertNull(intent.getData());
    assertNull(intent.getType());
    assertNull(intent.getClipData());
    assertNull(intent.getExtras());
    assertEquals(
        Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED, intent.getFlags());
  }

  private void assertActive(Dolphin adapter, DownloadStore.Item item, Dolphin.Prepared prepared) {
    assertEquals(item.id, adapter.activeId());
    assertEquals(prepared.uri.toString(), adapter.activeUri());
    assertEquals(prepared.fingerprint, adapter.trialFingerprint());
  }

  private void rejectConfirmation(Dolphin adapter, DownloadStore.Item item) throws Exception {
    try {
      adapter.confirm(item, "invalid-test-fingerprint");
      fail("An invalid or unobserved trial must never be confirmed");
    } catch (IOException expected) {
      // Never call confirm with the valid real-game fingerprint in an automated test.
    }
  }

  private void assertNoPriorGrant(Uri uri, int uid) {
    assertEquals(
        "A pre-existing READ grant must be preserved for explicit owner cleanup",
        PackageManager.PERMISSION_DENIED,
        permission(uri, uid, READ));
    assertEquals(
        "A pre-existing WRITE grant must never be cleared by this test",
        PackageManager.PERMISSION_DENIED,
        permission(uri, uid, WRITE));
  }

  private void assertReadOnlyGrant(Uri uri, int uid) {
    assertEquals(PackageManager.PERMISSION_GRANTED, permission(uri, uid, READ));
    assertEquals(PackageManager.PERMISSION_DENIED, permission(uri, uid, WRITE));
    Uri sibling = uri.buildUpon().path("/games/" + uri.getPathSegments().get(1) + "/not-granted.rvz").build();
    Uri parent = uri.buildUpon().path("/games/" + uri.getPathSegments().get(1)).build();
    assertEquals(PackageManager.PERMISSION_DENIED, permission(sibling, uid, READ));
    assertEquals(PackageManager.PERMISSION_DENIED, permission(parent, uid, READ));
  }

  private int permission(Uri uri, int uid, int flag) {
    return context().checkUriPermission(uri, -1, uid, flag);
  }

  private void evidence(String phase, Uri uri, int uid) {
    Log.i("RetroOneTest", "DolphinAdapter " + phase + " uri=" + uri + " targetUid=" + uid
        + " READ=" + permission(uri, uid, READ) + " WRITE=" + permission(uri, uid, WRITE));
  }

  private Map<String, ?> dolphinState() {
    return new HashMap<>(context().getSharedPreferences("dolphin", Context.MODE_PRIVATE).getAll());
  }

  private Map<String, ?> confirmations() {
    Map<String, Object> result = new HashMap<>();
    for (Map.Entry<String, ?> entry : dolphinState().entrySet())
      if (entry.getKey().startsWith("verified.")) result.put(entry.getKey(), entry.getValue());
    return result;
  }

  private final class Preserved {
    private final List<Object> record;
    private final Map<String, ?> connections;
    private final File file;
    private final long size, modified;
    private final boolean verified;

    Preserved(DownloadStore.Item item) throws Exception {
      record = record(item);
      connections =
          new HashMap<>(context().getSharedPreferences("connections", Context.MODE_PRIVATE).getAll());
      file = RetroApplication.from(context()).downloads.complete(item);
      size = file.length();
      modified = file.lastModified();
      verified = new Dolphin(context()).verified(item);
    }

    void assertUnchanged(Dolphin adapter, DownloadStore.Item item) {
      DownloadStore.Item after = RetroApplication.from(context()).downloads.get(item.id);
      assertNotNull(after);
      // Boolean comparisons intentionally avoid printing manifests, endpoints or credentials.
      assertTrue("Download record changed", record.equals(record(after)));
      assertTrue("Connection settings changed", connections.equals(
          context().getSharedPreferences("connections", Context.MODE_PRIVATE).getAll()));
      assertTrue(
          "Source file changed",
          file.isFile() && file.length() == size && file.lastModified() == modified);
      assertEquals("This test must not change compatibility confirmation", verified, adapter.verified(item));
    }
  }

  private static List<Object> record(DownloadStore.Item item) {
    return Arrays.asList(item.id, item.base, item.game, item.title, item.platform, item.state,
        item.manifest, item.name, item.message, item.metadata, item.http, item.bytes, item.size,
        item.created, item.lastPlayed);
  }
}
