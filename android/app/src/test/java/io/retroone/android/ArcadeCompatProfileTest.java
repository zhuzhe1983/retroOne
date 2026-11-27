package io.retroone.android;

import static org.junit.Assert.*;

import java.io.*;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import java.util.zip.*;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class ArcadeCompatProfileTest {
  @Rule public TemporaryFolder files = new TemporaryFolder();

  private static String hash(byte[] bytes) throws Exception {
    return FbneoArchive.hex(MessageDigest.getInstance("SHA-256").digest(bytes));
  }

  private static long crc(byte[] bytes) {
    CRC32 crc = new CRC32();
    crc.update(bytes);
    return crc.getValue();
  }

  private File zip(Map<String, byte[]> data) throws Exception {
    File file = files.newFile();
    try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(file))) {
      for (Map.Entry<String, byte[]> item : data.entrySet()) {
        out.putNextEntry(new ZipEntry(item.getKey()));
        out.write(item.getValue());
        out.closeEntry();
      }
    }
    return file;
  }

  private ArcadeCompatProfile.Profile fixture(File source, Map<String, byte[]> data)
      throws Exception {
    List<ArcadeCompatProfile.Member> members = new ArrayList<>();
    int i = 0;
    for (Map.Entry<String, byte[]> item : data.entrySet()) {
      byte[] bytes = item.getValue();
      members.add(
          new ArcadeCompatProfile.Member(
              item.getKey(), "mapped" + i++ + ".rom", bytes.length, crc(bytes), hash(bytes)));
    }
    return new ArcadeCompatProfile.Profile(
        "original-test-v1",
        "fixture_libretro_android.so",
        "fixture",
        "Original fixture",
        hash(Files.readAllBytes(source.toPath())),
        members.toArray(new ArcadeCompatProfile.Member[0]));
  }

  // Original fixture bytes exercise the private transformation engine without admitting them
  // through the production whitelist and without bundling a commercial ROM in tests.
  private File prepareFixture(File source, File cache, ArcadeCompatProfile.Profile profile)
      throws Exception {
    Method prepare =
        ArcadeCompatProfile.class.getDeclaredMethod(
            "prepareProfile", File.class, File.class, ArcadeCompatProfile.Profile.class);
    prepare.setAccessible(true);
    try {
      return (File) prepare.invoke(null, source, cache, profile);
    } catch (InvocationTargetException failed) {
      if (failed.getCause() instanceof Exception) throw (Exception) failed.getCause();
      throw failed;
    }
  }

  private Map<String, byte[]> data(int count) {
    Map<String, byte[]> data = new LinkedHashMap<>();
    for (int i = 0; i < count; i++) {
      byte[] bytes = new byte[1024 + i];
      new Random(i).nextBytes(bytes);
      data.put("original" + i + ".bin", bytes);
    }
    return data;
  }

  private IOException reject(ThrowingCall call) throws Exception {
    try {
      call.run();
      fail("Expected closed admission");
      return null;
    } catch (IOException expected) {
      return expected;
    }
  }

  private interface ThrowingCall {
    void run() throws Exception;
  }

  @Test
  public void onlyReviewedIdResolvesAndAll23FactsArePinned() throws Exception {
    ArcadeCompatProfile.Profile profile = ArcadeCompatProfile.profile("knights-mame-v1");
    assertNotNull(profile);
    assertEquals(ArcadeCompatProfile.VERSION, profile.id);
    assertEquals("mame2003_plus_libretro_android.so", profile.core);
    assertEquals("knights", profile.set);
    assertEquals("圆桌骑士 MOD · MAME2003-Plus 兼容模式", profile.label);
    assertEquals(
        "e7920e35a0a2528703361174349ad8bd39c240fce9e80bf81c6f0cca1094167f", profile.sourceSha256);
    assertNull(ArcadeCompatProfile.profile(null));
    assertNull(ArcadeCompatProfile.profile("knights"));
    assertNull(ArcadeCompatProfile.profile("../knights-mame-v1"));
    assertEquals(23, profile.members.size());
    long renamed =
        profile.members.stream()
            .filter(member -> !member.sourceName.equals(member.stageName))
            .count();
    assertEquals(13, renamed);
    ArcadeCompatProfile.Member program =
        profile.members.stream()
            .filter(member -> member.sourceName.equals("kr_23e.8f"))
            .findFirst()
            .get();
    assertEquals("kr_23e.rom", program.stageName);
    assertEquals(0xe0d06498L, program.crc);
    assertEquals(524288, program.size);
    assertEquals(
        "ce2389e42a2a8ed49a8093aaa7060ee12f1d1ac1bead03dfdf0be07bcdbb7138", program.sha256);
  }

  @Test
  public void allMemberBytesRoundTripAndValidCacheIsReused() throws Exception {
    Map<String, byte[]> data = data(23);
    File source = zip(data), cache = files.newFolder();
    byte[] original = Files.readAllBytes(source.toPath());
    ArcadeCompatProfile.Profile profile = fixture(source, data);
    File output = prepareFixture(source, cache, profile);
    assertEquals(
        new File(cache.getCanonicalFile(), "arcade-compat/original-test-v1/fixture.zip"), output);
    assertArrayEquals(original, Files.readAllBytes(source.toPath()));
    try (ZipFile zip = new ZipFile(output)) {
      assertEquals(23, zip.size());
      for (ArcadeCompatProfile.Member member : profile.members) {
        ZipEntry entry = zip.getEntry(member.stageName);
        assertNotNull(entry);
        assertNull(zip.getEntry(member.sourceName));
        try (InputStream in = zip.getInputStream(entry)) {
          assertArrayEquals(data.get(member.sourceName), in.readAllBytes());
        }
        assertEquals(member.crc, entry.getCrc());
      }
    }
    long modified = output.lastModified();
    byte[] staged = Files.readAllBytes(output.toPath());
    assertEquals(output, prepareFixture(source, cache, profile));
    assertEquals(modified, output.lastModified());
    assertArrayEquals(staged, Files.readAllBytes(output.toPath()));
    assertArrayEquals(new String[] {"fixture.zip"}, output.getParentFile().list());
  }

  @Test
  public void filenameCannotAdmitUnknownOrChangedArchive() throws Exception {
    Map<String, byte[]> data = data(1);
    File source = zip(data), cache = files.newFolder();
    byte[] before = Files.readAllBytes(source.toPath());
    assertNull(ArcadeCompatProfile.match(source));
    assertTrue(
        reject(() -> ArcadeCompatProfile.prepare(source, cache)).getMessage().contains("未匹配"));
    assertEquals(0, cache.list().length);
    assertArrayEquals(before, Files.readAllBytes(source.toPath()));

    ArcadeCompatProfile.Profile profile = fixture(source, data);
    try (RandomAccessFile edit = new RandomAccessFile(source, "rw")) {
      edit.seek(40);
      int value = edit.read();
      edit.seek(40);
      edit.write(value ^ 1);
    }
    byte[] changed = Files.readAllBytes(source.toPath());
    assertTrue(reject(() -> prepareFixture(source, cache, profile)).getMessage().contains("摘要"));
    assertArrayEquals(changed, Files.readAllBytes(source.toPath()));
    assertEquals(0, cache.list().length);
  }

  @Test
  public void wrongMemberDigestOrNameCannotPassExactArchiveHash() throws Exception {
    Map<String, byte[]> data = data(1);
    File source = zip(data), cache = files.newFolder();
    byte[] before = Files.readAllBytes(source.toPath());
    ArcadeCompatProfile.Profile valid = fixture(source, data);
    ArcadeCompatProfile.Member member = valid.members.get(0);
    for (boolean wrongName : new boolean[] {false, true}) {
      ArcadeCompatProfile.Member wrong =
          new ArcadeCompatProfile.Member(
              wrongName ? "other.bin" : member.sourceName,
              member.stageName,
              member.size,
              member.crc,
              wrongName ? member.sha256 : hash(new byte[0]));
      ArcadeCompatProfile.Profile invalid =
          new ArcadeCompatProfile.Profile(
              valid.id, valid.core, valid.set, valid.label, valid.sourceSha256, wrong);
      reject(() -> prepareFixture(source, cache, invalid));
      assertArrayEquals(before, Files.readAllBytes(source.toPath()));
      assertEquals(0, cache.list().length);
    }
  }

  @Test
  public void corruptExistingCacheIsPreservedAndRejected() throws Exception {
    Map<String, byte[]> data = data(2);
    File source = zip(data), cache = files.newFolder();
    ArcadeCompatProfile.Profile profile = fixture(source, data);
    File output = prepareFixture(source, cache, profile);
    byte[] invalid = "not a verified compatibility archive".getBytes(StandardCharsets.UTF_8);
    Files.write(output.toPath(), invalid);
    byte[] original = Files.readAllBytes(source.toPath());
    reject(() -> prepareFixture(source, cache, profile));
    assertArrayEquals(invalid, Files.readAllBytes(output.toPath()));
    assertArrayEquals(original, Files.readAllBytes(source.toPath()));
    assertArrayEquals(new String[] {"fixture.zip"}, output.getParentFile().list());
  }

  @Test
  public void malformedAndOversizedArchivesFailBeforeUnboundedParsing() throws Exception {
    File malformed = files.newFile();
    Files.write(malformed.toPath(), "not zip".getBytes(StandardCharsets.UTF_8));
    assertTrue(reject(() -> ArcadeCompatProfile.match(malformed)).getMessage().contains("标准 ZIP"));
    File sparse = files.newFile();
    try (RandomAccessFile out = new RandomAccessFile(sparse, "rw")) {
      out.setLength(FbneoArchive.MAX_ARCHIVE_BYTES + 1);
    }
    assertTrue(reject(() -> ArcadeCompatProfile.match(sparse)).getMessage().contains("512 MiB"));
    assertEquals(FbneoArchive.MAX_ARCHIVE_BYTES + 1, sparse.length());
    Map<String, byte[]> directories = new LinkedHashMap<>();
    for (int i = 0; i <= FbneoArchive.MAX_ENTRIES; i++)
      directories.put("directory" + i + "/", new byte[0]);
    File tooMany = zip(directories);
    assertTrue(reject(() -> ArcadeCompatProfile.match(tooMany)).getMessage().contains("条目数量"));
  }

  @Test
  public void managedCacheCannotRedirectToUnrelatedDirectory() throws Exception {
    Map<String, byte[]> data = data(1);
    File source = zip(data), cache = files.newFolder(), unrelated = files.newFolder();
    ArcadeCompatProfile.Profile profile = fixture(source, data);
    Files.createSymbolicLink(new File(cache, "arcade-compat").toPath(), unrelated.toPath());
    assertTrue(reject(() -> prepareFixture(source, cache, profile)).getMessage().contains("其他位置"));
    assertEquals(0, unrelated.list().length);
  }

  @Test
  public void cancellationDoesNotCreateOrReplaceFiles() throws Exception {
    Map<String, byte[]> data = data(1);
    File source = zip(data), cache = files.newFolder();
    ArcadeCompatProfile.Profile profile = fixture(source, data);
    byte[] original = Files.readAllBytes(source.toPath());
    Thread.currentThread().interrupt();
    try {
      assertTrue(reject(() -> prepareFixture(source, cache, profile)).getMessage().contains("取消"));
    } finally {
      Thread.interrupted();
    }
    assertEquals(0, cache.list().length);
    assertArrayEquals(original, Files.readAllBytes(source.toPath()));
  }

  @Test
  public void interruptionDuringCopyRemovesOnlyItsPendingArchive() throws Exception {
    Map<String, byte[]> data = new LinkedHashMap<>();
    byte[] content = new byte[32 * 1024 * 1024];
    new Random(240).nextBytes(content);
    data.put("original.bin", content);
    File source = zip(data), cache = files.newFolder();
    ArcadeCompatProfile.Profile profile = fixture(source, data);
    File directory = new File(cache, "arcade-compat/" + profile.id);
    assertTrue(directory.mkdirs());
    File preserved = new File(directory, "unrelated.txt");
    byte[] marker = "preserve unrelated cache".getBytes(StandardCharsets.UTF_8);
    Files.write(preserved.toPath(), marker);
    AtomicReference<Throwable> result = new AtomicReference<>();
    Thread worker =
        new Thread(
            () -> {
              try {
                prepareFixture(source, cache, profile);
              } catch (Throwable failure) {
                result.set(failure);
              }
            });
    worker.start();
    boolean interruptedPending = false;
    long deadline = System.nanoTime() + 10_000_000_000L;
    try {
      while (worker.isAlive() && System.nanoTime() < deadline) {
        String[] pending = directory.list((parent, name) -> name.startsWith(".pending-"));
        if (pending != null && pending.length > 0) {
          worker.interrupt();
          interruptedPending = true;
          break;
        }
        Thread.sleep(1);
      }
    } finally {
      if (worker.isAlive()) worker.interrupt();
      worker.join(10_000);
    }
    assertFalse(worker.isAlive());
    assertTrue("must cancel after the temporary output has been created", interruptedPending);
    assertTrue(String.valueOf(result.get()), result.get() instanceof IOException);
    assertTrue(result.get().getMessage().contains("取消"));
    assertArrayEquals(marker, Files.readAllBytes(preserved.toPath()));
    assertArrayEquals(new String[] {"unrelated.txt"}, directory.list());
    assertEquals(profile.sourceSha256, hash(Files.readAllBytes(source.toPath())));
  }
}
