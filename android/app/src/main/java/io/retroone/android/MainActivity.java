package io.retroone.android;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.util.LruCache;
import android.view.Gravity;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.widget.*;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

/** Native, keyboard-focusable UI. Remote catalog and on-device downloads have separate states. */
public final class MainActivity extends Activity {
  static final int BG = 0xff101722,
      PANEL = 0xff1b2939,
      TEXT = 0xffedf4fa,
      MUTED = 0xff9eb1c6,
      ACCENT = 0xff66d8c0;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private final ExecutorService io = Executors.newFixedThreadPool(2),
      images = Executors.newFixedThreadPool(2);
  private final LruCache<String, Bitmap> artwork = new LruCache<>(32);
  private final List<JSONObject> games = new ArrayList<>();
  private final List<String> platformIds = new ArrayList<>();
  private LinearLayout root, body, grid;
  private TextView status, connection;
  private ScrollView scroll;
  private EditText search;
  private Spinner platform;
  private ServerConfig config;
  private DownloadStore store;
  private String tab = "library", query = "", platformId = "", downloadSignature = "", exportId;
  private String scope = "all", sort = "popularity", direction = "desc", profileName = "选择玩家";
  private String saveExportPath, profileRequestedBase;
  private Button profileButton;
  private int requestVersion, total, pageOffset;
  private boolean destroyed, suppressPlatform;
  private final Runnable searchLater = () -> loadGames(false);
  private final Runnable poll =
      new Runnable() {
        public void run() {
          if (!destroyed) {
            if ("downloads".equals(tab)) renderDownloads(false);
            handler.postDelayed(this, 800);
          }
        }
      };

  @Override
  public void onCreate(Bundle state) {
    super.onCreate(state);
    store = RetroApplication.from(this).downloads;
    try {
      config = RetroApplication.from(this).credentials.current();
    } catch (Exception ignored) {
    }
    readLibraryPreferences();
    if (state != null) {
      tab = state.getString("tab", "library");
      query = state.getString("query", "");
      platformId = state.getString("platform", "");
      exportId = state.getString("exportId");
      saveExportPath = state.getString("saveExportPath");
    }
    if (getIntent().getBooleanExtra("downloads", false)) tab = "downloads";
    root = column();
    root.setBackgroundColor(BG);
    root.setPadding(dp(22), dp(12), dp(22), 0);
    root.setOnApplyWindowInsetsListener(
        (v, insets) -> {
          root.setPadding(
              dp(22) + insets.getSystemWindowInsetLeft(),
              dp(8) + insets.getSystemWindowInsetTop(),
              dp(22) + insets.getSystemWindowInsetRight(),
              insets.getSystemWindowInsetBottom());
          return insets;
        });
    setContentView(root);
    LinearLayout header = row();
    TextView brand = text("RETROONE", 25, TEXT);
    brand.setTypeface(null, Typeface.BOLD);
    if (getResources().getConfiguration().screenWidthDp < 600) root.addView(brand);
    else header.addView(brand, new LinearLayout.LayoutParams(0, dp(48), 1));
    header.addView(button("Games", () -> show("library")));
    header.addView(button("Apps", () -> show("apps")));
    profileButton =
        button(
            profileName,
            () -> {
              if (config != null) chooseProfile();
              else show("settings");
            });
    header.addView(profileButton);
    header.addView(button("本机与下载", () -> show("downloads")));
    header.addView(button("连接设置", () -> show("settings")));
    if (getResources().getConfiguration().screenWidthDp < 700) {
      HorizontalScrollView nav = new HorizontalScrollView(this);
      nav.addView(header);
      root.addView(nav);
    } else root.addView(header);
    connection = text("", 12, MUTED);
    root.addView(connection);
    body = column();
    root.addView(body, new LinearLayout.LayoutParams(-1, 0, 1));
    show(tab);
    handler.post(poll);
  }

  @Override
  protected void onNewIntent(Intent intent) {
    super.onNewIntent(intent);
    setIntent(intent);
    if (intent.getBooleanExtra("downloads", false)) show("downloads");
  }

  @Override
  protected void onSaveInstanceState(Bundle state) {
    state.putString("tab", tab);
    state.putString("query", query);
    state.putString("platform", platformId);
    state.putString("exportId", exportId);
    state.putString("saveExportPath", saveExportPath);
    super.onSaveInstanceState(state);
  }

  @Override
  protected void onDestroy() {
    destroyed = true;
    handler.removeCallbacksAndMessages(null);
    io.shutdownNow();
    images.shutdownNow();
    super.onDestroy();
  }

  private void ui(Runnable action) {
    handler.post(
        () -> {
          if (!destroyed) action.run();
        });
  }

  private void show(String page) {
    requestVersion++;
    handler.removeCallbacks(searchLater);
    tab = page;
    body.removeAllViews();
    connection.setText(config == null ? "私人游戏馆 · 连接你的 RetroOne 服务" : "私人游戏馆  /  " + config.base);
    if ("apps".equals(page)) {
      apps();
      return;
    }
    if ("settings".equals(page)) {
      settings();
      return;
    }
    if ("downloads".equals(page)) {
      scroll = new ScrollView(this);
      grid = column();
      scroll.addView(grid);
      body.addView(scroll);
      downloadSignature = "";
      renderDownloads(true);
      return;
    }
    library();
  }

