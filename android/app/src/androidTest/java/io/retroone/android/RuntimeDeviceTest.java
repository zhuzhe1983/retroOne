package io.retroone.android;

import static org.junit.Assert.*;

import android.content.Context;
import android.content.Intent;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.IOException;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Opt-in existing-download preparation. Successful dispatch does not mark a game playable. */
@RunWith(AndroidJUnit4.class)
public class RuntimeDeviceTest {
  @Test
  public void explicitReadyTaskRoundTrip() throws Exception {
    android.os.Bundle args = InstrumentationRegistry.getArguments();
    org.junit.Assume.assumeTrue(
        "Opt in only with an authorized shared folder and a saved/closed previous game",
        "true".equals(args.getString("runtimeFolder")));
    String id = args.getString("runtimeTaskId", "");
    assertTrue(
        "Exact downloaded task UUID required",
        id.matches("[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    DownloadStore.Item item = RetroApplication.from(context).downloads.get(id);
    assertNotNull(item);
    assertEquals(DownloadStore.READY, item.state);
    RuntimePolicy.Spec spec = RuntimePolicy.spec(item.platform);
    assertNotNull(spec);
    RuntimeAdapter adapter = new RuntimeAdapter(context);
    boolean wasVerified = adapter.verified(item);
    boolean nesVerified = adapter.retroarch.verified();
    String before = adapter.fingerprint(item);
    String path = adapter.prepare(item);
    assertEquals(path, adapter.prepare(item));
    Intent intent = adapter.retroarch.intent(path, item.platform);
    assertEquals(path, intent.getStringExtra("ROM"));
    assertTrue(intent.getStringExtra("LIBRETRO").endsWith("/cores/" + spec.core));
    assertEquals(adapter.retroarch.configPath(), intent.getStringExtra("CONFIGFILE"));
    assertEquals(adapter.retroarch.externalFilesPath(), intent.getStringExtra("EXTERNAL"));
    assertTrue(intent.hasExtra("DATADIR"));
    assertTrue(intent.hasExtra("APK"));
    assertTrue(intent.hasExtra("SDCARD"));
    assertTrue(intent.hasExtra("IME"));
    assertEquals(before, adapter.fingerprint(item));
    assertEquals(wasVerified, adapter.verified(item));
    assertEquals(nesVerified, adapter.retroarch.verified());
    try {
      adapter.retroarch.intent(path + "/../other", item.platform);
      fail("Accepted traversal");
    } catch (IOException expected) {
    }
    try {
      adapter.retroarch.intent(path, "nes");
      fail("Accepted another platform");
    } catch (IOException expected) {
    }
    if ("true".equals(args.getString("launchRuntime"))) {
      context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      android.util.Log.i(
          "RetroOneTest",
          "Runtime dispatched only. Verify real frame/input and dependencies separately; no"
              + " gameplay proof saved.");
    }
  }
}
