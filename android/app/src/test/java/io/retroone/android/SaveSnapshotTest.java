package io.retroone.android;

import static org.junit.Assert.*;

import org.json.*;
import org.junit.Test;

public class SaveSnapshotTest {
  private JSONObject file(String path, String data) throws Exception {
    return new JSONObject().put("path", path).put("data", data);
  }

  @Test
  public void comparesActualBytesWithoutTrustingOrderOrClock() throws Exception {
    JSONObject a = file("UCAS40063_GameData0/DATA.BIN", "AQI="),
        b = file("UCAS40063_LocoHouse000/DATA.BIN", "AwQ=");
    JSONArray one = new JSONArray().put(a).put(b), reverse = new JSONArray().put(b).put(a);
    assertEquals(SaveSnapshot.hash(one), SaveSnapshot.hash(reverse));
    assertEquals("UCAS40063", SaveSnapshot.prefix(one));
    JSONObject before =
        new JSONObject().put("files", one).put("revision", 2).put("updated_at", 999);
    JSONObject same =
        new JSONObject().put("files", reverse).put("revision", 2).put("updated_at", 1);
    SaveSnapshot.unchanged(before, same, true);
    same.put("revision", 3);
    assertThrows(java.io.IOException.class, () -> SaveSnapshot.unchanged(before, same, true));
    SaveSnapshot.unchanged(before, same, false);
    assertNotEquals(
        SaveSnapshot.hash(one),
        SaveSnapshot.hash(new JSONArray().put(file("UCAS40063_GameData0/DATA.BIN", "AQM="))));
  }

  @Test
  public void rejectsUnrelatedGamesAndTraversal() throws Exception {
    JSONArray mixed =
        new JSONArray()
            .put(file("UCAS40063_GameData0/DATA.BIN", "AQI="))
            .put(file("ULUS10340/DATA.BIN", "AQI="));
    assertThrows(java.io.IOException.class, () -> SaveSnapshot.prefix(mixed));
    JSONArray escape = new JSONArray().put(file("UCAS40063/../prod.keys", "AQI="));
    assertThrows(java.io.IOException.class, () -> SaveSnapshot.hash(escape));
  }
}
