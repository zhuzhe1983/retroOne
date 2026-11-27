package io.retroone.android;

import static org.junit.Assert.*;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ProviderInfo;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import androidx.core.content.FileProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Synthetic files only; never opens a downloaded game or invokes an emulator. */
@RunWith(AndroidJUnit4.class)
public class GamesFileProviderTest {
  private static final String MIME = DocumentsContract.Document.COLUMN_MIME_TYPE;
  private static final String NAME = OpenableColumns.DISPLAY_NAME;
  private static final String SIZE = OpenableColumns.SIZE;
  private static final byte[] CONTENT =
      "RetroOne metadata fixture\n".getBytes(StandardCharsets.UTF_8);
  private Context context;
  private String authority, recipientPackage;
  private int recipientUid;
  private File directory, file;
  private Uri uri;
  private final List<File> owned = new ArrayList<>();

  @Before
  public void setUp() throws Exception {
    context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    Context testPackage = InstrumentationRegistry.getInstrumentation().getContext();
    recipientPackage = testPackage.getPackageName();
    recipientUid = testPackage.getApplicationInfo().uid;
    authority = context.getPackageName() + ".games";
    directory = new File(context.getCacheDir(), "emulator-probe/provider-" + UUID.randomUUID());
    assertTrue(directory.mkdirs());
    file = fixture(directory, "metadata.iso");
    uri = FileProvider.getUriForFile(context, authority, file);
  }

