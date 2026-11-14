package io.retroone.android;

import static org.junit.Assert.*;

import java.io.*;
import java.nio.file.Files;
import java.util.*;
import java.util.zip.ZipInputStream;
import org.json.*;
import org.junit.*;
import org.junit.rules.TemporaryFolder;

public class SaveLibraryTest {
  @Rule public TemporaryFolder temp = new TemporaryFolder();

  private JSONObject game() throws Exception {
    return new JSONObject()
        .put("id", "012345678901234567890123")
        .put("title", "Original save fixture");
  }

  private JSONObject snapshot() throws Exception {
    return new JSONObject()
        .put("revision", 7)
        .put("digest", "test")
        .put(
            "files",
            new JSONArray()
                .put(
                    new JSONObject()
                        .put("path", "UCAS00000/DATA.BIN")
                        .put("data", Base64.getEncoder().encodeToString(new byte[] {1, 2, 3}))));
  }

  @Test
  public void playersAndVersionsRemainSeparateAndRemovalTouchesOneBackupOnly() throws Exception {
    SaveLibrary library = new SaveLibrary(temp.getRoot());
    ServerConfig a =
        new ServerConfig("http://localhost/p/player1", true, "secret-not-for-metadata");
    ServerConfig b = new ServerConfig("http://localhost/p/player2", true, "");
    SaveLibrary.Entry first = library.create(a, "测试甲", game(), snapshot());
    SaveLibrary.Entry second = library.create(b, "测试乙", game(), snapshot());
    SaveLibrary.Entry another = library.create(a, "测试甲", game(), snapshot());
    assertEquals(3, library.all().size());
    assertNotEquals(first.file.getParent(), second.file.getParent());
    assertNotEquals(first.file, another.file);
    assertFalse(first.info.toString().contains("secret-not-for-metadata"));
    try (ZipInputStream zip = new ZipInputStream(new FileInputStream(first.file))) {
      assertEquals("SAVEDATA/UCAS00000/DATA.BIN", zip.getNextEntry().getName());
      assertArrayEquals(new byte[] {1, 2, 3}, zip.readAllBytes());
    }
    library.delete(first);
    assertFalse(first.file.exists());
    assertTrue(second.file.isFile());
    assertTrue(another.file.isFile());
    assertEquals(2, library.all().size());
  }

  @Test
  public void legacyBackupsRemainVisibleWithoutInventedPlayerIdentity() throws Exception {
    SaveLibrary library = new SaveLibrary(temp.getRoot());
    SaveLibrary.Entry entry =
        library.create(new ServerConfig("http://localhost", true, ""), "甲", game(), snapshot());
    assertTrue(new File(entry.file.getPath() + ".json").delete());
    SaveLibrary.Entry legacy = library.all().get(0);
    assertEquals("旧版存档备份", legacy.title());
    assertEquals("所属玩家待确认", legacy.player());
    assertTrue(library.safe(legacy.file));
  }

  @Test
  public void traversalAndSymlinkCannotDeleteUnrelatedFiles() throws Exception {
    SaveLibrary library = new SaveLibrary(temp.getRoot());
    File original = temp.newFile("unrelated.zip");
    assertFalse(library.safe(original));
    assertThrows(
        IOException.class, () -> library.delete(new SaveLibrary.Entry(original, new JSONObject())));
    SaveLibrary.Entry entry =
        library.create(new ServerConfig("http://localhost", true, ""), "甲", game(), snapshot());
    assertTrue(entry.file.delete());
    Files.createSymbolicLink(entry.file.toPath(), original.toPath());
    assertTrue(library.all().isEmpty());
    assertThrows(IOException.class, () -> library.delete(entry));
    assertTrue(original.isFile());
  }
}
