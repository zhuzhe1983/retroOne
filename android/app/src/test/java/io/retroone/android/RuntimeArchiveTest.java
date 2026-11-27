package io.retroone.android;

import static org.junit.Assert.*;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.file.Files;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class RuntimeArchiveTest {
  @Rule public TemporaryFolder temporary = new TemporaryFolder();

  @Test
  public void dsZipOverheadDoesNotRelaxArcadePreflightBound() throws Exception {
    // Sparse structural fixture: never inflate this deliberately synthetic payload.
    File file = temporary.newFile();
    long directory = 512L * 1024 * 1024 + 1024;
    byte[] central = new byte[47];
    little(central, 0, 0x02014b50L, 4);
    little(central, 10, 8, 2);
    little(central, 20, directory - 31, 4);
    little(central, 24, 512L * 1024 * 1024, 4);
    little(central, 28, 1, 2);
    central[46] = 'a';
    byte[] end = new byte[22];
    little(end, 0, 0x06054b50L, 4);
    little(end, 8, 1, 2);
    little(end, 10, 1, 2);
    little(end, 12, central.length, 4);
    little(end, 16, directory, 4);
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.writeInt(0x504b0304);
      out.seek(directory);
      out.write(central);
      out.write(end);
    }
    assertEquals(1, FbneoArchive.checkLayout(file, RuntimePolicy.spec("nds").maxArchiveBytes));
    IOException old = assertThrows(IOException.class, () -> FbneoArchive.checkLayout(file));
    assertTrue(old.getMessage().contains("512 MiB"));
    assertThrows(IOException.class, () -> FbneoArchive.checkLayout(file, Long.MAX_VALUE));
  }

  private static void little(byte[] bytes, int offset, long value, int length) {
    for (int i = 0; i < length; i++) bytes[offset + i] = (byte) (value >>> (8 * i));
  }

  @Test
  public void n64ExtractsWithoutByteSwapAndReusesVerifiedCache() throws Exception {
    byte[] rom = n64();
    File zip = zip(Map.of("子目录/game.v64", rom));
    byte[] original = Files.readAllBytes(zip.toPath());
    File cache = temporary.newFolder();
    CartridgeArchive.Extracted result =
        CartridgeArchive.prepareRuntime(zip, "game.zip", RuntimePolicy.spec("n64"), cache);
    assertTrue(result.fromArchive);
    assertArrayEquals(rom, Files.readAllBytes(result.file.toPath()));
    assertEquals(
        result.file,
        CartridgeArchive.prepareRuntime(zip, "game.zip", RuntimePolicy.spec("n64"), cache).file);
    assertArrayEquals(original, Files.readAllBytes(zip.toPath()));
    assertTrue(result.file.getName().startsWith("cart-n64-"));
  }

  @Test
  public void dsZipAcceptsSingleDsPayloadAndNeverFeedsZipToCore() throws Exception {
    File zip =
        zip(Map.of("original.nds", nds(), "README.txt", "original format fixture".getBytes()));
    CartridgeArchive.Extracted result =
        CartridgeArchive.prepareRuntime(
            zip, "game.zip", RuntimePolicy.spec("nds"), temporary.newFolder());
    assertTrue(result.file.getName().endsWith(".nds"));
    assertArrayEquals(nds(), Files.readAllBytes(result.file.toPath()));
  }

  @Test
  public void dsOnlyOrMultipleCandidatesLeaveNoPublishedCache() throws Exception {
    Map<String, byte[]> members = new LinkedHashMap<>();
    members.put("a.nds", nds());
    members.put("b.nds", nds());
    reject(zip(members), "nds");
    byte[] dsi = nds();
    dsi[0x12] = 3;
    reject(zip(Map.of("a.nds", dsi)), "nds");
    reject(zip(Map.of("original.nds", nds(), "bios7.bin", new byte[16384])), "nds");
    reject(zip(Map.of("original.v64", nds())), "n64");
    reject(zip(Map.of("disc.chd", new byte[2048])), "psx");
  }

  @Test
  public void changedCacheIsNotOverwritten() throws Exception {
    File zip = zip(Map.of("game.v64", n64()));
    File cache = temporary.newFolder();
    CartridgeArchive.Extracted result =
        CartridgeArchive.prepareRuntime(zip, "game.zip", RuntimePolicy.spec("n64"), cache);
    byte[] altered = n64();
    altered[100] = 123;
    Files.write(result.file.toPath(), altered);
    try {
      CartridgeArchive.prepareRuntime(zip, "game.zip", RuntimePolicy.spec("n64"), cache);
      fail();
    } catch (IOException expected) {
      assertTrue(expected.getMessage().contains("缓存"));
    }
    assertArrayEquals(altered, Files.readAllBytes(result.file.toPath()));
    assertEquals(1, new File(cache, "cartridge-roms").list().length);
  }

  private void reject(File zip, String platform) throws Exception {
    File cache = temporary.newFolder();
    byte[] original = Files.readAllBytes(zip.toPath());
    try {
      CartridgeArchive.prepareRuntime(zip, "game.zip", RuntimePolicy.spec(platform), cache);
      fail("accepted " + platform);
    } catch (IOException expected) {
    }
    File extracted = new File(cache, "cartridge-roms");
    assertTrue(!extracted.exists() || extracted.list().length == 0);
    assertArrayEquals(original, Files.readAllBytes(zip.toPath()));
  }

  private File zip(Map<String, byte[]> files) throws Exception {
    File file = temporary.newFile();
    try (ZipOutputStream out = new ZipOutputStream(Files.newOutputStream(file.toPath()))) {
      for (Map.Entry<String, byte[]> entry : files.entrySet()) {
        out.putNextEntry(new ZipEntry(entry.getKey()));
        out.write(entry.getValue());
        out.closeEntry();
      }
    }
    return file;
  }

  private static byte[] n64() {
    byte[] bytes = new byte[4096];
    bytes[0] = 0x37;
    bytes[1] = (byte) 0x80;
    bytes[2] = 0x40;
    bytes[3] = 0x12;
    return bytes;
  }

  private static byte[] nds() {
    byte[] bytes = new byte[4096];
    // Synthetic DS header only, not a runnable game and contains no console firmware.
    bytes[0x21] = 2;
    bytes[0x2c] = 32;
    bytes[0x31] = 3;
    bytes[0x3c] = 32;
    return bytes;
  }
}
