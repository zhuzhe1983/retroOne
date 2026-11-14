package io.retroone.android;

import static org.junit.Assert.*;

import android.app.*;
import android.content.*;
import android.net.Uri;
import android.provider.DocumentsContract;
import android.view.accessibility.AccessibilityNodeInfo;
import androidx.documentfile.provider.DocumentFile;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;

/** No production saves. Exercises the real lease API, SAF writes, and both conflict choices. */
@RunWith(AndroidJUnit4.class)
public class SaveSyncUiTest {
  private Instrumentation inst;
  private Context context;
  private Activity activity;
  private ExecutorService worker;
  private Uri tree =
      DocumentsContract.buildTreeDocumentUri("io.retroone.android.test.saves", "root");

  private AccessibilityNodeInfo find(AccessibilityNodeInfo n, String label) {
    if (n == null) return null;
    if (n.getText() != null && n.getText().toString().contains(label)) return n;
    for (int i = 0; i < n.getChildCount(); i++) {
      AccessibilityNodeInfo f = find(n.getChild(i), label);
      if (f != null) return f;
    }
    return null;
  }

  private AccessibilityNodeInfo waitFor(String label) throws Exception {
    long end = System.currentTimeMillis() + 20000;
    do {
      AccessibilityNodeInfo n = find(inst.getUiAutomation().getRootInActiveWindow(), label);
      if (n != null) return n;
      Thread.sleep(100);
    } while (System.currentTimeMillis() < end);
    throw new AssertionError("Missing save UI: " + label);
  }

  private void click(String label) throws Exception {
    assertTrue(waitFor(label).performAction(AccessibilityNodeInfo.ACTION_CLICK));
    inst.waitForIdleSync();
    Thread.sleep(200);
  }

  private JSONArray files(int version) throws Exception {
    return new JSONArray()
        .put(
            new JSONObject()
                .put("path", "TEST00001_GameData/DATA.BIN")
                .put(
                    "data", Base64.getEncoder().encodeToString(new byte[] {(byte) version, 2, 3})));
  }

  private void local(int version) throws Exception {
    DocumentFile root = DocumentFile.fromTreeUri(context, tree),
        dir = root.findFile("TEST00001_GameData");
    if (dir == null) dir = root.createDirectory("TEST00001_GameData");
    DocumentFile data = dir.findFile("DATA.BIN");
    if (data == null) data = dir.createFile("application/octet-stream", "DATA.BIN");
    try (OutputStream out = context.getContentResolver().openOutputStream(data.getUri(), "wt")) {
      out.write(new byte[] {(byte) version, 2, 3});
    }
  }

  private void cloud(ServerConfig s, String id, int version) throws Exception {
    ApiClient api = new ApiClient(s);
    String path = "games/" + id + "/cloud-save";
    JSONObject lease =
        api.post(
            s.api(path + "/lease"),
            new JSONObject().put("client_id", UUID.randomUUID().toString()),
            1024 * 1024);
    try {
      api.put(
          s.api(path),
          new JSONObject()
              .put("lease_id", lease.getString("lease_id"))
              .put("base_revision", lease.getLong("revision"))
              .put("files", files(version)),
          16384);
    } finally {
      api.post(
          s.api(path + "/release"),
          new JSONObject().put("lease_id", lease.getString("lease_id")),
          16384);
    }
  }

  private void open(ServerConfig s, JSONObject game) throws Exception {
    inst.runOnMainSync(() -> new SaveSyncFlow(activity, worker, s, game, "测试玩家").begin());
    click("已退出，比较存档");
    waitFor("存档不同");
  }

