package io.retroone.android;

/** Current transfer, not a misleading average that omits queued files of unknown size. */
final class DownloadSummary {
  int active, ready, failed, paused, percent = -1;
  private int priority = -1;
  String phase = "idle";

  void add(String state, long bytes, long size, boolean unseen) {
    int rank =
        switch (state) {
          case "running" -> 3;
          case "preparing" -> 2;
          case "queued" -> 1;
          default -> 0;
        };
    if (rank > 0) {
      active++;
      if (rank > priority) {
        priority = rank;
        phase = state;
        percent =
            rank == 3 && size > 0
                ? (int) Math.min(99, Math.max(0, bytes) / (double) size * 100)
                : -1;
      }
    }
    if ("ready".equals(state) && unseen) ready++;
    if ("failed".equals(state)) failed++;
    if ("paused".equals(state)) paused++;
  }

  String label() {
    if (active > 0) return percent >= 0 ? percent + "%" : "preparing".equals(phase) ? "准备" : "排队";
    if (ready > 0) return "✓";
    if (failed > 0) return "!";
    if (paused > 0) return "Ⅱ";
    return "↓";
  }

  int count() {
    return active > 0 ? active : ready > 0 ? ready : failed > 0 ? failed : paused;
  }

  String description() {
    if (active > 0) return "下载进度：" + active + " 个任务，当前" + (percent < 0 ? label() : percent + "%");
    if (ready > 0) return "下载完成：" + ready + " 款，点击查看";
    if (failed > 0) return failed + " 个下载失败，点击重试";
    if (paused > 0) return paused + " 个下载已暂停";
    return "下载管理";
  }
}
