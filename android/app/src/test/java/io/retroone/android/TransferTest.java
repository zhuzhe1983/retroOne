package io.retroone.android;

import static org.junit.Assert.*;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONObject;
import org.junit.*;
import org.junit.rules.TemporaryFolder;

public class TransferTest {
  @Rule public TemporaryFolder temp = new TemporaryFolder();
  ServerSocket server;
  Thread serverThread;
  ServerConfig config;
  byte[] data = new byte[3 * 16384 + 17];
  List<String> ranges;
  String mode = "valid";
  AtomicInteger unauthorized = new AtomicInteger();

  @Before
  public void before() throws Exception {
    new Random(917).nextBytes(data);
    ranges = Collections.synchronizedList(new ArrayList<>());
    server = new ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"));
    config =
        new ServerConfig(
            "http://127.0.0.1:" + server.getLocalPort() + "/p/player1",
            true,
            "Bearer local-test-only");
    serverThread =
        new Thread(
            () -> {
              while (!server.isClosed())
                try (Socket socket = server.accept()) {
                  BufferedReader reader =
                      new BufferedReader(
                          new InputStreamReader(
                              socket.getInputStream(), StandardCharsets.US_ASCII));
                  String request = reader.readLine(), line;
                  Map<String, String> headers = new HashMap<>();
                  while ((line = reader.readLine()) != null && !line.isEmpty()) {
                    int colon = line.indexOf(':');
                    headers.put(
                        line.substring(0, colon).toLowerCase(Locale.ROOT),
                        line.substring(colon + 1).trim());
                  }
                  if (request.contains("/leak")) unauthorized.addAndGet(100);
                  if (!"Bearer local-test-only".equals(headers.get("authorization")))
                    unauthorized.incrementAndGet();
                  String range = headers.get("range");
                  ranges.add(range);
                  int start = Integer.parseInt(range.split("[=-]")[1]),
                      end = Integer.parseInt(range.split("[=-]")[2]);
                  byte[] body = Arrays.copyOfRange(data, start, end + 1);
                  int code = mode.equals("ignored") ? 200 : 206;
                  String extra =
                      "Content-Range: "
                          + (mode.equals("badRange")
                              ? "bytes 0-0/1"
                              : "bytes " + start + "-" + end + "/" + data.length)
                          + "\r\n";
                  if (mode.equals("corrupt")) body[0] ^= 1;
                  if (mode.equals("changed")) {
                    code = 409;
                    body = new byte[0];
                  }
                  if (mode.equals("redirect")) {
                    code = 302;
                    body = new byte[0];
                    extra = "Location: http://localhost:" + server.getLocalPort() + "/leak\r\n";
                  }
                  OutputStream out = socket.getOutputStream();
                  out.write(
                      ("HTTP/1.1 "
                              + code
                              + " Test\r\nConnection: close\r\n"
                              + extra
                              + "Content-Length: "
                              + body.length
                              + "\r\n\r\n")
                          .getBytes(StandardCharsets.US_ASCII));
                  if (mode.equals("truncated")) body = Arrays.copyOf(body, body.length / 2);
                  out.write(body);
                  out.flush();
                } catch (Exception e) {
                  if (!server.isClosed()) throw new RuntimeException(e);
                }
            });
    serverThread.start();
  }

  @After
  public void after() throws Exception {
    server.close();
    serverThread.join(2000);
  }

  static byte[] bencode(Object value) throws Exception {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    if (value instanceof Map) {
      out.write('d');
      for (Object k : ((Map<?, ?>) value).keySet()) {
        out.write(bencode(k.toString()));
        out.write(bencode(((Map<?, ?>) value).get(k)));
      }
      out.write('e');
    } else if (value instanceof Number)
      out.write(("i" + value + "e").getBytes(StandardCharsets.US_ASCII));
    else {
      byte[] bytes =
          value instanceof byte[]
              ? (byte[]) value
              : value.toString().getBytes(StandardCharsets.UTF_8);
      out.write((bytes.length + ":").getBytes(StandardCharsets.US_ASCII));
      out.write(bytes);
    }
    return out.toByteArray();
  }

  JSONObject json() throws Exception {
    ByteArrayOutputStream hashes = new ByteArrayOutputStream();
    for (int i = 0; i < data.length; i += 16384)
      hashes.write(
          MessageDigest.getInstance("SHA-1")
              .digest(Arrays.copyOfRange(data, i, Math.min(data.length, i + 16384))));
    Map<String, Object> info = new TreeMap<>();
    info.put("length", data.length);
    info.put("name", "fixture.pbp");
    info.put("piece length", 16384);
    info.put("pieces", hashes.toByteArray());
    info.put("private", 1);
    return new JSONObject()
        .put("name", "fixture.pbp")
        .put("revision", String.join("", Collections.nCopies(64, "a")))
        .put("size", data.length)
        .put("piece_length", 16384)
        .put("download_url", "/rom")
        .put("torrent_base64", Base64.getEncoder().encodeToString(bencode(Map.of("info", info))));
  }

