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

  private File withDirectories(int count) throws Exception {
    File file = File.createTempFile("retroone-dirs-", ".zip");
    try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(file))) {
      for (int i = 0; i < count; i++) {
        out.putNextEntry(new ZipEntry("dir" + i + "/"));
        out.closeEntry();
      }
      out.putNextEntry(new ZipEntry("original.program"));
      out.write(CONTENT);
      out.closeEntry();
    }
    return file;
  }

  private void rejectUnchanged(File file, String reason) throws Exception {
    byte[] before = java.nio.file.Files.readAllBytes(file.toPath());
    try {
      FbneoArchive.identify(file, index("own_fixture", ""));
      fail(reason);
    } catch (IOException expected) {
      assertTrue(expected.getMessage(), expected.getMessage().contains(reason));
    }
    assertArrayEquals(before, java.nio.file.Files.readAllBytes(file.toPath()));
  }

  private void patch(File file, long offset, long value, int bytes) throws Exception {
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.seek(offset);
      for (int i = 0; i < bytes; i++) {
        out.write((int) (value & 255));
        value >>>= 8;
      }
    }
  }

  @Test
  public void physicalArchiveLimitPrecedesZipParsing() throws Exception {
    File file = File.createTempFile("retroone-sparse-", ".zip");
    try {
      try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
        out.setLength(FbneoArchive.MAX_ARCHIVE_BYTES + 1);
      }
      try {
        FbneoArchive.identify(file, index("own_fixture", ""));
        fail();
      } catch (IOException expected) {
        assertTrue(expected.getMessage().contains("512 MiB"));
      }
      assertEquals(FbneoArchive.MAX_ARCHIVE_BYTES + 1, file.length());
    } finally {
      file.delete();
    }
  }

  @Test
  public void directoriesCountEvenThoughTheyContainNoRomBytes() throws Exception {
    File allowed = withDirectories(FbneoArchive.MAX_ENTRIES - 1);
    File excess = withDirectories(FbneoArchive.MAX_ENTRIES);
    try {
      assertEquals("own_fixture", FbneoArchive.identify(allowed, index("own_fixture", "")));
      rejectUnchanged(excess, "条目数量");
    } finally {
      allowed.delete();
      excess.delete();
    }
  }

  @Test
  public void forgedSmallEocdCountCannotHideExtraCentralRecords() throws Exception {
    File file = withDirectories(2);
    try {
      patch(file, file.length() - 22 + 8, 1, 2);
      patch(file, file.length() - 22 + 10, 1, 2);
      rejectUnchanged(file, "实际条目");
    } finally {
      file.delete();
    }
  }

  @Test
  public void oversizedDirectoryAndZip64AndMultivolumeFailBeforeZipFile() throws Exception {
    for (int mode = 0; mode < 3; mode++) {
      File file = archive("original.program");
      try {
        long end = file.length() - 22;
        if (mode == 0) patch(file, end + 12, FbneoArchive.MAX_DIRECTORY_BYTES + 1, 4);
        if (mode == 1) patch(file, end + 10, 65535, 2);
        if (mode == 2) patch(file, end + 4, 1, 2);
        rejectUnchanged(file, mode == 0 ? "中央目录" : "ZIP64");
      } finally {
        file.delete();
      }
    }
  }

  @Test
  public void prefixAndTruncatedEndCannotBeHiddenBehindValidMembers() throws Exception {
    File file = archive("original.program");
    try {
      patch(file, 0, 0, 4);
      rejectUnchanged(file, "标准 ZIP");
    } finally {
      file.delete();
    }
    file = archive("original.program");
    try {
      try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
        out.setLength(file.length() - 1);
      }
      rejectUnchanged(file, "结束记录");
    } finally {
      file.delete();
    }
  }
}
