package io.retroone.android;

import static org.junit.Assert.*;

import java.io.IOException;
import org.junit.Test;

public class RetroArchPolicyTest {
  @Test
  public void dependenciesAreSeparateAndOnlyNesIsEnabled() {
    assertTrue(RetroArchPolicy.supports("nes"));
    for (String platform :
        new String[] {"snes", "gb", "gbc", "gba", "megadrive", "psp", "switch", "fbneo"})
      assertFalse(platform, RetroArchPolicy.supports(platform));
    assertEquals(
        LaunchPolicy.State.INCOMPLETE,
        RetroArchPolicy.assess("game.nes", false, true, true, true, true));
    assertEquals(
        LaunchPolicy.State.MISSING,
        RetroArchPolicy.assess("game.nes", true, false, true, true, true));
    assertEquals(
        LaunchPolicy.State.PLAYABLE,
        RetroArchPolicy.assess("game.zip", true, true, true, true, true));
    assertEquals(
        LaunchPolicy.State.MANUAL,
        RetroArchPolicy.assess("game.fds", true, true, true, true, true));
    assertEquals(
        LaunchPolicy.State.INSTALL_RETROARCH,
        RetroArchPolicy.assess("game.nes", true, true, false, true, true));
    assertEquals(
        LaunchPolicy.State.CONFIGURE_RETROARCH,
        RetroArchPolicy.assess("game.nes", true, true, true, false, true));
    assertEquals(
        LaunchPolicy.State.VERIFY_RETROARCH,
        RetroArchPolicy.assess("game.nes", true, true, true, true, false));
    assertEquals(
        LaunchPolicy.State.PLAYABLE,
        RetroArchPolicy.assess("GAME.NES", true, true, true, true, true));
    assertEquals("", LaunchPolicy.formatReason("nes", "GAME.NES"));
    assertFalse(LaunchPolicy.formatReason("fbneo", "game.nes").isEmpty());
  }

  @Test
  public void sharedPathsCannotEscapePrimaryUserFolder() throws Exception {
    assertEquals("ROMs/我的游戏", RetroArchPolicy.relativeDirectory("primary:ROMs/我的游戏"));
    for (String path :
        new String[] {
          "primary:",
          "primary:/ROMs",
          "primary:ROMs/",
          "primary:../outside",
          "primary:ROMs/../Android",
          "primary:ROMs//NES",
          "primary:Android/data",
          "primary:android/obb",
          "primary:ROMs\\other",
          "primary:ROMs/\nNES",
          "ABCD:ROMs",
          "raw:/storage/emulated/0/ROMs"
        }) {
      try {
        RetroArchPolicy.relativeDirectory(path);
        fail(path);
      } catch (IOException expected) {
      }
    }
    assertEquals("ro-" + "a".repeat(64) + ".nes", RetroArchPolicy.fileName("a".repeat(64)));
    try {
      RetroArchPolicy.fileName("../../file");
      fail();
    } catch (IOException expected) {
    }
  }

  @Test
  public void extensionDoesNotSubstituteForPayloadValidation() throws Exception {
    byte[] header = {'N', 'E', 'S', 0x1a, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    RetroArchPolicy.validateHeader(header, 24592);
    try {
      RetroArchPolicy.validateHeader(header, 20000);
      fail();
    } catch (IOException expected) {
    }
    header[6] = 4;
    try {
      RetroArchPolicy.validateHeader(header, 24592);
      fail();
    } catch (IOException expected) {
    }
    RetroArchPolicy.validateHeader(header, 24592 + 512);
    header[0] = 'Z';
    try {
      RetroArchPolicy.validateHeader(header, 25000);
      fail();
    } catch (IOException expected) {
    }
  }
}
