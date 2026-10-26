/* RetroOne library UI. All catalog data comes from the mounted library API. */
(() => {
  'use strict';

  const I18n = window.RetroI18n;
  const Library = window.RetroLibrary;
  const clientRevision = new URL(document.currentScript?.src || location.href).searchParams.get('v');
  // Only our explicit status messages are replayed; catalog metadata is never scanned.
  const messageRecipes = new Map(), statusMessages = new Map();
  const t = (source, params = {}) => {
    const value = I18n.t(source, params);
    messageRecipes.set(value, {source, params});
    if (messageRecipes.size > 600) messageRecipes.delete(messageRecipes.keys().next().value);
    return value;
  };
  function localizedLabels(labels) {
    return new Proxy(labels, {get(target, key) { const value = Reflect.get(target, key); return typeof value === 'string' ? t(value) : value; }});
  }
  function statusText(id, message) {
    statusMessages.set(id, messageRecipes.get(message) || {raw: message});
    $(id).textContent = message;
  }
  function interfaceError(source, params = {}) { const error = new Error(t(source, params)); error.uiMessage = {source, params}; return error; }
  function errorMessage(error) { return error.uiMessage ? t(error.uiMessage.source, error.uiMessage.params) : error.rawDiagnostic ? diagnostic(error.rawDiagnostic) : error.message; }
  function diagnostic(message) { message = String(message).replaceAll('浏览器本地运行', '浏览器运行'); return I18n.locale() === 'en' ? t('服务原始诊断：{message}', {message}) : message; }
  I18n.setLocale(remembered('retroone:interface-locale') || 'zh-CN');

  const $ = (id) => document.getElementById(id);
  const escapeHTML = (value = '') => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = (name, cls = 'icon') => `<svg class="${cls}" aria-hidden="true"><use href="#i-${escapeHTML(name)}"/></svg>`;
  const number = (value) => I18n.number(value);
  const reviewFilters = localizedLabels({'': '全部', needs_attention: '需要处理', pending: '待验证', observed: '已有观察记录'});
  const reviewStatuses = localizedLabels({unverified: '未检测', fingerprinted: '已计算内容指纹', archive_verified: '文件已校验', dependency_error: '缺依赖', load_failed: '启动失败', boot_observed: '已观察启动画面', input_observed: '已观察输入响应', gameplay_observed: '已观察进入游戏', runtime_unavailable: '运行环境未配置', stale: '检测记录已过期'});
  function validReviewFilter(value) { return Object.hasOwn(reviewFilters, value) ? value : ''; }
  const initial = new URLSearchParams(location.search);
  const state = {
    librarySettings: Library.settings(), libraryFresh: false, librarySaving: false, librarySettingsRequest: 0,
    sort: Library.sorts.includes(initial.get('sort')) ? initial.get('sort') : initial.get('recent') === 'true' ? 'last_played' : 'popularity', direction: initial.get('direction') === 'asc' ? 'asc' : 'desc',
    detailOpen: initial.get('view') === 'detail', detailNeighbors: null, detailPaging: false, listScroll: 0, listSelection: null,
    interfaceRequest: 0, interfaceSaving: false, emptyState: null, migrationData: null,
    games: [], platforms: [], total: 0, platform: initial.get('platform') || '',
    query: initial.get('q') || '', mode: ['browser', 'stream', 'unavailable'].includes(initial.get('mode')) ? initial.get('mode') : '',
    favorites: initial.get('favorite') === 'true', recent: initial.get('recent') === 'true', reviewFilter: validReviewFilter(initial.get('review_filter')), selected: null, initialGame: initial.get('game'),
    loading: false, request: 0, controller: null, session: null, playMode: null, health: null, connectionStatus: 'connecting',
    favoritePending: new Set(), scanPending: false, wasScanning: false, healthRefreshing: false,
    detailRequest: 0, focusReturn: null, launching: false, launchOptions: null, launchChoice: 'auto', browserP2P: remembered('retroone:browser-p2p') !== 'false',
    downloadOptions: null, downloadSnapshot: null, downloadPending: null, downloadBusy: false, downloadEntryBusy: new Set(), downloadReturn: null, downloadFromSelf: false, deviceCacheBadges: new Map(),
    profile: null, profiles: [], profileEpoch: 0, profileRequests: new Set(), profileEditing: null, profileEditEnter: false, profileAvatar: 'pixel', pendingProfile: null, profileSaving: false,
    profileRegistryEpoch: 0, profileLoading: false, profileCreateRequestId: null, profileWatchId: null, profileRetrying: new Set(), profileCapacity: null,
    playGameId: null, familySelectedId: null, presenceStale: false, presenceEpoch: 0,
    familySessions: new Map(), familySessionsLoading: false, familySessionsAvailable: false, watchBusy: false, watchRequest: 0, watchTarget: null, watchFullscreenPending: false, watchFullscreenOwned: false,
    pendingLaunch: null,
    displayData: null, displayDraft: null, displayLoading: false, displaySaving: false, displayRequest: 0, displayFresh: false, displayReadController: null,
    managementData: null, managementFresh: false, managementLoading: false, managementBusy: false, managementRequest: 0, managementController: null, managementTimer: null,
    networkCapabilities: null, networkInvites: [], networkRoom: null, networkLoading: false, networkBusy: false, networkVersion: 0, networkCreate: null, networkSeen: new Set(), activeNetworkRoomId: null,
  };
  const presenceClientId = newRequestId();
  const deviceStorageKey = 'retroone:display-device';
  const savedDeviceId = remembered(deviceStorageKey);
  const displayDeviceId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(savedDeviceId || '') ? savedDeviceId.toLowerCase() : newRequestId();
  remember(deviceStorageKey, displayDeviceId);
  const deviceStorageAvailable = remembered(deviceStorageKey) === displayDeviceId;
  const displaySettingsPath = `/api/settings/display?device_id=${encodeURIComponent(displayDeviceId)}`;
  let presenceQueue = Promise.resolve();
  let toastTimer, searchTimer, gamepadFrame, gamepadLast = 0, gamepadPressed = {}, gamepadName = '';
  const gamepadHeldAt = {}, gamepadRepeatAt = {};
  let lastKeyboardMove = 0;
  const modeLabels = localizedLabels({ browser: '浏览器运行', stream: '云端串流', unavailable: '待配置' });
  const modeIcons = { browser: 'browser', stream: 'cloud', unavailable: 'info' };
  document.querySelector('.mode-filters').insertAdjacentHTML('beforeend', '<button class="filter-button review-filter" id="review-filter" type="button"></button>');
  $('scan-button').innerHTML = `${icon('library')}<span>${escapeHTML(t("管理游戏库"))}</span>`;
  document.body.insertAdjacentHTML('beforeend', `<dialog id="management-dialog" class="help-dialog management-dialog" aria-labelledby="management-title" aria-describedby="management-description"><div class="dialog-heading"><div><p class="eyebrow">LIBRARY CARE</p><h2 id="management-title" data-i18n="管理游戏库">${escapeHTML(t("管理游戏库"))}</h2></div><button class="icon-button" id="close-management" data-i18n-aria-label="关闭游戏库管理" aria-label="${escapeHTML(t("关闭游戏库管理"))}">${icon('close')}</button></div><p class="management-description" id="management-description" data-i18n="扫描识别文件并生成启动配置；内容核验检查重复和损坏；独立试运行需配置检测环境。">${escapeHTML(t("扫描识别文件并生成启动配置；内容核验检查重复和损坏；独立试运行需配置检测环境。"))}</p><dl class="management-summary" id="management-summary"></dl><section class="management-progress" data-i18n-aria-label="当前任务" aria-label="${escapeHTML(t("当前任务"))}"><p id="management-scan">${escapeHTML(t("正在读取扫描状态…"))}</p><strong id="management-job">${escapeHTML(t("正在读取内容核验状态…"))}</strong><p id="management-progress"></p><p class="is-warning" id="management-error" hidden></p></section><p class="management-message" id="management-message" role="status"></p><div class="management-actions"><button class="button button-secondary" id="management-scan-button" data-i18n="扫描游戏目录" disabled>${escapeHTML(t("扫描游戏目录"))}</button><button class="button" id="management-audit-button" disabled>${escapeHTML(t("开始内容核验"))}</button><button class="text-button" id="management-retry" data-i18n="重新读取状态" hidden>${escapeHTML(t("重新读取状态"))}</button></div><p class="management-note" id="management-storage">${escapeHTML(t("检测记录与游戏资料、原始文件及玩家存档分别保存。"))}</p><p class="management-note" data-i18n="核验会分批进行，暂停后可继续。关闭此窗口不会停止服务器任务。">${escapeHTML(t("核验会分批进行，暂停后可继续。关闭此窗口不会停止服务器任务。"))}</p><p class="management-note" id="management-observe">${escapeHTML(t("独立试运行尚未配置。"))}</p></dialog>`);

  $('family-invite-actions').insertAdjacentHTML('beforebegin', `<div class="family-watch-actions"><button class="button" id="family-watch" hidden></button><p id="family-watch-note" class="family-watch-note"></p></div>`);
  document.body.insertAdjacentHTML('beforeend', `<dialog id="watch-dialog" class="play-dialog watch-dialog" aria-labelledby="watch-title"><div class="play-toolbar"><button class="button button-secondary" id="close-watch" data-i18n="退出观看">${escapeHTML(t('退出观看'))}</button><div class="play-heading"><strong id="watch-title"></strong><span id="watch-note" data-i18n="只读观看 · 你的游戏会继续保留">${escapeHTML(t('只读观看 · 你的游戏会继续保留'))}</span></div><button class="icon-button" id="fullscreen-watch" aria-label="全屏观看" data-i18n-aria-label="全屏观看" title="全屏观看" data-i18n-title="全屏观看"><svg class="icon"><use href="#i-fullscreen"/></svg></button></div><div class="watch-frame-wrap" id="watch-frame-wrap"></div></dialog>`);

  function syncLanguagePickers() {
    for (const picker of document.querySelectorAll('[data-language-picker]')) {
      picker.value = I18n.getLocale(); picker.disabled = state.interfaceSaving;
      picker.setAttribute('aria-busy', String(state.interfaceSaving));
    }
  }
  function platformName(id, fallback) {
    const labels = {nes: 'FC · 红白机', fds: 'FC 磁碟机', snes: 'SFC · 超级任天堂', fbneo: '街机 · FinalBurn Neo', mame: '街机 · MAME', hbmame: '街机 · HBMAME', lightgun: '街机 · 光枪', teknoparrot: '街机 · TeknoParrot', dos: 'DOS · 经典电脑', unknown: '待识别收藏'};
    return Object.hasOwn(labels, id) ? t(labels[id]) : fallback || id || '';
  }
  function renderProfileHeading() {
    const profile = state.profileEditing;
    if (state.pendingProfile) {
      $('profile-title').textContent = t('先保存，再换玩家。');
      $('profile-description').textContent = t('准备切换到 {value1} 的游戏馆。', {value1: state.pendingProfile.name});
    } else if (profile) {
      $('profile-title').textContent = !profile.id ? t('添加家庭成员') : profile.configured ? t('这是你的玩家档案。') : t('先认识一下你。');
      $('profile-description').textContent = !profile.id ? t('取个昵称，选个头像。我们会准备独立的游戏空间，收藏与存档各自保留。') : t('昵称和头像会出现在你的游戏馆，也会用于今后的联机房间。');
      $('save-profile').textContent = !profile.id ? t('添加成员') : state.profileEditEnter ? t('保存并进入') : t('保存档案');
    } else {
      $('profile-title').textContent = t('今天，谁来玩？');
      $('profile-description').textContent = t('各自收藏，各自存档。选好玩家，接着上次的冒险。');
    }
  }
  function renderPendingLaunch() {
    const pending = state.pendingLaunch;
    if (!pending) return;
    const title = sessionTitle(pending.session), stop = pending.action === 'stop';
    $('replace-title').textContent = stop ? t('结束云端游戏？') : t('切换云端游戏？');
    $('replace-description').textContent = stop ? t('确定结束《{value1}》？', {value1: title}) : t('正在运行《{value1}》，结束并启动《{value2}》？', {value1: title, value2: pending.game.title});
    $('keep-current-game').textContent = t('继续玩《{value1}》', {value1: title});
    $('replace-current-game').textContent = stop ? t('结束《{value1}》', {value1: title}) : t('结束并启动《{value1}》', {value1: pending.game.title});
  }
  function applyInterfaceLanguage(locale) {
    const active = document.activeElement, search = $('search').value;
    const focusAttributes = ['id', 'data-select', 'data-favorite', 'data-platform', 'data-profile-action', 'data-avatar-option', 'data-display-resolution', 'data-network-action', 'data-family-profile'];
    const focusKey = focusAttributes.map((name) => [name, active?.getAttribute(name)]).find(([, value]) => value != null && value !== '');
    const scroll = [window.scrollX, window.scrollY], gridScroll = $('platform-nav').scrollLeft;
    const expanded = ['evidence', 'copies'].filter((name) => $('review-' + name) && !$('review-' + name).hidden);
    const video = $('detail-video');
    I18n.setLocale(locale); syncLanguagePickers();
    renderFilters(); renderPlatforms(); renderLibrarySort();
    if ($('platform-dialog').open) renderPlatformChooser();
    // Keep in-flight catalog state and error states rather than converting them to empty results.
    if (!state.loading) {
      if (state.games.length) renderGames();
      else if (state.emptyState) showEmpty(state.emptyState.kind, state.emptyState.message);
    }
    renderDetail(state.selected);
    for (const name of expanded) if ($('review-' + name)) { $('review-' + name).hidden = false; $('review-' + name + '-toggle').setAttribute('aria-expanded', 'true'); }
    if (video && $('detail-video')) { $('detail-video').replaceWith(video); $('preview-video').innerHTML = `${icon(video.hidden ? 'play' : 'close')}${escapeHTML(video.hidden ? t('观看游戏预览') : t('收起预览'))}`; }
    if (state.launching && $('launch-game')) { $('launch-game').disabled = true; $('launch-game').textContent = t('正在准备游戏…'); }
    renderCurrentProfile(); renderFamilyPresence(); renderProfileHeading();
    if (!$('profile-grid').hidden) renderProfileCards();
    if (!$('profile-editor').hidden) renderAvatarOptions();
    if ($('display-dialog').open) renderDisplaySettings();
    if ($('downloads-dialog').open) renderDownloads();
    if ($('management-dialog').open) renderLibraryManagement();
    if ($('network-dialog').open) renderNetwork();
    renderPendingLaunch();
    if (state.watchTarget) $('watch-title').textContent = t('正在观看 {value1} · {value2}', {value1: state.watchTarget.name, value2: state.watchTarget.title});
    $('scan-button').innerHTML = `${icon('library')}<span>${escapeHTML(t('管理游戏库'))}</span>`;
    $('gamepad-status').innerHTML = `${icon('controller')}${escapeHTML(gamepadName ? t('手柄已连接') : t('键盘 / 手柄'))}`;
    $('gamepad-help').textContent = window.isSecureContext ? t('若手柄没有响应，先点击游戏画面，再按手柄任意键。') : t('当前使用 HTTP。部分浏览器只允许在 HTTPS 下读取手柄；未识别时请使用键盘，或通过 HTTPS 访问。');
    if (state.playMode) {
      $('play-note').textContent = state.playMode === 'stream' ? t('返回游戏库会保留云端会话 · 退出前请保存进度') : t('退出前请在模拟器菜单保存进度');
      if ($('game-frame')) $('game-frame').title = t('{value1} · 游戏画面', {value1: $('play-title').textContent});
    }
    window.RetroMobilePlayer?.localize();
    if (state.health && state.connectionStatus === 'online') renderHealth();
    else if (state.connectionStatus === 'connecting') $('health-detail').textContent = t('正在检查服务状态…');
    if (state.migrationData) renderMigration(state.migrationData);
    for (const [id, recipe] of statusMessages) if ($(id)) $(id).textContent = recipe.source ? I18n.t(recipe.source, recipe.params) : recipe.raw;
    $('search').value = search; $('platform-nav').scrollLeft = gridScroll; updateClock();
    // Keep the same iframe. Only replaced controls need their focus restored.
    if (active?.isConnected) { if (document.activeElement !== active) active.focus({preventScroll: true}); }
    else if (focusKey) [...document.querySelectorAll(`[${focusKey[0]}]`)].find((element) => element.getAttribute(focusKey[0]) === focusKey[1])?.focus({preventScroll: true});
    window.scrollTo(...scroll);
  }
  async function loadInterfaceSettings() {
    if (state.interfaceSaving) return;
    const epoch = state.profileEpoch, request = ++state.interfaceRequest;
    try {
      const result = await api('/api/settings/interface', {timeoutMs: 6000});
      if (epoch !== state.profileEpoch || request !== state.interfaceRequest) return;
      if (!['auto', 'zh-CN', 'en'].includes(result?.settings?.locale)) throw interfaceError('界面语言设置返回不完整。');
      applyInterfaceLanguage(result.settings.locale);
    } catch (error) {
      if (epoch === state.profileEpoch && request === state.interfaceRequest && error.name !== 'AbortError') notify(t('未能读取玩家语言设置：{message}', {message: errorMessage(error)}), true);
    }
  }
  async function changeInterfaceLanguage(locale) {
    if (!['auto', 'zh-CN', 'en'].includes(locale) || state.interfaceSaving) return;
    const previous = I18n.getLocale(), epoch = state.profileEpoch, request = ++state.interfaceRequest;
    if (!state.profile) { remember('retroone:interface-locale', locale); applyInterfaceLanguage(locale); return; }
    const picker = document.activeElement?.matches('[data-language-picker]') ? document.activeElement : null;
    let focusMoved = false;
    const trackFocus = (event) => { if (event.target !== picker && event.target !== document.body) focusMoved = true; };
    document.addEventListener('focusin', trackFocus);
    state.interfaceSaving = true; applyInterfaceLanguage(locale);
    try {
      const result = await api('/api/settings/interface', {method: 'PUT', body: JSON.stringify({locale}), timeoutMs: 6000, cancelOnProfileChange: false});
      if (epoch !== state.profileEpoch || request !== state.interfaceRequest) return;
      if (!['auto', 'zh-CN', 'en'].includes(result?.settings?.locale)) throw interfaceError('界面语言设置返回不完整。');
      applyInterfaceLanguage(result.settings.locale);
    } catch (error) {
      if (epoch !== state.profileEpoch || request !== state.interfaceRequest) return;
      applyInterfaceLanguage(previous);
      const message = t('未能确认语言设置已保存，已恢复当前界面。重新选择玩家后可读取服务器设置。原始信息：{message}', {message: errorMessage(error)});
      notify(message, true);
      if ($('profile-dialog').open) profileMessage(message, true);
    } finally {
      document.removeEventListener('focusin', trackFocus);
      if (epoch === state.profileEpoch && request === state.interfaceRequest) {
        state.interfaceSaving = false; syncLanguagePickers();
        if (picker?.isConnected && !focusMoved && document.activeElement === document.body) picker.focus({preventScroll: true});
      }
    }
  }
  function activateFocused() {
    const active = document.activeElement;
    if (!active || active.disabled) return;
    if (active.matches('[data-language-picker]')) {
      const values = ['auto', 'zh-CN', 'en'];
      active.value = values[(values.indexOf(active.value) + 1) % values.length];
      active.dispatchEvent(new Event('change', {bubbles: true}));
    } else if (active.matches('select')) {
      const available = [...active.options].filter(option => !option.disabled);
      if (available.length) {
        active.value = available[(available.findIndex(option => option.selected) + 1) % available.length].value;
        active.dispatchEvent(new Event('change', {bubbles: true}));
      }
    } else if (active.matches('button, a, summary')) active.click();
  }

  async function api(path, options = {}) {
    const {timeoutMs = 25000, global: globalRequest = false, cancelOnProfileChange = true, ...requestOptions} = options;
    const epoch = state.profileEpoch;
    if (!globalRequest && !state.profile) throw new DOMException('No player selected', 'AbortError');
    const controller = new AbortController();
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    if (!globalRequest && cancelOnProfileChange) state.profileRequests.add(controller);
    const external = options.signal;
    const abort = () => controller.abort();
    external?.addEventListener('abort', abort, {once: true});
    if (external?.aborted) controller.abort();
    try {
      const url = globalRequest ? path : `${state.profile.base_path}${path}`;
      const response = await fetch(url, {...requestOptions, signal: controller.signal, headers: {'Accept': 'application/json', ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers}});
      let body;
      try { body = await response.json(); } catch (error) { if (error.name === 'AbortError') throw error; throw response.ok ? interfaceError('服务返回了无法识别的数据，请重试。') : interfaceError('服务暂时无法响应（{value1}）。', {value1: response.status}); }
      if (!globalRequest && epoch !== state.profileEpoch) throw new DOMException('Player changed', 'AbortError');
      if (!response.ok) {
        const detail = body.detail ?? body.error ?? body.message;
        const error = new Error(typeof detail === 'string' ? diagnostic(detail) : detail?.message ? diagnostic(detail.message) : t("请求未完成（{value1}），请稍后重试。", {value1: response.status}));
        error.status = response.status; error.detail = detail;
        error.rawDiagnostic = typeof detail === 'string' ? detail : detail?.message;
        if (!error.rawDiagnostic) error.uiMessage = {source: '请求未完成（{value1}），请稍后重试。', params: {value1: response.status}};
        throw error;
      }
      return body;
    } catch (error) {
      if (!globalRequest && epoch !== state.profileEpoch) throw new DOMException('Player changed', 'AbortError');
      if ((timedOut || error.name === 'AbortError') && !external?.aborted) { const timeoutError = interfaceError("连接超时，请检查服务器后重试。"); timeoutError.code = 'timeout'; throw timeoutError; }
      if (error instanceof TypeError) throw interfaceError("无法连接游戏服务，请检查局域网连接后重试。");
      throw error;
    } finally {
      clearTimeout(timeout);
      state.profileRequests.delete(controller);
      external?.removeEventListener('abort', abort);
    }
  }

  function localURL(value, prefixes) {
    if (!value || typeof value !== 'string') return '';
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) return '';
      if (prefixes && !prefixes.some((prefix) => url.pathname.startsWith(prefix))) return '';
      return url.pathname + url.search + url.hash;
    } catch { return ''; }
  }

  const avatarLabels = localizedLabels({pixel: '像素伙伴', leaf: '新叶', star: '星星', rocket: '火箭', cat: '猫咪'});
  function avatarHTML(avatar) {
    const key = Object.hasOwn(avatarLabels, avatar) ? avatar : 'pixel';
    return `<span class="profile-avatar" data-avatar="${key}" aria-hidden="true">${icon(key === 'star' ? 'star' : `avatar-${key}`)}</span>`;
  }
  function syncModalBody() { document.body.classList.toggle('body-modal', Boolean(document.querySelector('dialog[open]'))); }
  function remember(key, value) { try { localStorage.setItem(key, value); } catch {} }
  function remembered(key) { try { return localStorage.getItem(key); } catch { return null; } }
  function newRequestId() {
    return crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (char) => { const value = crypto.getRandomValues(new Uint8Array(1))[0] & 15; return (char === 'x' ? value : (value & 3) | 8).toString(16); });
  }
  function presenceLabel(profile) {
    const session = state.familySessions.get(profile?.id);
    if (session) return t('云端运行 {value1}', {value1: session.game_title || session.title || t('云端游戏')});
    const presence = profile?.presence;
    if (state.presenceStale || !presence) return t("动态暂不可用");
    if (!presence.online) return t("暂时离开");
    return presence.activity === 'library' ? t("正在挑游戏") : presence.game_title ? t("正在玩 {value1}", {value1: presence.game_title}) : t("正在玩游戏");
  }
  function renderFamilyPresence() {
    const strip = $('family-strip'), focusedId = document.activeElement?.dataset.familyProfile, inboxFocused = document.activeElement?.matches('[data-open-network]');
    const online = state.profiles.filter((profile) => profile.id !== state.profile?.id && (state.familySessions.has(profile.id) || (!state.presenceStale && profile.presence?.online)));
    const known = state.familySessionsAvailable || state.profiles.some((profile) => profile.presence);
    const inviteCount = state.networkInvites.filter((invite) => ['pending', 'accepted'].includes(invite.status) && !['closed', 'expired'].includes(invite.room_status)).length;
    const invitation = inviteCount ? `<button class="family-inbox" data-open-network aria-label="${escapeHTML(t("查看 {value1} 条联机邀请", {value1: inviteCount}))}">${escapeHTML(t("联机邀请 "))}<b>${inviteCount}</b></button>` : '';
    const html = invitation + (online.length ? `<span class="family-online-label"><i></i>${escapeHTML(t("{value1} 位家人有动态", {value1: online.length}))}</span>${online.map((profile) => `<button class="family-person" data-family-profile="${escapeHTML(profile.id)}" title="${escapeHTML(profile.name)} · ${escapeHTML(presenceLabel(profile))}" aria-label="${escapeHTML(t("{value1}，{value2}，查看动态", {value1: profile.name, value2: presenceLabel(profile)}))}">${avatarHTML(profile.avatar)}<span><strong>${escapeHTML(profile.name)}</strong><small>${escapeHTML(presenceLabel(profile))}</small></span></button>`).join('')}` : `<span class="family-empty">${escapeHTML(state.presenceStale || !known ? t("家人动态暂不可用") : t("其他家人暂时离开"))}</span>`);
    if (strip.innerHTML !== html) {
      const scroll = strip.scrollLeft;
      strip.innerHTML = html; strip.scrollLeft = scroll;
      if (focusedId) [...strip.querySelectorAll('[data-family-profile]')].find((button) => button.dataset.familyProfile === focusedId)?.focus({preventScroll: true});
      if (inboxFocused) strip.querySelector('[data-open-network]')?.focus({preventScroll: true});
    }
    if ($('family-dialog').open) renderFamilyDetails();
    $('network-play-button').hidden = !inviteCount && !state.activeNetworkRoomId;
    $('network-play-button').textContent = state.activeNetworkRoomId ? t("联机步骤") : t("联机邀请 {value1}", {value1: inviteCount});
  }
  function renderFamilyDetails() {
    const profile = state.profiles.find((item) => item.id === state.familySelectedId);
    if (!profile) return;
    $('family-title').textContent = profile.name;
    const presence = profile.presence, session = state.familySessions.get(profile.id);
    const watch = $('family-watch');
    watch.hidden = profile.id === state.profile?.id || !session;
    watch.disabled = state.watchBusy;
    watch.textContent = state.watchBusy ? t('正在连接观看…') : t('观看 {value1} 的游戏', {value1: profile.name});
    $('family-watch-note').textContent = session ? t('观看实时画面和声音，不会接管手柄或切换玩家。') : state.familySessionsLoading ? t('正在检查可观看的游戏…') : !state.familySessionsAvailable ? t('观看服务暂不可用，请稍后重试。') : presence?.activity === 'browser' ? t('浏览器游戏暂不支持观看。') : t('这位家人目前没有可观看的云端游戏。');
    $('family-details').innerHTML = `${avatarHTML(profile.avatar)}<div><strong>${escapeHTML(presenceLabel(profile))}</strong><p>${escapeHTML(session ? t('云端游戏') : state.presenceStale || !presence ? t("暂时无法获取在线状态，请稍后再看。") : !presence.online ? t("这位家人暂时离开了游戏馆。") : presence.activity === 'browser' ? t("浏览器游戏") : presence.activity === 'stream' ? t("云端游戏") : t("在游戏馆浏览收藏。"))}</p></div>`;
    const inviteButtons = profile.id !== state.profile?.id && presence?.online && !state.presenceStale && state.networkCapabilities?.available ? state.networkCapabilities.games.map((game) => `<button class="button" data-send-invite="${escapeHTML(game.game_id)}" ${state.networkBusy ? 'disabled' : ''}>${escapeHTML(t("邀请一起玩《{value1}》", {value1: game.title}))}</button>`).join('') : '';
    if ($('family-invite-actions').innerHTML !== inviteButtons) $('family-invite-actions').innerHTML = inviteButtons;
  }
  function showFamily(profileId) {
    $('family-invite-message').hidden = true;
    state.familySelectedId = profileId; renderFamilyDetails(); refreshFamilySessions();
    $('family-dialog').showModal(); syncModalBody(); $('close-family').focus();
  }
  function closeFamily() {
    $('family-dialog').close(); syncModalBody();
    ([...$('family-strip').querySelectorAll('[data-family-profile]')].find((button) => button.dataset.familyProfile === state.familySelectedId) || $('profile-button')).focus();
  }
  async function refreshFamilySessions() {
    if (state.familySessionsLoading) return;
    state.familySessionsLoading = true;
    try {
      const result = await api('/spectate/api/sessions', {global: true, timeoutMs: 6000});
      if (!Array.isArray(result.sessions)) throw interfaceError('观看服务暂不可用，请稍后重试。');
      const sessions = new Map();
      for (const entry of result.sessions) {
        const session = entry.session;
        if (typeof entry.profile_id === 'string' && session && typeof session.id === 'string' && session.id) sessions.set(entry.profile_id, session);
      }
      state.familySessions = sessions; state.familySessionsAvailable = true;
    } catch {
      state.familySessions = new Map(); state.familySessionsAvailable = false;
    } finally {
      state.familySessionsLoading = false; renderFamilyPresence();
      if ($('profile-dialog').open && !$('profile-grid').hidden) renderProfileCards();
    }
  }
  async function watchFamily() {
    const target = state.profiles.find((profile) => profile.id === state.familySelectedId);
    const session = state.familySessions.get(target?.id), viewer = state.profile;
    if (!target || !viewer || target.id === viewer.id || !session || state.watchBusy) return;
    const request = ++state.watchRequest, epoch = state.profileEpoch;
    state.watchBusy = true; $('family-invite-message').hidden = true; renderFamilyDetails();
    try {
      const result = await api('/spectate/api/watch', {global: true, method: 'POST', body: JSON.stringify({viewer_profile_id: viewer.id, target_profile_id: target.id, expected_session_id: session.id})});
      if (request !== state.watchRequest || epoch !== state.profileEpoch || !$('family-dialog').open || state.familySelectedId !== target.id) return;
      const url = new URL(result.watch_url, location.origin);
      if (url.origin !== location.origin || !/^\/spectate\/[A-Za-z0-9_-]{24,160}\/$/.test(url.pathname) || url.search || url.hash) throw interfaceError('观看服务返回了无效地址。');
      state.watchTarget = {name: target.name, title: session.game_title || session.title || t('云端游戏')};
      $('watch-title').textContent = t('正在观看 {value1} · {value2}', {value1: state.watchTarget.name, value2: state.watchTarget.title});
      const frame = document.createElement('iframe');
      frame.src = url.href; frame.title = $('watch-title').textContent; frame.allow = 'autoplay; fullscreen'; frame.referrerPolicy = 'no-referrer';
      $('watch-frame-wrap').replaceChildren(frame);
      closeFamily(); $('watch-dialog').showModal(); syncModalBody(); $('close-watch').focus();
    } catch (error) {
      if (request === state.watchRequest && epoch === state.profileEpoch && $('family-dialog').open) {
        $('family-invite-message').textContent = errorMessage(error); $('family-invite-message').hidden = false;
        refreshFamilySessions();
      }
    } finally {
      if (request === state.watchRequest) { state.watchBusy = false; if ($('family-dialog').open) renderFamilyDetails(); }
    }
  }
  async function toggleWatchFullscreen() {
    if (!$('watch-dialog').open || state.watchFullscreenPending) return;
    state.watchFullscreenPending = true;
    const request = state.watchRequest;
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen(); state.watchFullscreenOwned = false;
      } else {
        await document.documentElement.requestFullscreen({navigationUI: 'hide'});
        if (!$('watch-dialog').open || request !== state.watchRequest) {
          if (document.fullscreenElement === document.documentElement) await document.exitFullscreen();
        } else state.watchFullscreenOwned = true;
      }
    } catch { notify(t('无法进入全屏，请使用浏览器菜单或将网页添加到主屏幕。')); }
    finally { state.watchFullscreenPending = false; }
  }
  function closeWatch() {
    if (state.watchFullscreenOwned && document.fullscreenElement === document.documentElement) document.exitFullscreen().catch(() => {});
    state.watchFullscreenOwned = false;
    state.watchRequest++; state.watchBusy = false; state.watchTarget = null;
    $('watch-frame-wrap').replaceChildren(); $('watch-dialog').close(); syncModalBody();
    ([...$('family-strip').querySelectorAll('[data-family-profile]')].find((button) => button.dataset.familyProfile === state.familySelectedId) || $('profile-button')).focus();
  }

  function heartbeatPresence() {
    if (!state.profile || document.hidden) return;
    const epoch = state.presenceEpoch;
    const body = {profile_id: state.profile.id, client_id: presenceClientId, activity: state.playMode || 'library', game_id: state.playGameId};
    presenceQueue = presenceQueue.catch(() => {}).then(async () => {
      if (epoch !== state.presenceEpoch || document.hidden) return;
      try { await api('/api/presence', {global: true, method: 'POST', body: JSON.stringify(body)}); loadProfiles({silent: true}); }
      catch { state.presenceStale = true; renderFamilyPresence(); }
    });
  }
  function leavePresence(profile = state.profile, beacon = false) {
    if (!profile) return;
    state.presenceEpoch++;
    const body = JSON.stringify({profile_id: profile.id, client_id: presenceClientId});
    if (beacon) {
      if (!navigator.sendBeacon?.('/api/presence/leave', new Blob([body], {type: 'application/json'}))) fetch('/api/presence/leave', {method: 'POST', headers: {'Content-Type': 'application/json'}, body, keepalive: true}).catch(() => {});
      return;
    }
    presenceQueue = presenceQueue.catch(() => {}).then(() => api('/api/presence/leave', {global: true, method: 'POST', body, keepalive: true})).catch(() => {});
  }
  function saveProfileView() {
    if (!state.profile) return;
    const view = {platform: state.platform, query: state.query, mode: state.mode, reviewFilter: state.reviewFilter, favorites: state.favorites, recent: state.recent, initialGame: state.selected?.id || null, sort: state.sort, direction: state.direction};
    try { sessionStorage.setItem(`retroone:view:${state.profile.id}`, JSON.stringify(view)); } catch {}
  }
  function readProfileView(id) {
    try { return JSON.parse(sessionStorage.getItem(`retroone:view:${id}`)) || {}; } catch { return {}; }
  }
  function renderCurrentProfile() {
    const profile = state.profile;
    const activity = state.session ? t("继续 {value1}", {value1: sessionTitle(state.session)}) : state.playMode ? t("正在玩 {value1}", {value1: $('play-title').textContent}) : t("正在挑游戏");
    $('profile-button').innerHTML = `${profile ? avatarHTML(profile.avatar) : '<span class="profile-avatar" aria-hidden="true">?</span>'}<span class="current-player-copy"><strong>${escapeHTML(profile?.name || t("选择玩家"))}</strong>${profile ? `<small>${escapeHTML(activity)}</small>` : ''}</span>`;
    $('profile-button').dataset.playing = String(Boolean(state.session || state.playMode));
    $('profile-button').setAttribute('aria-label', profile ? t("{value1}，{value2}，打开我的游戏空间", {value1: profile.name, value2: activity}) : t("选择玩家"));
    $('profile-button').title = profile ? t("{value1} · {value2} · 查看继续 / 结束 / 切换玩家", {value1: profile.name, value2: activity}) : t("选择玩家");
    if ($('self-dialog').open) renderSelfDetails();
  }
  function renderSelfDetails() {
    $('self-title').textContent = state.profile?.name || t("我的游戏空间");
    $('self-avatar').innerHTML = avatarHTML(state.profile?.avatar);
    $('session-title').textContent = state.session ? sessionTitle(state.session) : t("正在挑游戏");
    $('self-session-note').textContent = state.session ? t("云端游戏仍在运行，可以继续上次的进度。") : t("选一款游戏，接着你的冒险。");
    $('resume-session').hidden = !state.session; $('stop-session').hidden = !state.session;
  }
  function showSelf() {
    if (!state.profile) { showProfiles(); return; }
    renderSelfDetails(); $('self-dialog').showModal(); syncModalBody();
    (state.session ? $('resume-session') : $('self-display-settings')).focus();
  }
  function closeSelf() { $('self-dialog').close(); syncModalBody(); $('profile-button').focus(); }
  function downloadMessage(message = '', error = false) {
    statusText('download-message', message); $('download-message').hidden = !message;
    $('download-message').dataset.error = String(error);
  }
  function downloadProblemMessage(problem) {
    const code = typeof problem === 'string' ? problem : problem?.code;
    const messages = {unsupported: '当前浏览器不支持设备目录下载。', permission: '目录访问已失效，请点击“重新授权”。', ownership: '无法确认目录归属，请选择新的下载目录。', 'revision-changed': '服务器上的游戏文件已更新，请重新下载。', 'directory-changed': '另一页面已更换缓存目录，请刷新后重试。', quota: '设备存储空间不足，请释放空间后继续。', invalid: '下载信息无效，请刷新游戏详情后重试。', component: '下载组件未能载入，请检查网络后重试。', network: '连接服务器失败，请检查网络后继续。', mismatch: '文件校验未通过，请重新下载。', modified: '设备上的文件已变化，需要重新校验。', busy: '另一页面正在使用这个文件，关闭对应任务后重试。', download: '下载未完成，请检查网络和目录权限后继续。', 'not-found': '找不到这个下载任务，请重新添加。', cancelled: '操作已取消。'};
    return Object.hasOwn(messages, code) ? t(messages[code]) : typeof problem === 'string' ? diagnostic(problem) : errorMessage(problem);
  }
  function downloadDirectoryReady(snapshot = state.downloadSnapshot) { return snapshot?.directory?.supported === true && snapshot.directory.permission === 'granted'; }
  function downloadEntryStatus(entry) {
    const labels = {downloading: '正在下载', checking: '正在校验', verifying: '正在校验', queued: '等待下载', paused: entry.complete ? '分享已暂停' : '下载已暂停', sharing: '正在分享', seeding: '正在分享', complete: '已下载', ready: '已下载', error: '需要处理', permission: '等待目录授权', busy: '其他页面正在使用'};
    return t(labels[entry.status] || (entry.complete ? '已下载' : '等待下载'));
  }
  function renderDownloads() {
    if (!$('downloads-dialog').open) return;
    const snapshot = state.downloadSnapshot, directory = snapshot?.directory || {}, manager = window.RetroRomLibrary;
    const supported = directory.supported === true, ready = downloadDirectoryReady(snapshot);
    $('download-directory-title').textContent = directory.name || t('尚未选择目录');
    $('download-directory-note').textContent = !manager ? t('下载组件尚未就绪，请刷新页面后重试。') : !supported ? directory.reason || (!window.isSecureContext ? t('需要 HTTPS 或本机安全地址，才能选择下载目录。') : t('此浏览器不支持选择并持续访问设备目录。请使用支持此功能的桌面版 Chrome 或 Edge。')) : ready ? t('已授权 · 文件保留在这个目录') : directory.name ? t('需要重新授权，才能读取文件、继续下载或分享。') : t('选择专门保存游戏的目录；不会自动下载到浏览器内存。');
    $('choose-download-directory').disabled = !supported || state.downloadBusy;
    $('choose-download-directory').textContent = t(directory.name ? '更换目录' : '选择目录');
    $('reconnect-download-directory').hidden = !supported || !directory.name || ready;
    $('reconnect-download-directory').disabled = state.downloadBusy;
    $('download-sharing-toggle').disabled = !manager;
    $('download-sharing-toggle').setAttribute('aria-checked', String(state.browserP2P));
    $('download-pending').hidden = !state.downloadPending;
    $('download-pending').textContent = state.downloadPending ? t('准备下载《{title}》。选择并授权目录后继续。', {title: state.downloadPending.title}) : '';
    const entries = Array.isArray(snapshot?.entries) ? snapshot.entries : [];
    $('download-summary').textContent = t('{count} 个文件 · {size}', {count: number(entries.length), size: formatSize(entries.reduce((total, entry) => total + (Number(entry.size) || 0), 0))});
    const list = $('download-entries'), active = list.contains(document.activeElement) ? {id: document.activeElement.dataset.downloadId, action: document.activeElement.dataset.downloadAction} : null;
    const html = entries.length ? entries.map(entry => {
      const total = Math.max(0, Number(entry.size) || 0), received = Math.max(0, Math.min(total, Number(entry.received) || 0));
      const percent = total ? Math.round((entry.complete ? total : received) / total * 100) : 0;
      const running = ['downloading', 'checking', 'verifying', 'sharing', 'seeding', 'queued'].includes(entry.status);
      const action = running ? 'pause' : 'resume', label = running ? entry.complete ? t('停止分享') : t('暂停下载') : entry.complete ? t('继续分享') : t('继续下载');
      const source = Number.isFinite(entry.http_bytes) || Number.isFinite(entry.peer_bytes) ? t('服务器 {http} · P2P {peer}', {http: formatSize(entry.http_bytes || 0), peer: formatSize(entry.peer_bytes || 0)}) : '';
      return `<article class="download-entry"><div class="download-entry-heading"><strong title="${escapeHTML(entry.title || entry.name)}">${escapeHTML(entry.title || entry.name || t('未命名游戏'))}</strong><span class="download-entry-status" data-status="${escapeHTML(entry.status)}">${escapeHTML(downloadEntryStatus(entry))}</span></div><div class="download-entry-progress"><progress value="${percent}" max="100" aria-label="${escapeHTML(t('下载进度'))}"></progress><span>${percent}%</span></div><p class="download-entry-size">${escapeHTML(t('{received} / {total}', {received: formatSize(entry.complete ? total : received), total: formatSize(total)}))}${source ? ` · ${escapeHTML(source)}` : ''}</p><div class="download-entry-footer"><span>${escapeHTML(t('已分享 {size} · {peers} 个连接', {size: formatSize(entry.uploaded || 0), peers: number(entry.peers || 0)}))}</span><button type="button" class="button button-secondary" data-download-id="${escapeHTML(entry.id)}" data-download-action="${action}" ${state.downloadEntryBusy.has(entry.id) || !ready || action === 'resume' && entry.complete && !state.browserP2P ? 'disabled' : ''}>${escapeHTML(label)}</button></div>${entry.error ? `<p class="download-entry-error">${escapeHTML(downloadProblemMessage(entry.error))}</p>` : ''}</article>`;
    }).join('') : `<div class="download-empty">${icon('cloud')}<strong>${escapeHTML(t('还没有保存游戏文件'))}</strong><p>${escapeHTML(t('在游戏详情选择“下载到设备”，即可加入这里。下载不会计入游玩记录。'))}</p></div>`;
    if (list.innerHTML !== html) {
      list.innerHTML = html;
      if (active) [...list.querySelectorAll('[data-download-id]')].find(button => button.dataset.downloadId === active.id)?.focus({preventScroll: true});
    }
  }
  function receiveDownloadSnapshot(snapshot) {
    state.downloadSnapshot = snapshot;
    updateDeviceCacheBadges(snapshot);
    if (typeof snapshot?.sharing === 'boolean' && snapshot.sharing !== state.browserP2P) {
      state.browserP2P = snapshot.sharing; remember('retroone:browser-p2p', String(snapshot.sharing));
      if (state.detailOpen && state.selected) renderLaunchChoices(state.selected);
    }
    renderDownloads();
  }
  function deviceCacheStates(snapshot) {
    const states = new Map(), permitted = snapshot?.directory?.permission === 'granted';
    const priority = {ready: 6, checking: 5, downloading: 4, permission: 3, cached: 3, paused: 2, sharing_paused: 2, error: 1};
    for (const entry of snapshot?.entries || []) {
      if (!entry.gameId) continue;
      let kind = null;
      if ((!permitted || entry.status === 'permission') && entry.cachedComplete === true) kind = 'permission';
      else if (permitted && entry.complete === true && ['ready', 'sharing', 'complete'].includes(entry.status)) kind = 'ready';
      else if (permitted && ['verifying', 'checking'].includes(entry.status)) kind = 'checking';
      else if (permitted && ['downloading', 'queued'].includes(entry.status)) kind = 'downloading';
      else if (permitted && entry.status === 'paused') kind = entry.complete ? 'sharing_paused' : entry.cachedComplete ? 'cached' : 'paused';
      else if (permitted && entry.status === 'error') kind = 'error';
      const id = String(entry.gameId);
      if (kind && (!states.has(id) || priority[kind] > priority[states.get(id)])) states.set(id, kind);
    }
    return states;
  }
  function deviceCacheLabel(kind, short = false) {
    const labels = {ready: short ? '已下载' : '已下载到此设备', checking: '缓存校验中', downloading: '正在下载', permission: '缓存待授权', paused: '下载已暂停', sharing_paused: short ? '已下载' : '已下载到此设备 · 分享已暂停', cached: '缓存待校验', error: '缓存需检查'};
    return labels[kind] ? t(labels[kind]) : '';
  }
  function deviceCacheContent(kind, layout) {
    return kind ? `${icon('device-download')}<span aria-hidden="true">${escapeHTML(deviceCacheLabel(kind, layout === 'card'))}</span>` : '';
  }
  function deviceCacheBadge(gameId, layout = 'card') {
    const kind = state.deviceCacheBadges?.get(String(gameId));
    return `<span class="device-cache-badge device-cache-${layout}" data-device-cache-game="${escapeHTML(gameId)}" data-cache-layout="${layout}" data-cache-state="${kind || ''}" title="${escapeHTML(deviceCacheLabel(kind))}" aria-label="${escapeHTML(deviceCacheLabel(kind))}" ${kind ? '' : 'hidden'}>${deviceCacheContent(kind, layout)}</span>`;
  }
  function updateDeviceCacheBadges(snapshot) {
    const next = deviceCacheStates(snapshot), previous = state.deviceCacheBadges || new Map();
    const changed = new Set([...previous.keys(), ...next.keys()].filter(id => previous.get(id) !== next.get(id)));
    state.deviceCacheBadges = next;
    if (!changed.size) return;
    // Byte counters change frequently. Only status transitions touch existing
    // badge nodes; game buttons, covers and controller focus are left intact.
    for (const badge of document.querySelectorAll('[data-device-cache-game]')) {
      const id = badge.dataset.deviceCacheGame;
      if (!changed.has(id)) continue;
      const kind = next.get(id), label = deviceCacheLabel(kind);
      badge.hidden = !kind; badge.dataset.cacheState = kind || ''; badge.title = label;
      badge.setAttribute('aria-label', label); badge.innerHTML = deviceCacheContent(kind, badge.dataset.cacheLayout);
    }
  }
  async function initializeDownloads() {
    const manager = window.RetroRomLibrary;
    if (!manager) return;
    manager.subscribe(receiveDownloadSnapshot);
    try { await manager.init(); receiveDownloadSnapshot(manager.snapshot()); }
    catch (error) { downloadMessage(downloadProblemMessage(error), true); receiveDownloadSnapshot(manager.snapshot()); }
  }
  function showDownloads({fromSelf = false} = {}) {
    state.downloadReturn = document.activeElement; state.downloadFromSelf = fromSelf;
    if ($('self-dialog').open) $('self-dialog').close();
    if (window.RetroRomLibrary) state.downloadSnapshot = window.RetroRomLibrary.snapshot();
    $('downloads-dialog').showModal(); syncModalBody(); renderDownloads();
    ($('reconnect-download-directory').hidden ? $('choose-download-directory') : $('reconnect-download-directory')).focus();
    if (document.activeElement === document.body || document.activeElement === $('downloads-dialog')) $('close-downloads').focus();
  }
  function closeDownloads() {
    state.downloadPending = null; $('downloads-dialog').close(); syncModalBody();
    if (state.downloadFromSelf) { showSelf(); $('self-download-settings').focus(); }
    else if (state.downloadReturn?.isConnected) state.downloadReturn.focus({preventScroll: true});
  }
  async function chooseDownloadDirectory(reconnect = false) {
    const manager = window.RetroRomLibrary;
    if (!manager || state.downloadBusy) return;
    // Invoke the picker/requestPermission synchronously inside this click turn.
    // No capability fetch or initialization may consume the user gesture first.
    state.downloadBusy = true;
    try {
      const choosing = reconnect ? manager.reconnect() : manager.chooseDirectory();
      renderDownloads(); await choosing; receiveDownloadSnapshot(manager.snapshot());
      if (downloadDirectoryReady()) { downloadMessage(); await flushPendingDownload(); }
    } catch (error) { if (error.name !== 'AbortError') downloadMessage(downloadProblemMessage(error), true); }
    finally { state.downloadBusy = false; renderDownloads(); }
  }
  async function flushPendingDownload() {
    const pending = state.downloadPending;
    if (!pending || pending.epoch !== state.profileEpoch || pending.profileId !== state.profile?.id || !downloadDirectoryReady()) return;
    state.downloadPending = null;
    try { await window.RetroRomLibrary.add({gameId: pending.gameId, title: pending.title, profileId: pending.profileId, manifestUrl: pending.manifestUrl}); }
    catch (error) { if (pending.epoch === state.profileEpoch) downloadMessage(downloadProblemMessage(error), true); }
    finally { if (pending.epoch === state.profileEpoch) renderDownloads(); }
  }
  async function downloadEntryAction(id, action) {
    if (!['pause', 'resume'].includes(action) || state.downloadEntryBusy.has(id) || !window.RetroRomLibrary) return;
    state.downloadEntryBusy.add(id); renderDownloads();
    try { await window.RetroRomLibrary[action](id); downloadMessage(); }
    catch (error) { downloadMessage(downloadProblemMessage(error), true); }
    finally { state.downloadEntryBusy.delete(id); receiveDownloadSnapshot(window.RetroRomLibrary.snapshot()); }
  }
  function displayMessage(message = '', error = false) {
    statusText('display-message', message); $('display-message').hidden = !message;
    $('display-message').dataset.error = String(error);
  }
  function displayOptions() {
    return (state.displayData?.capabilities?.stream_resolution || []).filter((option) => typeof option.value === 'string' && Number.isFinite(option.width) && Number.isFinite(option.height));
  }
  function renderDisplaySettings() {
    const data = state.displayData, active = data?.active, busy = state.displayLoading || state.displaySaving;
    const available = data?.capabilities?.available === true;
    const focusedValue = document.activeElement?.dataset.displayResolution;
    $('display-title').textContent = t("{value1} · 这台设备", {value1: state.profile?.name || t("玩家")});
    $('display-current').hidden = !(active || data?.active_unknown);
    if (active || data?.active_unknown) {
      $('display-current-title').textContent = !state.displayFresh ? t("上次读取的云端会话") : state.session && (data.active_unknown || sessionId(state.session) === active?.session_id) ? t("当前会话 · {value1}", {value1: sessionTitle(state.session)}) : t("当前云端会话");
      $('display-current-size').textContent = Number.isFinite(active?.width) && active.width > 0 && Number.isFinite(active?.height) && active.height > 0 ? `${active.width} × ${active.height}` : t("实际分辨率暂未提供");
      $('display-pending').hidden = !data.pending;
    }
    $('display-saved').textContent = data?.settings?.stream_resolution ? `${state.displayFresh ? t("已保存") : t("上次读取")}：${data.settings.stream_resolution}` : state.displayLoading ? t("正在读取设置…") : t("尚未读取设置");
    $('display-options').innerHTML = displayOptions().map((option) => `<button type="button" class="display-option" data-display-resolution="${escapeHTML(option.value)}" aria-pressed="${state.displayDraft === option.value}" ${!available || busy || !state.displayFresh ? 'disabled' : ''}><strong>${escapeHTML(option.value)}</strong><small>${option.width} × ${option.height}</small>${state.displayDraft === option.value ? icon('check') : ''}</button>`).join('');
    $('display-unavailable').hidden = !data || available;
    $('display-unavailable').textContent = (data?.capabilities?.reason && diagnostic(data.capabilities.reason)) || t("当前服务暂不支持调整串流分辨率。");
    $('save-display').disabled = busy || !state.displayFresh || !available || !displayOptions().some((option) => option.value === state.displayDraft) || (data?.source === 'device' && state.displayDraft === data?.settings?.stream_resolution);
    $('save-display').textContent = state.displaySaving ? t("正在保存…") : t("保存到这台设备");
    $('display-device-source').textContent = !data ? '' : data.source === 'device' ? t('已为这台设备单独保存。') : t('尚未单独设置，沿用玩家默认值：{value1}。', {value1: data.default_settings?.stream_resolution || data.settings.stream_resolution});
    $('display-device-storage').hidden = deviceStorageAvailable;
    $('reset-display').hidden = data?.source !== 'device';
    $('reset-display').disabled = busy || !state.displayFresh;
    $('display-options').setAttribute('aria-busy', String(busy));
    if (focusedValue) [...$('display-options').querySelectorAll('button')].find((button) => button.dataset.displayResolution === focusedValue && !button.disabled)?.focus({preventScroll: true});
  }
  async function loadDisplaySettings() {
    state.displayReadController?.abort();
    const controller = new AbortController(); state.displayReadController = controller;
    const epoch = state.profileEpoch, request = ++state.displayRequest;
    state.displayLoading = true; state.displayFresh = false; state.displayDraft = state.displayData?.settings?.stream_resolution || null;
    $('retry-display').hidden = true; displayMessage(state.displayData ? t("显示上次读取的设置，正在刷新…") : ''); renderDisplaySettings();
    try {
      const result = await api(displaySettingsPath, {timeoutMs: 6000, signal: controller.signal, priority: 'high'});
      if (epoch !== state.profileEpoch || request !== state.displayRequest) return;
      if (!result?.settings || !result?.capabilities) throw interfaceError("画面设置返回不完整，请重试。");
      if (result.scope !== 'device' || result.device_id !== displayDeviceId) throw interfaceError('服务尚未支持设备独立设置，请在升级完成后重试。');
      state.displayData = result; state.displayDraft = result.settings.stream_resolution; state.displayFresh = true; displayMessage();
    } catch (error) {
      if (epoch !== state.profileEpoch || request !== state.displayRequest) return;
      displayMessage(error.code === 'timeout' ? t("读取设置超过 6 秒，请点击「重新读取设置」重试。") : t("未能读取最新设置：{value1} 请重新读取后再保存。", {value1: errorMessage(error)}), true); $('retry-display').hidden = false;
    } finally {
      if (epoch === state.profileEpoch && request === state.displayRequest) { state.displayReadController = null; state.displayLoading = false; renderDisplaySettings(); }
    }
  }
  function showDisplaySettings() {
    if (!state.profile) return;
    if ($('self-dialog').open) closeSelf();
    $('display-dialog').showModal(); syncModalBody(); $('close-display').focus(); loadDisplaySettings();
  }
  function closeDisplaySettings() {
    state.displayRequest++; state.displayLoading = false; state.displaySaving = false;
    state.displayReadController?.abort(); state.displayReadController = null; state.displayFresh = false;
    $('display-dialog').close(); syncModalBody(); showSelf(); $('self-display-settings').focus();
  }
  async function saveDisplaySettings(reset = false) {
    if ((reset ? $('reset-display').disabled || $('reset-display').hidden : $('save-display').disabled) || !state.profile) return;
    const epoch = state.profileEpoch, request = ++state.displayRequest, resolution = state.displayDraft;
    state.displaySaving = true; displayMessage(); renderDisplaySettings();
    try {
      // A closed panel or player switch cannot undo a write already sent.
      const result = await api(displaySettingsPath, {method: reset ? 'DELETE' : 'PATCH', ...(reset ? {} : {body: JSON.stringify({stream_resolution: resolution})}), cancelOnProfileChange: false});
      if (epoch !== state.profileEpoch || request !== state.displayRequest) return;
      if (!result?.settings || !result?.capabilities) throw interfaceError("未收到完整的保存结果，请重新读取设置确认。");
      if (result.scope !== 'device' || result.device_id !== displayDeviceId) throw interfaceError('服务尚未支持设备独立设置，请在升级完成后重试。');
      state.displayData = result; state.displayDraft = result.settings.stream_resolution; state.displayFresh = true;
      displayMessage(t("已保存，下次启动云游戏时生效。{value1}", {value1: result.active || result.active_unknown ? t("当前游戏继续运行。") : ''}));
    } catch (error) {
      if (epoch === state.profileEpoch && request === state.displayRequest) {
        state.displayFresh = false; $('retry-display').hidden = false;
        displayMessage(t("未能确认保存结果：{value1} 请重新读取设置，确认是否已经保存。", {value1: errorMessage(error)}), true);
      }
    } finally {
      if (epoch === state.profileEpoch && request === state.displayRequest) { state.displaySaving = false; renderDisplaySettings(); }
    }
  }
  function focusModalAction(dialog, direction) {
    const buttons = [...dialog.querySelectorAll('button:not(:disabled):not([hidden]), select:not(:disabled):not([hidden])')].filter((button) => button.getClientRects().length);
    const index = buttons.indexOf(document.activeElement), delta = direction === 'left' || direction === 'up' ? -1 : 1;
    buttons[(index + delta + buttons.length) % buttons.length]?.focus();
  }
  function managementMessage(message = '', error = false) {
    statusText('management-message', message);
    $('management-message').classList.toggle('is-warning', error);
  }
  function managementJobText(job) {
    if (!job) return t("尚未开始内容核验");
    const labels = {running: t("正在核验内容"), complete: t("本轮内容核验完成"), paused: t("本批核验完成，可继续"), failed: t("内容核验未完成"), interrupted: t("核验已中断，可继续")};
    return Object.hasOwn(labels, job.status) ? labels[job.status] : t("核验状态待确认");
  }
  function managementProgressText(progress) {
    if (!progress || typeof progress !== 'object') return '';
    const labels = {discovering: t("识别目录"), discovery: t("识别目录"), hashing: t("计算内容指纹"), verifying: t("核对文件"), writing: t("保存检测记录"), complete: t("本轮完成")};
    const parts = [];
    if (Object.hasOwn(labels, progress.phase)) parts.push(labels[progress.phase]);
    if (Number.isFinite(progress.games_discovered)) parts.push(t("已识别 {value1}{value2} 项", {value1: number(progress.games_discovered), value2: Number.isFinite(progress.games_total) ? ` / ${number(progress.games_total)}` : ''}));
    if (Number.isFinite(progress.bytes_hashed)) parts.push(t("已读取 {value1}", {value1: formatSize(progress.bytes_hashed)}));
    return parts.join(' · ');
  }
  function renderLibraryManagement() {
    const data = state.managementData, summary = data?.summary || {}, scan = data?.scan || {}, job = data?.job;
    const fields = {catalog_entries: t("目录记录"), display_entries: t("合并后展示"), duplicate_copies: t("重复副本"), reviewed: t("有检测记录"), needs_attention: t("需要处理"), pending: t("待验证")};
    $('management-summary').innerHTML = Object.entries(fields).map(([key, label]) => `<div><dt>${escapeHTML(label)}</dt><dd>${Number.isFinite(summary[key]) ? number(summary[key]) : '—'}</dd></div>`).join('');
    $('management-scan').textContent = !data ? t("正在读取扫描状态…") : scan.running || state.scanPending ? t("正在扫描目录{value1}", {value1: Number.isFinite(scan.scanned) ? t(" · 已识别 {value1} 项", {value1: number(scan.scanned)}) : ''}) : scan.last_error ? t("上次目录扫描未完成") : scan.finished_at ? t("目录扫描已完成") : t("目录尚未扫描");
    $('management-job').textContent = data ? managementJobText(job) : t("正在读取内容核验状态…");
    $('management-progress').textContent = managementProgressText(job?.progress);
    const error = typeof job?.error === 'string' ? job.error : typeof scan.last_error === 'string' ? scan.last_error : '';
    $('management-error').textContent = error ? diagnostic(error) : ''; $('management-error').hidden = !error;
    const busy = state.managementBusy || job?.status === 'running' || scan.running || state.scanPending;
    $('management-scan-button').disabled = !state.managementFresh || busy;
    $('management-audit-button').disabled = !state.managementFresh || busy || data?.capabilities?.audit !== true;
    $('management-audit-button').textContent = job?.status === 'running' ? t("内容核验进行中…") : ['paused', 'interrupted', 'failed'].includes(job?.status) ? t("继续内容核验") : t("开始内容核验");
    $('management-observe').textContent = data?.capabilities?.observe === true ? t("独立试运行由检测环境处理，不会在这里启动家庭成员的游戏。") : t("独立试运行尚未配置；文件校验不代表已验证可玩。");
    $('management-storage').textContent = data?.storage?.separated === true ? t("检测记录与游戏资料、原始文件及玩家存档分别保存。") : t("正在确认检测记录与游戏资料的保存方式。");
    $('management-summary').setAttribute('aria-busy', String(state.managementLoading));
  }
  function cancelManagementRead() {
    state.managementRequest++; state.managementController?.abort(); state.managementController = null; state.managementLoading = false;
  }
  async function refreshLibraryManagement() {
    if (!$('management-dialog').open || document.hidden || state.managementLoading || state.managementBusy) return;
    const epoch = state.profileEpoch, request = ++state.managementRequest;
    const controller = new AbortController(); state.managementController = controller; state.managementLoading = true;
    renderLibraryManagement();
    try {
      const data = await api('/api/library-management', {signal: controller.signal, timeoutMs: 6000});
      if (epoch !== state.profileEpoch || request !== state.managementRequest || !$('management-dialog').open) return;
      if (!data?.scan || !data?.summary || !data?.capabilities) throw interfaceError("管理状态返回不完整，请重新读取。");
      const previous = state.managementData?.job;
      state.managementData = data; state.managementFresh = true; managementMessage(); $('management-retry').hidden = true;
      if (previous?.id === data.job?.id && previous?.status === 'running' && data.job?.status !== 'running') refreshLibraryAfterScan();
    } catch (error) {
      if (epoch !== state.profileEpoch || request !== state.managementRequest || error.name === 'AbortError') return;
      state.managementFresh = false;
      managementMessage(t("{value1}{value2} 请重新读取状态。", {value1: state.managementData ? t("当前显示上次读取的记录。") : '', value2: errorMessage(error)}), true); $('management-retry').hidden = false;
    } finally {
      if (epoch === state.profileEpoch && request === state.managementRequest) { state.managementController = null; state.managementLoading = false; renderLibraryManagement(); }
    }
  }
  function showLibraryManagement() {
    if (!state.profile) return;
    cancelManagementRead(); clearInterval(state.managementTimer); state.managementFresh = false;
    managementMessage(state.managementData ? t("显示上次读取的记录，正在刷新…") : t("正在读取游戏库状态…")); $('management-retry').hidden = true;
    renderLibraryManagement(); $('management-dialog').showModal(); syncModalBody(); $('close-management').focus();
    refreshLibraryManagement(); state.managementTimer = setInterval(refreshLibraryManagement, 2000);
  }
  function closeLibraryManagement({restoreFocus = true} = {}) {
    clearInterval(state.managementTimer); state.managementTimer = null; cancelManagementRead(); state.managementFresh = false;
    $('management-dialog').close(); syncModalBody();
    if (restoreFocus) $('scan-button').focus({preventScroll: true});
  }
  async function runLibraryManagement(kind, outside = false) {
    if (!state.profile || state.managementBusy || state.scanPending || (!outside && !state.managementFresh)) return;
    if (!outside && (state.managementData?.scan?.running || state.managementData?.job?.status === 'running' || (kind === 'audit' && state.managementData?.capabilities?.audit !== true))) return;
    const epoch = state.profileEpoch;
    cancelManagementRead(); state.managementBusy = true; state.managementFresh = false; managementMessage(t("正在提交任务…")); renderLibraryManagement();
    try {
      await api(kind === 'scan' ? '/api/scan' : '/api/library-management/jobs', {method: 'POST', ...(kind === 'audit' ? {body: JSON.stringify({kind: 'audit'})} : {}), cancelOnProfileChange: false});
      if (epoch !== state.profileEpoch) return;
      if (kind === 'scan') { state.scanPending = true; setTimeout(() => { if (epoch === state.profileEpoch) refreshHealth(); }, 1800); }
      notify(kind === 'scan' ? t("已开始扫描目录，完成后会自动刷新") : t("已提交内容核验，可在管理窗口查看进度"));
    } catch (error) {
      if (epoch !== state.profileEpoch) return;
      state.managementFresh = false;
      managementMessage(t("未能确认任务结果：{value1} 请重新读取状态后再试。", {value1: errorMessage(error)}), true); $('management-retry').hidden = false;
      if (!$('management-dialog').open) notify(t("未能确认任务结果，请打开「管理游戏库」查看状态。"), true);
    } finally {
      if (epoch === state.profileEpoch) { state.managementBusy = false; renderLibraryManagement(); refreshLibraryManagement(); }
    }
  }
  function networkMessage(message = '', error = false) {
    statusText('network-message', message); $('network-message').hidden = !message; $('network-message').dataset.error = String(error);
  }
  function validRoom(room) { return room && typeof room.id === 'string' && typeof room.game_id === 'string' && Array.isArray(room.members) && room.members.some((member) => member.profile_id === state.profile?.id); }
  function renderNetwork() {
    const root = $('network-content'), active = document.activeElement;
    const focus = active?.dataset.networkAction ? {action: active.dataset.networkAction, id: active.dataset.inviteId} : null;
    const room = state.networkRoom;
    if (room) {
      const mine = room.members.find((member) => member.profile_id === state.profile.id), ready = room.status === 'ready';
      const steps = Array.isArray(room.instructions?.[mine.role]) ? room.instructions[mine.role] : [];
      $('network-title').textContent = room.game_title;
      root.innerHTML = `<p class="network-intro">${escapeHTML(t("双方各自启动游戏，再在游戏内加入同一局。"))}</p><div class="network-members">${room.members.map((member) => `<div>${avatarHTML(member.avatar)}<span><strong>${escapeHTML(member.name)}</strong><small>${escapeHTML(member.role === 'host' ? t("创建游戏房间") : t("查找游戏房间"))}${escapeHTML(member.profile_id === state.profile.id ? t(" · 你") : '')}</small></span></div>`).join('')}</div>${ready ? `<ol class="network-steps">${steps.map((step) => `<li>${escapeHTML(t(step))}</li>`).join('')}</ol><p class="network-note">${escapeHTML(t("接受邀请不会自动进入比赛。两人都启动后，请在 WIRELESS BATTLE 中完成建房与加入。"))}</p><button class="button network-primary" data-network-action="start" ${state.networkBusy ? 'disabled' : ''}>${escapeHTML(sessionNetworkRoomId(state.session) === room.id ? t("继续我的游戏") : t("启动我的游戏"))}</button>` : `<p class="network-note">${escapeHTML(room.status === 'expired' ? t("这次联机邀请已过期，请重新邀请。") : t("这次联机邀请已关闭。已启动的游戏会继续运行。"))}</p>`}<div class="network-footer"><button class="text-button" data-network-action="list">${escapeHTML(t("返回邀请列表"))}</button>${ready ? `<button class="text-button" data-network-action="close-room" ${state.networkBusy ? 'disabled' : ''}>${escapeHTML(t("关闭这次邀请"))}</button>` : ''}</div>`;
    } else {
      $('network-title').textContent = t("联机邀请");
      const invites = state.networkInvites;
      root.innerHTML = invites.length ? invites.slice(0, 12).map((invite) => {
        const incoming = invite.to_profile_id === state.profile.id, other = incoming ? invite.from : invite.to;
        const status = {pending: incoming ? t("邀请你一起玩") : t("等待对方接受"), accepted: t("已接受 · 双方可以准备游戏"), declined: t("邀请已婉拒"), cancelled: t("邀请已取消"), expired: t("邀请已过期")}[invite.status] || t("邀请状态更新中");
        const canAccept = state.networkCapabilities?.available && state.networkCapabilities.games.some((game) => game.game_id === invite.game_id);
        const button = (action, label, secondary = false) => `<button class="button${secondary ? ' button-secondary' : ''}" data-network-action="${action}" data-invite-id="${escapeHTML(invite.id)}" ${state.networkBusy ? 'disabled' : ''}>${escapeHTML(label)}</button>`;
        return `<article class="network-invite"><div class="network-person">${avatarHTML(other?.avatar)}<div><strong>${escapeHTML(other?.name || t("家庭成员"))}</strong><small>${escapeHTML(status)}</small></div></div><p>${escapeHTML(invite.game_title)}</p><div class="network-actions">${invite.status === 'pending' ? incoming ? `${canAccept ? button('accept', t("接受邀请")) : ''}${button('decline', t("暂时不玩"), true)}` : button('cancel', t("取消邀请"), true) : invite.status === 'accepted' && invite.room_id && !['closed', 'expired'].includes(invite.room_status) ? button('open-room', t("查看联机步骤")) : ''}</div></article>`;
      }).join('') : `<p class="network-note">${escapeHTML(t("暂无联机邀请。点击顶栏在线家人的头像，可以邀请一起玩已经验证支持联机的游戏。"))}</p>`;
    }
    if (focus) [...root.querySelectorAll('[data-network-action]')].find((button) => button.dataset.networkAction === focus.action && button.dataset.inviteId === focus.id)?.focus({preventScroll: true});
  }
  function showNetwork({keepRoom = false} = {}) {
    if (!state.profile) return;
    if ($('family-dialog').open) closeFamily();
    if ($('self-dialog').open) closeSelf();
    if (!keepRoom) state.networkRoom = null;
    networkMessage(); renderNetwork();
    if (!$('network-dialog').open) $('network-dialog').showModal();
    syncModalBody(); $('close-network').focus(); refreshNetwork();
  }
  function closeNetwork() {
    $('network-dialog').close(); syncModalBody();
    if (state.playMode) $('network-play-button').focus();
    else ($('family-strip').querySelector('[data-open-network]') || $('profile-button')).focus();
  }
  async function refreshNetwork() {
    if (!state.profile || document.hidden || state.networkLoading || state.networkBusy) return;
    state.networkLoading = true;
    const epoch = state.profileEpoch, version = state.networkVersion, profileId = state.profile.id;
    try {
      const [capabilities, result] = await Promise.all([api('/api/network/capabilities', {global: true}), api(`/api/network/invites?profile_id=${encodeURIComponent(profileId)}`, {global: true})]);
      if (epoch !== state.profileEpoch || version !== state.networkVersion) return;
      state.networkCapabilities = {...capabilities, games: (capabilities.games || []).filter((game) => typeof game.game_id === 'string' && typeof game.title === 'string')};
      state.networkInvites = (result.invites || []).filter((invite) => invite.from_profile_id === profileId || invite.to_profile_id === profileId);
      for (const invite of state.networkInvites) {
        const key = `${invite.id}:${invite.status}`;
        if (!state.networkSeen.has(key) && invite.to_profile_id === profileId && invite.status === 'pending') notify(t("{value1} 邀请你一起玩《{value2}》", {value1: invite.from?.name || t("家人"), value2: invite.game_title}));
        if (!state.networkSeen.has(key) && invite.from_profile_id === profileId && invite.status === 'accepted') notify(t("{value1} 已接受邀请，双方可以分别启动游戏。", {value1: invite.to?.name || t("家人")}));
        state.networkSeen.add(key);
      }
      if (state.networkRoom && $('network-dialog').open) {
        const room = await api(`/api/network/rooms/${encodeURIComponent(state.networkRoom.id)}`, {global: true});
        if (epoch !== state.profileEpoch || version !== state.networkVersion) return;
        if (validRoom(room)) state.networkRoom = room;
      }
      renderFamilyPresence(); if ($('network-dialog').open) renderNetwork();
    } catch (error) { if (epoch === state.profileEpoch && $('network-dialog').open) networkMessage(errorMessage(error), true); }
    finally { if (epoch === state.profileEpoch) state.networkLoading = false; }
  }
  async function sendInvitation(gameId) {
    if (!state.profile || state.networkBusy || !state.networkCapabilities?.games.some((game) => game.game_id === gameId)) return;
    const target = state.profiles.find((profile) => profile.id === state.familySelectedId);
    if (!target?.presence?.online || target.id === state.profile.id) return;
    const epoch = state.profileEpoch;
    if (state.networkCreate?.game_id !== gameId || state.networkCreate?.to_profile_id !== target.id) state.networkCreate = {from_profile_id: state.profile.id, to_profile_id: target.id, game_id: gameId, request_id: newRequestId()};
    state.networkBusy = true; state.networkVersion++; renderFamilyDetails();
    $('family-invite-message').hidden = true;
    try {
      const invite = await api('/api/network/invites', {global: true, method: 'POST', body: JSON.stringify(state.networkCreate)});
      if (epoch !== state.profileEpoch) return;
      state.networkInvites = [invite, ...state.networkInvites.filter((item) => item.id !== invite.id)]; state.networkCreate = null;
      state.networkBusy = false; renderFamilyPresence(); showNetwork();
      networkMessage(t("邀请已发送。对方接受后，双方各自点击「启动我的游戏」。"));
    } catch (error) { if (epoch === state.profileEpoch) { $('family-invite-message').textContent = errorMessage(error); $('family-invite-message').hidden = false; } }
    finally { if (epoch === state.profileEpoch) { state.networkBusy = false; if ($('family-dialog').open) renderFamilyDetails(); } }
  }
  async function networkAction(action, inviteId) {
    if (!state.profile || state.networkBusy) return;
    const epoch = state.profileEpoch;
    if (action === 'list') { state.networkRoom = null; renderNetwork(); $('close-network').focus(); return; }
    if (action === 'start') {
      const room = state.networkRoom;
      if (!validRoom(room) || room.status !== 'ready') return;
      if (state.playMode) {
        if (state.activeNetworkRoomId === room.id) { closeNetwork(); $('game-frame')?.focus(); return; }
        if (state.playMode === 'browser') { notify(t("请先保存浏览器游戏并返回游戏库，再启动这次联机。")); return; }
        closePlayer({refresh: false});
      }
      state.networkBusy = true; renderNetwork(); networkMessage(t("正在准备你的游戏，首次解压可能需要数分钟…"));
      try { await launchGame({id: room.game_id, title: room.game_title, mode: 'stream'}, {networkRoomId: room.id}); }
      finally { if (epoch === state.profileEpoch) { state.networkBusy = false; if ($('network-dialog').open) renderNetwork(); } }
      return;
    }
    const invite = state.networkInvites.find((item) => item.id === inviteId);
    const roomId = action === 'open-room' ? invite?.room_id : state.networkRoom?.id;
    if (action === 'open-room' && !roomId) return;
    state.networkBusy = true; state.networkVersion++; networkMessage(t("正在处理…")); renderNetwork();
    try {
      if (action === 'open-room') {
        const room = await api(`/api/network/rooms/${encodeURIComponent(roomId)}`, {global: true});
        if (epoch !== state.profileEpoch) return;
        if (!validRoom(room)) throw interfaceError("联机邀请信息不完整，请重新打开。");
        state.networkRoom = room;
      } else if (action === 'close-room') {
        if (!roomId) return;
        await api(`/api/network/rooms/${encodeURIComponent(roomId)}/close`, {global: true, method: 'POST', body: JSON.stringify({profile_id: state.profile.id})});
        if (epoch !== state.profileEpoch) return;
        state.networkRoom = {...state.networkRoom, status: 'closed'};
      } else if (invite && ['accept', 'decline', 'cancel'].includes(action)) {
        const result = await api(`/api/network/invites/${encodeURIComponent(invite.id)}/${action}`, {global: true, method: 'POST', body: JSON.stringify({profile_id: state.profile.id})});
        if (epoch !== state.profileEpoch) return;
        const updated = result.invite || result;
        state.networkInvites = state.networkInvites.map((item) => item.id === invite.id ? updated : item);
        if (action === 'accept') { if (!validRoom(result.room)) throw interfaceError("邀请已处理，请重新查看联机步骤。"); state.networkRoom = result.room; }
      }
      networkMessage();
    } catch (error) { if (epoch === state.profileEpoch) networkMessage(errorMessage(error), true); }
    finally {
      if (epoch !== state.profileEpoch) return;
      state.networkBusy = false; renderNetwork(); renderFamilyPresence();
      if (state.networkRoom?.status === 'ready') $('network-content').querySelector('[data-network-action="start"]')?.focus();
      refreshNetwork();
    }
  }
  function profileStatus(profile) { return profile?.status || 'ready'; }
  function validProfile(profile) {
    return profile && typeof profile.id === 'string' && typeof profile.name === 'string' && typeof profile.base_path === 'string' && /^\/p\/[a-zA-Z0-9_-]+$/.test(profile.base_path);
  }
  function profileFocusKey(element = document.activeElement) {
    return element?.matches('[data-profile-action]') ? element.dataset.profileAction : null;
  }
  function focusProfileAction(key) {
    const action = [...$('profile-grid').querySelectorAll('[data-profile-action]')].find((button) => button.dataset.profileAction === key);
    action?.focus({preventScroll: true});
    return Boolean(action);
  }
  function profileCardActivity(profile) {
    const status = profileStatus(profile), presence = profile.presence;
    if (status === 'provisioning') return {kind: 'preparing', label: t('正在准备'), detail: t('准备好后就能进入游戏馆')};
    if (status === 'error') return {kind: 'error', label: t('需要重试'), detail: t('专属空间暂时无法启动')};
    if (!profile.configured) return {kind: 'new', label: t('设置昵称与头像'), detail: ''};
    const session = state.familySessions.get(profile.id);
    if (session) return {kind: 'playing', label: t('云端游戏进行中'), detail: session.game_title || session.title || t('云端游戏')};
    if (state.presenceStale) return {kind: 'unknown', label: t('动态暂不可用'), detail: ''};
    if (presence?.online && presence.activity !== 'library' && presence.game_title) return {kind: 'playing', label: t('正在游戏'), detail: presence.game_title};
    if (presence?.online) return {kind: 'online', label: t('在线 · 正在挑游戏'), detail: ''};
    return {kind: 'idle', label: presence ? t('暂时离开') : t('随时开始'), detail: ''};
  }
  function profileCardMarkup(profile) {
    const status = profileStatus(profile), id = escapeHTML(profile.id), activity = profileCardActivity(profile);
    const current = profile.id === state.profile?.id;
    const last = !state.profile && profile.id === remembered('retroone:last-profile') && profile.configured && status === 'ready';
    const badge = current ? t('当前玩家') : last ? t('上次使用') : '';
    const enter = status === 'ready' ? profile.configured ? t('进入游戏馆') : t('设置昵称与头像') : activity.label;
    const editLabel = t('编辑 {value1} 的昵称与头像', {value1: profile.name});
    return `<button class="profile-card" data-profile-id="${id}" data-profile-action="select:${id}" data-status="${escapeHTML(status)}" data-activity="${activity.kind}" aria-current="${current}" aria-disabled="${status !== 'ready'}" aria-label="${escapeHTML([profile.name, badge, activity.label, activity.detail, enter].filter(Boolean).join(' · '))}">${avatarHTML(profile.avatar)}<span class="profile-card-copy"><span class="profile-name-row"><strong>${escapeHTML(profile.name)}</strong>${badge ? `<span class="profile-card-badge">${escapeHTML(badge)}</span>` : ''}</span><span class="profile-card-activity" data-kind="${activity.kind}">${status === 'provisioning' ? '<span class="profile-busy" aria-hidden="true"></span>' : '<i aria-hidden="true"></i>'}<span>${escapeHTML(activity.label)}</span></span>${activity.detail ? `<span class="profile-game-title" title="${escapeHTML(activity.detail)}">${escapeHTML(activity.detail)}</span>` : ''}</span>${!profile.configured && status === 'ready' ? icon('arrow', 'profile-enter-icon') : ''}</button>${profile.configured ? `<button class="profile-edit" data-edit-profile="${id}" data-profile-action="edit:${id}" aria-label="${escapeHTML(editLabel)}" title="${escapeHTML(editLabel)}"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15v5Z"/></svg></button>` : ''}${status === 'error' ? `<div class="profile-recovery"><details class="profile-error-details"><summary data-profile-action="reason:${id}">${escapeHTML(t('查看原因'))}</summary><p class="profile-error">${escapeHTML(profile.error || profile.message || t('专属空间暂时无法启动，请重试。'))}</p></details><button class="profile-retry" data-retry-profile="${id}" data-profile-action="retry:${id}" ${state.profileRetrying.has(profile.id) ? 'disabled' : ''}>${icon('refresh')}${escapeHTML(state.profileRetrying.has(profile.id) ? t('正在重试…') : t('重新准备'))}</button></div>` : ''}`;
  }
  function renderProfileCards() {
    const focused = profileFocusKey(), grid = $('profile-grid'), scrollTop = $('profile-dialog').scrollTop;
    const entries = state.profiles.map(profile => ({key: profile.id, markup: profileCardMarkup(profile), className: 'profile-card-wrap'}));
    entries.push({key: '__add', className: 'profile-card-wrap profile-add-wrap', markup: `<button class="profile-add" data-add-profile data-profile-action="add" aria-disabled="${state.profileCapacity?.remaining === 0}"><span class="profile-add-symbol" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></span><span><strong>${escapeHTML(t('添加家庭成员'))}</strong><small>${escapeHTML(state.profileCapacity?.remaining === 0 ? t('已达到成员数量上限') : t('为新的冒险留一个位置'))}</small></span>${icon('arrow', 'profile-add-arrow')}</button>`});
    const existing = new Map([...grid.children].map(element => [element.dataset.profileKey, element]));
    entries.forEach((entry, index) => {
      const element = existing.get(entry.key) || document.createElement('div');
      element.className = entry.className; element.dataset.profileKey = entry.key;
      if (element._profileMarkup !== entry.markup) {
        const expanded = element.querySelector('.profile-error-details')?.open;
        element.innerHTML = entry.markup; element._profileMarkup = entry.markup;
        if (expanded && element.querySelector('.profile-error-details')) element.querySelector('.profile-error-details').open = true;
      }
      if (grid.children[index] !== element) grid.insertBefore(element, grid.children[index] || null);
      existing.delete(entry.key);
    });
    for (const element of existing.values()) element.remove();
    if (focused && profileFocusKey() !== focused && !focusProfileAction(focused)) {
      const profileId = focused.slice(focused.indexOf(':') + 1);
      if (!focusProfileAction(`select:${profileId}`)) grid.querySelector('[data-profile-id]')?.focus({preventScroll: true});
    }
    $('profile-dialog').scrollTop = scrollTop;
  }
  function profileMessage(message = '', error = false) {
    statusText('profile-message', message);
    $('profile-message').hidden = !message;
    $('profile-message').dataset.error = String(error);
  }
  function showProfileCards() {
    if (state.profileSaving) return;
    const languagePicker = document.activeElement?.matches('#profile-dialog [data-language-picker]') ? document.activeElement : null;
    state.profileEditing = null; state.pendingProfile = null; state.profileCreateRequestId = null;
    $('profile-title').textContent = t("今天，谁来玩？");
    $('profile-description').textContent = t("各自收藏，各自存档。选好玩家，接着上次的冒险。");
    $('profile-dialog').dataset.view = 'choose';
    $('profile-grid').hidden = false; $('profile-editor').hidden = true; $('profile-confirm').hidden = true;
    $('close-profiles').hidden = !state.profile;
    profileMessage(); renderProfileCards();
    if (languagePicker?.isConnected) { languagePicker.focus({preventScroll: true}); return; }
    const last = state.profile?.id || remembered('retroone:last-profile');
    const cards = [...$('profile-grid').querySelectorAll('.profile-card')];
    (cards.find((card) => card.dataset.profileId === last) || cards[0])?.focus();
  }
  async function loadProfiles({silent = false} = {}) {
    if (state.profileLoading || state.profileSaving || state.profileRetrying.size) return;
    state.profileLoading = true;
    const epoch = state.profileRegistryEpoch;
    if (!silent) { $('retry-profiles').hidden = true; profileMessage(t("正在读取玩家档案…")); }
    try {
      const result = await api('/api/profiles', {global: true});
      if (epoch !== state.profileRegistryEpoch) return;
      if (!Array.isArray(result.profiles)) throw interfaceError("玩家档案暂时无法读取，请重试。");
      const profiles = result.profiles.filter(validProfile);
      const capacity = result.capacity || null;
      const changed = JSON.stringify(profiles) !== JSON.stringify(state.profiles) || JSON.stringify(capacity) !== JSON.stringify(state.profileCapacity);
      state.profileCapacity = capacity;
      state.profiles = profiles;
      state.presenceStale = false; renderFamilyPresence(); refreshFamilySessions();
      if (!silent && !state.profileEditing && !state.pendingProfile) showProfileCards();
      else if (changed && !$('profile-grid').hidden) renderProfileCards();
      const current = profiles.find((profile) => profile.id === state.profile?.id);
      if (current && (current.name !== state.profile.name || current.avatar !== state.profile.avatar)) { state.profile = current; renderCurrentProfile(); }
      if (!$('profile-grid').hidden) {
        $('retry-profiles').hidden = true;
        const watched = profiles.find((profile) => profile.id === state.profileWatchId);
        if (watched) {
          const status = profileStatus(watched);
          profileMessage(status === 'ready' ? t("{value1} 的专属空间已准备好，选择头像即可进入。", {value1: watched.name}) : status === 'error' ? t("{value1} 准备失败，请点击「重新准备」。", {value1: watched.name}) : t("正在为 {value1} 准备专属空间，请稍候。", {value1: watched.name}), status === 'error');
        } else if ($('profile-message').dataset.connectionError === 'true') profileMessage();
        $('profile-message').dataset.connectionError = 'false';
      }
    } catch (error) {
      if (epoch !== state.profileRegistryEpoch) return;
      state.presenceStale = true; renderFamilyPresence();
      if (!$('profile-grid').hidden) renderProfileCards();
      if (!silent || !$('profile-grid').hidden) {
        profileMessage(silent ? t("状态更新失败：{value1}", {value1: errorMessage(error)}) : errorMessage(error), true);
        $('profile-message').dataset.connectionError = 'true'; $('retry-profiles').hidden = false;
        if (!silent) $('retry-profiles').focus();
      }
    } finally { state.profileLoading = false; }
  }
  function showProfiles() {
    if ($('help-dialog').open) $('help-dialog').close();
    if (!$('profile-dialog').open) $('profile-dialog').showModal();
    syncModalBody(); showProfileCards();
    loadProfiles({silent: Boolean(state.profiles.length)});
  }
  function closeProfiles() {
    if (!state.profile || state.profileSaving) return;
    $('profile-dialog').close(); syncModalBody();
    if (state.playMode) $('game-frame')?.focus(); else $('profile-button').focus();
  }
  function editProfile(profile = null, enterAfterSave = false) {
    const creating = !profile;
    if (creating && state.profileCapacity?.remaining === 0) { profileMessage(t("家庭成员已达到 {value1} 位上限，暂时无法继续添加。", {value1: state.profileCapacity.limit}), true); return; }
    profile ||= {name: '', avatar: 'pixel', configured: false};
    state.profileEditing = profile; state.profileEditEnter = enterAfterSave; state.profileAvatar = profile.avatar || 'pixel'; state.profileCreateRequestId = null;
    $('profile-dialog').dataset.view = 'edit';
    $('profile-grid').hidden = true; $('profile-confirm').hidden = true; $('profile-editor').hidden = false;
    $('retry-profiles').hidden = true;
    $('profile-title').textContent = creating ? t("添加家庭成员") : profile.configured ? t("这是你的玩家档案。") : t("先认识一下你。");
    $('profile-description').textContent = creating ? t("取个昵称，选个头像。我们会准备独立的游戏空间，收藏与存档各自保留。") : t("昵称和头像会出现在你的游戏馆，也会用于今后的联机房间。");
    $('profile-name').value = profile.configured ? profile.name : '';
    $('save-profile').textContent = creating ? t("添加成员") : enterAfterSave ? t("保存并进入") : t("保存档案");
    profileMessage(); renderAvatarOptions(); $('profile-name').focus();
  }
  function renderAvatarOptions() {
    $('avatar-options').innerHTML = Object.entries(avatarLabels).map(([key, name]) => `<button type="button" class="avatar-option" role="radio" data-avatar-option="${key}" aria-checked="${state.profileAvatar === key}" aria-label="${escapeHTML(name)}" title="${escapeHTML(name)}">${avatarHTML(key)}</button>`).join('');
  }
  async function saveEditedProfile(event) {
    event.preventDefault();
    if (!state.profileEditing || state.profileSaving) return;
    const name = $('profile-name').value.trim();
    if (!name || [...name].length > 20) { profileMessage(t("请输入 1–20 个字的昵称。"), true); $('profile-name').focus(); return; }
    const id = state.profileEditing.id, enter = state.profileEditEnter, creating = !id;
    if (creating && !state.profileCreateRequestId) state.profileCreateRequestId = newRequestId();
    state.profileRegistryEpoch++;
    state.profileSaving = true; $('save-profile').disabled = true; $('cancel-profile-edit').disabled = true;
    profileMessage(creating ? t("正在添加家庭成员…") : t("正在保存玩家档案…"));
    try {
      const result = await api(creating ? '/api/profiles' : `/api/profiles/${encodeURIComponent(id)}`, {global: true, method: creating ? 'POST' : 'PATCH', body: JSON.stringify({name, avatar: state.profileAvatar, ...(creating ? {request_id: state.profileCreateRequestId} : {})})});
      const profile = result.profile || result;
      if (!validProfile(profile)) throw interfaceError("玩家档案返回不完整，请重试。");
      const index = state.profiles.findIndex((item) => item.id === profile.id);
      if (index < 0) state.profiles.push(profile); else state.profiles[index] = {...state.profiles[index], ...profile};
      if (state.profile?.id === profile.id) { state.profile = profile; renderCurrentProfile(); }
      state.profileSaving = false;
      if (creating) {
        state.profileWatchId = profile.id; showProfileCards(); focusProfileAction(`select:${profile.id}`);
        profileMessage(profileStatus(profile) === 'ready' ? t("{value1} 的专属空间已准备好，选择头像即可进入。", {value1: profile.name}) : profileStatus(profile) === 'error' ? t("{value1} 准备失败，请点击「重新准备」。", {value1: profile.name}) : t("已添加 {value1}，正在准备专属空间，请稍候。", {value1: profile.name}), profileStatus(profile) === 'error');
      } else if (enter) chooseProfile(profile);
      else showProfileCards();
    } catch (error) { profileMessage(errorMessage(error), true); }
    finally { state.profileSaving = false; $('save-profile').disabled = false; $('cancel-profile-edit').disabled = false; }
  }
  function chooseProfile(profile) {
    if (!profile || state.profileSaving) return;
    if (profileStatus(profile) !== 'ready') {
      profileMessage(profileStatus(profile) === 'error' ? profile.error || profile.message || t("准备失败，请点击「重新准备」。") : t("专属空间正在准备中，准备完成后即可进入。"), profileStatus(profile) === 'error');
      if (profileStatus(profile) === 'error') focusProfileAction(`retry:${profile.id}`);
      return;
    }
    if (!profile.configured) { editProfile(profile, true); return; }
    if (state.profile?.id === profile.id) { closeProfiles(); loadInterfaceSettings(); return; }
    if (state.playMode === 'browser') {
      state.pendingProfile = profile;
      $('profile-dialog').dataset.view = 'confirm';
      $('profile-grid').hidden = true; $('profile-editor').hidden = true; $('profile-confirm').hidden = false;
      $('profile-title').textContent = t("先保存，再换玩家。");
      $('profile-description').textContent = t("准备切换到 {value1} 的游戏馆。", {value1: profile.name});
      profileMessage(); $('profile-back-to-game').focus(); return;
    }
    applyProfile(profile);
  }
  async function retryProfile(profile) {
    if (!profile || state.profileRetrying.has(profile.id) || state.profileSaving) return;
    state.profileRetrying.add(profile.id); state.profileRegistryEpoch++; renderProfileCards();
    profileMessage(t("正在重新准备 {value1} 的专属空间…", {value1: profile.name}));
    try {
      const result = await api(`/api/profiles/${encodeURIComponent(profile.id)}/retry`, {global: true, method: 'POST', body: '{}'});
      const updated = result.profile || result;
      if (!validProfile(updated)) throw interfaceError("准备状态返回不完整，请重试。");
      state.profiles = state.profiles.map((item) => item.id === profile.id ? updated : item);
      state.profileWatchId = profile.id;
      profileMessage(profileStatus(updated) === 'ready' ? t("专属空间已准备好，选择头像即可进入。") : profileStatus(updated) === 'error' ? updated.error || t("准备失败，请稍后重试。") : t("已重新开始准备，请稍候。"), profileStatus(updated) === 'error');
    } catch (error) { profileMessage(errorMessage(error), true); }
    finally {
      state.profileRetrying.delete(profile.id);
      if (!$('profile-grid').hidden) {
        const restore = !profileFocusKey() || profileFocusKey() === `retry:${profile.id}`;
        renderProfileCards();
        if (restore) focusProfileAction(profileStatus(state.profiles.find((item) => item.id === profile.id)) === 'error' ? `retry:${profile.id}` : `select:${profile.id}`);
      }
    }
  }
  function applyProfile(profile) {
    const first = !state.profile;
    leavePresence();
    saveProfileView(); clearTimeout(searchTimer);
    for (const controller of state.profileRequests) controller.abort();
    state.profileRequests.clear(); state.librarySettingsRequest++; state.librarySaving = false; state.libraryFresh = false; state.librarySettings = Library.settings();
    $('platform-dialog').close();
    state.profileEpoch++; state.interfaceRequest++; state.interfaceSaving = false; state.request++; state.detailRequest++;
    state.launchOptions = null; state.launchChoice = 'auto';
    state.downloadOptions = null; state.downloadPending = null;
    if ($('downloads-dialog').open) $('downloads-dialog').close();
    closeLibraryManagement({restoreFocus: false}); state.managementData = null; state.managementBusy = false;
    state.displayReadController?.abort(); state.displayReadController = null;
    state.displayRequest++; state.displayData = null; state.displayDraft = null; state.displayLoading = false; state.displaySaving = false; state.displayFresh = false;
    if ($('display-dialog').open) $('display-dialog').close();
    if (state.playMode) closePlayer({refresh: false, presence: false});
    state.profile = profile;
    I18n.setLocale(remembered('retroone:interface-locale') || 'zh-CN'); syncLanguagePickers();
    const defaults = {platform: '', query: '', mode: '', reviewFilter: '', favorites: false, recent: false, initialGame: null, detailOpen: false, sort: 'popularity', direction: 'desc'};
    const linkedView = first && initial.size && (!initial.has('player') || initial.get('player') === profile.id) ? {platform: initial.get('platform') || '', query: initial.get('q') || '', mode: ['browser', 'stream', 'unavailable'].includes(initial.get('mode')) ? initial.get('mode') : '', reviewFilter: validReviewFilter(initial.get('review_filter')), favorites: initial.get('favorite') === 'true', recent: initial.get('recent') === 'true', initialGame: initial.get('game'), detailOpen: initial.get('view') === 'detail', sort: Library.sorts.includes(initial.get('sort')) ? initial.get('sort') : initial.get('recent') === 'true' ? 'last_played' : 'popularity', direction: initial.get('direction') === 'asc' ? 'asc' : 'desc'} : null;
    Object.assign(state, defaults, linkedView || readProfileView(profile.id), {games: [], platforms: [], total: 0, selected: null, session: null, health: null, emptyState: null, migrationData: null, loading: false, scanPending: false, wasScanning: false, healthRefreshing: false, launching: false, pendingProfile: null, networkCapabilities: null, networkInvites: [], networkRoom: null, networkLoading: false, networkBusy: false, networkVersion: 0, networkCreate: null, networkSeen: new Set(), activeNetworkRoomId: null});
    state.reviewFilter = validReviewFilter(state.reviewFilter);
    state.detailNeighbors = null; state.detailPaging = false; state.listScroll = 0; state.listSelection = null; renderDetailPage();
    state.favoritePending.clear();
    $('stop-session').disabled = false; $('stop-play').disabled = false;
    $('scan-button').disabled = false; $('scan-button').innerHTML = `${icon('library')}<span>${escapeHTML(t("管理游戏库"))}</span>`;
    $('migration-banner').hidden = true; $('toast').hidden = true;
    state.connectionStatus = 'connecting';
    remember('retroone:last-profile', profile.id); renderCurrentProfile(); renderSession(); renderDetail(null); renderPlatforms(); renderFilters(); updateURL();
    renderFamilyPresence();
    $('profile-dialog').close(); syncModalBody();
    heartbeatPresence();
    refreshNetwork();
    const epoch = state.profileEpoch;
    Promise.allSettled([loadInterfaceSettings(), loadPlatforms(), loadLibrarySettings({preserveSort: state.recent || Boolean(linkedView && initial.has('sort'))}).then(() => { if (epoch === state.profileEpoch) return loadGames(); }), refreshHealth(), refreshSession(), refreshMigration()]).then(() => {
      if (epoch === state.profileEpoch && !document.querySelector('dialog[open]') && [document.body, $('profile-button')].includes(document.activeElement)) {
        // Focusing a card selects it. Keep an explicit link or restored selection,
        // including games whose card is beyond the first catalog page.
        const selectedCard = [...$('game-grid').querySelectorAll('.game-select')].find((button) => button.dataset.select === String(state.selected?.id));
        const detailFocus = state.selected && state.detailOpen ? ($('launch-game')?.disabled ? $('profile-button') : $('launch-game')) : null;
        (state.detailOpen ? $('detail-back') : selectedCard || detailFocus || $('game-grid').querySelector('.game-select'))?.focus({preventScroll: true});
      }
    });
  }
  function focusProfileNeighbor(direction) {
    const languages = [...$('profile-dialog').querySelectorAll('[data-language-picker]:not(:disabled)')];
    let buttons;
    if (!$('profile-confirm').hidden) buttons = [...$('profile-confirm').querySelectorAll('button:not(:disabled)')];
    else if (!$('profile-editor').hidden) buttons = [$('profile-name'), ...$('profile-editor').querySelectorAll('button:not(:disabled)')];
    else {
      const actions = [...languages, ...$('profile-grid').querySelectorAll('[data-profile-action]:not(:disabled), .profile-error-details summary'), $('close-profiles'), $('retry-profiles')].filter(button => !button.hidden && button.getClientRects().length);
      const active = document.activeElement, horizontal = direction === 'left' || direction === 'right';
      if (actions.length) {
        if (!actions.includes(active)) { actions[0].focus(); return; }
        const origin = active.getBoundingClientRect(), sign = direction === 'left' || direction === 'up' ? -1 : 1;
        const candidates = actions.filter((button) => button !== active).map((button) => {
          const rect = button.getBoundingClientRect();
          const dx = rect.x + rect.width / 2 - origin.x - origin.width / 2;
          const dy = rect.y + rect.height / 2 - origin.y - origin.height / 2;
          return {button, along: (horizontal ? dx : dy) * sign, across: Math.abs(horizontal ? dy : dx)};
        }).filter((item) => item.along > 5).sort((a, b) => a.along + a.across * 3 - b.along - b.across * 3);
        (candidates[0]?.button || actions[sign < 0 ? actions.length - 1 : 0]).focus();
        return;
      }
      buttons = [...$('profile-dialog').querySelectorAll('#retry-profiles:not([hidden])')];
    }
    buttons = [...languages, ...buttons];
    if (!buttons.length) return;
    const index = buttons.indexOf(document.activeElement);
    const delta = direction === 'left' || direction === 'up' ? -1 : 1;
    buttons[index < 0 ? 0 : (index + delta + buttons.length) % buttons.length]?.focus();
  }

  function formatSize(bytes) {
    if (bytes == null || !Number.isFinite(Number(bytes))) return t("未知");
    bytes = Number(bytes);
    if (bytes < 1024) return `${number(bytes)} B`;
    const unit = bytes >= 1024 ** 3 ? 'GB' : bytes >= 1024 ** 2 ? 'MB' : 'KB';
    const divisor = unit === 'GB' ? 1024 ** 3 : unit === 'MB' ? 1024 ** 2 : 1024;
    return `${I18n.number(bytes / divisor, {maximumFractionDigits: 1})} ${unit}`;
  }

  function platformIcon(id = '') {
    if (/^(gb|gbc|gba|nds|3ds|psp|psvita|ngp|ngpc|ws|wsc|gamegear|lynx)/i.test(id)) return 'handheld';
    if (/arcade|mame|fbneo|fba|cps|neogeo/i.test(id)) return 'arcade';
    if (/dos|windows|pc98|scumm|amiga|msx|pc$/i.test(id)) return 'pc';
    return 'console';
  }

  function notify(message, error = false) {
    statusText('toast', message);
    $('toast').dataset.error = String(error);
    $('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 6500 : 3500);
  }

  function updateURL(push = false) {
    const params = new URLSearchParams();
    if (state.profile) params.set('player', state.profile.id);
    if (state.platform) params.set('platform', state.platform);
    if (state.query) params.set('q', state.query);
    if (state.mode) params.set('mode', state.mode);
    if (state.reviewFilter) params.set('review_filter', state.reviewFilter);
    if (state.favorites) params.set('favorite', 'true');
    if (state.recent) params.set('recent', 'true');
    if (state.sort !== 'default') params.set('sort', state.sort);
    params.set('direction', state.direction);
    if (state.detailOpen) params.set('view', 'detail');
    const gameId = state.playGameId || state.initialGame || state.selected?.id;
    if (gameId) params.set('game', gameId);
    history[push ? 'pushState' : 'replaceState']({...history.state, retroDetail: state.detailOpen, detailFromLibrary: state.detailOpen && (push || Boolean(history.state?.detailFromLibrary)), listScroll: state.listScroll, listSelection: state.listSelection}, '', `${location.pathname}${params.size ? '?' + params.toString() : ''}`);
  }

  function renderFilters() {
    const nextReview = Object.keys(reviewFilters)[(Object.keys(reviewFilters).indexOf(state.reviewFilter) + 1) % Object.keys(reviewFilters).length];
    $('review-filter').textContent = t("检测：{value1}", {value1: reviewFilters[state.reviewFilter]});
    $('review-filter').classList.toggle('is-active', Boolean(state.reviewFilter));
    $('review-filter').setAttribute('aria-label', t("检测筛选：{value1}。按确认切换为{value2}", {value1: reviewFilters[state.reviewFilter], value2: reviewFilters[nextReview]}));
    $('review-filter').title = t("点击切换：全部 → 需要处理 → 待验证 → 已有观察记录");
    if (document.activeElement !== $('search')) $('search').value = state.query;
    $('nav-library').classList.toggle('is-active', !state.favorites);
    $('recent-filter').classList.toggle('is-active', state.recent);
    $('recent-filter').setAttribute('aria-pressed', String(state.recent));
    $('nav-library').setAttribute('aria-pressed', String(!state.favorites));
    $('nav-favorites').classList.toggle('is-active', state.favorites);
    $('nav-favorites').setAttribute('aria-pressed', String(state.favorites));
    document.querySelectorAll('[data-mode]').forEach((button) => {
      const active = button.dataset.mode === state.mode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    const platform = state.platforms.find((item) => item.id === state.platform);
    $('library-title').replaceChildren(document.createTextNode(state.favorites ? t("我的收藏 ") : state.recent ? t("最近游玩 ") : platform?.name ? `${platformName(platform.id, platform.name)} ` : t("全部游戏 ")));
    const count = document.createElement('span'); count.id = 'result-count';
    count.textContent = state.loading ? t("正在查找…") : t("{value1} 款", {value1: number(state.total)});
    $('library-title').append(count);
    $('section-eyebrow').textContent = state.favorites ? 'YOUR FAVORITES' : state.recent ? 'RECENTLY PLAYED' : 'THE COLLECTION';
    document.title = state.detailOpen && state.selected ? `${state.selected.title} · RetroOne` : `${state.favorites ? t("我的收藏") : state.recent ? t("最近游玩") : (platform && platformName(platform.id, platform.name)) || t("私人游戏馆")} · RetroOne`;
  }

  const librarySortLabels = localizedLabels({default: '默认顺序', popularity: '大众热度', release_date: '发行日期', added_at: '加入时间', play_count: '我的游玩次数', last_played: '最近游玩时间', name: '游戏名称'});
  function renderLibrarySort() {
    $('library-sort').innerHTML = Library.sorts.map(id => `<option value="${id}">${escapeHTML(librarySortLabels[id])}</option>`).join('');
    $('library-sort').value = state.sort;
    $('library-sort').disabled = state.librarySaving;
    $('sort-direction').disabled = state.librarySaving || state.sort === 'default';
    $('sort-direction').textContent = state.direction === 'asc' ? t('升序 ↑') : t('降序 ↓');
    $('sort-direction').setAttribute('aria-label', t('排序方向：{value1}', {value1: state.direction === 'asc' ? t('升序') : t('降序')}));
    $('sort-note').textContent = state.sort === 'popularity' ? t('按有出处的公开热度排序；没有热度资料的游戏按名称排在后面。') : state.sort === 'play_count' ? t('记录这个玩家的启动次数；尚未开始记录的历史次数无法补算。') : state.sort === 'added_at' ? t('按首次入库时间排序；早期未记录日期的游戏排在后面。') : state.sort === 'release_date' ? t('缺少发行日期的游戏排在后面。') : '';
  }
  async function loadLibrarySettings({preserveSort = false} = {}) {
    const epoch = state.profileEpoch, request = ++state.librarySettingsRequest;
    try {
      const result = await api('/api/settings/library', {timeoutMs: 6000});
      if (epoch !== state.profileEpoch || request !== state.librarySettingsRequest) return;
      if (!result.settings || !Array.isArray(result.settings.pinned_platforms)) throw interfaceError('游戏库偏好返回不完整。');
      state.librarySettings = Library.settings(result.settings); state.libraryFresh = true;
      if (!preserveSort) { state.sort = state.librarySettings.sort; state.direction = state.librarySettings.direction; }
    } catch (error) {
      if (epoch !== state.profileEpoch || request !== state.librarySettingsRequest || error.name === 'AbortError') return;
      state.libraryFresh = false; notify(t('未能读取游戏库偏好：{message}', {message: errorMessage(error)}), true);
    }
    renderLibrarySort(); renderPlatforms();
    if ($('platform-dialog').open) renderPlatformChooser();
  }
  async function saveLibrarySettings(changes) {
    if (!state.profile || state.librarySaving) return false;
    const enteredEpoch = state.profileEpoch;
    if (!state.libraryFresh) {
      await loadLibrarySettings({preserveSort: true});
      if (enteredEpoch !== state.profileEpoch || !state.libraryFresh) return false;
    }
    const epoch = state.profileEpoch, request = ++state.librarySettingsRequest, previous = state.librarySettings;
    const settings = Library.settings({...previous, ...changes});
    state.librarySaving = true; renderLibrarySort(); if ($('platform-dialog').open) renderPlatformChooser();
    try {
      const result = await api('/api/settings/library', {method: 'PUT', body: JSON.stringify(settings), timeoutMs: 6000});
      if (epoch !== state.profileEpoch || request !== state.librarySettingsRequest) return false;
      if (!result.settings) throw interfaceError('游戏库偏好返回不完整。');
      state.librarySettings = Library.settings(result.settings);
      if (Object.hasOwn(changes, 'sort')) state.sort = state.librarySettings.sort;
      if (Object.hasOwn(changes, 'direction')) state.direction = state.librarySettings.direction;
      return true;
    } catch (error) {
      if (epoch === state.profileEpoch && request === state.librarySettingsRequest) notify(t('未能保存游戏库偏好：{message}', {message: errorMessage(error)}), true);
      return false;
    } finally {
      if (epoch === state.profileEpoch && request === state.librarySettingsRequest) {
        state.librarySaving = false; renderLibrarySort(); renderPlatforms();
        if ($('platform-dialog').open) renderPlatformChooser();
      }
    }
  }
  async function changeLibrarySort(sort, direction) {
    if (await saveLibrarySettings({sort, direction})) setFilter({sort: state.sort, direction: state.direction});
  }
  function visiblePlatforms() { return Library.platforms(state.platforms, state.librarySettings.pinned_platforms, state.platform); }
  function renderPlatforms() {
    const total = state.platforms.reduce((sum, platform) => sum + (Number(platform.count) || 0), 0);
    const items = [{id: '', name: t('全部游戏'), count: total}, ...visiblePlatforms()];
    $('platform-nav').innerHTML = items.map(platform => `<button class="platform-button ${state.platform === platform.id ? 'is-active' : ''}" data-platform="${escapeHTML(platform.id)}" aria-pressed="${state.platform === platform.id}" title="${escapeHTML(t('{value1} · {value2} 款', {value1: platformName(platform.id, platform.name), value2: number(platform.count)}))}">${platform.id ? Library.platformArt(platform.id) : icon('library', 'platform-art')}<span>${escapeHTML(platformName(platform.id, platform.name))}</span><small>${state.librarySettings.pinned_platforms.includes(platform.id) ? escapeHTML(t('已置顶')) + ' · ' : ''}${number(platform.count)}</small></button>`).join('');
    $('stat-games').textContent = number(total); $('stat-platforms').textContent = number(state.platforms.length);
  }
  function renderPlatformChooser() {
    const pins = state.librarySettings.pinned_platforms, query = $('platform-search').value.trim().toLocaleLowerCase();
    const pinned = pins.map(id => state.platforms.find(item => item.id === id)).filter(Boolean);
    $('pinned-platforms').innerHTML = pinned.length ? `<h3>${escapeHTML(t('置顶顺序'))}</h3><ol>${pinned.map((item, index) => `<li><span>${Library.platformArt(item.id)}${escapeHTML(platformName(item.id, item.name))}</span><button class="icon-button" data-pin-move="${escapeHTML(item.id)}" data-delta="-1" aria-label="${escapeHTML(t('将 {value1} 前移', {value1: platformName(item.id, item.name)}))}" ${index === 0 || state.librarySaving ? 'disabled' : ''}>↑</button><button class="icon-button" data-pin-move="${escapeHTML(item.id)}" data-delta="1" aria-label="${escapeHTML(t('将 {value1} 后移', {value1: platformName(item.id, item.name)}))}" ${index === pinned.length - 1 || state.librarySaving ? 'disabled' : ''}>↓</button><button class="text-button" data-pin="${escapeHTML(item.id)}" ${state.librarySaving ? 'disabled' : ''}>${escapeHTML(t('取消置顶'))}</button></li>`).join('')}</ol>` : `<p class="platform-empty-pins">${escapeHTML(t('把常玩的平台置顶，首页也会显示最近玩过的平台。'))}</p>`;
    const matches = state.platforms.filter(item => [item.id, item.name, platformName(item.id, item.name)].join(' ').toLocaleLowerCase().includes(query));
    $('platform-choices').innerHTML = matches.length ? matches.map(item => `<div class="platform-choice ${pins.includes(item.id) ? 'is-pinned' : ''}"><button data-choose-platform="${escapeHTML(item.id)}" class="platform-choice-main">${Library.platformArt(item.id)}<span><strong>${escapeHTML(platformName(item.id, item.name))}</strong><small>${escapeHTML(t('{value1} 款', {value1: number(item.count)}))}</small></span></button><button class="icon-button" data-pin="${escapeHTML(item.id)}" aria-pressed="${pins.includes(item.id)}" aria-label="${escapeHTML((pins.includes(item.id) ? t('取消置顶') : t('置顶平台')) + ' ' + platformName(item.id, item.name))}" ${state.librarySaving ? 'disabled' : ''}>${icon('pin')}</button></div>`).join('') : `<p>${escapeHTML(t('没有找到这个平台。'))}</p>`;
    $('platform-save-note').textContent = state.librarySaving ? t('正在保存…') : !state.libraryFresh ? t('偏好暂未读取成功，操作时会重试。') : t('已与当前玩家同步');
  }
  function openPlatforms() { renderPlatformChooser(); $('platform-dialog').showModal(); syncModalBody(); $('platform-search').focus(); }
  function closePlatforms() { $('platform-dialog').close(); syncModalBody(); $('choose-platforms').focus({preventScroll: true}); }
  async function changePlatformPin(id, delta = 0) {
    if (state.librarySaving) return;
    const pins = [...state.librarySettings.pinned_platforms], index = pins.indexOf(id);
    if (delta && index >= 0) { const next = Math.max(0, Math.min(pins.length - 1, index + delta)); [pins[index], pins[next]] = [pins[next], pins[index]]; }
    else if (index >= 0) pins.splice(index, 1); else pins.push(id);
    const focused = document.activeElement, attr = delta ? 'data-pin-move' : 'data-pin';
    await saveLibrarySettings({pinned_platforms: pins});
    if (!focused?.isConnected && $('platform-dialog').open) [...$('platform-dialog').querySelectorAll(`[${attr}]`)].find(button => button.getAttribute(attr) === id && (!delta || button.dataset.delta === String(delta)) && !button.disabled)?.focus({preventScroll: true});
  }

  function fallbackCover(game) {
    return `<span class="cover-fallback">${icon(platformIcon(game.platform))}<strong>${escapeHTML(game.title)}</strong><small>${escapeHTML(platformName(game.platform, game.platform_name) || 'RETROONE')}</small></span>`;
  }

  function cover(game, detail = false) {
    const url = localURL(game.cover_url);
    return url ? `<span class="cover-image">${fallbackCover(game)}<img src="${escapeHTML(url)}" alt="${escapeHTML(detail ? t('《{title}》游戏封面', {title: game.title}) : '')}" loading="${detail ? 'eager' : 'lazy'}" decoding="async" data-cover-id="${escapeHTML(game.id)}"></span>` : fallbackCover(game);
  }

  function reviewStatus(game) {
    const status = game.review?.verification?.status;
    return Object.hasOwn(reviewStatuses, status) ? status : 'unverified';
  }
  function duplicateCount(game) {
    const count = game.review?.duplicates?.total_count;
    return Number.isSafeInteger(count) && count > 0 ? count : null;
  }
  function reviewCardNote(game) {
    const status = reviewStatus(game), count = duplicateCount(game);
    const warning = status === 'dependency_error' ? t("缺文件 / 依赖") : status === 'load_failed' ? t("启动失败") : '';
    return warning ? `<span class="card-review-note is-warning">${escapeHTML(warning)}</span>` : count > 1 ? `<span class="card-review-note">${escapeHTML(t("另 {value1} 份相同副本", {value1: number(count - 1)}))}</span>` : '';
  }
  function variantTags(game) {
    const tags = game.review?.variant_tags;
    if (!Array.isArray(tags)) return '';
    const valid = tags.filter((tag) => tag && typeof tag.raw === 'string' && tag.raw.trim());
    return valid.length ? `<div class="variant-tags" aria-label="${escapeHTML(t("版本标记"))}">${valid.map((tag) => `<span title="${escapeHTML([tag.raw, tag.source].filter((item) => typeof item === 'string' && item).join(' · '))}">${escapeHTML(typeof tag.label === 'string' && tag.label ? tag.label : tag.raw)}</span>`).join('')}</div>` : '';
  }
  function reviewDetails(game) {
    const review = game.review || {}, verification = review.verification || {}, status = reviewStatus(game);
    const notes = Array.isArray(verification.notes) ? verification.notes.filter((item) => typeof item === 'string') : [];
    const errors = Array.isArray(verification.errors) ? verification.errors.filter((item) => typeof item === 'string') : [];
    const timestamp = verification.checked_at;
    const date = timestamp == null ? null : new Date(typeof timestamp === 'number' ? timestamp * 1000 : timestamp);
    const checked = date && Number.isFinite(date.getTime()) ? I18n.date(date, {dateStyle: 'medium', timeStyle: 'short', hour12: false}) : '';
    const context = [checked, modeLabels[verification.mode], typeof verification.engine === 'string' ? verification.engine : ''].filter(Boolean).join(' · ');
    const fingerprint = review.fingerprint || {};
    const hash = typeof fingerprint.sha256 === 'string' && /^[a-f0-9]{64}$/i.test(fingerprint.sha256) ? fingerprint.sha256 : '';
    const tags = Array.isArray(review.variant_tags) ? review.variant_tags.filter((tag) => tag && typeof tag.raw === 'string') : [];
    const members = Array.isArray(review.duplicates?.members) ? review.duplicates.members.filter((member) => member && typeof member.id === 'string' && member.id) : [];
    const evidence = hash || tags.length;
    return `<section class="detail-review" aria-label="${escapeHTML(t("检测结果"))}"><h3>${escapeHTML(t("检测结果"))}</h3><p class="review-status ${['dependency_error', 'load_failed'].includes(status) ? 'is-warning' : ''}">${escapeHTML(reviewStatuses[status])}</p>${context ? `<p class="review-context">${escapeHTML(context)}</p>` : ''}${notes.length ? `<ul class="review-notes">${notes.map((note) => `<li>${escapeHTML(diagnostic(note))}</li>`).join('')}</ul>` : ''}${errors.length ? `<ul class="review-notes is-warning">${errors.map((error) => `<li>${escapeHTML(diagnostic(error))}</li>`).join('')}</ul>` : ''}<p class="review-caution">${escapeHTML(t("文件校验或内容相同，不代表已验证可玩；观察记录只对应记录时的运行环境。"))}</p>${evidence ? `<button class="text-button review-toggle" id="review-evidence-toggle" aria-expanded="false" aria-controls="review-evidence">${escapeHTML(t("查看指纹与版本依据"))}</button><div class="review-evidence" id="review-evidence" hidden>${hash ? `<dl><dt>SHA-256</dt><dd><code>${escapeHTML(hash)}</code></dd>${typeof fingerprint.scope === 'string' ? `<dt>${escapeHTML(t("计算范围"))}</dt><dd>${escapeHTML(fingerprint.scope)}</dd>` : ''}<dt>${escapeHTML(t("范围完整性"))}</dt><dd>${escapeHTML(fingerprint.complete === true ? t("已覆盖声明范围") : fingerprint.complete === false ? t("范围不完整") : t("尚未说明"))}</dd></dl>` : ''}${tags.length ? `<ul>${tags.map((tag) => `<li><span>${escapeHTML(tag.raw)}</span>${typeof tag.source === 'string' && tag.source ? `<small>${escapeHTML(tag.source)}</small>` : ''}</li>`).join('')}</ul>` : ''}</div>` : ''}${members.length > 1 ? `<button class="text-button review-toggle" id="review-copies-toggle" aria-expanded="false" aria-controls="review-copies">${escapeHTML(t("查看相同内容的记录（{value1} 份）", {value1: number(duplicateCount(game) || members.length)}))}</button><div class="review-copies" id="review-copies" hidden><p>${escapeHTML(t("每份记录保留自己的收藏、游玩记录和存档入口。"))}</p>${members.map((member, index) => `<div class="review-copy"><strong>${escapeHTML(member.title || t("未命名记录"))}</strong><small>${escapeHTML(member.collection || '')}${escapeHTML(member.favorite ? t(" · 已收藏") : '')}${escapeHTML(member.played_at ? t(" · 有游玩记录") : '')}</small><button class="button button-secondary button-small" id="review-copy-${index}" data-open-copy="${escapeHTML(member.id)}" ${member.id === game.id ? 'disabled' : ''}>${escapeHTML(member.id === game.id ? t("当前这份记录") : t("打开这份记录"))}</button></div>`).join('')}</div>` : ''}</section>`;
  }
  function launchConfigDetails(game) {
    const config = game.launch_config;
    if (!config || typeof config !== 'object') return '';
    const labels = {configured: t("配置已生成"), needs_review: t("配置待检查"), blocked: t("配置受阻")};
    const label = Object.hasOwn(labels, config.status) ? labels[config.status] : labels.needs_review;
    const settings = [config.engine, config.core, config.input_profile].filter((value) => typeof value === 'string' && value);
    const issues = Array.isArray(config.issues) ? config.issues.filter((value) => typeof value === 'string') : [];
    return `<section class="detail-launch-config" aria-label="${escapeHTML(t("启动配置"))}"><p><strong>${escapeHTML(t("启动配置"))}</strong> · ${escapeHTML(label)}</p>${settings.length ? `<p class="review-context">${escapeHTML(settings.join(' · '))}</p>` : ''}${issues.length ? `<ul class="review-notes ${config.status === 'blocked' ? 'is-warning' : ''}">${issues.map((issue) => `<li>${escapeHTML(diagnostic(issue))}</li>`).join('')}</ul>` : ''}<p class="review-caution">${escapeHTML(t("配置记录与实测结果分别记录。"))}</p></section>`;
  }

  async function openDuplicateRecord(id) {
    const request = ++state.detailRequest, previousId = state.selected?.id;
    try {
      const game = await api(`/api/games/${encodeURIComponent(id)}`);
      if (request !== state.detailRequest || state.selected?.id !== previousId) return;
      if (game.id !== id) throw interfaceError("副本记录返回不完整，请重新读取。");
      state.selected = game;
      document.querySelectorAll('[data-game-id]').forEach((item) => {
        const selected = item.dataset.gameId === id;
        item.classList.toggle('is-selected', selected);
        item.querySelector('.game-select').setAttribute('aria-pressed', String(selected));
      });
      renderDetail(game); updateURL();
      ($('launch-game')?.disabled ? $('review-copies-toggle') || $('detail-favorite') : $('launch-game'))?.focus({preventScroll: true});
    } catch (error) { if (error.name !== 'AbortError' && request === state.detailRequest) notify(errorMessage(error), true); }
  }

  function card(game) {
    const mode = modeLabels[game.mode] ? game.mode : 'unavailable';
    return `<article class="game-card ${state.selected?.id === game.id ? 'is-selected' : ''}" data-game-id="${escapeHTML(game.id)}"><button class="game-select" data-select="${escapeHTML(game.id)}" aria-label="${escapeHTML(t("查看 {value1}，{value2}，{value3}", {value1: game.title, value2: platformName(game.platform, game.platform_name), value3: modeLabels[mode]}))}" aria-pressed="${state.selected?.id === game.id}"><div class="game-cover">${cover(game)}${deviceCacheBadge(game.id)}<span class="game-mode ${mode}">${icon(modeIcons[mode])}${escapeHTML(modeLabels[mode])}</span></div><h3 class="game-title" title="${escapeHTML(game.title)}">${escapeHTML(game.title)}</h3><span class="game-subtitle">${icon(platformIcon(game.platform))}<span>${escapeHTML(platformName(game.platform, game.platform_name))}</span></span>${reviewCardNote(game)}</button><button class="icon-button card-favorite ${game.favorite ? 'is-favorite' : ''}" data-favorite="${escapeHTML(game.id)}" aria-label="${escapeHTML(game.favorite ? t("取消收藏") : t("收藏"))} ${escapeHTML(game.title)}" aria-pressed="${Boolean(game.favorite)}" ${state.favoritePending.has(game.id) ? 'disabled' : ''}>${icon('star')}</button></article>`;
  }

  function bindImageErrors(root) {
    root.querySelectorAll('img[data-cover-id]').forEach((img) => {
      const loaded = () => { if (img.parentElement?.classList.contains('cover-image')) img.parentElement.dataset.loaded = 'true'; };
      img.addEventListener('load', loaded, {once: true});
      if (img.complete && img.naturalWidth) loaded();
      img.addEventListener('error', () => {
        if (img.parentElement?.classList.contains('cover-image')) { img.remove(); return; }
        const game = state.games.find((item) => String(item.id) === img.dataset.coverId) || state.selected;
        if (!game) { img.remove(); return; }
        const fallback = document.createElement('span');
        fallback.innerHTML = fallbackCover(game);
        img.replaceWith(fallback.firstElementChild);
      }, {once: true});
    });
  }

  function renderGames(append = false, start = 0) {
    if (append) $('game-grid').insertAdjacentHTML('beforeend', state.games.slice(start).map(card).join(''));
    else $('game-grid').innerHTML = state.games.map(card).join('');
    if (state.games.length) state.emptyState = null;
    bindImageErrors($('game-grid'));
    $('empty-state').hidden = state.games.length > 0;
    if (!state.games.length) showEmpty('empty');
    $('pagination').hidden = state.games.length === 0;
    $('pagination-label').textContent = t("已显示 {value1} / {value2} 款", {value1: number(state.games.length), value2: number(state.total)});
    $('load-more').hidden = state.games.length >= state.total;
    $('load-more').disabled = false;
    $('load-more').innerHTML = `${escapeHTML(t("载入更多"))}${icon('arrow')}`;
    $('games-region').setAttribute('aria-busy', 'false');
  }

  function renderSkeletons() {
    $('empty-state').hidden = true;
    $('pagination').hidden = true;
    $('game-grid').innerHTML = Array.from({length: 8}, () => '<div class="skeleton" aria-hidden="true"><div class="game-cover"></div><div class="skeleton-line"></div><div class="skeleton-line short"></div></div>').join('');
    $('games-region').setAttribute('aria-busy', 'true');
  }

  function showEmpty(kind, message) {
    state.emptyState = {kind, message};
    const filtered = state.query || state.mode || state.platform || state.reviewFilter;
    $('empty-state').hidden = false;
    $('empty-action').dataset.action = kind === 'error' ? 'retry' : filtered ? 'reset' : state.favorites || state.recent ? 'library' : 'scan';
    $('empty-title').textContent = kind === 'error' ? t("暂时没能打开游戏库") : filtered ? t("没有找到匹配的游戏") : state.favorites ? t("留一个位置，给最爱的游戏") : state.recent ? t("从第一款游戏开始") : t("你的游戏，正在等待入馆");
    $('empty-copy').textContent = message || (filtered ? t("试试更短的关键词，或切换平台、运行方式和检测筛选。") : state.favorites ? t("点击游戏封面上的星标，下次就能更快找到它。") : state.recent ? t("开始游玩后，这里会留下你的最近记录。") : t("将整理好的游戏目录挂载后，更新游戏库即可开始浏览。"));
    $('empty-action').textContent = kind === 'error' ? t("重新加载") : filtered ? t("清除筛选") : state.favorites || state.recent ? t("浏览游戏库") : t("更新游戏库");
  }

  function detailFacts(game) {
    const data = game.details || {}, facts = [];
    const list = value => Array.isArray(value) ? value.filter(item => typeof item === 'string').join(' / ') : typeof value === 'string' || typeof value === 'number' ? String(value) : '';
    const add = (label, value) => { if (value !== undefined && value !== null && String(value).trim()) facts.push(`<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`); };
    add(t('发行日期'), data.release_date); add(t('发行商'), list(data.publisher)); add(t('开发商'), list(data.developer));
    add(t('类型'), list(data.genres)); add(t('玩家人数'), list(data.players)); add(t('语言'), list(data.languages));
    const added = game.added_at || game.first_added_at;
    if (Number.isFinite(added) && added > 0) add(t('加入游戏库'), I18n.date(new Date(added * 1000), {dateStyle: 'medium'}));
    if (Number.isSafeInteger(game.play_count)) add(t('已记录游玩次数'), t('{value1} 次', {value1: number(game.play_count)}));
    add(t('默认运行方式'), modeLabels[game.mode] || modeLabels.unavailable); add(t('游戏大小'), formatSize(game.unpacked_size ?? game.size));
    if (game.collection) add(t('来自收藏'), game.collection);
    if (duplicateCount(game)) add(t('相同内容副本'), t('{value1} 份（含当前）', {value1: number(duplicateCount(game))}));
    return facts.join('');
  }
  function detailPopularity(game) {
    const metric = game.details?.popularity;
    const url = Library.secureURL(metric?.url);
    if (!metric || !url || typeof metric.source !== 'string' || !Number.isFinite(metric.score)) return '';
    const unit = {million_copies: t('百万份'), sales_rank: t('销售排名'), copies: t('份'), rating: t('评分')}[metric.unit] || (typeof metric.unit === 'string' ? metric.unit : '');
    const value = Number.isFinite(metric.value) ? `${number(metric.value)} ${unit}`.trim() : '';
    return `<p class="detail-popularity">${icon('info')}<span>${escapeHTML(t('公开热度参考'))}${value ? ` · <strong>${escapeHTML(value)}</strong>` : ''} · <a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(metric.source)}</a>${typeof metric.as_of === 'string' ? ` · ${escapeHTML(metric.as_of)}` : ''}</span></p>`;
  }
  function launchOptionLabel(id) {
    return t({'auto': '自动选择（推荐）', 'stream': '云游戏', 'browser': '浏览器运行（实验）'}[id] || '未知启动方式');
  }
  let launchWebGL2;
  function launchClientCapabilities() {
    if (launchWebGL2 === undefined) {
      try {
        const context = document.createElement('canvas').getContext('webgl2');
        launchWebGL2 = Boolean(context);
        context?.getExtension('WEBGL_lose_context')?.loseContext();
      } catch { launchWebGL2 = false; }
    }
    return {secure: Boolean(window.isSecureContext), rtc: typeof window.RTCPeerConnection === 'function',
      crypto: Boolean(window.crypto?.subtle), threads: Boolean(window.crossOriginIsolated && typeof window.SharedArrayBuffer === 'function'), webgl2: launchWebGL2};
  }
  function usableLaunchOptions(data, capabilities) {
    const allowed = ['auto', 'stream', 'browser-http', 'browser-p2p'];
    const options = [];
    for (const id of allowed) {
      const original = data?.options?.find(option => option?.id === id);
      if (!original) continue;
      let reason = typeof original.reason === 'string' ? original.reason : '';
      let available = original.available === true;
      if (available && id === 'browser-p2p' && (!capabilities.secure || !capabilities.rtc || !capabilities.crypto)) {
        available = false; reason = t('P2P 分享需要 HTTPS，以及支持 WebRTC 和文件校验的浏览器。');
      }
      if (available && original.requires_threads && (!capabilities.threads || !capabilities.webgl2)) {
        available = false; reason = t('此内核需要 HTTPS、跨源隔离、共享内存和 WebGL 2；当前浏览器环境不满足。');
      }
      options.push({...original, available, reason});
    }
    return options;
  }
  function mergeBrowserLaunchOptions(options, allowP2P = true) {
    const http = options.find(option => option.id === 'browser-http');
    const p2p = options.find(option => option.id === 'browser-p2p');
    const chosen = allowP2P && p2p?.available ? p2p : http?.available ? http : null;
    return ['auto', 'stream'].map(id => options.find(option => option.id === id) || {id, available: false}).concat({
      ...(chosen || http || p2p), id: 'browser', available: Boolean(chosen), launchMode: chosen?.id || null,
      reason: chosen ? '' : http?.reason || (!allowP2P && p2p?.available ? t('当前无法直接下载；可开启 P2P 分享后重试，或选择云游戏。') : p2p?.reason || t('当前游戏暂不支持浏览器运行。')),
    });
  }
  function setBrowserP2PPreference(allowed) {
    state.browserP2P = Boolean(allowed);
    remember('retroone:browser-p2p', String(state.browserP2P));
    if (typeof window !== 'undefined' && window.RetroRomLibrary) {
      try { Promise.resolve(window.RetroRomLibrary.setSharing(state.browserP2P)).catch(error => downloadMessage(downloadProblemMessage(error), true)); }
      catch (error) { downloadMessage(downloadProblemMessage(error), true); }
      renderDownloads();
    }
  }
  function currentLaunchOptions(game) {
    const record = state.launchOptions;
    if (record && game && record.gameId === game.id && record.epoch === state.profileEpoch && record.data) {
      return usableLaunchOptions(record.data, launchClientCapabilities());
    }
    return [{id: 'auto', available: Boolean(game && game.mode !== 'unavailable'), reason: ''}];
  }
  function selectedLaunchMode(game) {
    const same = Boolean(game && state.launchOptions && state.launchOptions.gameId === game.id && state.launchOptions.epoch === state.profileEpoch);
    if (!same) return 'auto';
    return state.launchChoice === 'browser' ? mergeBrowserLaunchOptions(currentLaunchOptions(game), state.browserP2P).find(option => option.id === 'browser').launchMode : state.launchChoice;
  }
  function renderLaunchChoices(game) {
    const holder = $('launch-choices');
    if (!holder || !game || state.selected?.id !== game.id) return;
    const focusedId = holder.contains(document.activeElement) ? document.activeElement.id : null;
    const advancedOpen = holder.querySelector('.launch-advanced')?.open;
    const record = state.launchOptions, options = mergeBrowserLaunchOptions(currentLaunchOptions(game), state.browserP2P);
    if (!options.some(option => option.id === state.launchChoice)) state.launchChoice = 'auto';
    const selected = options.find(option => option.id === state.launchChoice);
    const experimental = selected?.id === 'browser';
    const advanced = `<details class="launch-advanced" ${advancedOpen ? 'open' : ''}><summary>${escapeHTML(t('高级设置'))}</summary><button type="button" id="launch-allow-p2p" class="launch-p2p-toggle" role="switch" aria-checked="${state.browserP2P}" aria-describedby="launch-p2p-note" ${state.launching ? 'disabled' : ''}><span>${escapeHTML(t('允许 P2P 分享'))}</span><span class="launch-switch-track" aria-hidden="true"></span></button><p id="launch-p2p-note">${escapeHTML(t('开启后可从其他家庭设备接收游戏分片，也会向它们分享。关闭后仅从服务器下载；不支持 P2P 时也会自动从服务器下载。'))}</p><p>${escapeHTML(t('仅保存在这台设备，也用于已下载游戏的 P2P 分享。'))}</p></details>`;
    const unavailable = options.filter(option => !option.available);
    const note = $('game-detail').querySelector('.detail-note');
    // The selected mode owns the description; the legacy note is reserved for
    // the cloud preparation progress and must not describe a different mode.
    if (note) note.hidden = !state.launching;
    const defaultReason = $('game-detail').querySelector('.detail-reason');
    if (defaultReason) defaultReason.hidden = selected?.id !== 'auto';
    holder.innerHTML = `<label for="launch-mode">${escapeHTML(t('启动方式'))}</label><select id="launch-mode" aria-describedby="launch-mode-note" ${state.launching ? 'disabled' : ''}>${options.map(option => `<option value="${escapeHTML(option.id)}" ${option.id === state.launchChoice ? 'selected' : ''} ${option.available ? '' : 'disabled'}>${escapeHTML(launchOptionLabel(option.id))}${option.available ? '' : ` · ${escapeHTML(t('不可用'))}`}</option>`).join('')}</select><div id="launch-mode-note" class="launch-mode-note" role="status">${record?.loading ? escapeHTML(t('正在读取可用启动方式…')) : record?.error ? `${escapeHTML(t('暂时无法读取实验选项，仍可按原方式启动。'))} <button type="button" class="text-button" id="retry-launch-options">${escapeHTML(t('重试'))}</button>` : experimental ? `<p>${escapeHTML(t('需下载完整 ROM：{size}，之后在浏览器运行。', {size: formatSize(selected.size_bytes)}))}</p><p>${escapeHTML(t('运行时内存占用可能为 ROM 的数倍，建议使用内存充足的电脑；手机优先选云游戏。'))}</p><p>${escapeHTML(t('浏览器与云端存档分开；需要家人或智能体观战时，请选云游戏。'))}</p>` : escapeHTML(selected?.id === 'stream' ? t('服务器运行，无需下载 ROM；支持家人观战。') : t('沿用自动规则：符合条件的小 ROM 在浏览器运行，其余使用云游戏。'))}</div>${advanced}${unavailable.length ? `<details class="launch-unavailable"><summary>${escapeHTML(t('查看不可用选项的原因'))}</summary><ul>${unavailable.map(option => `<li><strong>${escapeHTML(launchOptionLabel(option.id))}</strong><span>${escapeHTML(option.reason ? diagnostic(option.reason) : t('当前运行环境不支持此方式。'))}</span></li>`).join('')}</ul></details>` : ''}`;
    $('launch-mode').addEventListener('change', event => {
      const option = mergeBrowserLaunchOptions(currentLaunchOptions(game), state.browserP2P).find(item => item.id === event.target.value && item.available);
      if (!option) return;
      state.launchChoice = option.id; renderLaunchChoices(game); updateLaunchButton(); $('launch-mode')?.focus({preventScroll: true});
    });
    $('launch-allow-p2p')?.addEventListener('click', () => {
      setBrowserP2PPreference(!state.browserP2P); renderLaunchChoices(game); $('launch-allow-p2p')?.focus({preventScroll: true});
    });
    $('retry-launch-options')?.addEventListener('click', () => loadLaunchOptions(game, true));
    updateLaunchButton();
    if (focusedId) $(focusedId)?.focus({preventScroll: true});
  }
  async function loadLaunchOptions(game, retry = false) {
    if (!state.profile || !state.detailOpen || !game || state.selected?.id !== game.id) return;
    const previous = state.launchOptions;
    if (!retry && previous?.gameId === game.id && previous.epoch === state.profileEpoch) return;
    const epoch = state.profileEpoch;
    const record = {gameId: game.id, epoch, loading: true, data: null, error: false};
    state.launchOptions = record;
    if (!retry || previous?.gameId !== game.id) state.launchChoice = 'auto';
    renderLaunchChoices(game);
    try {
      const data = await api(`/api/games/${encodeURIComponent(game.id)}/launch-options`, {timeoutMs: 10000});
      if (epoch !== state.profileEpoch || state.launchOptions !== record || state.selected?.id !== game.id) return;
      if (!Array.isArray(data?.options) || !data.options.some(option => option.id === 'auto')) throw new Error('Invalid launch options');
      record.data = data;
    } catch (error) {
      if (epoch !== state.profileEpoch || state.launchOptions !== record || error.name === 'AbortError') return;
      record.error = true;
    } finally {
      if (epoch === state.profileEpoch && state.launchOptions === record) {
        record.loading = false;
        if (state.detailOpen && state.selected?.id === game.id) renderLaunchChoices(game);
      }
    }
  }
  function renderDownloadOption(game) {
    const button = $('download-game'), note = $('download-game-note');
    if (!button || !game || state.selected?.id !== game.id) return;
    const record = state.downloadOptions, data = record?.gameId === game.id && record.epoch === state.profileEpoch ? record.data : null;
    button.disabled = Boolean(record?.loading || !data?.available && !record?.error);
    button.textContent = record?.error ? t('重新读取下载信息') : t('下载到设备');
    note.textContent = record?.loading ? t('正在读取下载信息…') : record?.error ? t('暂时无法读取下载信息，请重试。') : data?.available ? t('文件大小 {size} · 下载与浏览器运行兼容性无关', {size: formatSize(data.size_bytes)}) : data?.reason ? diagnostic(data.reason) : t('此游戏暂不支持独立下载。');
  }
  async function loadDownloadOptions(game, retry = false) {
    if (!state.profile || !state.detailOpen || !game || state.selected?.id !== game.id) return;
    const previous = state.downloadOptions;
    if (!retry && previous?.gameId === game.id && previous.epoch === state.profileEpoch) return;
    const epoch = state.profileEpoch, record = {gameId: game.id, epoch, loading: true, error: false, data: null};
    state.downloadOptions = record; renderDownloadOption(game);
    try {
      const data = await api(`/api/games/${encodeURIComponent(game.id)}/download-options`, {timeoutMs: 15000});
      if (epoch !== state.profileEpoch || state.downloadOptions !== record || state.selected?.id !== game.id) return;
      if (typeof data?.available !== 'boolean') throw new Error('Invalid download options');
      record.data = data;
    } catch (error) {
      if (epoch !== state.profileEpoch || state.downloadOptions !== record || error.name === 'AbortError') return;
      record.error = true;
    } finally {
      if (epoch === state.profileEpoch && state.downloadOptions === record) { record.loading = false; renderDownloadOption(game); }
    }
  }
  async function downloadGame(game) {
    const record = state.downloadOptions;
    if (!state.profile || !game || record?.gameId !== game.id || record.epoch !== state.profileEpoch) return;
    if (record.error) { await loadDownloadOptions(game, true); return; }
    if (!record.data?.available) return;
    const suppliedURL = record.data.manifest_url ?? `${state.profile.base_path}/api/games/${encodeURIComponent(game.id)}/download-manifest?transport=p2p`;
    const manifestUrl = localURL(suppliedURL, [`${state.profile.base_path}/api/games/`, '/api/games/']);
    if (!manifestUrl) { notify(t('没有收到有效的下载入口，请重试。'), true); return; }
    state.downloadPending = {gameId: game.id, title: game.title, profileId: state.profile.id, epoch: state.profileEpoch, manifestUrl};
    downloadMessage(); showDownloads();
    if (downloadDirectoryReady()) await flushPendingDownload();
  }
  function renderDetail(game) {
    if (!state.detailOpen) { $('game-detail').replaceChildren(); renderDetailPage(); return; }
    if (!game) {
      $('game-detail').innerHTML = `<div class="detail-placeholder">${icon('controller', 'detail-placeholder-icon')}<p>${escapeHTML(t('选择一款游戏，查看详情。'))}</p><span>${escapeHTML(state.loading ? t('正在载入…') : t('这款游戏暂时无法读取，请返回游戏库重试。'))}</span></div>`;
      renderDetailPage(); return;
    }
    const mode = modeLabels[game.mode] ? game.mode : 'unavailable';
    const videoURL = localURL(game.video_url), data = game.details || {};
    const screenshots = (Array.isArray(data.screenshots) ? data.screenshots : []).map(Library.imageURL).filter(Boolean).slice(0, 12);
    const official = Library.secureURL(data.official_url);
    const sources = (Array.isArray(data.sources) ? data.sources : []).filter(source => source && typeof source.name === 'string' && Library.secureURL(source.url));
    const description = typeof data.description === 'string' && data.description || game.description;
    $('game-detail').innerHTML = `<div class="detail-hero"><div class="detail-cover">${cover(game, true)}</div><div class="detail-introduction"><div class="detail-eyebrow"><p class="eyebrow">IN YOUR COLLECTION</p><span>${escapeHTML(game.favorite ? t('已收藏') : t('游戏详情'))}</span></div><p class="detail-platform">${Library.platformArt(game.platform)}${escapeHTML(platformName(game.platform, game.platform_name))}</p><h1 class="detail-title">${escapeHTML(game.title)}</h1>${deviceCacheBadge(game.id, 'detail')}${variantTags(game)}${detailPopularity(game)}<div class="detail-actions"><button class="button detail-launch" id="launch-game" ${mode === 'unavailable' ? 'disabled' : ''}>${icon(mode === 'unavailable' ? 'info' : 'play')}${escapeHTML(mode === 'unavailable' ? t('等待配置') : mode === 'stream' ? t('开始云端游戏') : t('开始游戏'))}</button><button class="icon-button detail-favorite ${game.favorite ? 'is-favorite' : ''}" id="detail-favorite" aria-label="${escapeHTML(game.favorite ? t('取消收藏') : t('收藏'))} ${escapeHTML(game.title)}" aria-pressed="${Boolean(game.favorite)}" ${state.favoritePending.has(game.id) ? 'disabled' : ''}>${icon('star')}</button></div><div id="launch-choices" class="launch-choices"></div><div class="detail-download"><button type="button" class="button button-secondary" id="download-game" disabled>${escapeHTML(t('下载到设备'))}</button><p id="download-game-note"></p></div><p class="detail-note">${escapeHTML(mode === 'browser' ? t('在当前设备运行 · 进度可在模拟器中保存') : mode === 'stream' ? t('服务器运行 · 游戏镜像无需下载到本机') : t('保留在游戏库中，补齐环境后即可尝试启动'))}</p>${game.reason ? `<p class="detail-reason">${icon('info')}${escapeHTML(diagnostic(game.reason))}</p>` : ''}${official ? `<a class="button button-secondary official-link" href="${escapeHTML(official)}" target="_blank" rel="noopener noreferrer">${escapeHTML(t('访问官方页面'))}${icon('arrow')}</a>` : ''}</div></div><div class="detail-body"><section class="detail-about"><h2>${escapeHTML(t('关于这款游戏'))}</h2>${description ? `<p class="detail-description">${escapeHTML(description)}</p>` : `<p class="detail-missing">${escapeHTML(t('暂时没有可靠的游戏介绍，后续补充资料后会显示在这里。'))}</p>`}${videoURL ? `<button class="button button-secondary video-toggle" id="preview-video">${icon('play')}${escapeHTML(t('观看游戏预览'))}</button><video class="detail-video" id="detail-video" src="${escapeHTML(videoURL)}" controls preload="none" playsinline hidden></video>` : ''}${screenshots.length ? `<h2>${escapeHTML(t('游戏画面'))}</h2><div class="detail-screenshots">${screenshots.map((url, index) => `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer" aria-label="${escapeHTML(t('查看游戏画面 {value1}', {value1: index + 1}))}"><img src="${escapeHTML(url)}" alt="${escapeHTML(t('《{value1}》游戏画面 {value2}', {value1: game.title, value2: index + 1}))}" loading="lazy" decoding="async" crossorigin="anonymous" referrerpolicy="no-referrer"></a>`).join('')}</div>` : ''}${sources.length ? `<p class="detail-sources">${escapeHTML(t('资料来源'))} · ${sources.map(source => `<a href="${escapeHTML(Library.secureURL(source.url))}" target="_blank" rel="noopener noreferrer">${escapeHTML(source.name)}</a>`).join(' · ')}</p>` : ''}</section><section class="detail-information"><h2>${escapeHTML(t('游戏资料'))}</h2><dl class="detail-facts">${detailFacts(game)}</dl></section></div><details class="detail-technical"><summary>${escapeHTML(t('版本、文件与运行检查'))}</summary>${launchConfigDetails(game)}${reviewDetails(game)}</details>`;
    renderDetailPage();
    bindImageErrors($('game-detail'));
    $('game-detail').querySelectorAll('.detail-screenshots img').forEach(img => img.addEventListener('error', () => { const label = document.createElement('span'); label.className = 'screenshot-unavailable'; label.textContent = t('暂时无法载入这张画面'); img.replaceWith(label); }, {once: true}));
    updateLaunchButton();
    renderLaunchChoices(game); loadLaunchOptions(game); renderDownloadOption(game); loadDownloadOptions(game);
    $('launch-game').addEventListener('click', () => launchGame(game, {launchMode: selectedLaunchMode(game)}));
    $('download-game').addEventListener('click', () => downloadGame(game));
    $('detail-favorite').addEventListener('click', () => toggleFavorite(game.id));
    for (const name of ['evidence', 'copies']) $('review-' + name + '-toggle')?.addEventListener('click', (event) => {
      const panel = $('review-' + name); panel.hidden = !panel.hidden;
      event.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
    });
    $('review-copies')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-open-copy]');
      if (button && !button.disabled) openDuplicateRecord(button.dataset.openCopy);
    });
    $('preview-video')?.addEventListener('click', () => {
      const video = $('detail-video');
      video.hidden = !video.hidden;
      $('preview-video').innerHTML = `${icon(video.hidden ? 'play' : 'close')}${escapeHTML(video.hidden ? t("观看游戏预览") : t("收起预览"))}`;
      if (video.hidden) video.pause(); else video.play().catch(() => {});
    });
  }

  function renderDetailPage() {
    $('detail-page').hidden = !state.detailOpen;
    $('library').hidden = state.detailOpen;
    document.querySelector('.console-bar').hidden = state.detailOpen;
    document.body.classList.toggle('has-game-detail', state.detailOpen);
    if (state.detailOpen && state.selected) document.title = `${state.selected.title} · RetroOne`;
    const neighbors = state.detailNeighbors;
    $('detail-previous').disabled = state.detailPaging || !neighbors?.previous_id;
    $('detail-next').disabled = state.detailPaging || !neighbors?.next_id;
    $('detail-position').textContent = neighbors && Number.isInteger(neighbors.index) ? t('当前列表 · {value1} / {value2}', {value1: number(neighbors.index + 1), value2: number(neighbors.total)}) : state.detailOpen ? t('按当前筛选顺序浏览') : '';
  }
  function detailQuery() {
    const params = new URLSearchParams({direction: state.direction});
    for (const [key, value] of Object.entries({platform: state.platform, q: state.query, mode: state.mode, review_filter: state.reviewFilter})) if (value) params.set(key, value);
    if (state.favorites) params.set('favorite', 'true'); if (state.recent) params.set('recent', 'true');
    if (state.sort !== 'default') params.set('sort', state.sort);
    return params;
  }
  async function refreshDetailNeighbors() {
    const id = state.selected?.id, epoch = state.profileEpoch, query = detailQuery().toString();
    state.detailNeighbors = null; renderDetailPage();
    if (!id || !state.detailOpen) return;
    try {
      const result = await api(`/api/games/${encodeURIComponent(id)}/neighbors?${query}`, {timeoutMs: 10000});
      if (epoch !== state.profileEpoch || state.selected?.id !== id || !state.detailOpen || query !== detailQuery().toString()) return;
      state.detailNeighbors = result; renderDetailPage();
    } catch (error) {
      if (epoch === state.profileEpoch && state.selected?.id === id && state.detailOpen && error.name !== 'AbortError') {
        $('detail-position').textContent = t('暂时无法读取相邻游戏，请重新打开详情重试。');
      }
    }
  }
  async function openGameDetail(id, {push = true} = {}) {
    if (!state.profile) return;
    if (!state.detailOpen) {
      state.listScroll = window.scrollY; state.listSelection = state.selected?.id || id;
      updateURL(); state.detailOpen = true;
      state.selected = state.games.find(game => game.id === id) || state.selected;
      updateURL(push);
    }
    state.detailNeighbors = null; renderDetailPage();
    let game = state.games.find(item => item.id === id);
    if (!game) {
      const epoch = state.profileEpoch, request = ++state.detailRequest;
      try { game = await api(`/api/games/${encodeURIComponent(id)}`); }
      catch (error) { if (epoch === state.profileEpoch && error.name !== 'AbortError') notify(errorMessage(error), true); return; }
      if (epoch !== state.profileEpoch || request !== state.detailRequest || !state.detailOpen) return;
      state.selected = game; renderDetail(game); updateURL();
    } else await selectGame(id, {refresh: true});
    if (!state.detailOpen) return;
    refreshDetailNeighbors(); window.scrollTo({top: 0, behavior: 'instant'}); $('detail-back').focus({preventScroll: true});
  }
  function restoreLibraryPosition() {
    requestAnimationFrame(() => {
      const id = state.listSelection || history.state?.listSelection || state.selected?.id;
      const button = [...$('game-grid').querySelectorAll('[data-select]')].find(item => item.dataset.select === id);
      button?.focus({preventScroll: true});
      window.scrollTo({top: state.listScroll || history.state?.listScroll || 0, behavior: 'instant'});
    });
  }
  function closeGameDetail() {
    if (!state.detailOpen) return;
    state.detailOpen = false; state.detailNeighbors = null; state.detailRequest++;
    $('detail-video')?.pause();
    if (history.state?.detailFromLibrary) { history.back(); return; }
    renderDetailPage(); renderFilters(); updateURL(); restoreLibraryPosition();
  }
  async function stepGameDetail(delta) {
    const id = delta < 0 ? state.detailNeighbors?.previous_id : state.detailNeighbors?.next_id;
    if (!id || state.detailPaging) return;
    const epoch = state.profileEpoch;
    state.detailPaging = true; renderDetailPage();
    try { await openGameDetail(id, {push: false}); }
    finally { if (epoch === state.profileEpoch) { state.detailPaging = false; renderDetailPage(); } }
  }
  function focusDetailNeighbor(direction) {
    const controls = [...$('detail-page').querySelectorAll('button, a, summary, select')].filter(element => !element.disabled && element.getClientRects().length);
    const index = controls.indexOf(document.activeElement), step = direction === 'left' || direction === 'up' ? -1 : 1;
    controls[(index + step + controls.length) % controls.length]?.focus();
  }

  async function selectGame(id, options = {}) {
    const game = state.games.find((item) => String(item.id) === String(id));
    if (!game) return;
    const changed = state.selected?.id !== game.id;
    state.selected = game;
    document.querySelectorAll('[data-game-id]').forEach((item) => {
      const selected = item.dataset.gameId === String(id);
      item.classList.toggle('is-selected', selected);
      item.querySelector('.game-select').setAttribute('aria-pressed', String(selected));
    });
    if (changed || options.refresh) renderDetail(game);
    updateURL();
    if (options.focus && state.detailOpen) $('detail-back').focus({preventScroll: true});
    if (options.refresh) {
      const request = ++state.detailRequest;
      try {
        const fresh = await api(`/api/games/${encodeURIComponent(id)}`);
        if (request !== state.detailRequest || state.selected?.id !== game.id) return;
        state.selected = {...game, ...fresh};
        state.games[state.games.findIndex((item) => item.id === game.id)] = state.selected;
        const detailWasFocused = $('game-detail').contains(document.activeElement);
        const focusedId = detailWasFocused ? document.activeElement.id : null;
        renderDetail(state.selected);
        if (focusedId) $(focusedId)?.focus({preventScroll: true});
      } catch { /* List metadata remains useful if a single detail refresh fails. */ }
    }
  }

  async function loadGames(append = false, {preserveSelection = false} = {}) {
    if (append && state.loading) return;
    const keepSelection = preserveSelection || Boolean(state.playMode) || state.detailOpen;
    state.controller?.abort();
    const controller = new AbortController(); state.controller = controller;
    const request = ++state.request; state.loading = true;
    const start = append ? state.games.length : 0;
    if (append) { $('load-more').disabled = true; $('load-more').textContent = t("正在载入…"); }
    else renderSkeletons();
    renderFilters();
    const params = new URLSearchParams({offset: String(start), limit: '60'});
    if (state.query) params.set('q', state.query);
    if (state.platform) params.set('platform', state.platform);
    if (state.mode) params.set('mode', state.mode);
    if (state.reviewFilter) params.set('review_filter', state.reviewFilter);
    if (state.favorites) params.set('favorite', 'true');
    if (state.recent) params.set('recent', 'true');
    if (state.sort !== 'default') params.set('sort', state.sort);
    params.set('direction', state.direction);
    try {
      const result = await api(`/api/games?${params}`, {signal: controller.signal});
      if (request !== state.request) return;
      if (!Array.isArray(result.games)) throw interfaceError("游戏目录格式不正确，请稍后更新游戏库。");
      state.games = append ? [...state.games, ...result.games] : result.games;
      state.total = Number(result.total) || 0;
      const requestedGameId = state.initialGame;
      const previousId = requestedGameId || state.selected?.id;
      let selection = state.games.find((game) => String(game.id) === String(previousId));
      if (requestedGameId && !selection) {
        try {
          selection = await api(`/api/games/${encodeURIComponent(requestedGameId)}`, {signal: controller.signal});
        } catch { /* A removed or inaccessible deep link leaves the library usable. */ }
      }
      if (request !== state.request) return;
      state.initialGame = null;
      state.selected = requestedGameId && state.detailOpen ? selection || null : selection || (keepSelection ? state.selected : null) || state.games[0] || null;
      renderGames(append, start);
      if (!append) renderDetail(state.selected);
      if (state.detailOpen && state.selected) refreshDetailNeighbors();
      updateURL();
    } catch (error) {
      if (error.name === 'AbortError' || request !== state.request) return;
      if (append) {
        notify(errorMessage(error), true); $('load-more').disabled = false; $('load-more').textContent = t("重试载入");
      } else {
        state.games = []; state.total = 0;
        if (!keepSelection) state.selected = null;
        $('game-grid').replaceChildren(); renderDetail(state.selected); showEmpty('error', errorMessage(error));
      }
    } finally {
      if (request === state.request) { state.loading = false; $('games-region').setAttribute('aria-busy', 'false'); renderFilters(); }
    }
  }

  function setFilter(changes) {
    if (!state.profile) return;
    clearTimeout(searchTimer);
    if (Object.hasOwn(changes, 'recent') && changes.recent !== state.recent && !Object.hasOwn(changes, 'sort')) {
      changes.sort = changes.recent ? 'last_played' : state.librarySettings.sort;
      changes.direction = changes.recent ? 'desc' : state.librarySettings.direction;
    }
    state.detailOpen = false; state.detailNeighbors = null; renderDetailPage();
    Object.assign(state, changes); state.reviewFilter = validReviewFilter(state.reviewFilter); updateURL(); renderFilters(); renderPlatforms(); loadGames();
  }

  async function toggleFavorite(id) {
    const epoch = state.profileEpoch;
    if (state.favoritePending.has(id)) return;
    const game = state.games.find((item) => String(item.id) === String(id)) || state.selected;
    if (!game) return;
    state.favoritePending.add(game.id);
    document.querySelectorAll('[data-favorite]').forEach((button) => { if (button.dataset.favorite === String(id)) button.disabled = true; });
    if (state.selected?.id === game.id && $('detail-favorite')) $('detail-favorite').disabled = true;
    try {
      const result = await api(`/api/games/${encodeURIComponent(id)}/favorite`, {method: 'POST', body: JSON.stringify({favorite: !game.favorite})});
      game.favorite = typeof result.favorite === 'boolean' ? result.favorite : !game.favorite;
      if (state.selected?.id === game.id) state.selected.favorite = game.favorite;
      notify(game.favorite ? t("已加入我的收藏") : t("已取消收藏"));
      if (state.favorites && !game.favorite) { await loadGames(); return; }
      const article = [...document.querySelectorAll('[data-game-id]')].find((item) => item.dataset.gameId === String(id));
      const button = article?.querySelector('[data-favorite]');
      if (button) { button.classList.toggle('is-favorite', game.favorite); button.setAttribute('aria-pressed', String(game.favorite)); button.setAttribute('aria-label', `${game.favorite ? t("取消收藏") : t("收藏")} ${game.title}`); }
      if (state.selected?.id === game.id) {
        const detail = $('detail-favorite');
        detail?.classList.toggle('is-favorite', game.favorite);
        detail?.setAttribute('aria-pressed', String(game.favorite));
        detail?.setAttribute('aria-label', `${game.favorite ? t("取消收藏") : t("收藏")} ${game.title}`);
        const label = $('game-detail').querySelector('.detail-eyebrow > span'); if (label) label.textContent = game.favorite ? t("已收藏") : t("游戏详情");
      }
    } catch (error) { if (epoch === state.profileEpoch) notify(errorMessage(error), true); }
    finally {
      if (epoch !== state.profileEpoch) return;
      state.favoritePending.delete(game.id);
      document.querySelectorAll('[data-favorite]').forEach((button) => { if (button.dataset.favorite === String(id)) button.disabled = false; });
      if (state.selected?.id === game.id && $('detail-favorite')) $('detail-favorite').disabled = false;
    }
  }

  function sessionId(session) { return session?.id || session?.session_id; }
  function sessionGameId(session) { return session?.game?.id || session?.game_id; }
  function sessionNetworkRoomId(session) { return session?.network?.room_id || session?.network_room_id || null; }
  function sessionTitle(session) { return session?.game?.title || session?.title || session?.game_title || t("当前游戏"); }
  function updateLaunchButton() {
    const game = state.selected, button = $('launch-game');
    if (state.launching || !game || !button) return;
    const choice = selectedLaunchMode(game), option = currentLaunchOptions(game).find(item => item.id === choice);
    const cloud = choice === 'stream' || choice === 'auto' && game.mode === 'stream';
    button.disabled = !option?.available;
    const label = button.disabled ? t('等待配置') : cloud ? state.session && sessionGameId(state.session) === game.id ? t('继续游戏') : t('开始云端游戏') : choice?.startsWith('browser-') ? t('下载并在浏览器启动') : t('开始游戏');
    button.innerHTML = `${icon(button.disabled ? 'info' : 'play')}${escapeHTML(label)}`;
  }
  function showLaunchConflict(game, detail, {networkRoomId = null, launchMode = null} = {}) {
    const session = detail.session;
    if (!sessionId(session)) { notify(detail.message || t("当前云端会话已变化，请重新选择游戏。"), true); refreshSession(); return; }
    state.session = {...session, ...(detail.game ? {game: detail.game} : {})}; renderSession();
    if ($('network-dialog').open) closeNetwork();
    state.pendingLaunch = {action: 'replace', game, session: state.session, epoch: state.profileEpoch, networkRoomId, launchMode};
    $('replace-title').textContent = t("切换云端游戏？");
    $('replace-description').textContent = t("正在运行《{value1}》，结束并启动《{value2}》？", {value1: sessionTitle(state.session), value2: game.title});
    $('keep-current-game').textContent = t("继续玩《{value1}》", {value1: sessionTitle(state.session)});
    $('replace-current-game').textContent = t("结束并启动《{value1}》", {value1: game.title});
    $('keep-current-game').disabled = false; $('replace-current-game').disabled = false;
    if (!$('replace-dialog').open) $('replace-dialog').showModal();
    syncModalBody(); $('keep-current-game').focus();
  }
  function showStopConfirmation(session) {
    if (!sessionId(session)) return;
    if ($('self-dialog').open) closeSelf();
    state.pendingLaunch = {action: 'stop', session, epoch: state.profileEpoch};
    $('replace-title').textContent = t("结束云端游戏？");
    $('replace-description').textContent = t("确定结束《{value1}》？", {value1: sessionTitle(session)});
    $('keep-current-game').textContent = t("继续玩《{value1}》", {value1: sessionTitle(session)});
    $('replace-current-game').textContent = t("结束《{value1}》", {value1: sessionTitle(session)});
    $('keep-current-game').disabled = false; $('replace-current-game').disabled = false;
    if (!$('replace-dialog').open) $('replace-dialog').showModal();
    syncModalBody(); $('keep-current-game').focus();
  }
  function closeLaunchConflict() {
    const action = state.pendingLaunch?.action;
    state.pendingLaunch = null; $('replace-dialog').close(); syncModalBody();
    if (state.playMode) $('stop-play').focus();
    else if (action === 'stop') $('profile-button').focus();
    else $('launch-game')?.focus();
  }
  async function continueCurrentGame() {
    const pending = state.pendingLaunch;
    closeLaunchConflict();
    if (!pending || pending.epoch !== state.profileEpoch) return;
    try {
      const result = await api('/api/session');
      const current = result.session;
      state.session = current ? {...current, ...(result.game ? {game: result.game} : {})} : null;
      renderSession();
      if (sessionId(current) !== sessionId(pending.session)) { notify(t("原云端会话已变化，请在游戏库查看当前会话。")); return; }
      resumeCloudSession(state.session);
    } catch (error) { if (pending.epoch === state.profileEpoch) notify(errorMessage(error), true); }
  }
  function resumeCloudSession(session) {
    if (state.playMode === 'stream' && $('play-dialog').open) { $('game-frame')?.focus(); return; }
    state.activeNetworkRoomId = sessionNetworkRoomId(session); renderFamilyPresence();
    if (session) openPlayer(localURL(session.play_url, [`${state.profile.base_path}/stream/`]) || `${state.profile.base_path}/stream/`, sessionTitle(session), 'stream', sessionGameId(session) || null, session.game || session);
  }
  async function requestGameLaunch(gameId, body) {
    const path = `/api/games/${encodeURIComponent(gameId)}/launch`;
    const request = value => api(path, {method: 'POST', body: JSON.stringify(value), timeoutMs: 30 * 60 * 1000});
    try { return await request(body); }
    catch (error) {
      // Old workers reject the newer device preference before launching anything.
      // Retry only this exact validation failure; never replay uncertain launches
      // or remove session-replacement / network safeguards.
      const issue = Array.isArray(error.detail) && error.detail.length === 1 ? error.detail[0] : null;
      if (error.status !== 422 || issue?.type !== 'extra_forbidden' ||
          JSON.stringify(issue.loc) !== '["body","device_id"]' || !Object.hasOwn(body, 'device_id')) throw error;
      const {device_id, ...compatible} = body;
      const result = await request(compatible);
      return {...result, device_preferences_supported: false};
    }
  }

  async function launchGame(game, {replaceSessionId = null, networkRoomId = null, launchMode = null} = {}) {
    const epoch = state.profileEpoch;
    if (!game || state.playMode || state.launching) return;
    const requestedMode = networkRoomId ? 'stream' : launchMode || selectedLaunchMode(game);
    if (!networkRoomId && !replaceSessionId && !currentLaunchOptions(game).some(option => option.id === requestedMode && option.available)) return;
    state.launching = true;
    if ($('launch-mode')) $('launch-mode').disabled = true;
    if ($('launch-allow-p2p')) $('launch-allow-p2p').disabled = true;
    const button = state.selected?.id === game.id ? $('launch-game') : null;
    if (button) { button.disabled = true; button.textContent = t("正在准备游戏…"); }
    const note = state.selected?.id === game.id ? $('game-detail').querySelector('.detail-note') : null;
    if (note && (requestedMode === 'stream' || requestedMode === 'auto' && game.mode === 'stream')) { note.hidden = false; note.textContent = t("首次解压可能需要数分钟，请稍候。"); }
    try {
      const result = await requestGameLaunch(game.id, {mode: requestedMode, device_id: displayDeviceId, ...(replaceSessionId ? {replace_session_id: replaceSessionId} : {}), ...(networkRoomId ? {network_room_id: networkRoomId} : {})});
      if (epoch !== state.profileEpoch) return;
      const url = localURL(result.play_url, [`${state.profile.base_path}/play/`, `${state.profile.base_path}/stream/`]);
      if (!url) throw interfaceError("没有收到有效的游戏入口，请重新加载游戏库。");
      state.activeNetworkRoomId = networkRoomId || sessionNetworkRoomId(result.session); renderFamilyPresence();
      if ($('network-dialog').open) closeNetwork();
      openPlayer(url, game.title, result.mode || game.mode, game.id, game);
      if (result.device_preferences_supported === false && result.mode === 'stream') notify(t('当前游戏实例沿用玩家默认画质。'));
      refreshSession();
    } catch (error) { if (epoch === state.profileEpoch) { if (error.status === 409 && error.detail?.code === 'session_conflict') showLaunchConflict(game, error.detail, {networkRoomId, launchMode: requestedMode}); else { notify(errorMessage(error), true); if (networkRoomId && $('network-dialog').open) networkMessage(errorMessage(error), true); refreshSession(); } } }
    finally { if (epoch !== state.profileEpoch) return; state.launching = false; if (state.selected?.id === game.id) renderDetail(state.selected); }
  }

  function openPlayer(url, title, mode, gameId = null, game = null) {
    if ($('play-dialog').open) return;
    state.focusReturn = document.activeElement;
    state.playMode = mode; state.playGameId = gameId;
    updateURL();
    $('detail-video')?.pause();
    $('play-title').textContent = title || t("云端游戏");
    renderCurrentProfile();
    $('play-note').textContent = mode === 'stream' ? t("返回游戏库会保留云端会话 · 退出前请保存进度") : t("退出前请在模拟器菜单保存进度");
    $('stop-play').hidden = mode !== 'stream';
    $('play-loading').hidden = false;
    const iframe = document.createElement('iframe');
    iframe.id = 'game-frame'; iframe.title = t("{value1} · 游戏画面", {value1: title || 'RetroOne'});
    iframe.allow = 'autoplay; fullscreen; gamepad; clipboard-write; keyboard-map';
    iframe.addEventListener('load', () => { $('play-loading').hidden = true; iframe.focus(); }, {once: true});
    if (mode === 'stream' && clientRevision) {
      const streamURL = new URL(url, location.href);
      streamURL.searchParams.set('retroone_client', clientRevision);
      url = streamURL.href;
    }
    iframe.src = url;
    $('play-frame-wrap').append(iframe);
    $('play-dialog').showModal(); document.body.classList.add('body-modal');
    const metadata = game || (state.selected?.id === gameId ? state.selected : state.games.find(item => item.id === gameId));
    window.RetroMobilePlayer?.open({iframe, profileId: state.profile.id, platform: metadata?.platform || '', mode, engine: metadata?.engine || ''});
    heartbeatPresence();
  }

  function closePlayer({refresh = true, presence = true} = {}) {
    window.RetroMobilePlayer?.close();
    $('game-frame')?.remove();
    state.playMode = null; state.playGameId = null; state.activeNetworkRoomId = null;
    updateURL();
    renderCurrentProfile();
    if ($('play-dialog').open) $('play-dialog').close();
    syncModalBody();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    if (state.focusReturn?.isConnected) state.focusReturn.focus({preventScroll: true});
    else $('launch-game')?.focus({preventScroll: true});
    syncModalBody();
    if (refresh) refreshSession();
    if (presence) heartbeatPresence();
  }

  function renderSession() {
    renderCurrentProfile();
    updateLaunchButton();
  }

  async function refreshSession() {
    try {
      const result = await api('/api/session');
      state.session = result?.session ?? (result?.session_id ? result : null);
      if (state.session && result.game) state.session.game = result.game;
      renderSession();
    } catch { /* Health owns connection error reporting. */ }
  }

  async function stopSession() {
    const epoch = state.profileEpoch;
    try {
      const result = await api('/api/session');
      state.session = result.session ? {...result.session, ...(result.game ? {game: result.game} : {})} : null;
      renderSession();
      if (state.session) showStopConfirmation(state.session); else notify(t("当前没有正在运行的云端游戏。"));
    } catch (error) { if (epoch === state.profileEpoch) notify(errorMessage(error), true); }
  }
  async function confirmStopSession(expected) {
    const epoch = state.profileEpoch;
    $('stop-session').disabled = true; $('stop-play').disabled = true;
    try {
      await api('/api/session/stop', {method: 'POST', body: JSON.stringify({expected_session_id: sessionId(expected)})});
      if (state.playMode === 'stream') closePlayer();
      state.session = null; renderSession(); notify(t("云端游戏已结束"));
    } catch (error) { if (epoch === state.profileEpoch) { if (error.status === 409 && error.detail?.code === 'session_conflict') { state.session = {...error.detail.session, ...(error.detail.game ? {game: error.detail.game} : {})}; renderSession(); showStopConfirmation(state.session); } else notify(errorMessage(error), true); } }
    finally { if (epoch !== state.profileEpoch) return; $('stop-session').disabled = false; $('stop-play').disabled = false; }
  }

  async function loadPlatforms() {
    try {
      const result = await api('/api/platforms');
      state.platforms = Array.isArray(result.platforms) ? result.platforms : [];
      renderPlatforms(); renderFilters();
    } catch { /* The catalog request displays a retry action on connection failure. */ }
  }

  function scanIsRunning(health) {
    const scan = health?.library?.scan ?? health?.scan;
    return scan?.running === true || ['running', 'scanning', 'queued'].includes(scan?.status) || health?.status === 'scanning';
  }

  async function refreshLibraryAfterScan() {
    const focused = document.activeElement;
    const focusKey = focused?.dataset.select != null ? ['data-select', focused.dataset.select]
      : focused?.dataset.favorite != null ? ['data-favorite', focused.dataset.favorite]
      : focused?.dataset.platform != null ? ['data-platform', focused.dataset.platform]
      : focused?.id ? ['id', focused.id] : null;
    await Promise.allSettled([loadPlatforms(), loadGames(false, {preserveSelection: true})]);
    // Restore a replaced control, but never steal focus after the user moves it.
    if (focusKey && !focused.isConnected && document.activeElement === document.body) {
      const replacement = [...document.querySelectorAll(`[${focusKey[0]}]`)].find((element) => element.getAttribute(focusKey[0]) === focusKey[1]);
      replacement?.focus({preventScroll: true});
    }
  }

  function renderHealth() {
    const health = state.health, scan = health?.library?.scan ?? health?.scan, scanning = scanIsRunning(health);
      state.connectionStatus = 'online';
      const browser = health.browser?.available;
      const count = health.library?.count;
      $('health-detail').textContent = t("服务已连接{value1}{value2}{value3}。各游戏能否启动，以详情中的运行状态为准。", {value1: count != null ? t(" · 已索引 {value1} 款游戏", {value1: number(count)}) : '', value2: browser === false ? t(" · 浏览器模拟器资源尚未就绪") : '', value3: scanning ? t(" · 正在扫描游戏目录{value1}", {value1: scan?.scanned != null ? t("，已检查 {value1} 项", {value1: number(scan.scanned)}) : ''}) : ''});
  }

  async function refreshHealth() {
    const epoch = state.profileEpoch;
    if (state.healthRefreshing) return;
    state.healthRefreshing = true;
    try {
      const previousScan = state.health?.library?.scan ?? state.health?.scan;
      const health = await api('/api/health');
      const scan = health?.library?.scan ?? health?.scan;
      state.health = health;
      const scanning = scanIsRunning(health);
      const scanFinished = !scanning && (state.wasScanning || state.scanPending || (previousScan && scan?.finished_at != null && scan.finished_at !== previousScan.finished_at));
      state.wasScanning = scanning;
      renderHealth();
      if (scanning) {
        const count = health.library?.count;
        // The card grid and platform controls remain stable during a background scan.
        if (count != null) $('stat-games').textContent = number(count);
      }
      if (scanFinished) {
        state.scanPending = false;
        await refreshLibraryAfterScan();
        if (epoch === state.profileEpoch) notify(t("游戏库已更新"));
      }
    } catch (error) {
      if (epoch !== state.profileEpoch || error.name === 'AbortError') return;
      if (state.connectionStatus === 'online') notify(t("连接暂时中断"), true);
      state.connectionStatus = 'error';
      $('health-detail').textContent = errorMessage(error);
    } finally {
      if (epoch === state.profileEpoch) state.healthRefreshing = false;
    }
  }

  async function scanLibrary() {
    await runLibraryManagement('scan', true);
  }

  function renderMigration(result) {
      if (!result || result.available === false) { $('migration-banner').hidden = true; return; }
      const data = result.migration || result;
      const status = data.phase || data.status || data.state;
      const suppliedMessage = data.message || data.summary;
      const errors = Array.isArray(data.errors) ? data.errors.length : Number(data.errors) || 0;
      const reviews = Array.isArray(data.symlinks_needing_review) ? data.symlinks_needing_review.length : Number(data.symlinks_needing_review) || 0;
      const progress = data.total_collections != null ? t("{value1} / {value2} 个目录", {value1: number(data.completed_collections), value2: number(data.total_collections)}) : '';
      const current = typeof data.current === 'string' && data.current ? t(" · 当前：{value1}", {value1: data.current}) : '';
      let message = '';
      if (status === 'copying') message = t("正在整理游戏{value1}{value2}", {value1: progress ? `：${progress}` : '', value2: current});
      else if (status === 'copied') message = t("游戏目录已复制完成{value1}{value2}", {value1: progress ? `（${progress}）` : '', value2: data.verification === 'pending' ? t("，等待文件核对") : ''});
      else if (status === 'verifying') message = t("正在核对文件{value1}{value2}{value3}", {value1: progress ? `：${progress}` : '', value2: data.verified_files != null ? t(" · 已检查 {value1} 个文件", {value1: number(data.verified_files)}) : '', value3: current});
      else if (status === 'verification_complete') message = t("目录核对已完成{value1}{value2}", {value1: data.files != null ? t(" · {value1} 个文件", {value1: number(data.files)}) : '', value2: reviews ? t(" · {value1} 项链接待检查", {value1: number(reviews)}) : ''});
      else if (status === 'copy_errors') message = t("游戏整理尚有异常{value1}", {value1: progress ? t(" · 已完成 {value1}", {value1: progress}) : ''});
      else if (['running', 'scanning', 'in_progress', 'error', 'failed'].includes(status) && typeof suppliedMessage === 'string') message = suppliedMessage;
      if (message && errors) message += t(" · {value1} 项异常待处理", {value1: number(errors)});
      if (message) {
        $('migration-text').textContent = message;
        $('migration-banner').hidden = false;
      } else $('migration-banner').hidden = true;
  }

  async function refreshMigration() {
    try {
      const result = await api('/api/migration');
      state.migrationData = result; renderMigration(result);
    } catch { /* Migration status is optional. */ }
  }

  function showHelp() { $('help-dialog').showModal(); document.body.classList.add('body-modal'); }
  function closeHelp() { $('help-dialog').close(); syncModalBody(); }

  function focusNeighbor(direction) {
    const active = document.activeElement;
    if (state.detailOpen) { focusDetailNeighbor(direction); return; }
    if (active?.closest('.filter-bar, .library-sort-row')) {
      const buttons = [...document.querySelectorAll('.filter-bar button, .library-sort-row button, .library-sort-row select')].filter((button) => !button.disabled && button.getClientRects().length);
      const index = buttons.indexOf(active);
      if (direction === 'up') $('platform-nav').querySelector('.is-active')?.focus();
      else if (direction === 'down') ($('game-grid').querySelector('.game-select') || $('empty-action'))?.focus();
      else buttons[(index + (direction === 'left' ? -1 : 1) + buttons.length) % buttons.length]?.focus();
      return;
    }
    if (active?.closest('#game-detail')) {
      const buttons = [...$('game-detail').querySelectorAll('button')].filter((button) => !button.disabled && button.getClientRects().length);
      const index = buttons.indexOf(active), next = index + (direction === 'left' || direction === 'up' ? -1 : 1);
      if (next < 0 || next >= buttons.length) ($('game-grid').querySelector('.is-selected .game-select') || $('game-grid').querySelector('.game-select') || $('review-filter'))?.focus();
      else buttons[next]?.focus();
      return;
    }
    if (active === $('empty-action') && direction === 'up') { $('review-filter').focus(); return; }
    if (active === $('profile-button') || active?.closest('.family-strip') || active?.matches('.topbar [data-language-picker]')) {
      const buttons = [...$('family-strip').querySelectorAll('button'), ...document.querySelectorAll('.topbar [data-language-picker]'), $('profile-button')].filter((button) => !button.disabled && !button.hidden && button.getClientRects().length);
      const index = buttons.indexOf(active);
      if (direction === 'down') $('platform-nav').querySelector('.is-active')?.focus();
      else if (direction === 'left' || direction === 'right') {
        const next = buttons[(index + (direction === 'left' ? -1 : 1) + buttons.length) % buttons.length];
        next?.focus({preventScroll: true}); next?.scrollIntoView({block: 'nearest', inline: 'nearest'});
      }
      return;
    }
    if (active?.closest('.platform-nav')) {
      const buttons = [...$('platform-nav').querySelectorAll('button')];
      const index = buttons.indexOf(active);
      if (direction === 'down') { $('review-filter').focus(); return; }
      if (direction === 'up') { $('profile-button').focus(); return; }
      if (direction === 'left' || direction === 'right') buttons[Math.max(0, Math.min(buttons.length - 1, index + (direction === 'right' ? 1 : -1)))]?.focus();
      return;
    }
    const buttons = [...$('game-grid').querySelectorAll('.game-select')];
    if (!buttons.length) return;
    let index = buttons.indexOf(active);
    if (index < 0) { buttons[0].focus(); return; }
    const columns = getComputedStyle($('game-grid')).gridTemplateColumns.split(' ').length;
    const delta = {left: -1, right: 1, up: -columns, down: columns}[direction];
    const next = index + delta;
    if (next < 0 && direction === 'up') { $('review-filter').focus(); return; }
    if (next >= buttons.length && direction === 'down' && !$('load-more').hidden) { $('load-more').focus(); return; }
    buttons[Math.max(0, Math.min(buttons.length - 1, next))]?.focus();
  }

  function cyclePlatform(delta) {
    const ids = ['', ...visiblePlatforms().map(platform => platform.id)];
    const index = ids.indexOf(state.platform);
    const id = ids[(index + delta + ids.length) % ids.length];
    setFilter({platform: id});
    $('platform-nav').querySelector('.is-active')?.scrollIntoView({block: 'nearest', inline: 'center', behavior: 'smooth'});
  }

  function pollGamepads(time) {
    gamepadFrame = requestAnimationFrame(pollGamepads);
    if (document.hidden || (state.playMode && !window.RetroMobilePlayer?.isMenuOpen() && !$('profile-dialog').open && !$('replace-dialog').open && !$('network-dialog').open && !$('display-dialog').open && !$('management-dialog').open && !$('downloads-dialog').open) || time - gamepadLast < 95) return;
    gamepadLast = time;
    let pads;
    try { pads = [...(navigator.getGamepads?.() || [])].filter(Boolean); } catch { return; }
    if (!pads.length) {
      gamepadPressed = {};
      if (gamepadName) { gamepadName = ''; $('gamepad-status').innerHTML = `${icon('controller')}${escapeHTML(t("键盘 / 手柄"))}`; $('gamepad-status').removeAttribute('title'); }
      return;
    }
    const pad = pads[0];
    if (gamepadName !== pad.id) {
      gamepadName = pad.id; $('gamepad-status').innerHTML = `${icon('controller')}${escapeHTML(t("手柄已连接"))}`; $('gamepad-status').title = pad.id;
      if (document.activeElement === document.body) $('game-grid').querySelector('.game-select')?.focus();
    }
    const pressed = (index) => Boolean(pad.buttons[index]?.pressed);
    const x = pad.axes[0] || 0, y = pad.axes[1] || 0;
    const verticalStick = Math.abs(y) >= Math.abs(x);
    const inputs = {up: pressed(12) || verticalStick && y < -.6, down: pressed(13) || verticalStick && y > .6, left: pressed(14) || !verticalStick && x < -.6, right: pressed(15) || !verticalStick && x > .6, accept: pressed(0), back: pressed(1), prev: pressed(4), next: pressed(5), profiles: pressed(9)};
    for (const [key, down] of Object.entries(inputs)) {
      const directional = ['up', 'down', 'left', 'right'].includes(key);
      if (!down) { delete gamepadHeldAt[key]; delete gamepadRepeatAt[key]; continue; }
      if (!gamepadPressed[key]) { gamepadHeldAt[key] = time; gamepadRepeatAt[key] = time; }
      const repeat = directional && time - gamepadHeldAt[key] >= 420 && time - gamepadRepeatAt[key] >= 170;
      if (!gamepadPressed[key] || repeat) {
        gamepadRepeatAt[key] = time;
        if ($('profile-dialog').open) {
          if (key === 'back') { if (state.profileEditing) showProfileCards(); else closeProfiles(); }
          else if (directional) focusProfileNeighbor(key);
          else if (key === 'accept') activateFocused();
        }
        else if ($('help-dialog').open) {
          if (key === 'back') closeHelp();
          else if (key === 'accept') $('close-help').click();
          else if (key === 'up' || key === 'down') $('help-dialog').scrollBy({top: key === 'up' ? -130 : 130, behavior: 'smooth'});
        }
        else if ($('watch-dialog').open) {
          if (key === 'back') closeWatch();
          else if (directional) focusModalAction($('watch-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('family-dialog').open) {
          if (key === 'back') closeFamily();
          else if (directional) focusModalAction($('family-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('replace-dialog').open) {
          if (key === 'back') closeLaunchConflict();
          else if (directional) (document.activeElement === $('keep-current-game') ? $('replace-current-game') : $('keep-current-game')).focus();
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('downloads-dialog').open) {
          if (key === 'back') closeDownloads();
          else if (directional) focusModalAction($('downloads-dialog'), key);
          else if (key === 'accept') activateFocused();
        }
        else if ($('management-dialog').open) {
          if (key === 'back') closeLibraryManagement();
          else if (directional) focusModalAction($('management-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('display-dialog').open) {
          if (key === 'back') closeDisplaySettings();
          else if (directional) focusModalAction($('display-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('self-dialog').open) {
          if (key === 'back') closeSelf();
          else if (directional) focusModalAction($('self-dialog'), key);
          else if (key === 'accept') activateFocused();
        }
        else if ($('network-dialog').open) {
          if (key === 'back') closeNetwork();
          else if (directional) focusModalAction($('network-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('platform-dialog').open) {
          if (key === 'back') closePlatforms();
          else if (directional) focusModalAction($('platform-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if (state.detailOpen && !state.playMode) {
          if (key === 'back') closeGameDetail();
          else if (key === 'prev' || key === 'next') stepGameDetail(key === 'prev' ? -1 : 1);
          else if (directional) focusDetailNeighbor(key);
          else if (key === 'accept') activateFocused();
        }
        else if (window.RetroMobilePlayer?.isMenuOpen()) window.RetroMobilePlayer.navigate(key);
        else if (key === 'profiles') showProfiles();
        else if (directional) focusNeighbor(key);
        else if (key === 'accept') {
          const active = document.activeElement;
          if (active?.matches('.game-select')) openGameDetail(active.dataset.select);
          else if (active?.matches('button, select') && !active.disabled) activateFocused();
          else $('game-grid').querySelector('.game-select')?.focus();
        } else if (key === 'back') $('game-grid').querySelector('.is-selected .game-select')?.focus();
        else if (key === 'prev') cyclePlatform(-1);
        else if (key === 'next') cyclePlatform(1);
      }
    }
    gamepadPressed = inputs;
  }

  function startGamepads() { if (!gamepadFrame) gamepadFrame = requestAnimationFrame(pollGamepads); }

  for (const picker of document.querySelectorAll('[data-language-picker]')) picker.addEventListener('change', () => changeInterfaceLanguage(picker.value));
  syncLanguagePickers();

  $('family-strip').addEventListener('click', (event) => { const button = event.target.closest('[data-family-profile]'); if (button) showFamily(button.dataset.familyProfile); else if (event.target.closest('[data-open-network]')) showNetwork(); });
  $('family-invite-actions').addEventListener('click', (event) => { const button = event.target.closest('[data-send-invite]'); if (button) sendInvitation(button.dataset.sendInvite); });
  $('network-content').addEventListener('click', (event) => { const button = event.target.closest('[data-network-action]'); if (button) networkAction(button.dataset.networkAction, button.dataset.inviteId); });
  $('close-network').addEventListener('click', closeNetwork);
  $('network-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeNetwork(); });
  $('network-dialog').addEventListener('close', syncModalBody);
  $('network-play-button').addEventListener('click', async () => {
    if (!state.activeNetworkRoomId) { showNetwork(); return; }
    const epoch = state.profileEpoch;
    try {
      const room = await api(`/api/network/rooms/${encodeURIComponent(state.activeNetworkRoomId)}`, {global: true});
      if (epoch !== state.profileEpoch) return;
      if (!validRoom(room)) throw interfaceError("联机步骤暂时无法读取，请稍后再试。");
      state.networkRoom = room; showNetwork({keepRoom: true});
    } catch (error) { if (epoch === state.profileEpoch) notify(errorMessage(error), true); }
  });
  $('family-watch').addEventListener('click', watchFamily);
  $('close-watch').addEventListener('click', closeWatch);
  $('fullscreen-watch').addEventListener('click', toggleWatchFullscreen);
  document.addEventListener('fullscreenchange', () => {
    const key = document.fullscreenElement ? '退出全屏' : '全屏观看';
    const button = $('fullscreen-watch'); button.dataset.i18nAriaLabel = key; button.dataset.i18nTitle = key;
    button.setAttribute('aria-label', t(key)); button.title = t(key);
    if (!document.fullscreenElement) state.watchFullscreenOwned = false;
  });
  $('watch-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeWatch(); });
  $('watch-dialog').addEventListener('close', syncModalBody);
  window.addEventListener('message', (event) => {
    const frame = $('watch-frame-wrap').querySelector('iframe');
    if (frame && event.source === frame.contentWindow && event.origin === location.origin && event.data?.type === 'retroone:watch-close') closeWatch();
  });
  $('close-family').addEventListener('click', closeFamily);
  $('back-from-family').addEventListener('click', closeFamily);
  $('family-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeFamily(); });
  $('family-dialog').addEventListener('close', syncModalBody);
  $('self-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeSelf(); });
  $('self-dialog').addEventListener('close', syncModalBody);
  $('close-self').addEventListener('click', closeSelf);
  $('self-display-settings').addEventListener('click', showDisplaySettings);
  $('self-download-settings').addEventListener('click', () => showDownloads({fromSelf: true}));
  $('close-downloads').addEventListener('click', closeDownloads);
  $('downloads-dialog').addEventListener('cancel', event => { event.preventDefault(); closeDownloads(); });
  $('downloads-dialog').addEventListener('close', syncModalBody);
  $('choose-download-directory').addEventListener('click', () => chooseDownloadDirectory());
  $('reconnect-download-directory').addEventListener('click', () => chooseDownloadDirectory(true));
  $('download-sharing-toggle').addEventListener('click', () => { setBrowserP2PPreference(!state.browserP2P); if (state.detailOpen && state.selected) renderLaunchChoices(state.selected); });
  $('download-entries').addEventListener('click', event => { const button = event.target.closest('[data-download-action]'); if (button && !button.disabled) downloadEntryAction(button.dataset.downloadId, button.dataset.downloadAction); });
  $('close-display').addEventListener('click', closeDisplaySettings);
  $('back-from-display').addEventListener('click', closeDisplaySettings);
  $('retry-display').addEventListener('click', loadDisplaySettings);
  $('save-display').addEventListener('click', () => saveDisplaySettings());
  $('reset-display').addEventListener('click', () => saveDisplaySettings(true));
  $('display-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeDisplaySettings(); });
  $('display-dialog').addEventListener('close', syncModalBody);
  $('display-options').addEventListener('click', (event) => {
    const button = event.target.closest('[data-display-resolution]');
    if (!button || button.disabled) return;
    state.displayDraft = button.dataset.displayResolution; displayMessage(); renderDisplaySettings();
  });
  $('self-switch-profile').addEventListener('click', () => { closeSelf(); showProfiles(); });
  $('replace-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeLaunchConflict(); });
  $('replace-dialog').addEventListener('close', syncModalBody);
  $('keep-current-game').addEventListener('click', continueCurrentGame);
  $('replace-current-game').addEventListener('click', async () => {
    const pending = state.pendingLaunch;
    closeLaunchConflict();
    if (pending?.epoch === state.profileEpoch) {
      if (pending.action === 'stop') confirmStopSession(pending.session);
      else if (pending.networkRoomId) {
        state.launching = true;
        try {
          await api('/api/session/stop', {method: 'POST', body: JSON.stringify({expected_session_id: sessionId(pending.session)})});
          if (pending.epoch !== state.profileEpoch) return;
          state.launching = false; state.session = null; renderSession();
          launchGame(pending.game, {networkRoomId: pending.networkRoomId});
        } catch (error) {
          if (pending.epoch !== state.profileEpoch) return;
          state.launching = false;
          if (error.status === 409 && error.detail?.code === 'session_conflict') showLaunchConflict(pending.game, error.detail, {networkRoomId: pending.networkRoomId});
          else notify(errorMessage(error), true);
        }
      }
      else launchGame(pending.game, {replaceSessionId: sessionId(pending.session), launchMode: pending.launchMode});
    }
  });
  $('profile-button').addEventListener('click', showSelf);
  $('switch-play-profile').addEventListener('click', showProfiles);
  $('close-profiles').addEventListener('click', closeProfiles);
  $('retry-profiles').addEventListener('click', () => loadProfiles());
  $('profile-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeProfiles(); });
  $('profile-dialog').addEventListener('close', syncModalBody);
  $('profile-grid').addEventListener('click', (event) => {
    const select = event.target.closest('[data-profile-id]');
    const edit = event.target.closest('[data-edit-profile]');
    const retry = event.target.closest('[data-retry-profile]');
    if (select) chooseProfile(state.profiles.find((profile) => profile.id === select.dataset.profileId));
    else if (edit) editProfile(state.profiles.find((profile) => profile.id === edit.dataset.editProfile));
    else if (retry) retryProfile(state.profiles.find((profile) => profile.id === retry.dataset.retryProfile));
    else if (event.target.closest('[data-add-profile]')) editProfile();
  });
  $('profile-editor').addEventListener('submit', saveEditedProfile);
  $('cancel-profile-edit').addEventListener('click', showProfileCards);
  $('avatar-options').addEventListener('click', (event) => {
    const button = event.target.closest('[data-avatar-option]');
    if (!button) return;
    state.profileAvatar = button.dataset.avatarOption;
    $('avatar-options').querySelectorAll('[data-avatar-option]').forEach((option) => option.setAttribute('aria-checked', String(option === button)));
  });
  $('profile-back-to-game').addEventListener('click', closeProfiles);
  $('profile-confirm-switch').addEventListener('click', () => { if (state.pendingProfile) applyProfile(state.pendingProfile); });
  $('choose-platforms').addEventListener('click', openPlatforms);
  $('close-platforms').addEventListener('click', closePlatforms);
  $('platform-dialog').addEventListener('cancel', event => { event.preventDefault(); closePlatforms(); });
  $('platform-search').addEventListener('input', renderPlatformChooser);
  $('platform-dialog').addEventListener('click', event => {
    const choose = event.target.closest('[data-choose-platform]'), pin = event.target.closest('[data-pin]'), move = event.target.closest('[data-pin-move]');
    if (choose) { closePlatforms(); setFilter({platform: choose.dataset.choosePlatform}); }
    else if (pin) changePlatformPin(pin.dataset.pin);
    else if (move) changePlatformPin(move.dataset.pinMove, Number(move.dataset.delta));
  });
  $('library-sort').addEventListener('change', event => changeLibrarySort(event.target.value, event.target.value === 'name' ? 'asc' : 'desc'));
  $('sort-direction').addEventListener('click', () => changeLibrarySort(state.sort, state.direction === 'asc' ? 'desc' : 'asc'));
  $('detail-back').addEventListener('click', closeGameDetail);
  $('detail-previous').addEventListener('click', () => stepGameDetail(-1));
  $('detail-next').addEventListener('click', () => stepGameDetail(1));
  $('recent-filter').addEventListener('click', () => setFilter({recent: !state.recent, favorites: false}));
  $('review-filter').addEventListener('click', () => {
    const filters = Object.keys(reviewFilters);
    setFilter({reviewFilter: filters[(filters.indexOf(state.reviewFilter) + 1) % filters.length]});
  });
  $('platform-nav').addEventListener('click', (event) => {
    const button = event.target.closest('[data-platform]');
    if (button) setFilter({platform: button.dataset.platform});
  });
  document.querySelector('.mode-filters').addEventListener('click', (event) => {
    const button = event.target.closest('[data-mode]');
    if (button) setFilter({mode: button.dataset.mode});
  });
  $('nav-library').addEventListener('click', () => setFilter({favorites: false, recent: false}));
  $('nav-favorites').addEventListener('click', () => setFilter({favorites: true, recent: false}));
  $('search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => setFilter({query: $('search').value.trim()}), 250);
  });
  $('game-grid').addEventListener('click', (event) => {
    const favorite = event.target.closest('[data-favorite]');
    if (favorite) { toggleFavorite(favorite.dataset.favorite); return; }
    const button = event.target.closest('[data-select]');
    if (button) openGameDetail(button.dataset.select);
  });
  $('game-grid').addEventListener('focusin', (event) => {
    const button = event.target.closest('[data-select]');
    if (button) selectGame(button.dataset.select);
  });
  $('load-more').addEventListener('click', () => loadGames(true));
  $('scan-button').addEventListener('click', showLibraryManagement);
  $('close-management').addEventListener('click', () => closeLibraryManagement());
  $('management-scan-button').addEventListener('click', () => runLibraryManagement('scan'));
  $('management-audit-button').addEventListener('click', () => runLibraryManagement('audit'));
  $('management-retry').addEventListener('click', () => { cancelManagementRead(); refreshLibraryManagement(); });
  $('management-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeLibraryManagement(); });
  $('empty-action').addEventListener('click', () => {
    const action = $('empty-action').dataset.action;
    if (action === 'reset') setFilter({query: '', platform: '', mode: '', reviewFilter: ''});
    else if (action === 'library') setFilter({favorites: false, recent: false});
    else if (action === 'scan') scanLibrary();
    else { loadPlatforms(); loadGames(); refreshHealth(); }
  });
  ['help-button', 'footer-help'].forEach((id) => $(id).addEventListener('click', showHelp));
  $('close-help').addEventListener('click', closeHelp);
  $('help-dialog').addEventListener('close', () => syncModalBody());
  $('help-dialog').addEventListener('click', (event) => { if (event.target === $('help-dialog')) { const rect = $('help-dialog').getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeHelp(); } });
  $('close-play').addEventListener('click', closePlayer);
  $('play-dialog').addEventListener('cancel', (event) => { event.preventDefault(); if (window.RetroMobilePlayer?.isMenuOpen()) window.RetroMobilePlayer.setMenu(false); else closePlayer(); });
  window.addEventListener('message', (event) => {
    if (event.origin !== location.origin || event.source !== $('game-frame')?.contentWindow) return;
    if (event.data?.type === 'retroone:exit') closePlayer();
  });
  $('stop-session').addEventListener('click', stopSession); $('stop-play').addEventListener('click', stopSession);
  $('resume-session').addEventListener('click', () => {
    closeSelf();
    resumeCloudSession(state.session);
  });
  $('fullscreen-play').addEventListener('click', async () => {
    if (window.RetroMobilePlayer) { await window.RetroMobilePlayer.toggleFullscreen(); return; }
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen({navigationUI: 'hide'}); }
    catch { notify(t("当前浏览器不支持此全屏方式，可使用游戏内的全屏按钮。")); }
  });
  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing || (state.playMode && !$('profile-dialog').open && !$('replace-dialog').open && !$('network-dialog').open && !$('display-dialog').open && !$('management-dialog').open && !$('downloads-dialog').open) || $('help-dialog').open) return;
    if ($('downloads-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeDownloads(); }
      else { const direction = {ArrowLeft:'left', ArrowRight:'right', ArrowUp:'up', ArrowDown:'down', KeyA:'left', KeyD:'right', KeyW:'up', KeyS:'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('downloads-dialog'), direction); } }
      return;
    }
    if ($('management-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeLibraryManagement(); }
      else { const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('management-dialog'), direction); } }
      return;
    }
    if ($('display-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeDisplaySettings(); }
      else { const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('display-dialog'), direction); } }
      return;
    }
    if ($('network-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeNetwork(); }
      else { const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('network-dialog'), direction); } }
      return;
    }
    if ($('self-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeSelf(); }
      else if (event.target.matches('select') && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.code)) return;
      else { const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('self-dialog'), direction); } }
      return;
    }
    if ($('replace-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeLaunchConflict(); }
      else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'KeyA', 'KeyD', 'KeyW', 'KeyS'].includes(event.code)) { event.preventDefault(); (document.activeElement === $('keep-current-game') ? $('replace-current-game') : $('keep-current-game')).focus(); }
      return;
    }
    if ($('watch-dialog').open) {
      if (event.code === 'Escape') { event.preventDefault(); closeWatch(); }
      else if (event.code === 'KeyF') { event.preventDefault(); toggleWatchFullscreen(); }
      else { const direction = {ArrowLeft:'left', ArrowRight:'right', ArrowUp:'up', ArrowDown:'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('watch-dialog'), direction); } }
      return;
    }
    if ($('family-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeFamily(); }
      else { const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('family-dialog'), direction); } }
      return;
    }
    const typing = event.target.matches('input,textarea,select,[contenteditable=true]');
    if ($('profile-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); if (state.profileEditing) showProfileCards(); else closeProfiles(); return; }
      if (typing) return;
      const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code];
      if (direction) { event.preventDefault(); focusProfileNeighbor(direction); }
      return;
    }

    if ($('platform-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closePlatforms(); }
      else if (!typing) { const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('platform-dialog'), direction); } }
      return;
    }
    if (state.detailOpen && !typing) {
      if (event.key === 'Escape') { event.preventDefault(); closeGameDetail(); return; }
      if (['[', ']', 'PageUp', 'PageDown'].includes(event.key)) { event.preventDefault(); stepGameDetail(['[', 'PageUp'].includes(event.key) ? -1 : 1); return; }
    }
    if (event.key === '/' && !typing) { event.preventDefault(); $('search').focus(); return; }
    if (event.key === 'Escape' && typing) { event.target.blur(); return; }
    if (typing) return;
    if (event.key === 'Escape') { event.preventDefault(); $('game-grid').querySelector('.is-selected .game-select')?.focus(); return; }
    const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code];
    if (direction) {
      event.preventDefault();
      const now = performance.now();
      if (!event.repeat || now - lastKeyboardMove >= 110) { lastKeyboardMove = now; focusNeighbor(direction); }
    }
  });
  window.addEventListener('gamepadconnected', startGamepads);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { if (state.profile) { refreshHealth(); refreshSession(); refreshMigration(); heartbeatPresence(); } loadProfiles({silent: true}); startGamepads(); } else leavePresence(state.profile, true); });
  window.addEventListener('online', () => { if (!state.profile) { loadProfiles(); return; } heartbeatPresence(); refreshHealth(); refreshSession(); if (!state.games.length) loadGames(); });
  window.addEventListener('offline', () => { state.connectionStatus = 'error'; $('health-detail').textContent = t("网络已断开"); notify(t("网络已断开"), true); state.presenceStale = true; renderFamilyPresence(); });
  window.addEventListener('popstate', async () => {
    const params = new URLSearchParams(location.search), detail = params.get('view') === 'detail';
    const changes = {platform: params.get('platform') || '', query: params.get('q') || '', mode: params.get('mode') || '', reviewFilter: validReviewFilter(params.get('review_filter')), favorites: params.get('favorite') === 'true', recent: params.get('recent') === 'true', sort: Library.sorts.includes(params.get('sort')) ? params.get('sort') : params.get('recent') === 'true' ? 'last_played' : 'popularity', direction: params.get('direction') === 'asc' ? 'asc' : 'desc'};
    const changed = Object.keys(changes).some(key => state[key] !== changes[key]);
    Object.assign(state, changes, {detailOpen: detail, initialGame: params.get('game'), detailNeighbors: null});
    renderDetailPage(); renderFilters(); renderPlatforms(); renderLibrarySort();
    if (changed || detail && state.selected?.id !== params.get('game')) await loadGames(false, {preserveSelection: true});
    else { state.initialGame = null; if (detail) { renderDetail(state.selected); refreshDetailNeighbors(); } }
    if (!detail) restoreLibraryPosition();
  });
  function updateClock() { const now = new Date(); $('clock').textContent = I18n.date(now, {hour: '2-digit', minute: '2-digit', hour12: false}); $('clock').dateTime = now.toISOString(); }
  updateClock(); setInterval(updateClock, 30000);
  $('gamepad-help').textContent = window.isSecureContext ? t("若手柄没有响应，先点击游戏画面，再按手柄任意键。") : t("当前使用 HTTP。部分浏览器只允许在 HTTPS 下读取手柄；未识别时请使用键盘，或通过 HTTPS 访问。");
  initializeDownloads();
  renderFilters();
  renderCurrentProfile(); applyInterfaceLanguage(I18n.getLocale()); showProfiles();
  setInterval(() => { if (!document.hidden && state.profile) { refreshHealth(); refreshSession(); } }, 12000);
  setInterval(() => { if (!document.hidden && state.profile) refreshMigration(); }, 12000);
  setInterval(() => { if (!document.hidden && $('profile-dialog').open) loadProfiles({silent: true}); }, 2000);
  setInterval(() => { if (!document.hidden) { heartbeatPresence(); loadProfiles({silent: true}); } }, 15000);
  setInterval(refreshNetwork, 5000);
  window.addEventListener('pagehide', () => { saveProfileView(); leavePresence(state.profile, true); });
  startGamepads();
})();
