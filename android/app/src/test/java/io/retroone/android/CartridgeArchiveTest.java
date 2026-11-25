package io.retroone.android;

import static org.junit.Assert.*;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class CartridgeArchiveTest {
  @Rule public final TemporaryFolder temporary = new TemporaryFolder();

  @Test
  public void bareRomUsesOriginalObjectNameAndBytesWithoutCreatingCache() throws Exception {
    File source = temporary.newFile();
    byte[] bytes = nes();
    Files.write(source.toPath(), bytes);
    File cache = temporary.newFolder();
    CartridgeArchive.Extracted result = CartridgeArchive.prepare(source, "原创.NES", "nes", cache);
    assertSame(source, result.file);
    assertEquals("原创.NES", result.name);
    assertFalse(result.fromArchive);
    assertArrayEquals(bytes, Files.readAllBytes(source.toPath()));
    assertEquals(0, cache.list().length);
  }

  @Test
  public void uniqueNestedRomAndTextNotesProduceVerifiedContentAddressedCache() throws Exception {
    byte[] bytes = nes();
    File source =
        zip(
            Map.of(
                "folder/",
                new byte[0],
                "folder/Original.NES",
                bytes,
                "README.txt",
                "Original fixture only\n".getBytes(StandardCharsets.UTF_8)));
    byte[] before = Files.readAllBytes(source.toPath());
    File cache = temporary.newFolder();
    CartridgeArchive.Extracted result =
        CartridgeArchive.prepare(source, "download.zip", "nes", cache);
    assertTrue(result.fromArchive);
    assertEquals("Original.NES", result.name);
    String hash = FbneoArchive.hex(MessageDigest.getInstance("SHA-256").digest(bytes));
    assertEquals("cart-nes-" + hash + ".nes", result.file.getName());
    assertEquals(new File(cache, "cartridge-roms"), result.file.getParentFile());
    assertArrayEquals(bytes, Files.readAllBytes(result.file.toPath()));
    CartridgeArchive.Extracted reused =
        CartridgeArchive.prepare(source, "download.zip", "nes", cache);
    assertEquals(result.file, reused.file);
    assertEquals(1, result.file.getParentFile().list().length);
    assertArrayEquals(before, Files.readAllBytes(source.toPath()));
  }

  @Test
  public void unicodeUtf8AndLegacyCp437NamesKeepAsciiExtensionWithoutUsingPaths() throws Exception {
    for (Charset encoding : new Charset[] {StandardCharsets.UTF_8, Charset.forName("CP437")}) {
      String name = encoding.equals(StandardCharsets.UTF_8) ? "测试/原创.nes" : "folder/╦≈─ß.nes";
      File source = zip(Map.of(name, nes()), encoding, false);
      CartridgeArchive.Extracted result =
          CartridgeArchive.prepare(source, "game.ZIP", "nes", temporary.newFolder());
      assertEquals(name.substring(name.lastIndexOf('/') + 1), result.name);
      assertArrayEquals(nes(), Files.readAllBytes(result.file.toPath()));
      assertFalse(result.file.getName().contains("╦"));
    }
  }

  @Test
  public void allSupportedPlatformsUseSameArchivePreparationAndHeaderChecks() throws Exception {
    for (String platform :
        new String[] {
          "nes", "snes", "gb", "gbc", "gba", "megadrive", "mastersystem", "gamegear", "pce"
        }) {
      String extension =
          platform.equals("nes") ? ".nes" : CartridgePolicy.spec(platform).extensions.get(0);
      byte[] bytes = rom(platform);
      File source = zip(Map.of("nested/deeper/original" + extension, bytes));
      CartridgeArchive.Extracted result =
          CartridgeArchive.prepare(source, "game.zip", platform, temporary.newFolder());
      assertArrayEquals(platform, bytes, Files.readAllBytes(result.file.toPath()));
    }
  }

  @Test
  public void multipleRomsNestedArchivesAndExternalDependenciesFailWithoutCache() throws Exception {
    for (Map<String, byte[]> entries :
        Arrays.asList(
            Map.of("one.nes", nes(), "two.nes", nes()),
            Map.of("one.nes", nes(), "inside.zip", new byte[] {1}),
            Map.of("one.nes", nes(), "game.msu", new byte[] {1}),
            Map.of("one.nes", nes(), "game-1.pcm", new byte[] {1}),
            Map.of("one.nes", nes(), "front.png", new byte[] {1}),
            Map.of("one.nes", nes(), "random.txt", "note".getBytes(StandardCharsets.UTF_8)),
            Map.of("wrong.gba", rom("gba")))) {
      rejectUnchanged(zip(entries), "nes", null);
    }
    rejectUnchanged(zip(Map.of("one.nes", new byte[32768])), "nes", "NES");
    rejectUnchanged(zip(Map.of("one.gba", nes())), "gba", "其他平台");
  }

  @Test
  public void renamedSegaPayloadsInsideZipAreRejectedAndLeaveNoExtractedCopy() throws Exception {
    for (String platform : new String[] {"mastersystem", "gamegear"}) {
      byte[] sega = rom(platform);
      System.arraycopy("TMR SEGA".getBytes(StandardCharsets.US_ASCII), 0, sega, 0x7ff0, 8);
      sega[0x7fff] = platform.equals("mastersystem") ? (byte) 0x4c : (byte) 0x6c;
      sega[0x1fff] = (byte) 0xe0; // Would otherwise satisfy the limited HuCard vector check.
      rejectUnchanged(zip(Map.of("renamed.pce", sega)), "pce", "SMS / GG 平台标识");
    }
    byte[] logical = rom("megadrive");
    System.arraycopy("SEGA MEGA DRIVE".getBytes(StandardCharsets.US_ASCII), 0, logical, 0x100, 15);
    byte[] smd = new byte[logical.length + 512];
    for (int block = 0; block < logical.length; block += 16384) {
      for (int i = 0; i < 8192; i++) {
        smd[512 + block + i] = logical[block + i * 2 + 1];
        smd[512 + block + 8192 + i] = logical[block + i * 2];
      }
    }
    for (String platform : new String[] {"pce", "mastersystem", "gamegear"}) {
      String extension = CartridgePolicy.spec(platform).extensions.get(0);
      rejectUnchanged(zip(Map.of("renamed" + extension, smd)), platform, "SMD 交织布局的 MD 平台标识");
    }
  }

  @Test
  public void unsafeAndDuplicateNamesNeverEscapeTheControlledCache() throws Exception {
    for (String path :
        new String[] {
          "../game.nes",
          "folder/../game.nes",
          "/game.nes",
          "C:/game.nes",
          "folder\\game.nes",
          "folder//game.nes",
          "folder/./game.nes",
          "a\n\ngame.nes"
        }) {
      rejectUnchanged(zip(Map.of(path, nes())), "nes", "路径");
    }
    rejectUnchanged(zip(Map.of("GAME.NES", nes(), "game.nes", nes())), "nes", "重名");
    for (String name : new String[] {"../game.zip", "a\\game.zip", "a\n\n.zip", ".zip", "", null})
      assertFalse(String.valueOf(name), CartridgeArchive.isZipName(name));
    assertTrue(CartridgeArchive.isZipName("游戏.ZIP"));
  }

  @Test
  public void crcAndActualInflatedSizeMustMatchDeclarationsAndOriginalIsUntouched()
      throws Exception {
    File badCrc = zip(Map.of("game.nes", nes()));
    patchCentral(badCrc, 16, 0, 4);
    rejectUnchanged(badCrc, "nes", "CRC");
    File understated = zip(Map.of("game.nes", nes()));
    patchCentral(understated, 24, 1, 4);
    rejectUnchanged(understated, "nes", "实际展开大小");
    File truncated = zip(Map.of("game.nes", nes()));
    try (RandomAccessFile out = new RandomAccessFile(truncated, "rw")) {
      out.setLength(truncated.length() - 1);
    }
    rejectUnchanged(truncated, "nes", "结束记录");
  }

  @Test
  public void physicalEntryAndDocumentationLimitsPrecedeExtraction() throws Exception {
    File sparse = temporary.newFile();
    try (RandomAccessFile out = new RandomAccessFile(sparse, "rw")) {
      out.setLength(CartridgeArchive.MAX_ARCHIVE_BYTES + 1);
    }
    IOException physical =
        assertThrows(
            IOException.class,
            () -> CartridgeArchive.prepare(sparse, "game.zip", "nes", temporary.newFolder()));
    assertTrue(physical.getMessage().contains("128 MiB"));
    assertEquals(CartridgeArchive.MAX_ARCHIVE_BYTES + 1, sparse.length());
    byte[] longNote = new byte[(int) CartridgeArchive.MAX_NOTE_BYTES + 1];
    Arrays.fill(longNote, (byte) 'A');
    rejectUnchanged(zip(Map.of("game.nes", nes(), "README.txt", longNote)), "nes", "说明文件");
    rejectUnchanged(
        zip(Map.of("game.nes", nes(), "README.txt", new byte[] {'P', 'K', 3, 4})), "nes", "普通文本");
    File hugeDeclaration = zip(Map.of("game.gba", rom("gba")));
    patchCentral(hugeDeclaration, 24, CartridgePolicy.spec("gba").maxBytes + 1, 4);
    rejectUnchanged(hugeDeclaration, "gba", "声明大小");
    Map<String, byte[]> directories = new LinkedHashMap<>();
    directories.put("game.nes", nes());
    for (int i = 0; i < FbneoArchive.MAX_ENTRIES; i++) directories.put("d" + i + "/", new byte[0]);
    rejectUnchanged(zip(directories), "nes", "条目数量");
  }

  @Test
  public void zipSymlinksAndCacheSymlinksAreRejectedWithoutFollowingThem() throws Exception {
    File linkEntry = zip(Map.of("game.nes", nes()));
    patchCentral(linkEntry, 38, 0120777L << 16, 4);
    rejectUnchanged(linkEntry, "nes", "符号链接");
    File cache = temporary.newFolder();
    File outside = temporary.newFolder();
    Files.createSymbolicLink(new File(cache, "cartridge-roms").toPath(), outside.toPath());
    IOException error =
        assertThrows(
            IOException.class,
            () ->
                CartridgeArchive.prepare(zip(Map.of("game.nes", nes())), "game.zip", "nes", cache));
    assertTrue(error.getMessage().contains("符号链接"));
    assertEquals(0, outside.list().length);
  }

  @Test
  public void unknownOrModifiedExistingCacheIsNeverOverwrittenAndPendingIsRemoved()
      throws Exception {
    File source = zip(Map.of("game.nes", nes()));
    File cache = temporary.newFolder();
    CartridgeArchive.Extracted valid = CartridgeArchive.prepare(source, "game.zip", "nes", cache);
    byte[] damaged = nes();
    damaged[100] ^= 1;
    Files.write(valid.file.toPath(), damaged);
    IOException error =
        assertThrows(
            IOException.class, () -> CartridgeArchive.prepare(source, "game.zip", "nes", cache));
    assertTrue(error.getMessage().contains("不会覆盖"));
    assertArrayEquals(damaged, Files.readAllBytes(valid.file.toPath()));
    assertEquals(1, valid.file.getParentFile().list().length);
    File invalid = zip(Map.of("game.nes", new byte[32768]));
    assertThrows(
        IOException.class, () -> CartridgeArchive.prepare(invalid, "bad.zip", "nes", cache));
    assertArrayEquals(damaged, Files.readAllBytes(valid.file.toPath()));
    assertEquals(1, valid.file.getParentFile().list().length);
  }

  @Test
  public void interruptedPreparationRetainsOriginalAndDoesNotLeavePendingFiles() throws Exception {
    File source = zip(Map.of("game.nes", nes()));
    byte[] before = Files.readAllBytes(source.toPath());
    File cache = temporary.newFolder();
    try {
      Thread.currentThread().interrupt();
      IOException error =
          assertThrows(
              IOException.class, () -> CartridgeArchive.prepare(source, "game.zip", "nes", cache));
      assertTrue(error.getMessage().contains("取消"));
    } finally {
      Thread.interrupted();
    }
    assertArrayEquals(before, Files.readAllBytes(source.toPath()));
    assertEquals(0, cache.list().length);
  }

  @Test
  public void cancellationDuringInflationDeletesTheAlreadyCreatedTemporaryFile() throws Exception {
    // A tiny compressed ZIP expands to the allowed NES boundary, giving cancellation a real
    // streaming operation to interrupt without a large archive or a timing-only mock.
    File source = zip(Map.of("game.nes", Arrays.copyOf(nes(), 64 * 1024 * 1024)));
    assertTrue(source.length() < 128 * 1024);
    byte[] before = Files.readAllBytes(source.toPath());
    File cache = temporary.newFolder();
    File unpacked = new File(cache, "cartridge-roms");
    AtomicReference<Throwable> failure = new AtomicReference<>();
    Thread worker =
        new Thread(
            () -> {
              try {
                CartridgeArchive.prepare(source, "game.zip", "nes", cache);
              } catch (Throwable error) {
                failure.set(error);
              }
            });
    worker.start();
    boolean pendingObserved = false;
    long deadline = System.nanoTime() + 5_000_000_000L;
    try {
      while (worker.isAlive() && System.nanoTime() < deadline) {
        String[] names = unpacked.list();
        if (names != null && Arrays.stream(names).anyMatch(name -> name.startsWith("pending-"))) {
          pendingObserved = true;
          worker.interrupt();
          break;
        }
        Thread.yield();
      }
      worker.join(5000);
      assertTrue("Expected to observe the actual extraction temporary file", pendingObserved);
      assertFalse("Extraction worker must exit on interruption", worker.isAlive());
      assertTrue(String.valueOf(failure.get()), failure.get() instanceof IOException);
      assertTrue(failure.get().getMessage(), failure.get().getMessage().contains("取消"));
      assertArrayEquals(before, Files.readAllBytes(source.toPath()));
      assertEquals(0, unpacked.list().length);
    } finally {
      worker.interrupt();
      worker.join(5000);
    }
  }

  @Test
  public void cancellationWhileWaitingToPublishDoesNotPublishOrReturnAFile() throws Exception {
    File source = zip(Map.of("original.nes", nes()));
    byte[] original = Files.readAllBytes(source.toPath());
    File cache = temporary.newFolder();
    AtomicReference<Throwable> failure = new AtomicReference<>();
    AtomicReference<CartridgeArchive.Extracted> result = new AtomicReference<>();
    Thread worker =
        new Thread(
            () -> {
              try {
                result.set(CartridgeArchive.prepare(source, "original.zip", "nes", cache));
              } catch (Throwable e) {
                failure.set(e);
              }
            });
    try {
      synchronized (CartridgeArchive.class) {
        worker.start();
        long until = System.nanoTime() + 5_000_000_000L;
        while (worker.isAlive()
            && worker.getState() != Thread.State.BLOCKED
            && System.nanoTime() < until) Thread.sleep(1);
        assertEquals(
            "Worker must reach the actual publication monitor",
            Thread.State.BLOCKED,
            worker.getState());
        worker.interrupt();
      }
      worker.join(5000);
      assertFalse(worker.isAlive());
      assertNull(result.get());
      assertTrue(String.valueOf(failure.get()), failure.get() instanceof IOException);
      assertTrue(failure.get().getMessage().contains("取消"));
      assertEquals(0, new File(cache, "cartridge-roms").list().length);
      assertArrayEquals(original, Files.readAllBytes(source.toPath()));
    } finally {
      worker.interrupt();
      worker.join(5000);
    }
  }

  @Test
  public void simultaneousPublishersReuseOneCompleteFile() throws Exception {
    File source = zip(Map.of("original.nes", nes()));
    File cache = temporary.newFolder();
    java.util.concurrent.ExecutorService executor =
        java.util.concurrent.Executors.newFixedThreadPool(4);
    try {
      java.util.List<java.util.concurrent.Future<CartridgeArchive.Extracted>> results =
          new java.util.ArrayList<>();
      for (int i = 0; i < 4; i++)
        results.add(
            executor.submit(() -> CartridgeArchive.prepare(source, "game.zip", "nes", cache)));
      File expected = results.get(0).get(10, java.util.concurrent.TimeUnit.SECONDS).file;
      for (java.util.concurrent.Future<CartridgeArchive.Extracted> result : results)
        assertEquals(expected, result.get(10, java.util.concurrent.TimeUnit.SECONDS).file);
      assertArrayEquals(nes(), Files.readAllBytes(expected.toPath()));
      assertEquals(1, expected.getParentFile().list().length);
    } finally {
      executor.shutdownNow();
    }
  }

  private File zip(Map<String, byte[]> entries) throws Exception {
    return zip(entries, StandardCharsets.UTF_8, false);
  }

  private File zip(Map<String, byte[]> entries, Charset encoding, boolean stored) throws Exception {
    File file = temporary.newFile();
    try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(file), encoding)) {
      for (Map.Entry<String, byte[]> value : entries.entrySet()) {
        ZipEntry entry = new ZipEntry(value.getKey());
        if (stored) {
          CRC32 crc = new CRC32();
          crc.update(value.getValue());
          entry.setMethod(ZipEntry.STORED);
          entry.setSize(value.getValue().length);
          entry.setCrc(crc.getValue());
        }
        out.putNextEntry(entry);
        out.write(value.getValue());
        out.closeEntry();
      }
    }
    return file;
  }

  private void rejectUnchanged(File file, String platform, String reason) throws Exception {
    byte[] before = Files.readAllBytes(file.toPath());
    File cache = temporary.newFolder();
    IOException error =
        assertThrows(
            IOException.class, () -> CartridgeArchive.prepare(file, "game.zip", platform, cache));
    if (reason != null) assertTrue(error.getMessage(), error.getMessage().contains(reason));
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
    File unpacked = new File(cache, "cartridge-roms");
    if (unpacked.exists()) assertEquals(0, unpacked.list().length);
  }

  private void patchCentral(File file, int offset, long value, int bytes) throws Exception {
    byte[] data = Files.readAllBytes(file.toPath());
    for (int i = 0; i + 4 <= data.length; i++) {
      if (data[i] == 'P' && data[i + 1] == 'K' && data[i + 2] == 1 && data[i + 3] == 2) {
        try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
          out.seek(i + offset);
          for (int j = 0; j < bytes; j++) {
            out.write((int) value & 255);
            value >>>= 8;
          }
        }
        return;
      }
    }
    fail("No central directory found");
  }

  private static byte[] nes() {
    byte[] bytes = new byte[16 + 16384];
    bytes[0] = 'N';
    bytes[1] = 'E';
    bytes[2] = 'S';
    bytes[3] = 0x1a;
    bytes[4] = 1;
    return bytes;
  }

  private static byte[] rom(String platform) {
    if (platform.equals("nes")) return nes();
    byte[] bytes = new byte[32768];
    switch (platform) {
      case "gb", "gbc" -> {
        bytes[0x100] = (byte) 0xc3;
        bytes[0x14d] = (byte) -25;
      }
      case "gba" -> {
        bytes[0xb2] = (byte) 0x96;
        bytes[0xbd] = (byte) -(0x19 + 0x96);
      }
      case "snes" -> {
        bytes[0x7fd5] = 0x20;
        bytes[0x7ffd] = (byte) 0x80;
      }
      case "megadrive" -> {
        bytes[1] = (byte) 0xff;
        bytes[6] = 2;
      }
      case "mastersystem", "gamegear" -> bytes[0] = (byte) 0xf3;
      case "pce" -> {
        bytes[0] = 0x78;
        bytes[0x1fff] = (byte) 0xe0;
      }
    }
    return bytes;
  }
}
