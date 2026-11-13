package io.retroone.android;

import static org.junit.Assert.*;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.zip.*;
import org.json.*;
import org.junit.*;
import org.junit.rules.TemporaryFolder;

public class PlayerDataTest {
  @Rule public TemporaryFolder temp = new TemporaryFolder();
  ServerSocket server;
  Thread serverThread;
  volatile Throwable serverError;
  ServerConfig config;
  String mode = "ok";
  AtomicInteger releases = new AtomicInteger(), escaped = new AtomicInteger();
  List<String> requests = new ArrayList<>();

  @Before
  public void start() throws Exception {
    server = new ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"));
    config =
        new ServerConfig(
            "http://127.0.0.1:" + server.getLocalPort() + "/p/player2", true, "Bearer fixture");
    serverThread =
        new Thread(
            () -> {
              while (!server.isClosed())
                try (Socket socket = server.accept()) {
                  BufferedReader reader =
                      new BufferedReader(
                          new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
                  String request = reader.readLine(), line;
                  Map<String, String> headers = new HashMap<>();
                  while ((line = reader.readLine()) != null && !line.isEmpty()) {
                    int colon = line.indexOf(':');
                    headers.put(
                        line.substring(0, colon).toLowerCase(Locale.ROOT),
                        line.substring(colon + 1).trim());
                  }
                  String path = request.split(" ")[1];
                  requests.add(path);
                  assertEquals("Bearer fixture", headers.get("authorization"));
                  char[] chars =
                      new char[Integer.parseInt(headers.getOrDefault("content-length", "0"))];
                  int consumed = 0;
                  while (consumed < chars.length) {
                    int n = reader.read(chars, consumed, chars.length - consumed);
                    if (n < 0) break;
                    consumed += n;
                  }
                  String input = new String(chars);
                  String extra = "";
                  int code = 200;
                  String body = "{}";
                  if (path.endsWith("/favorite")) {
                    assertEquals("POST", request.split(" ")[0]);
                    body =
                        new JSONObject()
                            .put("favorite", new JSONObject(input).getBoolean("favorite"))
                            .toString();
                  } else if (path.endsWith("/lease")) {
                    if (mode.equals("busy")) {
                      code = 409;
                    } else if (mode.equals("redirect")) {
                      code = 307;
                      extra = "Location: /p/player1/stolen\r\n";
                    } else {
                      body =
                          new JSONObject()
                              .put("lease_id", "fixture-lease")
                              .put("revision", 3)
                              .put(
                                  "files",
                                  files(mode.equals("unsafe") ? "../secret" : "ULUS00001/DATA.BIN"))
                              .toString();
                    }
                  } else if (path.endsWith("/release")) {
                    assertEquals("fixture-lease", new JSONObject(input).getString("lease_id"));
                    releases.incrementAndGet();
                  } else if (path.contains("stolen")) {
                    escaped.incrementAndGet();
                  } else {
                    body =
                        new JSONObject()
                            .put("games", new JSONArray())
                            .put("total", 36623)
                            .toString();
                  }
                  byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
                  OutputStream out = socket.getOutputStream();
                  out.write(
                      ("HTTP/1.1 "
                              + code
                              + " Test\r\nConnection: close\r\n"
                              + extra
                              + "Content-Length: "
                              + bytes.length
                              + "\r\n\r\n")
                          .getBytes(StandardCharsets.US_ASCII));
                  out.write(bytes);
                } catch (Throwable e) {
                  if (!server.isClosed()) serverError = e;
                }
            });
    serverThread.start();
  }

  @After
  public void stop() throws Exception {
    server.close();
    serverThread.join(2000);
    if (serverError != null) throw new AssertionError(serverError);
  }

  static JSONArray files(String path) throws Exception {
    return new JSONArray().put(new JSONObject().put("path", path).put("data", "cHJvZ3Jlc3M="));
  }

  @Test
  public void entireLibraryAndFavoriteMutationStayOnSelectedPlayer() throws Exception {
    ApiClient api = new ApiClient(config);
    String route = LibraryQuery.route("空之轨迹&", "psp", "favorites", "play_count", "desc", 120);
    assertEquals(36623, api.json(config.api(route)).getInt("total"));
    assertTrue(requests.get(0).startsWith("/p/player2/api/games?"));
    assertTrue(requests.get(0).contains("offset=120"));
    assertTrue(requests.get(0).contains("favorite=true"));
    assertTrue(requests.get(0).contains("sort=play_count&direction=desc"));
    assertTrue(requests.get(0).contains("%26"));
    assertTrue(
        api.post(config.api("games/abc/favorite"), new JSONObject().put("favorite", true), 4096)
            .getBoolean("favorite"));
    assertFalse(
        api.post(config.api("games/abc/favorite"), new JSONObject().put("favorite", false), 4096)
            .getBoolean("favorite"));
    assertTrue(requests.stream().allMatch(p -> p.startsWith("/p/player2/")));
  }

  @Test
  public void fetchReleasesLeaseAndExportsActualSaveBytes() throws Exception {
    JSONObject data = SaveBackup.fetch(config, "abc");
    assertEquals(1, releases.get());
    File zip = temp.newFile("save.zip");
    SaveBackup.zip(data, zip);
    try (ZipInputStream in = new ZipInputStream(new FileInputStream(zip))) {
      assertEquals("SAVEDATA/ULUS00001/DATA.BIN", in.getNextEntry().getName());
      assertEquals("progress", new String(in.readAllBytes(), StandardCharsets.UTF_8));
      assertNull(in.getNextEntry());
    }
  }

  @Test
  public void unsafeSnapshotStillReleasesLease() throws Exception {
    mode = "unsafe";
    assertThrows(IOException.class, () -> SaveBackup.fetch(config, "abc"));
    assertEquals(1, releases.get());
  }

  @Test
  public void activeCloudGameIsNotStoppedOrOverwritten() {
    mode = "busy";
    assertThrows(IOException.class, () -> SaveBackup.fetch(config, "abc"));
    assertEquals(0, releases.get());
    assertEquals(1, requests.size());
  }

  @Test
  public void mutationRedirectCannotChangePlayerOrLeakPayload() {
    mode = "redirect";
    assertThrows(IOException.class, () -> SaveBackup.fetch(config, "abc"));
    assertEquals(0, escaped.get());
    assertEquals(1, requests.size());
  }

  @Test
  public void rejectAmbiguousAndDangerousArchivePaths() throws Exception {
    for (String path :
        List.of("../x", "/save/x", "a/../x", "a/b\\c", "a/x.keys", "a/x.state", "a//x")) {
      JSONArray f = files(path);
      assertThrows(Exception.class, () -> SaveBackup.validate(f));
    }
    JSONArray duplicate = files("SAVE/DATA");
    duplicate.put(files("save/data").getJSONObject(0));
    assertThrows(IOException.class, () -> SaveBackup.validate(duplicate));
    JSONArray conflict = files("SAVE/DATA");
    conflict.put(files("SAVE/DATA/CHILD").getJSONObject(0));
    assertThrows(IOException.class, () -> SaveBackup.validate(conflict));
  }
}