  @Test
  public void chooseCloudOrLocalAndRejectConcurrentChanges() throws Exception {
    String base = InstrumentationRegistry.getArguments().getString("fixtureBase", "");
    Assume.assumeTrue(!base.isEmpty());
    inst = InstrumentationRegistry.getInstrumentation();
    context = inst.getTargetContext();
    worker = Executors.newSingleThreadExecutor();
    context.startActivity(
        new Intent()
            .setClassName(
                "io.retroone.android.test", "io.retroone.android.SaveFixtureGrantActivity")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    Thread.sleep(800);
    ServerConfig source = new ServerConfig(base, true, "");
    JSONObject game =
        new ApiClient(source)
            .json(source.api("games?q=RetroOne-ISO-Self-Test"))
            .getJSONArray("games")
            .getJSONObject(0);
    String id = game.getString("id");
    android.content.SharedPreferences prefs = context.getSharedPreferences("psp-save-sync", 0);
    String oldTree = prefs.getString("tree", ""),
        key = source.key() + "." + id,
        oldBinding = prefs.getString(key, "");
    prefs.edit().putString("tree", tree.toString()).remove(key).commit();
    activity =
        inst.startActivitySync(
            new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    DocumentFile root = DocumentFile.fromTreeUri(context, tree);
    for (DocumentFile f : root.listFiles()) assertTrue(f.delete());
    DocumentFile other = root.createDirectory("OTHR99999_Save");
    DocumentFile safe = other.createFile("application/octet-stream", "KEEP.BIN");
    try (OutputStream out = context.getContentResolver().openOutputStream(safe.getUri(), "wt")) {
      out.write(77);
    }
    try {
      local(1);
      cloud(source, id, 2);
      open(source, game);
      click("暂不同步");
      PspSaveDirectory dir = new PspSaveDirectory(context, tree);
      assertEquals(
          SaveSnapshot.hash(files(1)),
          SaveSnapshot.hash(dir.read("TEST00001").getJSONArray("files")));
      open(source, game);
      Thread.sleep(600);
      android.graphics.Bitmap shot = inst.getUiAutomation().takeScreenshot();
      File qa = new File(context.getFilesDir(), "qa");
      qa.mkdirs();
      try (FileOutputStream out = new FileOutputStream(new File(qa, "save-conflict.png"))) {
        shot.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out);
      }
      click("使用云端进度 → 本机");
      waitFor("已同步本机与云端进度");
      click("知道了");
      assertEquals(
          SaveSnapshot.hash(files(2)),
          SaveSnapshot.hash(dir.read("TEST00001").getJSONArray("files")));
      local(3);
      open(source, game);
      click("使用本机进度 → 云端");
      waitFor("已同步本机与云端进度");
      click("知道了");
      assertEquals(
          SaveSnapshot.hash(files(3)),
          SaveSnapshot.hash(SaveBackup.fetch(source, id).getJSONArray("files")));
      local(4);
      open(source, game);
      cloud(source, id, 5);
      click("使用本机进度 → 云端");
      waitFor("云端存档在选择期间发生变化");
      click("知道了");
      assertEquals(
          SaveSnapshot.hash(files(5)),
          SaveSnapshot.hash(SaveBackup.fetch(source, id).getJSONArray("files")));
      open(source, game);
      local(6);
      click("使用云端进度 → 本机");
      waitFor("本机存档在选择期间发生变化");
      click("知道了");
      assertEquals(
          SaveSnapshot.hash(files(6)),
          SaveSnapshot.hash(dir.read("TEST00001").getJSONArray("files")));
      open(source, game);
      context.getContentResolver().call(tree, "testFailRename", null, null);
      click("使用云端进度 → 本机");
      waitFor("无法启用新存档");
      click("知道了");
      assertEquals(
          SaveSnapshot.hash(files(6)),
          SaveSnapshot.hash(dir.read("TEST00001").getJSONArray("files")));
      for (DocumentFile f : root.listFiles()) assertFalse(f.getName().startsWith("RO_"));
      try (InputStream in = context.getContentResolver().openInputStream(safe.getUri())) {
        assertEquals(77, in.read());
      }
      assertFalse(
          new ApiClient(source)
              .json(source.api("games/" + id + "/cloud-save"))
              .getBoolean("leased"));
      assertTrue(
          new SaveLibrary(context.getFilesDir())
              .all().stream().anyMatch(e -> "local".equals(e.info.optString("origin"))));
    } finally {
      prefs.edit().putString("tree", oldTree).putString(key, oldBinding).commit();
      worker.shutdownNow();
      inst.runOnMainSync(activity::finish);
      for (DocumentFile f : root.listFiles()) f.delete();
    }
  }
}