  private void library() {
    TextView title = text("你的收藏，随时开玩", 28, TEXT);
    title.setTypeface(null, Typeface.BOLD);
    body.addView(title);
    LinearLayout choices = row();
    String[] scopes = {"all", "favorites", "recent"};
    String[] labels = {"全部游戏", "我的收藏", "最近游玩"};
    for (int i = 0; i < scopes.length; i++) {
      String value = scopes[i];
      choices.addView(
          button(
              (scope.equals(value) ? "● " : "") + labels[i],
              () -> {
                scope = value;
                writeLibraryPreferences();
                show("library");
              }));
    }
    choices.addView(
        button(
            "排序：" + LibraryQuery.LABELS[java.util.Arrays.asList(LibraryQuery.SORTS).indexOf(sort)],
            this::chooseSort));
    choices.addView(
        button(
            "asc".equals(direction) ? "升序 ↑" : "降序 ↓",
            () -> {
              direction = "asc".equals(direction) ? "desc" : "asc";
              writeLibraryPreferences();
              show("library");
            }));
    HorizontalScrollView choiceScroll = new HorizontalScrollView(this);
    choiceScroll.addView(choices);
    body.addView(choiceScroll);
    LinearLayout filters = row();
    search = edit("搜索游戏名称", false);
    search.setText(query);
    search.setSingleLine(true);
    search.setImeOptions(android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH);
    boolean narrow = getResources().getConfiguration().screenWidthDp < 600;
    if (narrow) body.addView(search, new LinearLayout.LayoutParams(-1, dp(56)));
    else filters.addView(search, new LinearLayout.LayoutParams(0, dp(56), 1));
    platform = new Spinner(this);
    filters.addView(platform, new LinearLayout.LayoutParams(dp(200), dp(56)));
    filters.addView(
        button(
            "刷新",
            () -> {
              ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE))
                  .hideSoftInputFromWindow(search.getWindowToken(), 0);
              loadGames(false);
            }));
    body.addView(filters);
    status = text("", 13, MUTED);
    body.addView(status);
    scroll = new ScrollView(this);
    grid = column();
    scroll.addView(grid);
    body.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
    search.addTextChangedListener(
        new TextWatcher() {
          public void beforeTextChanged(CharSequence s, int st, int c, int a) {}

          public void onTextChanged(CharSequence s, int st, int b, int c) {
            query = s.toString();
            handler.removeCallbacks(searchLater);
            handler.postDelayed(searchLater, 400);
          }

          public void afterTextChanged(Editable s) {}
        });
    search.setOnEditorActionListener(
        (v, action, event) -> {
          handler.removeCallbacks(searchLater);
          loadGames(false);
          return true;
        });
    if (config == null) {
      status.setText("尚未连接服务器。本机下载可离线查看。");
      grid.addView(button("配置服务地址", () -> show("settings")));
      return;
    }
    refreshProfile();
    loadPlatforms();
    loadGames(false);
  }

  private void loadPlatforms() {
    ServerConfig current = config;
    Spinner target = platform;
    io.execute(
        () -> {
          try {
            JSONArray rows =
                new ApiClient(current).json(current.api("platforms")).getJSONArray("platforms");
            List<String> names = new ArrayList<>(), ids = new ArrayList<>();
            names.add("全部平台");
            ids.add("");
            for (int i = 0; i < rows.length(); i++) {
              JSONObject p = rows.getJSONObject(i);
              ids.add(p.getString("id"));
              names.add(p.getString("name") + " · " + p.optInt("count"));
            }
            ui(
                () -> {
                  if (target != platform || !"library".equals(tab)) return;
                  suppressPlatform = true;
                  platformIds.clear();
                  platformIds.addAll(ids);
                  target.setAdapter(
                      new ArrayAdapter<>(
                          this, android.R.layout.simple_spinner_dropdown_item, names));
                  target.setSelection(Math.max(0, ids.indexOf(platformId)));
                  target.post(() -> suppressPlatform = false);
                  target.setOnItemSelectedListener(
                      new AdapterView.OnItemSelectedListener() {
                        public void onNothingSelected(AdapterView<?> p) {}

                        public void onItemSelected(
                            AdapterView<?> p, View v, int position, long id) {
                          if (!suppressPlatform && !platformId.equals(platformIds.get(position))) {
                            platformId = platformIds.get(position);
                            loadGames(false);
                          }
                        }
                      });
                });
          } catch (Exception ignored) {
            /* Games request owns the visible connection error. */
          }
        });
  }

  private void loadGames(boolean more) {
    loadPage(more ? pageOffset + 60 : 0);
  }

  private void loadPage(int offset) {
    if (config == null || !"library".equals(tab)) return;
    int version = ++requestVersion;
    ServerConfig current = config;
    String route = LibraryQuery.route(query, platformId, scope, sort, direction, offset);
    writeLibraryPreferences();
    status.setText("正在读取游戏库…");
    io.execute(
        () -> {
          try {
            JSONObject result = new ApiClient(current).json(current.api(route));
            JSONArray values = result.getJSONArray("games");
            List<JSONObject> items = new ArrayList<>();
            for (int i = 0; i < values.length(); i++) items.add(values.getJSONObject(i));
            ui(
                () -> {
                  if (version != requestVersion || !"library".equals(tab)) return;
                  games.clear();
                  games.addAll(items);
                  pageOffset = offset;
                  total = result.optInt("total", games.size());
                  if (offset > 0 && offset >= total) {
                    loadPage(Math.max(0, ((total - 1) / 60) * 60));
                    return;
                  }
                  status.setText("共 " + total + " 款 · 第 " + (offset / 60 + 1) + " 页");
                  renderGames(false);
                });
          } catch (Exception e) {
            ui(
                () -> {
                  if (version != requestVersion || !"library".equals(tab)) return;
                  status.setText(ApiClient.message(e));
                  games.clear();
                  grid.removeAllViews();
                  grid.addView(button("重试", () -> loadPage(offset)));
                });
          }
        });
  }

  private void renderGames(boolean more) {
    int y = more ? scroll.getScrollY() : 0;
    grid.removeAllViews();
    if (games.isEmpty()) {
      grid.addView(
          text(query.isEmpty() ? "此平台还没有游戏。请在服务端导入自己的收藏。" : "没有匹配的游戏，试试其他名称或平台。", 17, MUTED));
      return;
    }
    int width = getResources().getConfiguration().screenWidthDp - 44;
    int columns = Math.max(1, Math.min(6, width / 190));
    List<DownloadStore.Item> localItems = store.all();
    boolean hasEmulator = !Emulators.installed(this).isEmpty();
    LinearLayout line = null;
    for (int i = 0; i < games.size(); i++) {
      JSONObject game = games.get(i);
      if (i % columns == 0) {
        line = row();
        grid.addView(line);
      }
      LinearLayout card = column();
      card.setPadding(dp(12), dp(12), dp(12), dp(14));
      card.setBackground(shape(PANEL, 12));
      LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(0, -2, 1);
      cp.setMargins(dp(4), dp(6), dp(4), dp(6));
      line.addView(card, cp);
      FrameLayout cover = new FrameLayout(this);
      cover.setBackground(shape(0xff263b50, 8));
      card.addView(cover, new LinearLayout.LayoutParams(-1, dp(132)));
      TextView monogram =
          text(
              game.optString("platform_name", game.optString("platform")).toUpperCase(Locale.ROOT),
              22,
              ACCENT);
      monogram.setGravity(Gravity.CENTER);
      cover.addView(monogram, new FrameLayout.LayoutParams(-1, -1));
      String coverUrl = game.optString("cover_url", "");
      if (!coverUrl.isEmpty() && !"null".equals(coverUrl)) {
        ImageView image = new ImageView(this);
        image.setScaleType(ImageView.ScaleType.CENTER_CROP);
        cover.addView(image, new FrameLayout.LayoutParams(-1, -1));
        loadCover(image, coverUrl, config);
      }
      TextView name =
          text((game.optBoolean("favorite") ? "★ " : "") + game.optString("title"), 16, TEXT);
      name.setMaxLines(2);
      name.setMinHeight(dp(48));
      name.setTypeface(null, Typeface.BOLD);
      card.addView(name);
      card.addView(
          text(
              game.optString("platform_name", game.optString("platform"))
                  + "  ·  "
                  + bytes(game.optLong("size")),
              12,
              MUTED));
      String localLabel =
          "psp".equals(game.optString("platform"))
              ? (hasEmulator ? "未下载" : "需安装 PPSSPP")
              : "尚未接入本机模拟器";
      for (DownloadStore.Item item : localItems) {
        if (item.base.equals(config.base)
            && item.game.equals(game.optString("id"))
            && !DownloadStore.CANCELED.equals(item.state)) {
          localLabel =
              switch (item.state) {
                case DownloadStore.READY -> "已下载";
                case DownloadStore.RUNNING, DownloadStore.PREPARING, DownloadStore.QUEUED -> "下载中";
                case DownloadStore.PAUSED -> "下载已暂停";
                default -> "下载需重试";
              };
          break;
        }
      }
      card.addView(text(localLabel, 12, ACCENT));
      card.setFocusable(true);
      card.setClickable(true);
      card.setContentDescription(game.optString("title") + "，查看详情");
      card.setForeground(getDrawable(android.R.drawable.list_selector_background));
      card.setOnClickListener(v -> detail(game));
    }
    if (line != null)
      for (int i = games.size() % columns; i > 0 && i < columns; i++)
        line.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
    LinearLayout pages = row();
    grid.addView(pages);
    if (pageOffset > 0) pages.addView(button("上一页", () -> loadPage(Math.max(0, pageOffset - 60))));
    if (pageOffset + games.size() < total) pages.addView(button("下一页", () -> loadGames(true)));
    scroll.post(() -> scroll.scrollTo(0, y));
  }

  private void loadCover(ImageView view, String value, ServerConfig source) {
    final String url;
    try {
      url = source.resolve(value).toString();
    } catch (Exception e) {
      return;
    }
    Bitmap cached = artwork.get(url);
    if (cached != null) {
      view.setImageBitmap(cached);
      return;
    }
    images.execute(
        () -> {
          HttpURLConnection c = null;
          try {
            c = new ApiClient(source).open(java.net.URI.create(url), null);
            if (c.getResponseCode() != 200) return;
            byte[] data;
            try (java.io.InputStream input = c.getInputStream()) {
              data = ApiClient.read(input, 3 * 1024 * 1024);
            }
            BitmapFactory.Options options = new BitmapFactory.Options();
            options.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(data, 0, data.length, options);
            if (options.outWidth <= 0 || options.outHeight <= 0) return;
            options.inSampleSize = Math.max(1, Math.max(options.outWidth, options.outHeight) / 512);
            options.inJustDecodeBounds = false;
            Bitmap bitmap = BitmapFactory.decodeByteArray(data, 0, data.length, options);
            if (bitmap != null) {
              artwork.put(url, bitmap);
              ui(() -> view.setImageBitmap(bitmap));
            }
          } catch (Exception ignored) {
          } finally {
            if (c != null) c.disconnect();
          }
        });
  }

  private void detail(JSONObject basic) {
    ServerConfig source = config;
    LinearLayout content = column();
    content.setPadding(dp(22), dp(10), dp(22), dp(18));
    TextView info = text("正在读取游戏详情与下载资格…", 15, MUTED);
    content.addView(info);
    ScrollView detailScroll = new ScrollView(this);
    detailScroll.addView(content);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle(basic.optString("title"))
            .setView(detailScroll)
            .setNegativeButton("返回", null)
            .create();
    dialog.show();
    io.execute(
        () -> {
          try {
            ApiClient api = new ApiClient(source);
            String path = "games/" + ServerConfig.encode(basic.getString("id"));
            JSONObject game = api.json(source.api(path)),
                options = api.json(source.api(path + "/download-options"));
            ui(
                () -> {
                  if (!dialog.isShowing()) return;
                  String desc = game.optString("description", "");
                  if (desc.length() > 1200) desc = desc.substring(0, 1200) + "…";
                  String name = options.optString("name", "");
                  String reason = Emulators.support(game.optString("platform"), name);
                  info.setText(
                      game.optString("platform_name", game.optString("platform"))
                          + "  ·  "
                          + bytes(options.optLong("size_bytes", game.optLong("size")))
                          + "\n\n"
                          + (desc.isEmpty() ? "暂无游戏介绍。" : desc)
                          + "\n\n"
                          + (reason.isEmpty()
                              ? "PPSSPP 接口可接收此格式。具体游戏兼容性由模拟器与文件决定；此处不代表已验证可玩。"
                              : reason)
                          + "\n\n本机模拟器管理游玩存档；云存档可在下方查看并下载备份。");
                  Button favorite =
                      button(game.optBoolean("favorite") ? "★ 已收藏" : "☆ 收藏", () -> {});
                  favorite.setOnClickListener(
                      v -> {
                        favorite.setEnabled(false);
                        io.execute(
                            () -> {
                              try {
                                JSONObject result =
                                    new ApiClient(source)
                                        .post(
                                            source.api(path + "/favorite"),
                                            new JSONObject()
                                                .put("favorite", !game.optBoolean("favorite")),
                                            16384);
                                game.put("favorite", result.getBoolean("favorite"));
                                ui(
                                    () -> {
                                      favorite.setText(
                                          game.optBoolean("favorite") ? "★ 已收藏" : "☆ 收藏");
                                      favorite.setEnabled(true);
                                      if (config != null && config.base.equals(source.base))
                                        loadPage(pageOffset);
                                    });
                              } catch (Exception e) {
                                ui(
                                    () -> {
                                      favorite.setEnabled(true);
                                      alert(ApiClient.message(e));
                                    });
                              }
                            });
                      });
                  content.addView(favorite);
                  content.addView(button("云存档", () -> cloudSave(source, game)));
                  content.addView(text("已记录游玩 " + game.optInt("play_count") + " 次", 13, MUTED));
                  DownloadStore.Item local = null;
                  for (DownloadStore.Item item : store.all())
                    if (item.base.equals(source.base)
                        && item.game.equals(game.optString("id"))
                        && !DownloadStore.CANCELED.equals(item.state)) {
                      local = item;
                      break;
                    }
                  if (local != null) {
                    DownloadStore.Item item = local;
                    content.addView(
                        button(
                            DownloadStore.READY.equals(item.state) ? "用本机模拟器启动" : "查看下载任务",
                            () -> {
                              dialog.dismiss();
                              if (DownloadStore.READY.equals(item.state)) launch(item);
                              else show("downloads");
                            }));
                  } else if (options.optBoolean("available"))
                    content.addView(
                        button(
                            "下载到这台设备",
                            () -> {
                              try {
                                store.enqueue(source, game);
                                requestNotifications();
                                DownloadService.command(this, "start", null);
                                dialog.dismiss();
                                show("downloads");
                              } catch (Exception e) {
                                alert(ApiClient.message(e));
                              }
                            }));
                  else
                    content.addView(
                        text(
                            "暂不可下载：" + options.optString("reason", "服务尚未提供可下载文件"), 15, 0xffffbb88));
                });
          } catch (Exception e) {
            ui(
                () -> {
                  if (dialog.isShowing()) info.setText(ApiClient.message(e));
                });
          }
        });
  }

  private void requestNotifications() {
    if (Build.VERSION.SDK_INT >= 33
        && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED)
      requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 10);
  }

  private void renderDownloads(boolean force) {
    List<DownloadStore.Item> items = store.all();
    StringBuilder signature = new StringBuilder();
    for (DownloadStore.Item i : items)
      signature.append(i.id).append(i.state).append(i.bytes).append(i.message);
    if (!force && signature.toString().equals(downloadSignature)) return;
    downloadSignature = signature.toString();
    int y = scroll.getScrollY();
    Object focus = getCurrentFocus() == null ? null : getCurrentFocus().getTag();
    grid.removeAllViews();
    grid.addView(text("本机与下载", 28, TEXT));
    grid.addView(text("完整文件通过校验后才能启动。离开本页，下载仍继续；进程中断后可手动续传。", 14, MUTED));
    if (items.stream().noneMatch(i -> !DownloadStore.CANCELED.equals(i.state)))
      grid.addView(text("这台设备还没有下载。去游戏库选择自己的收藏。", 17, MUTED));
    for (DownloadStore.Item item : items) {
      if (DownloadStore.CANCELED.equals(item.state)) continue;
      LinearLayout card = column();
      card.setPadding(dp(16), dp(12), dp(16), dp(12));
      card.setBackground(shape(PANEL, 10));
      LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, -2);
      p.setMargins(0, dp(8), 0, dp(4));
      grid.addView(card, p);
      card.addView(text(item.title, 19, TEXT));
      card.addView(text(item.platform.toUpperCase(Locale.ROOT) + " · " + item.base, 12, MUTED));
      card.addView(
          text(
              stateLabel(item.state) + "  " + bytes(item.bytes) + " / " + bytes(item.size),
              15,
              ACCENT));
      ProgressBar progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
      progress.setMax(1000);
      progress.setProgress(item.size > 0 ? (int) (item.bytes * 1000 / item.size) : 0);
      progress.setIndeterminate(DownloadStore.PREPARING.equals(item.state));
      card.addView(progress, new LinearLayout.LayoutParams(-1, dp(8)));
      if (!item.message.isEmpty()) card.addView(text(item.message, 13, MUTED));
      LinearLayout actions = row();
      card.addView(actions);
      boolean active =
          DownloadStore.RUNNING.equals(item.state)
              || DownloadStore.PREPARING.equals(item.state)
              || DownloadStore.QUEUED.equals(item.state);
      if (active)
        actions.addView(
            tagged(
                button("暂停", () -> DownloadService.command(this, DownloadService.PAUSE, item.id)),
                item.id + "pause"));
      else if (DownloadStore.PAUSED.equals(item.state) || DownloadStore.FAILED.equals(item.state))
        actions.addView(
            tagged(
                button(
                    "继续 / 重试",
                    () -> {
                      requestNotifications();
                      DownloadService.command(this, DownloadService.RESUME, item.id);
                    }),
                item.id + "resume"));
      else if (DownloadStore.READY.equals(item.state)) {
        String reason = Emulators.support(item.platform, item.name);
        if (reason.isEmpty())
          actions.addView(tagged(button("启动 PPSSPP", () -> launch(item)), item.id + "launch"));
        else card.addView(text(reason, 13, 0xffffbb88));
        actions.addView(tagged(button("导出副本…", () -> export(item)), item.id + "export"));
      }
      if (!active)
        actions.addView(
            tagged(
                button(
                    "移除本机副本",
                    () ->
                        new AlertDialog.Builder(this)
                            .setTitle("移除「" + item.title + "」的本机下载？")
                            .setMessage(
                                "只移除 RetroOne 保存的这个文件与未完成分片。请先退出正在使用它的模拟器。服务器原文件和模拟器存档不受影响。")
                            .setNegativeButton("保留", null)
                            .setPositiveButton(
                                "移除",
                                (d, w) -> {
                                  try {
                                    store.cancel(item.id);
                                    renderDownloads(true);
                                  } catch (Exception e) {
                                    alert(ApiClient.message(e));
                                  }
                                })
                            .show()),
                item.id + "delete"));
    }
    if (focus != null) {
      View target = grid.findViewWithTag(focus);
      if (target != null) target.requestFocus();
    }
    scroll.post(() -> scroll.scrollTo(0, y));
  }

  private View tagged(View view, String tag) {
    view.setTag(tag);
    return view;
  }

  private void export(DownloadStore.Item item) {
    exportId = item.id;
    Intent create =
        new Intent(Intent.ACTION_CREATE_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("application/octet-stream")
            .putExtra(
                Intent.EXTRA_TITLE,
                item.title.replaceAll("[\\\\/:*?\"<>|]", "_")
                    + item.name.substring(item.name.lastIndexOf('.')));
    try {
      startActivityForResult(create, 40);
    } catch (android.content.ActivityNotFoundException e) {
      alert("设备未提供文件保存界面，无法导出；本机副本仍保留");
    }
  }

  @Override
  protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (request == 41) {
      if (result == RESULT_OK && data != null && data.getData() != null && saveExportPath != null) {
        java.io.File backup = new java.io.File(saveExportPath);
        android.net.Uri uri = data.getData();
        io.execute(
            () -> {
              try {
                if (!backup
                    .getCanonicalPath()
                    .startsWith(
                        new java.io.File(getFilesDir(), "save-backups").getCanonicalPath()
                            + java.io.File.separator)) throw new java.io.IOException("备份路径无效");
                try (java.io.InputStream in = new java.io.FileInputStream(backup);
                    java.io.OutputStream out = getContentResolver().openOutputStream(uri, "w")) {
                  if (out == null) throw new java.io.IOException("无法写入目标文件");
                  byte[] block = new byte[65536];
                  int n;
                  while ((n = in.read(block)) != -1) out.write(block, 0, n);
                }
                ui(() -> alert("云存档 ZIP 已导出。此操作不覆盖模拟器现有存档。"));
              } catch (Exception e) {
                ui(() -> alert(ApiClient.message(e) + "；目标可能留有不完整副本"));
              }
            });
      }
      return;
    }
    if (request != 40
        || result != RESULT_OK
        || data == null
        || data.getData() == null
        || exportId == null) return;
    String id = exportId;
    exportId = null;
    android.net.Uri destination = data.getData();
    Toast.makeText(this, "正在导出，请保持应用前台，完成后会显示结果", Toast.LENGTH_LONG).show();
    io.execute(
        () -> {
          try {
            DownloadStore.Item item = store.get(id);
            if (item == null || !DownloadStore.READY.equals(item.state))
              throw new java.io.IOException("本机副本已移除或尚未就绪");
            try (java.io.InputStream input = new java.io.FileInputStream(store.complete(item));
                java.io.OutputStream output =
                    getContentResolver().openOutputStream(destination, "w")) {
              if (output == null) throw new java.io.IOException("目标位置不可写入");
              byte[] block = new byte[65536];
              int n;
              long copied = 0;
              while ((n = input.read(block)) != -1) {
                output.write(block, 0, n);
                copied += n;
              }
              if (copied != item.size) throw new java.io.IOException("导出未完整，请删除目标不完整副本后重试");
            }
            ui(() -> alert("副本已导出。原下载仍保留，可在模拟器中手动选择导出的文件。"));
          } catch (Exception e) {
            ui(() -> alert(ApiClient.message(e) + "。导出失败时，目标可能留有不完整副本。"));
          }
        });
  }

  private void launch(DownloadStore.Item item) {
    List<Integer> installed = Emulators.installed(this);
    if (installed.isEmpty()) {
      alert("未找到可用的 PPSSPP。请从 PPSSPP 官方渠道安装后重试；RetroOne 不附带模拟器、游戏或固件。");
      return;
    }
    if (installed.size() == 1) {
      launchWith(item, installed.get(0));
      return;
    }
    String[] labels = new String[installed.size()];
    for (int i = 0; i < labels.length; i++) labels[i] = Emulators.LABELS[installed.get(i)];
    new AlertDialog.Builder(this)
        .setTitle("选择模拟器")
        .setItems(labels, (d, w) -> launchWith(item, installed.get(w)))
        .show();
  }

  private void launchWith(DownloadStore.Item item, int index) {
    try {
      startActivity(Emulators.intent(this, store.get(item.id), index));
    } catch (Exception e) {
      alert(
          e instanceof android.content.ActivityNotFoundException
              ? "模拟器入口不可用，请检查安装版本"
              : ApiClient.message(e));
    }
  }

  private void settings() {
    ScrollView settingsScroll = new ScrollView(this);
    LinearLayout form = column();
    settingsScroll.addView(form);
    body.addView(settingsScroll);
    form.addView(text("连接你的私人游戏馆", 28, TEXT));
    form.addView(text("可填写服务根地址，或指定玩家入口 /p/player1。家庭玩家不是登录账户。", 14, MUTED));
    EditText address = edit("https://你的服务器:8781/p/player1", false);
    address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
    address.setSingleLine();
    address.setText(config == null ? "" : config.base);
    form.addView(address);
    CheckBox http = new CheckBox(this);
    http.setText("允许 HTTP 明文连接（仅用于可信网络；认证与文件也不加密）");
    http.setChecked(config != null && config.allowHttp);
    form.addView(http);
    form.addView(text("反向代理认证", 16, TEXT));
    Spinner auth = new Spinner(this);
    auth.setAdapter(
        new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            new String[] {"保留此地址已有认证", "无认证", "HTTP Basic", "Bearer Token"}));
    auth.setSelection(config == null ? 1 : 0);
    form.addView(auth);
    EditText username = edit("Basic 用户名", false),
        secret = edit("Basic 密码或 Bearer Token（不会显示已保存值）", true);
    form.addView(username);
    form.addView(secret);
    form.addView(
        text("HTTPS 验证系统与用户安装的 CA，不接受忽略证书。网页登录、Cookie、OAuth 尚未接入。认证凭据仅保存在本机加密存储中。", 13, MUTED));
    form.addView(
        button(
            "保存并打开游戏库",
            () -> {
              try {
                String value = "";
                int mode = auth.getSelectedItemPosition();
                ServerConfig candidate =
                    new ServerConfig(address.getText().toString(), http.isChecked(), "");
                if (mode == 0)
                  value =
                      RetroApplication.from(this)
                          .credentials
                          .load(candidate.base, candidate.allowHttp)
                          .authorization;
                if (mode == 2) {
                  String user = username.getText().toString();
                  if (user.contains(":") || user.contains("\n"))
                    throw new IllegalArgumentException("Basic 用户名不能包含冒号或换行");
                  value =
                      "Basic "
                          + java.util.Base64.getEncoder()
                              .encodeToString(
                                  (user + ":" + secret.getText()).getBytes(StandardCharsets.UTF_8));
                }
                if (mode == 3) {
                  if (secret.getText().toString().trim().isEmpty())
                    throw new IllegalArgumentException("请填写 Bearer Token");
                  value = "Bearer " + secret.getText().toString().trim();
                }
                candidate = new ServerConfig(candidate.base, candidate.allowHttp, value);
                RetroApplication.from(this).credentials.save(candidate);
                config = candidate;
                artwork.evictAll();
                platformId = "";
                readLibraryPreferences();
                show("library");
              } catch (Exception e) {
                alert(ApiClient.message(e));
              }
            }));
    if (config != null) form.addView(button("读取并切换服务端玩家", this::chooseProfile));
    form.addView(
        button(
            "选择默认桌面…",
            () -> {
              try {
                startActivity(new Intent(android.provider.Settings.ACTION_HOME_SETTINGS));
              } catch (android.content.ActivityNotFoundException e) {
                alert("此系统未提供默认桌面设置入口");
              }
            }));
    List<Integer> installed = Emulators.installed(this);
    StringBuilder emulator = new StringBuilder("已发现模拟器：");
    for (int i : installed) emulator.append(Emulators.LABELS[i]).append("  ");
    if (installed.isEmpty()) emulator.append("无可用 PPSSPP");
    form.addView(text(emulator.toString(), 15, ACCENT));
    form.addView(
        text(
            "当前接入 PSP 独立 ISO / CSO 的 PPSSPP 启动接口。PBP 存在实测路径兼容问题；CHD"
                + " 尚未验证可靠直启。其他平台仅提供下载管理。多文件光盘、目录游戏和压缩包不会被自动改造。\n\n"
                + "本机文件保存在 RetroOne 私有目录，卸载应用会移除这些下载。模拟器独立管理存档；本版没有云存档同步，也不会为不同 RetroOne"
                + " 玩家隔离同一模拟器的存档。\n\n"
                + "RetroOne Android 0.2.0 · 自有内容客户端",
            14,
            MUTED));
  }

  private void chooseProfile() {
    ServerConfig source = config;
    io.execute(
        () -> {
          try {
            JSONArray profiles =
                new ApiClient(source).json(source.api("profiles")).getJSONArray("profiles");
            List<JSONObject> ready = new ArrayList<>();
            List<String> names = new ArrayList<>();
            for (int i = 0; i < profiles.length(); i++) {
              JSONObject p = profiles.getJSONObject(i);
              if ("ready".equals(p.optString("status"))) {
                ready.add(p);
                JSONObject presence = p.optJSONObject("presence");
                String playing = presence == null ? "" : presence.optString("game_title", "");
                names.add(
                    p.optString("name")
                        + (playing.isEmpty() || playing.equals("null") ? "" : " · 正在玩 " + playing));
              }
            }
            ui(
                () -> {
                  if (ready.isEmpty()) {
                    alert("服务尚未提供可用玩家；可手动填写玩家路径");
                    return;
                  }
                  new AlertDialog.Builder(this)
                      .setTitle("选择玩家")
                      .setItems(
                          names.toArray(new String[0]),
                          (d, w) -> {
                            try {
                              ServerConfig next =
                                  new ServerConfig(
                                      source
                                          .resolve(ready.get(w).getString("base_path"))
                                          .toString(),
                                      source.allowHttp,
                                      source.authorization);
                              RetroApplication.from(this).credentials.save(next);
                              config = next;
                              platformId = "";
                              profileName = ready.get(w).optString("name", "玩家");
                              getPreferences(0)
                                  .edit()
                                  .putString(next.key() + ".name", profileName)
                                  .apply();
                              readLibraryPreferences();
                              show("library");
                            } catch (Exception e) {
                              alert(ApiClient.message(e));
                            }
                          })
                      .show();
                });
          } catch (Exception e) {
            ui(() -> alert(ApiClient.message(e)));
          }
        });
  }

  private void refreshProfile() {
    ServerConfig source = config;
    if (source == null || source.base.equals(profileRequestedBase)) return;
    profileRequestedBase = source.base;
    io.execute(
        () -> {
          try {
            JSONObject result = new ApiClient(source).json(source.api("profiles"));
            JSONArray profiles = result.getJSONArray("profiles");
            for (int i = 0; i < profiles.length(); i++) {
              JSONObject p = profiles.getJSONObject(i);
              if (!p.optString("id").equals(result.optString("current_profile_id"))) continue;
              String name = p.optString("name", "玩家");
              ui(
                  () -> {
                    if (config == null || !source.base.equals(config.base)) return;
                    profileName = name;
                    profileButton.setText(name);
                    getPreferences(0).edit().putString(source.key() + ".name", name).apply();
                  });
              break;
            }
          } catch (Exception ignored) {
            profileRequestedBase = null;
          }
        });
  }

  private void readLibraryPreferences() {
    String key = config == null ? "unconnected" : config.key();
    android.content.SharedPreferences prefs = getPreferences(0);
    sort = prefs.getString(key + ".sort", "popularity");
    if (!java.util.Arrays.asList(LibraryQuery.SORTS).contains(sort)) sort = "popularity";
    direction = prefs.getString(key + ".direction", "desc");
    if (!"asc".equals(direction)) direction = "desc";
    scope = prefs.getString(key + ".scope", "all");
    profileName = prefs.getString(key + ".name", "选择玩家");
    query = "";
    if (profileButton != null) profileButton.setText(profileName);
  }

  private void writeLibraryPreferences() {
    if (config != null)
      getPreferences(0)
          .edit()
          .putString(config.key() + ".sort", sort)
          .putString(config.key() + ".direction", direction)
          .putString(config.key() + ".scope", scope)
          .apply();
  }

  private void chooseSort() {
    new AlertDialog.Builder(this)
        .setTitle("按整个游戏库排序")
        .setSingleChoiceItems(
            LibraryQuery.LABELS,
            java.util.Arrays.asList(LibraryQuery.SORTS).indexOf(sort),
            (d, w) -> {
              sort = LibraryQuery.SORTS[w];
              direction = (sort.equals("name") || sort.equals("default")) ? "asc" : "desc";
              writeLibraryPreferences();
              d.dismiss();
              show("library");
            })
        .setNegativeButton("返回", null)
        .show();
  }

  private void cloudSave(ServerConfig source, JSONObject game) {
    LinearLayout content = column();
    content.setPadding(dp(20), dp(12), dp(20), dp(12));
    TextView info = text("正在读取当前玩家的云存档…", 16, TEXT);
    content.addView(info);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("云存档 · " + game.optString("title"))
            .setView(content)
            .setNegativeButton("返回", null)
            .create();
    dialog.show();
    io.execute(
        () -> {
          try {
            JSONObject state =
                new ApiClient(source)
                    .json(
                        source.api(
                            "games/" + ServerConfig.encode(game.getString("id")) + "/cloud-save"));
            ui(
                () -> {
                  if (!dialog.isShowing()) return;
                  if (!state.optBoolean("supported")) {
                    info.setText("此平台尚未接入云存档传输。现有进度仍保存在对应模拟器中。");
                    return;
                  }
                  JSONObject snapshot = state.optJSONObject("snapshot");
                  String description =
                      snapshot == null
                          ? "尚未建立同步快照。读取最新备份时会检查云端模拟器存档。"
                          : "版本 "
                              + snapshot.optInt("revision")
                              + " · "
                              + snapshot.optInt("file_count")
                              + " 个文件 · "
                              + bytes(snapshot.optLong("size_bytes"))
                              + "\n"
                              + java.text.DateFormat.getDateTimeInstance()
                                  .format(
                                      new java.util.Date(
                                          (long) (snapshot.optDouble("updated_at") * 1000)));
                  boolean busy = state.optBoolean("native_active") || state.optBoolean("leased");
                  info.setText(description + (busy ? "\n当前游戏正在运行或同步，请退出后读取备份。" : ""));
                  Button fetch = button("下载最新存档备份…", () -> {});
                  fetch.setEnabled(!busy);
                  content.addView(fetch);
                  content.addView(text("备份为 PSP 游戏内存档，导出 ZIP。不会自动替换本机进度；自动双向同步尚未开放。", 13, MUTED));
                  fetch.setOnClickListener(
                      v -> {
                        fetch.setEnabled(false);
                        info.setText("正在读取并保存云存档…");
                        io.execute(
                            () -> {
                              try {
                                JSONObject latest = SaveBackup.fetch(source, game.getString("id"));
                                if (latest.getJSONArray("files").length() == 0)
                                  throw new java.io.IOException("该玩家尚未保存过这款游戏的云端进度");
                                java.io.File dir =
                                    new java.io.File(
                                        getFilesDir(),
                                        "save-backups/"
                                            + source.key()
                                            + "/"
                                            + ServerConfig.hash(game.getString("id")));
                                if (!dir.isDirectory() && !dir.mkdirs())
                                  throw new java.io.IOException("无法创建备份目录");
                                java.io.File file =
                                    new java.io.File(
                                        dir,
                                        "revision-"
                                            + latest.getLong("revision")
                                            + "-"
                                            + java.util.UUID.randomUUID()
                                            + ".zip");
                                SaveBackup.zip(latest, file);
                                ui(
                                    () -> {
                                      info.setText("备份已保存 · 版本 " + latest.optInt("revision"));
                                      fetch.setEnabled(true);
                                      if (!dialog.isShowing()) return;
                                      saveExportPath = file.getAbsolutePath();
                                      try {
                                        startActivityForResult(
                                            new Intent(Intent.ACTION_CREATE_DOCUMENT)
                                                .addCategory(Intent.CATEGORY_OPENABLE)
                                                .setType("application/zip")
                                                .putExtra(
                                                    Intent.EXTRA_TITLE,
                                                    "RetroOne-save-"
                                                        + latest.optInt("revision")
                                                        + ".zip"),
                                            41);
                                      } catch (android.content.ActivityNotFoundException e) {
                                        alert("系统没有文件保存界面，备份已保留在本应用内");
                                      }
                                    });
                              } catch (Exception e) {
                                ui(
                                    () -> {
                                      info.setText(ApiClient.message(e));
                                      fetch.setEnabled(true);
                                    });
                              }
                            });
                      });
                });
          } catch (Exception e) {
            ui(() -> info.setText(ApiClient.message(e)));
          }
        });
  }

  private void apps() {
    body.addView(text("Apps · 设备上的应用", 28, TEXT));
    ScrollView list = new ScrollView(this);
    LinearLayout rows = column();
    list.addView(rows);
    body.addView(list, new LinearLayout.LayoutParams(-1, 0, 1));
    Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
    List<android.content.pm.ResolveInfo> apps =
        getPackageManager().queryIntentActivities(launcher, 0);
    apps.sort(new android.content.pm.ResolveInfo.DisplayNameComparator(getPackageManager()));
    int count = Math.max(2, Math.min(6, getResources().getConfiguration().screenWidthDp / 170));
    LinearLayout line = null;
    int index = 0;
    for (android.content.pm.ResolveInfo app : apps) {
      if (getPackageName().equals(app.activityInfo.packageName)) continue;
      if (index++ % count == 0) {
        line = row();
        rows.addView(line);
      }
      LinearLayout card = column();
      card.setGravity(Gravity.CENTER);
      card.setPadding(dp(12), dp(12), dp(12), dp(12));
      ImageView icon = new ImageView(this);
      icon.setImageDrawable(app.loadIcon(getPackageManager()));
      card.addView(icon, new LinearLayout.LayoutParams(dp(64), dp(64)));
      TextView name = text(app.loadLabel(getPackageManager()).toString(), 15, TEXT);
      name.setGravity(Gravity.CENTER);
      name.setMaxLines(2);
      card.addView(name);
      card.setFocusable(true);
      card.setClickable(true);
      card.setBackground(shape(PANEL, 12));
      card.setForeground(getDrawable(android.R.drawable.list_selector_background));
      card.setOnClickListener(
          v -> {
            try {
              startActivity(
                  new Intent(Intent.ACTION_MAIN)
                      .addCategory(Intent.CATEGORY_LAUNCHER)
                      .setComponent(
                          new android.content.ComponentName(
                              app.activityInfo.packageName, app.activityInfo.name))
                      .addFlags(
                          Intent.FLAG_ACTIVITY_NEW_TASK
                              | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED));
            } catch (Exception e) {
              alert("应用暂时无法打开，请检查是否已停用或卸载");
            }
          });
      LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(0, dp(140), 1);
      layout.setMargins(dp(5), dp(5), dp(5), dp(5));
      line.addView(card, layout);
    }
    if (line != null)
      for (int i = index % count; i > 0 && i < count; i++)
        line.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
  }

  private void alert(String message) {
    new AlertDialog.Builder(this)
        .setTitle("RetroOne")
        .setMessage(message)
        .setPositiveButton("知道了", null)
        .show();
  }

  private LinearLayout column() {
    LinearLayout l = new LinearLayout(this);
    l.setOrientation(LinearLayout.VERTICAL);
    return l;
  }

  private LinearLayout row() {
    LinearLayout l = new LinearLayout(this);
    l.setOrientation(LinearLayout.HORIZONTAL);
    l.setGravity(Gravity.CENTER_VERTICAL);
    return l;
  }

  private TextView text(String value, int size, int color) {
    TextView v = new TextView(this);
    v.setText(value);
    v.setTextSize(size);
    v.setTextColor(color);
    v.setPadding(0, dp(6), 0, dp(6));
    return v;
  }

  private EditText edit(String hint, boolean password) {
    EditText v = new EditText(this);
    v.setTextColor(TEXT);
    v.setHintTextColor(MUTED);
    v.setHint(hint);
    v.setTextSize(16);
    v.setSingleLine();
    v.setInputType(
        InputType.TYPE_CLASS_TEXT | (password ? InputType.TYPE_TEXT_VARIATION_PASSWORD : 0));
    v.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
    return v;
  }

  private Button button(String value, Runnable action) {
    Button v = new Button(this);
    v.setText(value);
    v.setAllCaps(false);
    v.setTextColor(ACCENT);
    v.setTextSize(14);
    v.setMinHeight(dp(48));
    v.setOnClickListener(w -> action.run());
    return v;
  }

  private GradientDrawable shape(int color, int radius) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(color);
    d.setCornerRadius(dp(radius));
    return d;
  }

  private int dp(int n) {
    return Math.round(n * getResources().getDisplayMetrics().density);
  }

  static String bytes(long value) {
    if (value <= 0) return "—";
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return String.format(Locale.ROOT, "%.1f KiB", value / 1024d);
    if (value < 1024L * 1024 * 1024)
      return String.format(Locale.ROOT, "%.1f MiB", value / (1024d * 1024));
    return String.format(Locale.ROOT, "%.2f GiB", value / (1024d * 1024 * 1024));
  }

  private static String stateLabel(String value) {
    return switch (value) {
      case DownloadStore.QUEUED -> "等待下载";
      case DownloadStore.PREPARING -> "正在准备";
      case DownloadStore.RUNNING -> "下载与校验中";
      case DownloadStore.PAUSED -> "已暂停";
      case DownloadStore.FAILED -> "下载失败，可重试";
      case DownloadStore.READY -> "已就绪";
      default -> "已移除";
    };
  }
}
