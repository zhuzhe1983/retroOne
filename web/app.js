/* RetroOne library UI. All catalog data comes from the mounted library API. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const escapeHTML = (value = '') => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = (name, cls = 'icon') => `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const number = (value) => new Intl.NumberFormat('zh-CN').format(Number(value) || 0);
  const initial = new URLSearchParams(location.search);
  const state = {
    games: [], platforms: [], total: 0, platform: initial.get('platform') || '',
    query: initial.get('q') || '', mode: ['browser', 'stream', 'unavailable'].includes(initial.get('mode')) ? initial.get('mode') : '',
    favorites: initial.get('favorite') === 'true', recent: initial.get('recent') === 'true', selected: null, initialGame: initial.get('game'),
    loading: false, request: 0, controller: null, session: null, playMode: null, health: null,
    favoritePending: new Set(), scanPending: false, wasScanning: false, healthRefreshing: false,
    detailRequest: 0, focusReturn: null, launching: false,
    profile: null, profiles: [], profileEpoch: 0, profileRequests: new Set(), profileEditing: null, profileEditEnter: false, profileAvatar: 'pixel', pendingProfile: null, profileSaving: false,
    profileRegistryEpoch: 0, profileLoading: false, profileCreateRequestId: null, profileWatchId: null, profileRetrying: new Set(), profileCapacity: null,
    playGameId: null, familySelectedId: null, presenceStale: false, presenceEpoch: 0,
    pendingLaunch: null,
    displayData: null, displayDraft: null, displayLoading: false, displaySaving: false, displayRequest: 0, displayFresh: false, displayReadController: null,
    networkCapabilities: null, networkInvites: [], networkRoom: null, networkLoading: false, networkBusy: false, networkVersion: 0, networkCreate: null, networkSeen: new Set(), activeNetworkRoomId: null,
  };
  const presenceClientId = newRequestId();
  let presenceQueue = Promise.resolve();
  let toastTimer, searchTimer, gamepadFrame, gamepadLast = 0, gamepadPressed = {}, gamepadName = '';
  const gamepadHeldAt = {}, gamepadRepeatAt = {};
  let lastKeyboardMove = 0;
  const modeLabels = { browser: '浏览器运行', stream: '云端串流', unavailable: '待配置' };
  const modeIcons = { browser: 'browser', stream: 'cloud', unavailable: 'info' };

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
      try { body = await response.json(); } catch (error) { if (error.name === 'AbortError') throw error; throw new Error(response.ok ? '服务返回了无法识别的数据，请重试。' : `服务暂时无法响应（${response.status}）。`); }
      if (!globalRequest && epoch !== state.profileEpoch) throw new DOMException('Player changed', 'AbortError');
      if (!response.ok) {
        const detail = body.detail ?? body.error ?? body.message;
        const error = new Error(typeof detail === 'string' ? detail : detail?.message || `请求未完成（${response.status}），请稍后重试。`);
        error.status = response.status; error.detail = detail;
        throw error;
      }
      return body;
    } catch (error) {
      if (!globalRequest && epoch !== state.profileEpoch) throw new DOMException('Player changed', 'AbortError');
      if ((timedOut || error.name === 'AbortError') && !external?.aborted) throw new Error('连接超时，请检查服务器后重试。');
      if (error instanceof TypeError) throw new Error('无法连接游戏服务，请检查局域网连接后重试。');
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

  const avatarLabels = {pixel: '像素伙伴', leaf: '新叶', star: '星星', rocket: '火箭', cat: '猫咪'};
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
    const presence = profile?.presence;
    if (state.presenceStale || !presence) return '动态暂不可用';
    if (!presence.online) return '暂时离开';
    return presence.activity === 'library' ? '正在挑游戏' : presence.game_title ? `正在玩 ${presence.game_title}` : '正在玩游戏';
  }
  function renderFamilyPresence() {
    const strip = $('family-strip'), focusedId = document.activeElement?.dataset.familyProfile, inboxFocused = document.activeElement?.matches('[data-open-network]');
    const online = state.presenceStale ? [] : state.profiles.filter((profile) => profile.presence?.online && profile.id !== state.profile?.id);
    const known = state.profiles.some((profile) => profile.presence);
    const inviteCount = state.networkInvites.filter((invite) => ['pending', 'accepted'].includes(invite.status) && !['closed', 'expired'].includes(invite.room_status)).length;
    const invitation = inviteCount ? `<button class="family-inbox" data-open-network aria-label="查看 ${inviteCount} 条联机邀请">联机邀请 <b>${inviteCount}</b></button>` : '';
    const html = invitation + (online.length ? `<span class="family-online-label"><i></i>${online.length} 位家人在线</span>${online.map((profile) => `<button class="family-person" data-family-profile="${escapeHTML(profile.id)}" title="${escapeHTML(profile.name)} · ${escapeHTML(presenceLabel(profile))}" aria-label="${escapeHTML(profile.name)}，${escapeHTML(presenceLabel(profile))}，查看动态">${avatarHTML(profile.avatar)}<span><strong>${escapeHTML(profile.name)}</strong><small>${escapeHTML(presenceLabel(profile))}</small></span></button>`).join('')}` : `<span class="family-empty">${state.presenceStale || !known ? '家人动态暂不可用' : '其他家人暂时离开'}</span>`);
    if (strip.innerHTML !== html) {
      const scroll = strip.scrollLeft;
      strip.innerHTML = html; strip.scrollLeft = scroll;
      if (focusedId) [...strip.querySelectorAll('[data-family-profile]')].find((button) => button.dataset.familyProfile === focusedId)?.focus({preventScroll: true});
      if (inboxFocused) strip.querySelector('[data-open-network]')?.focus({preventScroll: true});
    }
    if ($('family-dialog').open) renderFamilyDetails();
    $('network-play-button').hidden = !inviteCount && !state.activeNetworkRoomId;
    $('network-play-button').textContent = state.activeNetworkRoomId ? '联机步骤' : `联机邀请 ${inviteCount}`;
  }
  function renderFamilyDetails() {
    const profile = state.profiles.find((item) => item.id === state.familySelectedId);
    if (!profile) return;
    $('family-title').textContent = profile.name;
    const presence = profile.presence;
    $('family-details').innerHTML = `${avatarHTML(profile.avatar)}<div><strong>${escapeHTML(presenceLabel(profile))}</strong><p>${state.presenceStale || !presence ? '暂时无法获取在线状态，请稍后再看。' : !presence.online ? '这位家人暂时离开了游戏馆。' : presence.activity === 'browser' ? '浏览器游戏' : presence.activity === 'stream' ? '云端游戏' : '在游戏馆浏览收藏。'}</p></div>`;
    const inviteButtons = profile.id !== state.profile?.id && presence?.online && !state.presenceStale && state.networkCapabilities?.available ? state.networkCapabilities.games.map((game) => `<button class="button" data-send-invite="${escapeHTML(game.game_id)}" ${state.networkBusy ? 'disabled' : ''}>邀请一起玩《${escapeHTML(game.title)}》</button>`).join('') : '';
    if ($('family-invite-actions').innerHTML !== inviteButtons) $('family-invite-actions').innerHTML = inviteButtons;
  }
  function showFamily(profileId) {
    $('family-invite-message').hidden = true;
    state.familySelectedId = profileId; renderFamilyDetails();
    $('family-dialog').showModal(); syncModalBody(); $('close-family').focus();
  }
  function closeFamily() {
    $('family-dialog').close(); syncModalBody();
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
    const view = {platform: state.platform, query: state.query, mode: state.mode, favorites: state.favorites, recent: state.recent, initialGame: state.selected?.id || null};
    try { sessionStorage.setItem(`retroone:view:${state.profile.id}`, JSON.stringify(view)); } catch {}
  }
  function readProfileView(id) {
    try { return JSON.parse(sessionStorage.getItem(`retroone:view:${id}`)) || {}; } catch { return {}; }
  }
  function renderCurrentProfile() {
    const profile = state.profile;
    const activity = state.session ? `继续 ${sessionTitle(state.session)}` : state.playMode ? `正在玩 ${$('play-title').textContent}` : '正在挑游戏';
    $('profile-button').innerHTML = `${profile ? avatarHTML(profile.avatar) : '<span class="profile-avatar" aria-hidden="true">?</span>'}<span class="current-player-copy"><strong>${escapeHTML(profile?.name || '选择玩家')}</strong>${profile ? `<small>${escapeHTML(activity)}</small>` : ''}</span>`;
    $('profile-button').dataset.playing = String(Boolean(state.session || state.playMode));
    $('profile-button').setAttribute('aria-label', profile ? `${profile.name}，${activity}，打开我的游戏空间` : '选择玩家');
    $('profile-button').title = profile ? `${profile.name} · ${activity} · 查看继续 / 结束 / 切换玩家` : '选择玩家';
    if ($('self-dialog').open) renderSelfDetails();
  }
  function renderSelfDetails() {
    $('self-title').textContent = state.profile?.name || '我的游戏空间';
    $('self-avatar').innerHTML = avatarHTML(state.profile?.avatar);
    $('session-title').textContent = state.session ? sessionTitle(state.session) : '正在挑游戏';
    $('self-session-note').textContent = state.session ? '云端游戏仍在运行，可以继续上次的进度。' : '选一款游戏，接着你的冒险。';
    $('resume-session').hidden = !state.session; $('stop-session').hidden = !state.session;
  }
  function showSelf() {
    if (!state.profile) { showProfiles(); return; }
    renderSelfDetails(); $('self-dialog').showModal(); syncModalBody();
    (state.session ? $('resume-session') : $('self-switch-profile')).focus();
  }
  function closeSelf() { $('self-dialog').close(); syncModalBody(); $('profile-button').focus(); }
  function displayMessage(message = '', error = false) {
    $('display-message').textContent = message; $('display-message').hidden = !message;
    $('display-message').dataset.error = String(error);
  }
  function displayOptions() {
    return (state.displayData?.capabilities?.stream_resolution || []).filter((option) => typeof option.value === 'string' && Number.isFinite(option.width) && Number.isFinite(option.height));
  }
  function renderDisplaySettings() {
    const data = state.displayData, active = data?.active, busy = state.displayLoading || state.displaySaving;
    const available = data?.capabilities?.available === true;
    const focusedValue = document.activeElement?.dataset.displayResolution;
    $('display-title').textContent = `${state.profile?.name || '玩家'} 的画面设置`;
    $('display-current').hidden = !(active || data?.active_unknown);
    if (active || data?.active_unknown) {
      $('display-current-title').textContent = !state.displayFresh ? '上次读取的云端会话' : state.session && (data.active_unknown || sessionId(state.session) === active?.session_id) ? `当前会话 · ${sessionTitle(state.session)}` : '当前云端会话';
      $('display-current-size').textContent = Number.isFinite(active?.width) && active.width > 0 && Number.isFinite(active?.height) && active.height > 0 ? `${active.width} × ${active.height}` : '实际分辨率暂未提供';
      $('display-pending').hidden = !data.pending;
    }
    $('display-saved').textContent = data?.settings?.stream_resolution ? `${state.displayFresh ? '已保存' : '上次读取'}：${data.settings.stream_resolution}` : state.displayLoading ? '正在读取设置…' : '尚未读取设置';
    $('display-options').innerHTML = displayOptions().map((option) => `<button type="button" class="display-option" data-display-resolution="${escapeHTML(option.value)}" aria-pressed="${state.displayDraft === option.value}" ${!available || busy || !state.displayFresh ? 'disabled' : ''}><strong>${escapeHTML(option.value)}</strong><small>${option.width} × ${option.height}</small>${state.displayDraft === option.value ? icon('check') : ''}</button>`).join('');
    $('display-unavailable').hidden = !data || available;
    $('display-unavailable').textContent = data?.capabilities?.reason || '当前服务暂不支持调整串流分辨率。';
    $('save-display').disabled = busy || !state.displayFresh || !available || !displayOptions().some((option) => option.value === state.displayDraft) || state.displayDraft === data?.settings?.stream_resolution;
    $('save-display').textContent = state.displaySaving ? '正在保存…' : '保存设置';
    $('display-options').setAttribute('aria-busy', String(busy));
    if (focusedValue) [...$('display-options').querySelectorAll('button')].find((button) => button.dataset.displayResolution === focusedValue && !button.disabled)?.focus({preventScroll: true});
  }
  async function loadDisplaySettings() {
    state.displayReadController?.abort();
    const controller = new AbortController(); state.displayReadController = controller;
    const epoch = state.profileEpoch, request = ++state.displayRequest;
    state.displayLoading = true; state.displayFresh = false; state.displayDraft = state.displayData?.settings?.stream_resolution || null;
    $('retry-display').hidden = true; displayMessage(state.displayData ? '显示上次读取的设置，正在刷新…' : ''); renderDisplaySettings();
    try {
      const result = await api('/api/settings/display', {timeoutMs: 6000, signal: controller.signal, priority: 'high'});
      if (epoch !== state.profileEpoch || request !== state.displayRequest) return;
      if (!result?.settings || !result?.capabilities) throw new Error('画面设置返回不完整，请重试。');
      state.displayData = result; state.displayDraft = result.settings.stream_resolution; state.displayFresh = true; displayMessage();
    } catch (error) {
      if (epoch !== state.profileEpoch || request !== state.displayRequest) return;
      displayMessage(error.message.includes('超时') ? '读取设置超过 6 秒，请点击「重新读取设置」重试。' : `未能读取最新设置：${error.message} 请重新读取后再保存。`, true); $('retry-display').hidden = false;
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
  async function saveDisplaySettings() {
    if ($('save-display').disabled || !state.profile) return;
    const epoch = state.profileEpoch, request = ++state.displayRequest, resolution = state.displayDraft;
    state.displaySaving = true; displayMessage(); renderDisplaySettings();
    try {
      // A closed panel or player switch cannot undo a write already sent.
      const result = await api('/api/settings/display', {method: 'PATCH', body: JSON.stringify({stream_resolution: resolution}), cancelOnProfileChange: false});
      if (epoch !== state.profileEpoch || request !== state.displayRequest) return;
      if (!result?.settings || !result?.capabilities) throw new Error('未收到完整的保存结果，请重新读取设置确认。');
      state.displayData = result; state.displayDraft = result.settings.stream_resolution; state.displayFresh = true;
      displayMessage(`已保存，下次启动云游戏时生效。${result.active || result.active_unknown ? '当前游戏继续运行。' : ''}`);
    } catch (error) {
      if (epoch === state.profileEpoch && request === state.displayRequest) {
        state.displayFresh = false; $('retry-display').hidden = false;
        displayMessage(`未能确认保存结果：${error.message} 请重新读取设置，确认是否已经保存。`, true);
      }
    } finally {
      if (epoch === state.profileEpoch && request === state.displayRequest) { state.displaySaving = false; renderDisplaySettings(); }
    }
  }
  function focusModalAction(dialog, direction) {
    const buttons = [...dialog.querySelectorAll('button:not(:disabled):not([hidden])')];
    const index = buttons.indexOf(document.activeElement), delta = direction === 'left' || direction === 'up' ? -1 : 1;
    buttons[(index + delta + buttons.length) % buttons.length]?.focus();
  }
  function networkMessage(message = '', error = false) {
    $('network-message').textContent = message; $('network-message').hidden = !message; $('network-message').dataset.error = String(error);
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
      root.innerHTML = `<p class="network-intro">双方各自启动游戏，再在游戏内加入同一局。</p><div class="network-members">${room.members.map((member) => `<div>${avatarHTML(member.avatar)}<span><strong>${escapeHTML(member.name)}</strong><small>${member.role === 'host' ? '创建游戏房间' : '查找游戏房间'}${member.profile_id === state.profile.id ? ' · 你' : ''}</small></span></div>`).join('')}</div>${ready ? `<ol class="network-steps">${steps.map((step) => `<li>${escapeHTML(step)}</li>`).join('')}</ol><p class="network-note">接受邀请不会自动进入比赛。两人都启动后，请在 WIRELESS BATTLE 中完成建房与加入。</p><button class="button network-primary" data-network-action="start" ${state.networkBusy ? 'disabled' : ''}>${sessionNetworkRoomId(state.session) === room.id ? '继续我的游戏' : '启动我的游戏'}</button>` : `<p class="network-note">${room.status === 'expired' ? '这次联机邀请已过期，请重新邀请。' : '这次联机邀请已关闭。已启动的游戏会继续运行。'}</p>`}<div class="network-footer"><button class="text-button" data-network-action="list">返回邀请列表</button>${ready ? `<button class="text-button" data-network-action="close-room" ${state.networkBusy ? 'disabled' : ''}>关闭这次邀请</button>` : ''}</div>`;
    } else {
      $('network-title').textContent = '联机邀请';
      const invites = state.networkInvites;
      root.innerHTML = invites.length ? invites.slice(0, 12).map((invite) => {
        const incoming = invite.to_profile_id === state.profile.id, other = incoming ? invite.from : invite.to;
        const status = {pending: incoming ? '邀请你一起玩' : '等待对方接受', accepted: '已接受 · 双方可以准备游戏', declined: '邀请已婉拒', cancelled: '邀请已取消', expired: '邀请已过期'}[invite.status] || '邀请状态更新中';
        const canAccept = state.networkCapabilities?.available && state.networkCapabilities.games.some((game) => game.game_id === invite.game_id);
        const button = (action, label, secondary = false) => `<button class="button${secondary ? ' button-secondary' : ''}" data-network-action="${action}" data-invite-id="${escapeHTML(invite.id)}" ${state.networkBusy ? 'disabled' : ''}>${label}</button>`;
        return `<article class="network-invite"><div class="network-person">${avatarHTML(other?.avatar)}<div><strong>${escapeHTML(other?.name || '家庭成员')}</strong><small>${status}</small></div></div><p>${escapeHTML(invite.game_title)}</p><div class="network-actions">${invite.status === 'pending' ? incoming ? `${canAccept ? button('accept', '接受邀请') : ''}${button('decline', '暂时不玩', true)}` : button('cancel', '取消邀请', true) : invite.status === 'accepted' && invite.room_id && !['closed', 'expired'].includes(invite.room_status) ? button('open-room', '查看联机步骤') : ''}</div></article>`;
      }).join('') : `<p class="network-note">暂无联机邀请。点击顶栏在线家人的头像，可以邀请一起玩已经验证支持联机的游戏。</p>`;
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
        if (!state.networkSeen.has(key) && invite.to_profile_id === profileId && invite.status === 'pending') notify(`${invite.from?.name || '家人'} 邀请你一起玩《${invite.game_title}》`);
        if (!state.networkSeen.has(key) && invite.from_profile_id === profileId && invite.status === 'accepted') notify(`${invite.to?.name || '家人'} 已接受邀请，双方可以分别启动游戏。`);
        state.networkSeen.add(key);
      }
      if (state.networkRoom && $('network-dialog').open) {
        const room = await api(`/api/network/rooms/${encodeURIComponent(state.networkRoom.id)}`, {global: true});
        if (epoch !== state.profileEpoch || version !== state.networkVersion) return;
        if (validRoom(room)) state.networkRoom = room;
      }
      renderFamilyPresence(); if ($('network-dialog').open) renderNetwork();
    } catch (error) { if (epoch === state.profileEpoch && $('network-dialog').open) networkMessage(error.message, true); }
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
      networkMessage('邀请已发送。对方接受后，双方各自点击「启动我的游戏」。');
    } catch (error) { if (epoch === state.profileEpoch) { $('family-invite-message').textContent = error.message; $('family-invite-message').hidden = false; } }
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
        if (state.playMode === 'browser') { notify('请先保存浏览器游戏并返回游戏库，再启动这次联机。'); return; }
        closePlayer({refresh: false});
      }
      state.networkBusy = true; renderNetwork(); networkMessage('正在准备你的游戏，首次解压可能需要数分钟…');
      try { await launchGame({id: room.game_id, title: room.game_title, mode: 'stream'}, {networkRoomId: room.id}); }
      finally { if (epoch === state.profileEpoch) { state.networkBusy = false; if ($('network-dialog').open) renderNetwork(); } }
      return;
    }
    const invite = state.networkInvites.find((item) => item.id === inviteId);
    const roomId = action === 'open-room' ? invite?.room_id : state.networkRoom?.id;
    if (action === 'open-room' && !roomId) return;
    state.networkBusy = true; state.networkVersion++; networkMessage('正在处理…'); renderNetwork();
    try {
      if (action === 'open-room') {
        const room = await api(`/api/network/rooms/${encodeURIComponent(roomId)}`, {global: true});
        if (epoch !== state.profileEpoch) return;
        if (!validRoom(room)) throw new Error('联机邀请信息不完整，请重新打开。');
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
        if (action === 'accept') { if (!validRoom(result.room)) throw new Error('邀请已处理，请重新查看联机步骤。'); state.networkRoom = result.room; }
      }
      networkMessage();
    } catch (error) { if (epoch === state.profileEpoch) networkMessage(error.message, true); }
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
    [...$('profile-grid').querySelectorAll('[data-profile-action]')].find((button) => button.dataset.profileAction === key)?.focus({preventScroll: true});
  }
  function renderProfileCards() {
    const focused = profileFocusKey();
    $('profile-grid').innerHTML = state.profiles.map((profile) => {
      const status = profileStatus(profile), id = escapeHTML(profile.id);
      const label = status === 'provisioning' ? '正在准备专属空间…' : status === 'error' ? '准备失败' : profile.configured ? profile.id === state.profile?.id ? '当前玩家' : '进入游戏馆' : '设置昵称与头像';
      return `<div class="profile-card-wrap"><button class="profile-card" data-profile-id="${id}" data-profile-action="select:${id}" data-status="${escapeHTML(status)}" aria-current="${profile.id === state.profile?.id}" aria-disabled="${status !== 'ready'}">${avatarHTML(profile.avatar)}<strong>${escapeHTML(profile.name)}</strong><small>${status === 'provisioning' ? '<span class="profile-busy" aria-hidden="true"></span>' : ''}${label}</small>${profile.presence && status === 'ready' ? `<span class="profile-presence" data-online="${profile.presence.online && !state.presenceStale}">${escapeHTML(presenceLabel(profile))}</span>` : ''}</button>${status === 'error' ? `<p class="profile-error">${escapeHTML(profile.error || profile.message || '专属空间暂时无法启动，请重试。')}</p><button class="profile-retry" data-retry-profile="${id}" data-profile-action="retry:${id}" ${state.profileRetrying.has(profile.id) ? 'disabled' : ''}>${state.profileRetrying.has(profile.id) ? '正在重试…' : '重新准备'}</button>` : ''}${profile.configured ? `<button class="profile-edit" data-edit-profile="${id}" data-profile-action="edit:${id}">编辑昵称与头像</button>` : ''}</div>`;
    }).join('') + `<div class="profile-card-wrap"><button class="profile-card profile-add" data-add-profile data-profile-action="add" aria-disabled="${state.profileCapacity?.remaining === 0}"><span class="profile-avatar profile-add-icon" aria-hidden="true">＋</span><strong>添加家庭成员</strong><small>${state.profileCapacity?.remaining === 0 ? '已达到成员数量上限' : '独立收藏与存档'}</small></button></div>`;
    if (focused) focusProfileAction(focused);
  }
  function profileMessage(message = '', error = false) {
    $('profile-message').textContent = message;
    $('profile-message').hidden = !message;
    $('profile-message').dataset.error = String(error);
  }
  function showProfileCards() {
    if (state.profileSaving) return;
    state.profileEditing = null; state.pendingProfile = null; state.profileCreateRequestId = null;
    $('profile-title').textContent = '今天，谁来玩？';
    $('profile-description').textContent = '各自收藏，各自存档。选好玩家，接着上次的冒险。';
    $('profile-grid').hidden = false; $('profile-editor').hidden = true; $('profile-confirm').hidden = true;
    $('close-profiles').hidden = !state.profile;
    profileMessage(); renderProfileCards();
    const last = state.profile?.id || remembered('retroone:last-profile');
    const cards = [...$('profile-grid').querySelectorAll('.profile-card')];
    (cards.find((card) => card.dataset.profileId === last) || cards[0])?.focus();
  }
  async function loadProfiles({silent = false} = {}) {
    if (state.profileLoading || state.profileSaving || state.profileRetrying.size) return;
    state.profileLoading = true;
    const epoch = state.profileRegistryEpoch;
    if (!silent) { $('retry-profiles').hidden = true; profileMessage('正在读取玩家档案…'); }
    try {
      const result = await api('/api/profiles', {global: true});
      if (epoch !== state.profileRegistryEpoch) return;
      if (!Array.isArray(result.profiles)) throw new Error('玩家档案暂时无法读取，请重试。');
      const profiles = result.profiles.filter(validProfile);
      const capacity = result.capacity || null;
      const changed = JSON.stringify(profiles) !== JSON.stringify(state.profiles) || JSON.stringify(capacity) !== JSON.stringify(state.profileCapacity);
      state.profileCapacity = capacity;
      state.profiles = profiles;
      state.presenceStale = false; renderFamilyPresence();
      if (!silent && !state.profileEditing && !state.pendingProfile) showProfileCards();
      else if (changed && !$('profile-grid').hidden) renderProfileCards();
      const current = profiles.find((profile) => profile.id === state.profile?.id);
      if (current && (current.name !== state.profile.name || current.avatar !== state.profile.avatar)) { state.profile = current; renderCurrentProfile(); }
      if (!$('profile-grid').hidden) {
        $('retry-profiles').hidden = true;
        const watched = profiles.find((profile) => profile.id === state.profileWatchId);
        if (watched) {
          const status = profileStatus(watched);
          profileMessage(status === 'ready' ? `${watched.name} 的专属空间已准备好，选择头像即可进入。` : status === 'error' ? `${watched.name} 准备失败，请点击「重新准备」。` : `正在为 ${watched.name} 准备专属空间，请稍候。`, status === 'error');
        } else if ($('profile-message').dataset.connectionError === 'true') profileMessage();
        $('profile-message').dataset.connectionError = 'false';
      }
    } catch (error) {
      if (epoch !== state.profileRegistryEpoch) return;
      state.presenceStale = true; renderFamilyPresence();
      if (!silent || !$('profile-grid').hidden) {
        profileMessage(silent ? `状态更新失败：${error.message}` : error.message, true);
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
    if (creating && state.profileCapacity?.remaining === 0) { profileMessage(`家庭成员已达到 ${state.profileCapacity.limit} 位上限，暂时无法继续添加。`, true); return; }
    profile ||= {name: '', avatar: 'pixel', configured: false};
    state.profileEditing = profile; state.profileEditEnter = enterAfterSave; state.profileAvatar = profile.avatar || 'pixel'; state.profileCreateRequestId = null;
    $('profile-grid').hidden = true; $('profile-confirm').hidden = true; $('profile-editor').hidden = false;
    $('retry-profiles').hidden = true;
    $('profile-title').textContent = creating ? '添加家庭成员' : profile.configured ? '这是你的玩家档案。' : '先认识一下你。';
    $('profile-description').textContent = creating ? '取个昵称，选个头像。我们会准备独立的游戏空间，收藏与存档各自保留。' : '昵称和头像会出现在你的游戏馆，也会用于今后的联机房间。';
    $('profile-name').value = profile.configured ? profile.name : '';
    $('save-profile').textContent = creating ? '添加成员' : enterAfterSave ? '保存并进入' : '保存档案';
    profileMessage(); renderAvatarOptions(); $('profile-name').focus();
  }
  function renderAvatarOptions() {
    $('avatar-options').innerHTML = Object.entries(avatarLabels).map(([key, name]) => `<button type="button" class="avatar-option" role="radio" data-avatar-option="${key}" aria-checked="${state.profileAvatar === key}" aria-label="${name}" title="${name}">${avatarHTML(key)}</button>`).join('');
  }
  async function saveEditedProfile(event) {
    event.preventDefault();
    if (!state.profileEditing || state.profileSaving) return;
    const name = $('profile-name').value.trim();
    if (!name || [...name].length > 20) { profileMessage('请输入 1–20 个字的昵称。', true); $('profile-name').focus(); return; }
    const id = state.profileEditing.id, enter = state.profileEditEnter, creating = !id;
    if (creating && !state.profileCreateRequestId) state.profileCreateRequestId = newRequestId();
    state.profileRegistryEpoch++;
    state.profileSaving = true; $('save-profile').disabled = true; $('cancel-profile-edit').disabled = true;
    profileMessage(creating ? '正在添加家庭成员…' : '正在保存玩家档案…');
    try {
      const result = await api(creating ? '/api/profiles' : `/api/profiles/${encodeURIComponent(id)}`, {global: true, method: creating ? 'POST' : 'PATCH', body: JSON.stringify({name, avatar: state.profileAvatar, ...(creating ? {request_id: state.profileCreateRequestId} : {})})});
      const profile = result.profile || result;
      if (!validProfile(profile)) throw new Error('玩家档案返回不完整，请重试。');
      const index = state.profiles.findIndex((item) => item.id === profile.id);
      if (index < 0) state.profiles.push(profile); else state.profiles[index] = {...state.profiles[index], ...profile};
      if (state.profile?.id === profile.id) { state.profile = profile; renderCurrentProfile(); }
      state.profileSaving = false;
      if (creating) {
        state.profileWatchId = profile.id; showProfileCards(); focusProfileAction(`select:${profile.id}`);
        profileMessage(profileStatus(profile) === 'ready' ? `${profile.name} 的专属空间已准备好，选择头像即可进入。` : profileStatus(profile) === 'error' ? `${profile.name} 准备失败，请点击「重新准备」。` : `已添加 ${profile.name}，正在准备专属空间，请稍候。`, profileStatus(profile) === 'error');
      } else if (enter) chooseProfile(profile);
      else showProfileCards();
    } catch (error) { profileMessage(error.message, true); }
    finally { state.profileSaving = false; $('save-profile').disabled = false; $('cancel-profile-edit').disabled = false; }
  }
  function chooseProfile(profile) {
    if (!profile || state.profileSaving) return;
    if (profileStatus(profile) !== 'ready') {
      profileMessage(profileStatus(profile) === 'error' ? profile.error || profile.message || '准备失败，请点击「重新准备」。' : '专属空间正在准备中，准备完成后即可进入。', profileStatus(profile) === 'error');
      if (profileStatus(profile) === 'error') focusProfileAction(`retry:${profile.id}`);
      return;
    }
    if (!profile.configured) { editProfile(profile, true); return; }
    if (state.profile?.id === profile.id) { closeProfiles(); return; }
    if (state.playMode === 'browser') {
      state.pendingProfile = profile;
      $('profile-grid').hidden = true; $('profile-editor').hidden = true; $('profile-confirm').hidden = false;
      $('profile-title').textContent = '先保存，再换玩家。';
      $('profile-description').textContent = `准备切换到 ${profile.name} 的游戏馆。`;
      profileMessage(); $('profile-back-to-game').focus(); return;
    }
    applyProfile(profile);
  }
  async function retryProfile(profile) {
    if (!profile || state.profileRetrying.has(profile.id) || state.profileSaving) return;
    state.profileRetrying.add(profile.id); state.profileRegistryEpoch++; renderProfileCards();
    profileMessage(`正在重新准备 ${profile.name} 的专属空间…`);
    try {
      const result = await api(`/api/profiles/${encodeURIComponent(profile.id)}/retry`, {global: true, method: 'POST', body: '{}'});
      const updated = result.profile || result;
      if (!validProfile(updated)) throw new Error('准备状态返回不完整，请重试。');
      state.profiles = state.profiles.map((item) => item.id === profile.id ? updated : item);
      state.profileWatchId = profile.id;
      profileMessage(profileStatus(updated) === 'ready' ? '专属空间已准备好，选择头像即可进入。' : profileStatus(updated) === 'error' ? updated.error || '准备失败，请稍后重试。' : '已重新开始准备，请稍候。', profileStatus(updated) === 'error');
    } catch (error) { profileMessage(error.message, true); }
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
    state.profileRequests.clear(); state.profileEpoch++; state.request++; state.detailRequest++;
    state.displayReadController?.abort(); state.displayReadController = null;
    state.displayRequest++; state.displayData = null; state.displayDraft = null; state.displayLoading = false; state.displaySaving = false; state.displayFresh = false;
    if ($('display-dialog').open) $('display-dialog').close();
    if (state.playMode) closePlayer({refresh: false, presence: false});
    state.profile = profile;
    const defaults = {platform: '', query: '', mode: '', favorites: false, recent: false, initialGame: null};
    const linkedView = first && initial.size && (!initial.has('player') || initial.get('player') === profile.id) ? {platform: initial.get('platform') || '', query: initial.get('q') || '', mode: ['browser', 'stream', 'unavailable'].includes(initial.get('mode')) ? initial.get('mode') : '', favorites: initial.get('favorite') === 'true', recent: initial.get('recent') === 'true', initialGame: initial.get('game')} : null;
    Object.assign(state, defaults, linkedView || readProfileView(profile.id), {games: [], platforms: [], total: 0, selected: null, session: null, health: null, loading: false, scanPending: false, wasScanning: false, healthRefreshing: false, launching: false, pendingProfile: null, networkCapabilities: null, networkInvites: [], networkRoom: null, networkLoading: false, networkBusy: false, networkVersion: 0, networkCreate: null, networkSeen: new Set(), activeNetworkRoomId: null});
    state.favoritePending.clear();
    $('stop-session').disabled = false; $('stop-play').disabled = false;
    $('scan-button').disabled = false; $('scan-button').innerHTML = `${icon('refresh')}<span>更新游戏库</span>`;
    $('migration-banner').hidden = true; $('toast').hidden = true;
    $('connection-label').textContent = '连接中'; $('status-button').dataset.state = 'connecting';
    remember('retroone:last-profile', profile.id); renderCurrentProfile(); renderSession(); renderDetail(null); renderPlatforms(); renderFilters(); updateURL();
    renderFamilyPresence();
    $('profile-dialog').close(); syncModalBody();
    heartbeatPresence();
    refreshNetwork();
    const epoch = state.profileEpoch;
    Promise.allSettled([loadPlatforms(), loadGames(), refreshHealth(), refreshSession(), refreshMigration()]).then(() => {
      if (epoch === state.profileEpoch && !document.querySelector('dialog[open]') && [document.body, $('profile-button')].includes(document.activeElement)) {
        // Focusing a card selects it. Keep an explicit link or restored selection,
        // including games whose card is beyond the first catalog page.
        const selectedCard = [...$('game-grid').querySelectorAll('.game-select')].find((button) => button.dataset.select === String(state.selected?.id));
        const detailFocus = state.selected ? ($('launch-game')?.disabled ? $('profile-button') : $('launch-game')) : null;
        (selectedCard || detailFocus || $('game-grid').querySelector('.game-select'))?.focus({preventScroll: true});
      }
    });
  }
  function focusProfileNeighbor(direction) {
    let buttons;
    if (!$('profile-confirm').hidden) buttons = [...$('profile-confirm').querySelectorAll('button:not(:disabled)')];
    else if (!$('profile-editor').hidden) buttons = [$('profile-name'), ...$('profile-editor').querySelectorAll('button:not(:disabled)')];
    else {
      const actions = [...$('profile-grid').querySelectorAll('[data-profile-action]:not(:disabled)')];
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
    if (!buttons.length) return;
    const index = buttons.indexOf(document.activeElement);
    const delta = direction === 'left' || direction === 'up' ? -1 : 1;
    buttons[index < 0 ? 0 : (index + delta + buttons.length) % buttons.length]?.focus();
  }

  function formatSize(bytes) {
    if (bytes == null || !Number.isFinite(Number(bytes))) return '未知';
    bytes = Number(bytes);
    if (bytes < 1024) return `${number(bytes)} B`;
    const unit = bytes >= 1024 ** 3 ? 'GB' : bytes >= 1024 ** 2 ? 'MB' : 'KB';
    const divisor = unit === 'GB' ? 1024 ** 3 : unit === 'MB' ? 1024 ** 2 : 1024;
    return `${(bytes / divisor).toLocaleString('zh-CN', {maximumFractionDigits: 1})} ${unit}`;
  }

  function platformIcon(id = '') {
    if (/^(gb|gbc|gba|nds|3ds|psp|psvita|ngp|ngpc|ws|wsc|gamegear|lynx)/i.test(id)) return 'handheld';
    if (/arcade|mame|fbneo|fba|cps|neogeo/i.test(id)) return 'arcade';
    if (/dos|windows|pc98|scumm|amiga|msx|pc$/i.test(id)) return 'pc';
    return 'console';
  }

  function notify(message, error = false) {
    $('toast').textContent = message;
    $('toast').dataset.error = String(error);
    $('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 6500 : 3500);
  }

  function updateURL() {
    const params = new URLSearchParams();
    if (state.profile) params.set('player', state.profile.id);
    if (state.platform) params.set('platform', state.platform);
    if (state.query) params.set('q', state.query);
    if (state.mode) params.set('mode', state.mode);
    if (state.favorites) params.set('favorite', 'true');
    if (state.recent) params.set('recent', 'true');
    const gameId = state.playGameId || state.initialGame || state.selected?.id;
    if (gameId) params.set('game', gameId);
    history.replaceState(null, '', `${location.pathname}${params.size ? '?' + params.toString() : ''}`);
  }

  function renderFilters() {
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
    $('library-title').replaceChildren(document.createTextNode(state.favorites ? '我的收藏 ' : state.recent ? '最近游玩 ' : platform?.name ? `${platform.name} ` : '全部游戏 '));
    const count = document.createElement('span'); count.id = 'result-count';
    count.textContent = state.loading ? '正在查找…' : `${number(state.total)} 款`;
    $('library-title').append(count);
    $('section-eyebrow').textContent = state.favorites ? 'YOUR FAVORITES' : state.recent ? 'RECENTLY PLAYED' : 'THE COLLECTION';
    document.title = `${state.favorites ? '我的收藏' : state.recent ? '最近游玩' : platform?.name || '私人游戏馆'} · RetroOne`;
  }

  function renderPlatforms() {
    const total = state.platforms.reduce((sum, platform) => sum + (Number(platform.count) || 0), 0);
    const items = [{id: '', name: '全部游戏', count: total}, ...state.platforms];
    $('platform-nav').innerHTML = items.map((platform) => `<button class="platform-button ${state.platform === platform.id ? 'is-active' : ''}" data-platform="${escapeHTML(platform.id)}" aria-pressed="${state.platform === platform.id}" title="${escapeHTML(platform.name)} · ${number(platform.count)} 款">${icon(platform.id ? platformIcon(platform.id) : 'library', 'platform-icon')}<span>${escapeHTML(platform.name)}</span><small>${number(platform.count)}</small></button>`).join('');
    $('stat-games').textContent = number(total);
    $('stat-platforms').textContent = number(state.platforms.length);
  }

  function fallbackCover(game) {
    return `<span class="cover-fallback">${icon(platformIcon(game.platform))}<strong>${escapeHTML(game.title)}</strong><small>${escapeHTML(game.platform_name || game.platform || 'RETROONE')}</small></span>`;
  }

  function cover(game, detail = false) {
    const url = localURL(game.cover_url);
    return url ? `<img src="${escapeHTML(url)}" alt="${detail ? escapeHTML(game.title) + ' 游戏封面' : ''}" loading="${detail ? 'eager' : 'lazy'}" decoding="async" data-cover-id="${escapeHTML(game.id)}">` : fallbackCover(game);
  }

  function card(game) {
    const mode = modeLabels[game.mode] ? game.mode : 'unavailable';
    return `<article class="game-card ${state.selected?.id === game.id ? 'is-selected' : ''}" data-game-id="${escapeHTML(game.id)}"><button class="game-select" data-select="${escapeHTML(game.id)}" aria-label="查看 ${escapeHTML(game.title)}，${escapeHTML(game.platform_name || game.platform)}，${modeLabels[mode]}" aria-pressed="${state.selected?.id === game.id}"><div class="game-cover">${cover(game)}<span class="game-mode ${mode}">${icon(modeIcons[mode])}${modeLabels[mode]}</span></div><h3 class="game-title" title="${escapeHTML(game.title)}">${escapeHTML(game.title)}</h3><span class="game-subtitle">${icon(platformIcon(game.platform))}<span>${escapeHTML(game.platform_name || game.platform)}</span></span></button><button class="icon-button card-favorite ${game.favorite ? 'is-favorite' : ''}" data-favorite="${escapeHTML(game.id)}" aria-label="${game.favorite ? '取消收藏' : '收藏'} ${escapeHTML(game.title)}" aria-pressed="${Boolean(game.favorite)}" ${state.favoritePending.has(game.id) ? 'disabled' : ''}>${icon('star')}</button></article>`;
  }

  function bindImageErrors(root) {
    root.querySelectorAll('img[data-cover-id]').forEach((img) => {
      img.addEventListener('error', () => {
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
    bindImageErrors($('game-grid'));
    $('empty-state').hidden = state.games.length > 0;
    if (!state.games.length) showEmpty('empty');
    $('pagination').hidden = state.games.length === 0;
    $('pagination-label').textContent = `已显示 ${number(state.games.length)} / ${number(state.total)} 款`;
    $('load-more').hidden = state.games.length >= state.total;
    $('load-more').disabled = false;
    $('load-more').innerHTML = `载入更多${icon('arrow')}`;
    $('games-region').setAttribute('aria-busy', 'false');
  }

  function renderSkeletons() {
    $('empty-state').hidden = true;
    $('pagination').hidden = true;
    $('game-grid').innerHTML = Array.from({length: 8}, () => '<div class="skeleton" aria-hidden="true"><div class="game-cover"></div><div class="skeleton-line"></div><div class="skeleton-line short"></div></div>').join('');
    $('games-region').setAttribute('aria-busy', 'true');
  }

  function showEmpty(kind, message) {
    $('empty-state').hidden = false;
    $('empty-action').dataset.action = kind === 'error' ? 'retry' : state.query || state.mode || state.platform ? 'reset' : state.favorites || state.recent ? 'library' : 'scan';
    $('empty-title').textContent = kind === 'error' ? '暂时没能打开游戏库' : state.query || state.mode || state.platform ? '没有找到匹配的游戏' : state.favorites ? '留一个位置，给最爱的游戏' : state.recent ? '从第一款游戏开始' : '你的游戏，正在等待入馆';
    $('empty-copy').textContent = message || (state.query || state.mode || state.platform ? '试试更短的关键词，或切换平台和运行方式。' : state.favorites ? '点击游戏封面上的星标，下次就能更快找到它。' : state.recent ? '开始游玩后，这里会留下你的最近记录。' : '将整理好的游戏目录挂载后，更新游戏库即可开始浏览。');
    $('empty-action').textContent = kind === 'error' ? '重新加载' : state.query || state.mode || state.platform ? '清除筛选' : state.favorites || state.recent ? '浏览游戏库' : '更新游戏库';
  }

  function renderDetail(game) {
    if (!game) {
      $('game-detail').innerHTML = `<div class="detail-placeholder">${icon('controller', 'detail-placeholder-icon')}<p>把回忆握在手里</p><span>选择一款游戏，查看详情。</span></div>`;
      return;
    }
    const mode = modeLabels[game.mode] ? game.mode : 'unavailable';
    const videoURL = localURL(game.video_url);
    const size = game.unpacked_size ?? game.size;
    $('game-detail').innerHTML = `<div class="detail-eyebrow"><p class="eyebrow">IN YOUR COLLECTION</p><span>${game.favorite ? '已收藏' : '游戏详情'}</span></div><div class="detail-cover">${cover(game, true)}</div><p class="detail-platform">${escapeHTML(game.platform_name || game.platform)}</p><h2 class="detail-title">${escapeHTML(game.title)}</h2><div class="detail-actions"><button class="button detail-launch" id="launch-game" ${mode === 'unavailable' ? 'disabled' : ''}>${icon(mode === 'unavailable' ? 'info' : 'play')}${mode === 'unavailable' ? '等待配置' : mode === 'stream' ? '开始云端游戏' : '开始游戏'}</button><button class="icon-button detail-favorite ${game.favorite ? 'is-favorite' : ''}" id="detail-favorite" aria-label="${game.favorite ? '取消收藏' : '收藏'} ${escapeHTML(game.title)}" aria-pressed="${Boolean(game.favorite)}" ${state.favoritePending.has(game.id) ? 'disabled' : ''}>${icon('star')}</button></div><p class="detail-note">${mode === 'browser' ? '在当前设备运行 · 进度可在模拟器中保存' : mode === 'stream' ? '服务器运行 · 游戏镜像无需下载到本机' : '保留在游戏库中，补齐环境后即可尝试启动'}</p>${game.description ? `<p class="detail-description">${escapeHTML(game.description)}</p>` : ''}${videoURL ? `<button class="button button-secondary video-toggle" id="preview-video">${icon('play')}观看游戏预览</button><video class="detail-video" id="detail-video" src="${escapeHTML(videoURL)}" controls preload="none" playsinline hidden></video>` : ''}<dl class="detail-facts"><div><dt>运行方式</dt><dd>${modeLabels[mode]}</dd></div><div><dt>游戏大小</dt><dd>${formatSize(size)}</dd></div>${game.collection ? `<div><dt>来自收藏</dt><dd>${escapeHTML(game.collection)}</dd></div>` : ''}</dl>${game.reason ? `<p class="detail-reason">${icon('info')}${escapeHTML(game.reason)}</p>` : ''}`;
    bindImageErrors($('game-detail'));
    updateLaunchButton();
    $('launch-game').addEventListener('click', () => launchGame(game));
    $('detail-favorite').addEventListener('click', () => toggleFavorite(game.id));
    $('preview-video')?.addEventListener('click', () => {
      const video = $('detail-video');
      video.hidden = !video.hidden;
      $('preview-video').innerHTML = `${icon(video.hidden ? 'play' : 'close')}${video.hidden ? '观看游戏预览' : '收起预览'}`;
      if (video.hidden) video.pause(); else video.play().catch(() => {});
    });
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
    if (options.scroll && matchMedia('(max-width: 650px)').matches) $('game-detail').scrollIntoView({behavior: 'smooth', block: 'start'});
    if (options.focus) $('launch-game')?.focus({preventScroll: !options.scroll});
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
    const keepSelection = preserveSelection || Boolean(state.playMode);
    state.controller?.abort();
    const controller = new AbortController(); state.controller = controller;
    const request = ++state.request; state.loading = true;
    const start = append ? state.games.length : 0;
    if (append) { $('load-more').disabled = true; $('load-more').textContent = '正在载入…'; }
    else renderSkeletons();
    renderFilters();
    const params = new URLSearchParams({offset: String(start), limit: '60'});
    if (state.query) params.set('q', state.query);
    if (state.platform) params.set('platform', state.platform);
    if (state.mode) params.set('mode', state.mode);
    if (state.favorites) params.set('favorite', 'true');
    if (state.recent) params.set('recent', 'true');
    try {
      const result = await api(`/api/games?${params}`, {signal: controller.signal});
      if (request !== state.request) return;
      if (!Array.isArray(result.games)) throw new Error('游戏目录格式不正确，请稍后更新游戏库。');
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
      state.selected = selection || (keepSelection ? state.selected : null) || state.games[0] || null;
      renderGames(append, start);
      if (!append) renderDetail(state.selected);
      updateURL();
    } catch (error) {
      if (error.name === 'AbortError' || request !== state.request) return;
      if (append) {
        notify(error.message, true); $('load-more').disabled = false; $('load-more').textContent = '重试载入';
      } else {
        state.games = []; state.total = 0;
        if (!keepSelection) state.selected = null;
        $('game-grid').replaceChildren(); renderDetail(state.selected); showEmpty('error', error.message);
      }
    } finally {
      if (request === state.request) { state.loading = false; $('games-region').setAttribute('aria-busy', 'false'); renderFilters(); }
    }
  }

  function setFilter(changes) {
    if (!state.profile) return;
    clearTimeout(searchTimer);
    Object.assign(state, changes); updateURL(); renderFilters(); renderPlatforms(); loadGames();
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
      notify(game.favorite ? '已加入我的收藏' : '已取消收藏');
      if (state.favorites && !game.favorite) { await loadGames(); return; }
      const article = [...document.querySelectorAll('[data-game-id]')].find((item) => item.dataset.gameId === String(id));
      const button = article?.querySelector('[data-favorite]');
      if (button) { button.classList.toggle('is-favorite', game.favorite); button.setAttribute('aria-pressed', String(game.favorite)); button.setAttribute('aria-label', `${game.favorite ? '取消收藏' : '收藏'} ${game.title}`); }
      if (state.selected?.id === game.id) {
        const detail = $('detail-favorite');
        detail?.classList.toggle('is-favorite', game.favorite);
        detail?.setAttribute('aria-pressed', String(game.favorite));
        detail?.setAttribute('aria-label', `${game.favorite ? '取消收藏' : '收藏'} ${game.title}`);
        const label = $('game-detail').querySelector('.detail-eyebrow > span'); if (label) label.textContent = game.favorite ? '已收藏' : '游戏详情';
      }
    } catch (error) { if (epoch === state.profileEpoch) notify(error.message, true); }
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
  function sessionTitle(session) { return session?.game?.title || session?.title || session?.game_title || '当前游戏'; }
  function updateLaunchButton() {
    if (state.launching || state.selected?.mode !== 'stream' || !$('launch-game')) return;
    $('launch-game').innerHTML = `${icon('play')}${state.session && sessionGameId(state.session) === state.selected.id ? '继续游戏' : '开始云端游戏'}`;
  }
  function showLaunchConflict(game, detail, {networkRoomId = null} = {}) {
    const session = detail.session;
    if (!sessionId(session)) { notify(detail.message || '当前云端会话已变化，请重新选择游戏。', true); refreshSession(); return; }
    state.session = {...session, ...(detail.game ? {game: detail.game} : {})}; renderSession();
    if ($('network-dialog').open) closeNetwork();
    state.pendingLaunch = {action: 'replace', game, session: state.session, epoch: state.profileEpoch, networkRoomId};
    $('replace-title').textContent = '切换云端游戏？';
    $('replace-description').textContent = `正在运行《${sessionTitle(state.session)}》，结束并启动《${game.title}》？`;
    $('keep-current-game').textContent = `继续玩《${sessionTitle(state.session)}》`;
    $('replace-current-game').textContent = `结束并启动《${game.title}》`;
    $('keep-current-game').disabled = false; $('replace-current-game').disabled = false;
    if (!$('replace-dialog').open) $('replace-dialog').showModal();
    syncModalBody(); $('keep-current-game').focus();
  }
  function showStopConfirmation(session) {
    if (!sessionId(session)) return;
    if ($('self-dialog').open) closeSelf();
    state.pendingLaunch = {action: 'stop', session, epoch: state.profileEpoch};
    $('replace-title').textContent = '结束云端游戏？';
    $('replace-description').textContent = `确定结束《${sessionTitle(session)}》？`;
    $('keep-current-game').textContent = `继续玩《${sessionTitle(session)}》`;
    $('replace-current-game').textContent = `结束《${sessionTitle(session)}》`;
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
      if (sessionId(current) !== sessionId(pending.session)) { notify('原云端会话已变化，请在游戏库查看当前会话。'); return; }
      resumeCloudSession(state.session);
    } catch (error) { if (pending.epoch === state.profileEpoch) notify(error.message, true); }
  }
  function resumeCloudSession(session) {
    if (state.playMode === 'stream' && $('play-dialog').open) { $('game-frame')?.focus(); return; }
    state.activeNetworkRoomId = sessionNetworkRoomId(session); renderFamilyPresence();
    if (session) openPlayer(localURL(session.play_url, [`${state.profile.base_path}/stream/`]) || `${state.profile.base_path}/stream/`, sessionTitle(session), 'stream', sessionGameId(session) || null);
  }
  async function launchGame(game, {replaceSessionId = null, networkRoomId = null} = {}) {
    const epoch = state.profileEpoch;
    if (!game || game.mode === 'unavailable' || state.playMode || state.launching) return;
    state.launching = true;
    const button = state.selected?.id === game.id ? $('launch-game') : null;
    if (button) { button.disabled = true; button.textContent = '正在准备游戏…'; }
    const note = state.selected?.id === game.id ? $('game-detail').querySelector('.detail-note') : null;
    if (note && game.mode === 'stream') note.textContent = '首次解压可能需要数分钟，请稍候。';
    try {
      const result = await api(`/api/games/${encodeURIComponent(game.id)}/launch`, {method: 'POST', body: JSON.stringify({mode: game.mode, ...(replaceSessionId ? {replace_session_id: replaceSessionId} : {}), ...(networkRoomId ? {network_room_id: networkRoomId} : {})}), timeoutMs: 30 * 60 * 1000});
      const url = localURL(result.play_url, [`${state.profile.base_path}/play/`, `${state.profile.base_path}/stream/`]);
      if (!url) throw new Error('没有收到有效的游戏入口，请重新加载游戏库。');
      state.activeNetworkRoomId = networkRoomId || sessionNetworkRoomId(result.session); renderFamilyPresence();
      if ($('network-dialog').open) closeNetwork();
      openPlayer(url, game.title, result.mode || game.mode, game.id);
      refreshSession();
    } catch (error) { if (epoch === state.profileEpoch) { if (error.status === 409 && error.detail?.code === 'session_conflict') showLaunchConflict(game, error.detail, {networkRoomId}); else { notify(error.message, true); if (networkRoomId && $('network-dialog').open) networkMessage(error.message, true); refreshSession(); } } }
    finally { if (epoch !== state.profileEpoch) return; state.launching = false; if (state.selected?.id === game.id) renderDetail(state.selected); }
  }

  function openPlayer(url, title, mode, gameId = null) {
    if ($('play-dialog').open) return;
    state.focusReturn = document.activeElement;
    state.playMode = mode; state.playGameId = gameId;
    updateURL();
    $('detail-video')?.pause();
    $('play-title').textContent = title || '云端游戏';
    renderCurrentProfile();
    $('play-note').textContent = mode === 'stream' ? '返回游戏库会保留云端会话 · 退出前请保存进度' : '退出前请在模拟器菜单保存进度';
    $('stop-play').hidden = mode !== 'stream';
    $('play-loading').hidden = false;
    const iframe = document.createElement('iframe');
    iframe.id = 'game-frame'; iframe.title = `${title || 'RetroOne'} · 游戏画面`;
    iframe.allow = 'autoplay; fullscreen; gamepad; clipboard-write; keyboard-map';
    iframe.addEventListener('load', () => { $('play-loading').hidden = true; iframe.focus(); }, {once: true});
    iframe.src = url;
    $('play-frame-wrap').append(iframe);
    $('play-dialog').showModal(); document.body.classList.add('body-modal');
    heartbeatPresence();
  }

  function closePlayer({refresh = true, presence = true} = {}) {
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
      if (state.session) showStopConfirmation(state.session); else notify('当前没有正在运行的云端游戏。');
    } catch (error) { if (epoch === state.profileEpoch) notify(error.message, true); }
  }
  async function confirmStopSession(expected) {
    const epoch = state.profileEpoch;
    $('stop-session').disabled = true; $('stop-play').disabled = true;
    try {
      await api('/api/session/stop', {method: 'POST', body: JSON.stringify({expected_session_id: sessionId(expected)})});
      if (state.playMode === 'stream') closePlayer();
      state.session = null; renderSession(); notify('云端游戏已结束');
    } catch (error) { if (epoch === state.profileEpoch) { if (error.status === 409 && error.detail?.code === 'session_conflict') { state.session = {...error.detail.session, ...(error.detail.game ? {game: error.detail.game} : {})}; renderSession(); showStopConfirmation(state.session); } else notify(error.message, true); } }
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
      $('status-button').dataset.state = 'online';
      $('connection-label').textContent = scanning ? '正在整理目录' : '局域网已连接';
      const browser = health.browser?.available;
      const count = health.library?.count;
      $('health-detail').textContent = `服务已连接${count != null ? ` · 已索引 ${number(count)} 款游戏` : ''}${browser === false ? ' · 浏览器模拟器资源尚未就绪' : ''}${scanning ? ` · 正在扫描游戏目录${scan?.scanned != null ? `，已检查 ${number(scan.scanned)} 项` : ''}` : ''}。各游戏能否启动，以详情中的运行状态为准。`;
      if (scanning) {
        // The card grid and platform controls remain stable during a background scan.
        if (count != null) $('stat-games').textContent = number(count);
        $('scan-button').disabled = true;
        $('scan-button').innerHTML = `${icon('refresh')}<span>正在更新…</span>`;
      }
      if (scanFinished) {
        state.scanPending = false; $('scan-button').disabled = false;
        $('scan-button').innerHTML = `${icon('refresh')}<span>更新游戏库</span>`;
        await refreshLibraryAfterScan();
        if (epoch === state.profileEpoch) notify('游戏库已更新');
      }
    } catch (error) {
      if (epoch !== state.profileEpoch || error.name === 'AbortError') return;
      $('status-button').dataset.state = 'error'; $('connection-label').textContent = '连接暂时中断';
      $('health-detail').textContent = error.message;
    } finally {
      if (epoch === state.profileEpoch) state.healthRefreshing = false;
    }
  }

  async function scanLibrary() {
    const epoch = state.profileEpoch;
    if (state.scanPending) return;
    state.scanPending = true; $('scan-button').disabled = true;
    $('scan-button').innerHTML = `${icon('refresh')}<span>正在更新…</span>`;
    try {
      await api('/api/scan', {method: 'POST'});
      notify('已开始更新游戏库，完成后会自动刷新');
      setTimeout(refreshHealth, 1800);
    } catch (error) {
      if (epoch !== state.profileEpoch) return;
      state.scanPending = false; $('scan-button').disabled = false;
      $('scan-button').innerHTML = `${icon('refresh')}<span>更新游戏库</span>`;
      notify(error.message, true);
    }
  }

  async function refreshMigration() {
    try {
      const result = await api('/api/migration');
      if (!result || result.available === false) { $('migration-banner').hidden = true; return; }
      const data = result.migration || result;
      const status = data.phase || data.status || data.state;
      const suppliedMessage = data.message || data.summary;
      const errors = Array.isArray(data.errors) ? data.errors.length : Number(data.errors) || 0;
      const reviews = Array.isArray(data.symlinks_needing_review) ? data.symlinks_needing_review.length : Number(data.symlinks_needing_review) || 0;
      const progress = data.total_collections != null ? `${number(data.completed_collections)} / ${number(data.total_collections)} 个目录` : '';
      const current = typeof data.current === 'string' && data.current ? ` · 当前：${data.current}` : '';
      let message = '';
      if (status === 'copying') message = `正在整理游戏${progress ? `：${progress}` : ''}${current}`;
      else if (status === 'copied') message = `游戏目录已复制完成${progress ? `（${progress}）` : ''}${data.verification === 'pending' ? '，等待文件核对' : ''}`;
      else if (status === 'verifying') message = `正在核对文件${progress ? `：${progress}` : ''}${data.verified_files != null ? ` · 已检查 ${number(data.verified_files)} 个文件` : ''}${current}`;
      else if (status === 'verification_complete') message = `目录核对已完成${data.files != null ? ` · ${number(data.files)} 个文件` : ''}${reviews ? ` · ${number(reviews)} 项链接待检查` : ''}`;
      else if (status === 'copy_errors') message = `游戏整理尚有异常${progress ? ` · 已完成 ${progress}` : ''}`;
      else if (['running', 'scanning', 'in_progress', 'error', 'failed'].includes(status) && typeof suppliedMessage === 'string') message = suppliedMessage;
      if (message && errors) message += ` · ${number(errors)} 项异常待处理`;
      if (message) {
        $('migration-text').textContent = message;
        $('migration-banner').hidden = false;
      } else $('migration-banner').hidden = true;
    } catch { /* Migration status is optional. */ }
  }

  function showHelp() { $('help-dialog').showModal(); document.body.classList.add('body-modal'); }
  function closeHelp() { $('help-dialog').close(); syncModalBody(); }

  function focusNeighbor(direction) {
    const active = document.activeElement;
    if (active === $('profile-button') || active?.closest('.family-strip')) {
      const buttons = [...$('family-strip').querySelectorAll('button'), $('profile-button')].filter((button) => !button.disabled && !button.hidden && button.getClientRects().length);
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
      if (direction === 'down') { $('game-grid').querySelector('.game-select')?.focus(); return; }
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
    if (next < 0 && direction === 'up') { $('platform-nav').querySelector('.is-active')?.focus(); return; }
    if (next >= buttons.length && direction === 'down' && !$('load-more').hidden) { $('load-more').focus(); return; }
    buttons[Math.max(0, Math.min(buttons.length - 1, next))]?.focus();
  }

  function cyclePlatform(delta) {
    const ids = ['', ...state.platforms.map((platform) => platform.id)];
    const index = ids.indexOf(state.platform);
    const id = ids[(index + delta + ids.length) % ids.length];
    setFilter({platform: id});
    $('platform-nav').querySelector('.is-active')?.scrollIntoView({block: 'nearest', inline: 'center', behavior: 'smooth'});
  }

  function pollGamepads(time) {
    gamepadFrame = requestAnimationFrame(pollGamepads);
    if (document.hidden || (state.playMode && !$('profile-dialog').open && !$('replace-dialog').open && !$('network-dialog').open && !$('display-dialog').open) || time - gamepadLast < 95) return;
    gamepadLast = time;
    let pads;
    try { pads = [...(navigator.getGamepads?.() || [])].filter(Boolean); } catch { return; }
    if (!pads.length) {
      gamepadPressed = {};
      if (gamepadName) { gamepadName = ''; $('gamepad-status').innerHTML = `${icon('controller')}键盘 / 手柄`; $('gamepad-status').removeAttribute('title'); }
      return;
    }
    const pad = pads[0];
    if (gamepadName !== pad.id) {
      gamepadName = pad.id; $('gamepad-status').innerHTML = `${icon('controller')}手柄已连接`; $('gamepad-status').title = pad.id;
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
          else if (key === 'accept') { const active = document.activeElement; if (active?.matches('button') && !active.disabled) active.click(); }
        }
        else if ($('help-dialog').open) {
          if (key === 'back') closeHelp();
          else if (key === 'accept') $('close-help').click();
          else if (key === 'up' || key === 'down') $('help-dialog').scrollBy({top: key === 'up' ? -130 : 130, behavior: 'smooth'});
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
        else if ($('display-dialog').open) {
          if (key === 'back') closeDisplaySettings();
          else if (directional) focusModalAction($('display-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('self-dialog').open) {
          if (key === 'back') closeSelf();
          else if (directional) focusModalAction($('self-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if ($('network-dialog').open) {
          if (key === 'back') closeNetwork();
          else if (directional) focusModalAction($('network-dialog'), key);
          else if (key === 'accept') document.activeElement?.click();
        }
        else if (key === 'profiles') showProfiles();
        else if (directional) focusNeighbor(key);
        else if (key === 'accept') {
          const active = document.activeElement;
          if (active?.matches('.game-select') && state.selected?.id === active.dataset.select) { $('launch-game')?.focus(); $('game-detail').scrollIntoView({block: 'nearest', behavior: 'smooth'}); }
          else if (active?.matches('button') && !active.disabled) active.click();
          else $('game-grid').querySelector('.game-select')?.focus();
        } else if (key === 'back') $('game-grid').querySelector('.is-selected .game-select')?.focus();
        else if (key === 'prev') cyclePlatform(-1);
        else if (key === 'next') cyclePlatform(1);
      }
    }
    gamepadPressed = inputs;
  }

  function startGamepads() { if (!gamepadFrame) gamepadFrame = requestAnimationFrame(pollGamepads); }

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
      if (!validRoom(room)) throw new Error('联机步骤暂时无法读取，请稍后再试。');
      state.networkRoom = room; showNetwork({keepRoom: true});
    } catch (error) { if (epoch === state.profileEpoch) notify(error.message, true); }
  });
  $('close-family').addEventListener('click', closeFamily);
  $('back-from-family').addEventListener('click', closeFamily);
  $('family-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeFamily(); });
  $('family-dialog').addEventListener('close', syncModalBody);
  $('self-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeSelf(); });
  $('self-dialog').addEventListener('close', syncModalBody);
  $('close-self').addEventListener('click', closeSelf);
  $('self-display-settings').addEventListener('click', showDisplaySettings);
  $('close-display').addEventListener('click', closeDisplaySettings);
  $('back-from-display').addEventListener('click', closeDisplaySettings);
  $('retry-display').addEventListener('click', loadDisplaySettings);
  $('save-display').addEventListener('click', saveDisplaySettings);
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
          else notify(error.message, true);
        }
      }
      else launchGame(pending.game, {replaceSessionId: sessionId(pending.session)});
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
  $('recent-filter').addEventListener('click', () => setFilter({recent: !state.recent, favorites: false}));
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
    if (button) selectGame(button.dataset.select, {scroll: true, refresh: true, focus: event.detail === 0});
  });
  $('game-grid').addEventListener('focusin', (event) => {
    const button = event.target.closest('[data-select]');
    if (button) selectGame(button.dataset.select);
  });
  $('load-more').addEventListener('click', () => loadGames(true));
  $('scan-button').addEventListener('click', scanLibrary);
  $('empty-action').addEventListener('click', () => {
    const action = $('empty-action').dataset.action;
    if (action === 'reset') setFilter({query: '', platform: '', mode: ''});
    else if (action === 'library') setFilter({favorites: false, recent: false});
    else if (action === 'scan') scanLibrary();
    else { loadPlatforms(); loadGames(); refreshHealth(); }
  });
  ['help-button', 'footer-help', 'status-button'].forEach((id) => $(id).addEventListener('click', showHelp));
  $('close-help').addEventListener('click', closeHelp);
  $('help-dialog').addEventListener('close', () => syncModalBody());
  $('help-dialog').addEventListener('click', (event) => { if (event.target === $('help-dialog')) { const rect = $('help-dialog').getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeHelp(); } });
  $('close-play').addEventListener('click', closePlayer);
  $('play-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closePlayer(); });
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
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await $('play-dialog').requestFullscreen(); }
    catch { notify('当前浏览器不支持此全屏方式，可使用游戏内的全屏按钮。'); }
  });
  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing || (state.playMode && !$('profile-dialog').open && !$('replace-dialog').open && !$('network-dialog').open && !$('display-dialog').open) || $('help-dialog').open) return;
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
      else { const direction = {ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', KeyA: 'left', KeyD: 'right', KeyW: 'up', KeyS: 'down'}[event.code]; if (direction) { event.preventDefault(); focusModalAction($('self-dialog'), direction); } }
      return;
    }
    if ($('replace-dialog').open) {
      if (event.key === 'Escape') { event.preventDefault(); closeLaunchConflict(); }
      else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'KeyA', 'KeyD', 'KeyW', 'KeyS'].includes(event.code)) { event.preventDefault(); (document.activeElement === $('keep-current-game') ? $('replace-current-game') : $('keep-current-game')).focus(); }
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
  window.addEventListener('offline', () => { $('status-button').dataset.state = 'error'; $('connection-label').textContent = '网络已断开'; state.presenceStale = true; renderFamilyPresence(); });
  window.addEventListener('popstate', () => {
    const params = new URLSearchParams(location.search);
    setFilter({platform: params.get('platform') || '', query: params.get('q') || '', mode: params.get('mode') || '', favorites: params.get('favorite') === 'true', recent: params.get('recent') === 'true'});
  });
  function updateClock() { const now = new Date(); $('clock').textContent = now.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false}); $('clock').dateTime = now.toISOString(); }
  updateClock(); setInterval(updateClock, 30000);
  $('gamepad-help').textContent = window.isSecureContext ? '若手柄没有响应，先点击游戏画面，再按手柄任意键。' : '当前使用 HTTP。部分浏览器只允许在 HTTPS 下读取手柄；未识别时请使用键盘，或通过 HTTPS 访问。';
  renderFilters();
  renderCurrentProfile(); showProfiles();
  setInterval(() => { if (!document.hidden && state.profile) { refreshHealth(); refreshSession(); } }, 12000);
  setInterval(() => { if (!document.hidden && state.profile) refreshMigration(); }, 12000);
  setInterval(() => { if (!document.hidden && $('profile-dialog').open) loadProfiles({silent: true}); }, 2000);
  setInterval(() => { if (!document.hidden) { heartbeatPresence(); loadProfiles({silent: true}); } }, 15000);
  setInterval(refreshNetwork, 5000);
  window.addEventListener('pagehide', () => { saveProfileView(); leavePresence(state.profile, true); });
  startGamepads();
})();
