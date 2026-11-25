package io.retroone.android;

import static org.junit.Assert.*;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.zip.*;
import org.json.*;
import org.junit.Test;

public class FbneoArchiveTest {
  private static final byte[] CONTENT =
      "RetroOne original CRC fixture".getBytes(StandardCharsets.UTF_8);

  private File archive(String entry) throws Exception {
    File file = File.createTempFile("retroone-original-", ".zip");
    try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(file))) {
      out.putNextEntry(new ZipEntry(entry));
      out.write(CONTENT);
      out.closeEntry();
    }
    return file;
  }

  private JSONObject index(String name, String dependency) throws Exception {
    CRC32 crc = new CRC32();
    crc.update(CONTENT);
    String member =
        String.format(java.util.Locale.ROOT, "%08x:%d\n", crc.getValue(), CONTENT.length);
    String signature =
        FbneoArchive.hex(
            MessageDigest.getInstance("SHA-256").digest(member.getBytes(StandardCharsets.UTF_8)));
    return new JSONObject()
        .put("source_sha256", FbneoArchive.DAT)
        .put(
            "sets",
            new JSONObject()
                .put(signature, new JSONArray().put(new JSONArray().put(name).put(dependency))));
  }

  @Test
  public void contentIdentityDoesNotDependOnDownloadedHashName() throws Exception {
    File file = archive("original.program");
    byte[] before = java.nio.file.Files.readAllBytes(file.toPath());
    try {
      assertEquals("own_fixture", FbneoArchive.identify(file, index("own_fixture", "")));
      assertArrayEquals(before, java.nio.file.Files.readAllBytes(file.toPath()));
      assertEquals("", LaunchPolicy.formatReason("fbneo", "hash.ZIP"));
      assertFalse(LaunchPolicy.formatReason("fbneo", "hash.7z").isEmpty());
      assertEquals("", LaunchPolicy.formatReason("switch", "hash.XCI"));
      assertFalse(LaunchPolicy.formatReason("switch", "hash.nsz").isEmpty());
    } finally {
      file.delete();
    }
  }

  @Test
  public void unknownModifiedSetsAndMissingDependenciesAreNotSilentlyRenamed() throws Exception {
    File file = archive("original.program");
    try {
      try {
        FbneoArchive.identify(file, index("own_fixture", "parent_set"));
        fail();
      } catch (IOException e) {
        assertTrue(e.getMessage().contains("parent_set"));
      }
      JSONObject empty =
          new JSONObject().put("source_sha256", FbneoArchive.DAT).put("sets", new JSONObject());
      try {
        FbneoArchive.identify(file, empty);
        fail();
      } catch (IOException e) {
        assertTrue(e.getMessage().contains("MOD"));
      }
      try {
        FbneoArchive.identify(file, index("../escape", ""));
        fail();
      } catch (IOException e) {
        assertTrue(e.getMessage().contains("标识"));
      }
      JSONObject ambiguous = index("own_fixture", "");
      JSONObject sets = ambiguous.getJSONObject("sets");
      sets.getJSONArray(sets.keys().next()).put(new JSONArray().put("other").put(""));
      try {
        FbneoArchive.identify(file, ambiguous);
        fail();
      } catch (IOException e) {
        assertTrue(e.getMessage().contains("多个"));
      }
    } finally {
      file.delete();
    }
  }

  @Test
  public void nestedArchivesAndWrongIndexRevisionFailClosed() throws Exception {
    File nested = archive("../original.program");
    File flat = archive("original.program");
    try {
      try {
        FbneoArchive.identify(nested, index("own_fixture", ""));
        fail();
      } catch (IOException e) {
        assertTrue(e.getMessage().contains("嵌套"));
      }
      try {
        FbneoArchive.identify(flat, index("own_fixture", "").put("source_sha256", "unreviewed"));
        fail();
      } catch (IOException e) {
        assertTrue(e.getMessage().contains("索引版本"));
      }
    } finally {
      nested.delete();
      flat.delete();
    }
  }
}
