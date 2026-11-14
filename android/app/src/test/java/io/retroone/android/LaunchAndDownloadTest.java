package io.retroone.android;

import static org.junit.Assert.*;

import org.junit.Test;

public class LaunchAndDownloadTest {
  @Test
  public void playRequiresCompleteExistingFileAndMatchingInstalledEmulator() {
    assertEquals(
        LaunchPolicy.State.PLAYABLE,
        LaunchPolicy.assess("psp", "game.iso", 1048576, true, true, true));
    assertEquals(
        LaunchPolicy.State.INCOMPLETE,
        LaunchPolicy.assess("psp", "game.iso", 1048576, false, true, true));
    assertEquals(
        LaunchPolicy.State.MISSING,
        LaunchPolicy.assess("psp", "game.iso", 1048576, true, false, true));
    assertEquals(
        LaunchPolicy.State.INSTALL,
        LaunchPolicy.assess("psp", "game.iso", 1048576, true, true, false));
    assertEquals(
        LaunchPolicy.State.UNSUPPORTED,
        LaunchPolicy.assess("ps2", "game.iso", 1048576, true, true, true));
    assertEquals(
        LaunchPolicy.State.UNSUPPORTED,
        LaunchPolicy.assess("switch", "game.xci", 6314501120L, true, true, true));
  }

  @Test
  public void formatsWithKnownUriProblemsNeverOfferDirectPlay() {
    assertEquals(
        LaunchPolicy.State.MANUAL,
        LaunchPolicy.assess("psp", "EBOOT.PBP", 1048576, true, true, true));
    assertEquals(
        LaunchPolicy.State.MANUAL,
        LaunchPolicy.assess("psp", "game.zip", 1048576, true, true, true));
    assertEquals(
        LaunchPolicy.State.MANUAL,
        LaunchPolicy.assess("psp", "tiny.cso", 0x87ff, true, true, true));
    assertEquals(
        LaunchPolicy.State.PLAYABLE,
        LaunchPolicy.assess("psp", "game.CSO", 0x8800, true, true, true));
  }

  @Test
  public void queueOfUnknownSizesDoesNotMisrepresentOverallProgress() {
    DownloadSummary summary = new DownloadSummary();
    summary.add("queued", 0, 0, false);
    summary.add("running", 2_000_000_000L, 4_000_000_000L, false);
    summary.add("ready", 100, 100, false);
    assertEquals(2, summary.active);
    assertEquals("50%", summary.label());
    assertTrue(summary.description().contains("当前50%"));
    DownloadSummary preparing = new DownloadSummary();
    preparing.add("preparing", 0, 0, false);
    assertEquals(-1, preparing.percent);
    assertEquals("准备", preparing.label());
  }

  @Test
  public void hundredPercentIsReservedForVerifiedCompletionAndCanceledTasksDisappear() {
    DownloadSummary checking = new DownloadSummary();
    checking.add("running", Long.MAX_VALUE, Long.MAX_VALUE, false);
    assertEquals("99%", checking.label());
    DownloadSummary done = new DownloadSummary();
    done.add("ready", 1, 1, true);
    done.add("canceled", 1, 1, true);
    assertEquals("✓", done.label());
    assertEquals(1, done.count());
    DownloadSummary acknowledged = new DownloadSummary();
    acknowledged.add("ready", 1, 1, false);
    assertEquals("↓", acknowledged.label());
    assertEquals(0, acknowledged.count());
    acknowledged.add("paused", 0, 100, false);
    assertEquals("Ⅱ", acknowledged.label());
    acknowledged.add("failed", 0, 100, false);
    assertEquals("!", acknowledged.label());
  }
}