  @After
  public void cleanUp() {
    if (uri != null)
      context.revokeUriPermission(recipientPackage, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
    for (File entry : owned) assertTrue("Cannot remove test fixture", entry.delete());
    if (directory != null) assertTrue(directory.delete());
  }

  @Test
  public void dolphinProjectionHasMimeAndNumericSizeAtItsFixedIndexes() {
    try (Cursor cursor = query(uri, new String[] {MIME, SIZE})) {
      assertArrayEquals(new String[] {MIME, SIZE}, cursor.getColumnNames());
      assertEquals(1, cursor.getCount());
      assertTrue(cursor.moveToFirst());
      assertEquals(context.getContentResolver().getType(uri), cursor.getString(0));
      assertNotEquals(DocumentsContract.Document.MIME_TYPE_DIR, cursor.getString(0));
      assertEquals(Cursor.FIELD_TYPE_INTEGER, cursor.getType(1));
      assertEquals(file.length(), cursor.getLong(1));
    }
  }

  @Test
  public void defaultAndExistingPspEdenProjectionsMatchOriginalFileProvider() {
    ProviderInfo info =
        context.getPackageManager().resolveContentProvider(authority, PackageManager.GET_META_DATA);
    assertNotNull(info);
    assertEquals(GamesFileProvider.class.getName(), info.name);
    assertFalse(info.exported);
    assertTrue(info.grantUriPermissions);
    FileProvider baseline = new FileProvider();
    baseline.attachInfo(context, info);
    for (String[] projection :
        new String[][] {null, {NAME}, {SIZE}, {SIZE, NAME}, {NAME, SIZE}, {"_data", NAME}}) {
      try (Cursor expected = baseline.query(uri, projection, null, null, null);
          Cursor actual = query(uri, projection)) {
        assertArrayEquals(expected.getColumnNames(), actual.getColumnNames());
        assertEquals(expected.getCount(), actual.getCount());
        assertTrue(expected.moveToFirst());
        assertTrue(actual.moveToFirst());
        for (int i = 0; i < expected.getColumnCount(); i++) {
          assertEquals(expected.getType(i), actual.getType(i));
          assertEquals(expected.getString(i), actual.getString(i));
        }
      }
    }
  }

  @Test
  public void existingGamesRootKeepsItsStableUriAndDefaultProjection() throws Exception {
    File games = new File(context.getFilesDir(), "games");
    assertTrue(games.isDirectory());
    File game = fixture(games, "provider-" + UUID.randomUUID() + ".iso");
    Uri gameUri = FileProvider.getUriForFile(context, authority, game);
    assertEquals("content", gameUri.getScheme());
    assertEquals(authority, gameUri.getAuthority());
    assertEquals("/games/" + game.getName(), gameUri.getPath());
    assertNull(gameUri.getQuery());
    try (Cursor cursor = query(gameUri, null)) {
      assertArrayEquals(new String[] {NAME, SIZE}, cursor.getColumnNames());
      assertTrue(cursor.moveToFirst());
      assertEquals(game.getName(), cursor.getString(0));
      assertEquals(CONTENT.length, cursor.getLong(1));
    }
  }

  @Test
  public void mimeProjectionPreservesOrderDuplicatesAndDisplayNameOverride() {
    Uri named =
        FileProvider.getUriForFile(context, authority, file, "Friendly PSP or Eden name.iso");
    String[] projection = {SIZE, MIME, NAME, MIME, SIZE};
    try (Cursor cursor = query(named, projection)) {
      assertArrayEquals(projection, cursor.getColumnNames());
      assertTrue(cursor.moveToFirst());
      assertEquals(file.length(), cursor.getLong(0));
      assertEquals(context.getContentResolver().getType(uri), cursor.getString(1));
      assertEquals("Friendly PSP or Eden name.iso", cursor.getString(2));
      assertEquals(cursor.getString(1), cursor.getString(3));
      assertEquals(file.length(), cursor.getLong(4));
    }
    try (Cursor cursor = query(uri, new String[] {MIME})) {
      assertArrayEquals(new String[] {MIME}, cursor.getColumnNames());
      assertTrue(cursor.moveToFirst());
      assertEquals(context.getContentResolver().getType(uri), cursor.getString(0));
    }
  }

  @Test
  public void arbitraryColumnsAreOmittedWithoutExposingPathsOrOtherFields() {
    try (Cursor cursor =
        query(uri, new String[] {"_data", MIME, "document_id", SIZE, "secret", NAME})) {
      assertArrayEquals(new String[] {MIME, SIZE, NAME}, cursor.getColumnNames());
      assertTrue(cursor.moveToFirst());
      assertEquals(file.getName(), cursor.getString(2));
    }
    try (Cursor cursor = query(uri, new String[] {"_data", "document_id", "secret"})) {
      assertEquals(0, cursor.getColumnCount());
    }
  }

  @Test
  public void mimeOnlyStillRejectsTraversalUnknownRootsAndSymlinkEscape() throws Exception {
    File outside = fixture(context.getCacheDir(), "provider-outside-" + UUID.randomUUID() + ".iso");
    Uri traversal =
        uri.buildUpon().encodedPath("/emulator-probe/%2E%2E/" + outside.getName()).build();
    assertQueryRejected(traversal);
    assertQueryRejected(uri.buildUpon().encodedPath("/unknown-root/metadata.iso").build());
    File link = new File(directory, "outside.iso");
    Os.symlink(outside.getAbsolutePath(), link.getAbsolutePath());
    owned.add(link);
    Uri escaped =
        uri.buildUpon().path("/emulator-probe/" + directory.getName() + "/outside.iso").build();
    assertQueryRejected(escaped);
    try {
      FileProvider.getUriForFile(context, authority, outside);
      fail("An unconfigured cache path must not be exported");
    } catch (IllegalArgumentException expected) {
      // Only games/ and emulator-probe/ are configured roots.
    }
  }

  @Test
  public void exactReadGrantDoesNotGrantWriteSiblingOrParentAccess() throws Exception {
    // Inspect the test APK's separate package UID, not this process's target-app Binder identity.
    // This exercises system grant state without launching another app or executing shell commands.
    int otherUid = recipientUid;
    assertNotEquals(context.getApplicationInfo().uid, otherUid);
    Uri sibling = FileProvider.getUriForFile(context, authority, fixture(directory, "sibling.iso"));
    Uri parent = uri.buildUpon().path("/emulator-probe/" + directory.getName()).build();
    assertPermission(uri, otherUid, Intent.FLAG_GRANT_READ_URI_PERMISSION, false);
    try {
      context.grantUriPermission(recipientPackage, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
      assertPermission(uri, otherUid, Intent.FLAG_GRANT_READ_URI_PERMISSION, true);
      assertPermission(uri, otherUid, Intent.FLAG_GRANT_WRITE_URI_PERMISSION, false);
      assertPermission(sibling, otherUid, Intent.FLAG_GRANT_READ_URI_PERMISSION, false);
      assertPermission(parent, otherUid, Intent.FLAG_GRANT_READ_URI_PERMISSION, false);
      try (ParcelFileDescriptor descriptor =
          context.getContentResolver().openFileDescriptor(uri, "r")) {
        assertNotNull(descriptor);
        assertEquals(file.length(), descriptor.getStatSize());
        try {
          Os.write(descriptor.getFileDescriptor(), new byte[] {0}, 0, 1);
          fail("A read descriptor must reject writes");
        } catch (ErrnoException expected) {
          assertEquals(OsConstants.EBADF, expected.errno);
        }
      }
      assertArrayEquals(CONTENT, Files.readAllBytes(file.toPath()));
    } finally {
      context.revokeUriPermission(recipientPackage, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
    }
    assertPermission(uri, otherUid, Intent.FLAG_GRANT_READ_URI_PERMISSION, false);
  }

  private File fixture(File parent, String name) throws Exception {
    File result = new File(parent, name);
    Files.write(result.toPath(), CONTENT);
    owned.add(result);
    return result;
  }

  private Cursor query(Uri target, String[] projection) {
    Cursor result = context.getContentResolver().query(target, projection, null, null, null);
    assertNotNull(result);
    return result;
  }

  private void assertQueryRejected(Uri target) {
    try (Cursor ignored = query(target, new String[] {MIME})) {
      fail("Out-of-root URI was accepted");
    } catch (SecurityException | IllegalArgumentException expected) {
      // FileProvider performs canonical path validation even without a size/name projection.
    }
  }

  private void assertPermission(Uri target, int uid, int flag, boolean granted) {
    assertEquals(
        granted ? PackageManager.PERMISSION_GRANTED : PackageManager.PERMISSION_DENIED,
        context.checkUriPermission(target, -1, uid, flag));
  }
}
