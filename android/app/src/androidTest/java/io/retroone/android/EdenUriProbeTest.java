package io.retroone.android;

import static org.junit.Assert.*;

import android.content.ClipData;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import androidx.core.content.FileProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.util.Locale;
import java.util.UUID;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Test APK only. Dispatch one explicitly named, already-downloaded Switch file to Eden. */
@RunWith(AndroidJUnit4.class)
public class EdenUriProbeTest {
  @Test
  public void explicitReadyTaskReadOnlyUri() throws Exception {
    android.os.Bundle arguments = InstrumentationRegistry.getArguments();
    org.junit.Assume.assumeTrue(
        "Explicit opt-in required; first save and exit any current Eden game",
        "true".equals(arguments.getString("edenUriProbe")));
    String id = arguments.getString("edenTaskId", "");
    assertTrue(
        "Pass one exact downloaded task UUID, never a title or auto-selected game",
        id.matches("[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"));
    Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    DownloadStore store = RetroApplication.from(context).downloads;
    DownloadStore.Item item = store.get(id);
    assertNotNull("Task not found; this probe does not download files", item);
    assertEquals("switch", item.platform);
    assertEquals("File must already be downloaded and verified", DownloadStore.READY, item.state);
    String name = item.name.toLowerCase(Locale.ROOT);
    assertTrue(
        "Only standalone NSP/XCI are part of this probe",
        name.endsWith(".nsp") || name.endsWith(".xci"));
    File file = store.complete(item);
    assertTrue(file.isFile());
    assertEquals(item.size, file.length());
    Uri uri =
        FileProvider.getUriForFile(context, context.getPackageName() + ".games", file)
            .buildUpon()
            .appendQueryParameter("launch", UUID.randomUUID().toString())
            .build();
    try (android.os.ParcelFileDescriptor descriptor =
        context.getContentResolver().openFileDescriptor(uri, "r")) {
      assertNotNull(descriptor);
      assertEquals(item.size, descriptor.getStatSize());
    }
    Intent launch =
        new Intent(Intent.ACTION_VIEW)
            .setComponent(
                new ComponentName(
                    "dev.eden.eden_emulator", "org.yuzu.yuzu_emu.activities.EmulationActivity"))
            .setDataAndType(uri, "application/octet-stream")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
    launch.setClipData(ClipData.newRawUri("RetroOne explicit Eden probe", uri));
    assertEquals(0, launch.getFlags() & Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
    context.startActivity(launch);
    android.util.Log.i(
        "RetroOneTest",
        "Eden readonly URI dispatched; inspect actual game frame, input and logs separately.");
    // No save/firmware writes, process stops, config changes, or copying large images here.
    // Eden controls its own lifecycle and may record recently played content as usual.
  }
}