  DownloadManifest manifest() throws Exception {
    return new DownloadManifest(json(), config);
  }

  @Test
  public void fullDownloadAuthenticatesAndVerifiesEveryPiece() throws Exception {
    File file = temp.newFile();
    Transfer.download(new ApiClient(config), manifest(), file, () -> false, n -> {});
    assertArrayEquals(data, Files.readAllBytes(file.toPath()));
    assertEquals(4, ranges.size());
    assertEquals(0, unauthorized.get());
  }

  @Test
  public void resumesOnlyVerifiedPrefixAndRepairsCorruptTail() throws Exception {
    File file = temp.newFile();
    byte[] existing = Arrays.copyOf(data, 2 * 16384 + 100);
    existing[20000] ^= 1;
    Files.write(file.toPath(), existing);
    Transfer.download(new ApiClient(config), manifest(), file, () -> false, n -> {});
    assertEquals("bytes=16384-32767", ranges.get(0));
    assertArrayEquals(data, Files.readAllBytes(file.toPath()));
  }

  @Test
  public void pauseAndResumePreserveValidatedPieces() throws Exception {
    File file = temp.newFile();
    AtomicBoolean pause = new AtomicBoolean();
    assertThrows(
        InterruptedIOException.class,
        () ->
            Transfer.download(
                new ApiClient(config),
                manifest(),
                file,
                pause::get,
                n -> {
                  if (n >= 16384) pause.set(true);
                }));
    assertEquals(16384, file.length());
    pause.set(false);
    Transfer.download(new ApiClient(config), manifest(), file, pause::get, n -> {});
    assertArrayEquals(data, Files.readAllBytes(file.toPath()));
    assertEquals("bytes=16384-32767", ranges.get(1));
  }

  @Test
  public void rejectsIgnoredRangeAndWrongRange() throws Exception {
    for (String problem : List.of("ignored", "badRange", "changed")) {
      mode = problem;
      File file = temp.newFile();
      assertThrows(
          IOException.class,
          () -> Transfer.download(new ApiClient(config), manifest(), file, () -> false, n -> {}));
      assertEquals(0, file.length());
    }
  }

  @Test
  public void rejectsCorruptionAndTruncation() throws Exception {
    mode = "corrupt";
    File file = temp.newFile();
    assertThrows(
        IOException.class,
        () -> Transfer.download(new ApiClient(config), manifest(), file, () -> false, n -> {}));
    assertEquals(0, file.length());
    mode = "truncated";
    assertThrows(
        IOException.class,
        () -> Transfer.download(new ApiClient(config), manifest(), file, () -> false, n -> {}));
    assertTrue(file.length() < 16384);
    mode = "valid";
    Transfer.download(new ApiClient(config), manifest(), file, () -> false, n -> {});
    assertArrayEquals(data, Files.readAllBytes(file.toPath()));
  }

  @Test
  public void refusesCrossOriginRedirectWithoutLeakingAuth() throws Exception {
    mode = "redirect";
    assertThrows(
        IllegalArgumentException.class,
        () ->
            Transfer.download(
                new ApiClient(config), manifest(), temp.newFile(), () -> false, n -> {}));
    assertEquals(0, unauthorized.get());
  }

  @Test
  public void rejectsTraversalAndMismatchedManifest() throws Exception {
    assertThrows(
        IOException.class, () -> new DownloadManifest(json().put("name", "../secret.pbp"), config));
    assertThrows(
        IOException.class, () -> new DownloadManifest(json().put("size", data.length + 1), config));
    assertThrows(
        IOException.class, () -> new DownloadManifest(json().put("piece_length", 1), config));
    assertThrows(
        IllegalArgumentException.class,
        () ->
            new DownloadManifest(
                json().put("download_url", "https://elsewhere.invalid/rom"), config));
    assertThrows(
        IOException.class,
        () ->
            new DownloadManifest(
                json()
                    .put(
                        "torrent_base64",
                        Base64.getEncoder().encodeToString("99999999999999999999:x".getBytes())),
                config));
  }

  @Test
  public void configurationProtectsOriginAndRequiresHttpOptIn() {
    assertThrows(
        IllegalArgumentException.class, () -> new ServerConfig("http://host/p/player1", false, ""));
    assertThrows(
        IllegalArgumentException.class,
        () -> new ServerConfig("https://user:pass@host", false, ""));
    assertThrows(
        IllegalArgumentException.class, () -> new ServerConfig("https://host?token=x", false, ""));
    assertThrows(IllegalArgumentException.class, () -> config.resolve("//elsewhere.invalid/path"));
    assertThrows(
        IllegalArgumentException.class, () -> config.resolve("http://127.0.0.1:9999/path"));
    ServerConfig tls = new ServerConfig("https://example.com/p/player1/", false, "");
    assertEquals("https://example.com/p/player1/api/games", tls.api("games").toString());
    assertThrows(IllegalArgumentException.class, () -> tls.resolve("http://example.com/rom"));
  }
}
