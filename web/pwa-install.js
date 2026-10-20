/* Optional installation affordance; no player state, storage or navigation. */
((global) => {
  'use strict';
  const messages = {
    'zh-CN': {install: '安装应用', installed: '已安装', installing: '正在打开安装提示…', unavailable: '可在浏览器菜单中选择“安装应用”或“添加到主屏幕”。是否支持取决于当前浏览器。', insecure: '安装应用需要 HTTPS；当前网页仍可正常使用。', ready: '安装后可从主屏幕打开。游玩仍需要连接游戏服务器。', error: '暂时无法准备安装，请检查连接后重试。', update: '有新版本可用。关闭所有 RetroOne 页面后重新打开即可更新，当前游戏不会自动刷新。', cancelled: '已取消安装。仍可继续使用网页。'},
    en: {install: 'Install app', installed: 'Installed', installing: 'Opening installation prompt…', unavailable: 'Use “Install app” or “Add to Home Screen” in your browser menu. Availability depends on your browser.', insecure: 'Installing requires HTTPS. You can still use this website.', ready: 'Open RetroOne from your home screen after installing. Playing still requires a connection to your game server.', error: 'Installation could not be prepared. Check your connection and try again.', update: 'An update is ready. Close all RetroOne pages, then reopen the app to update. Your current game will not reload automatically.', cancelled: 'Installation cancelled. You can keep using the website.'}
  };
  const standalone = () => global.matchMedia?.('(display-mode: standalone)').matches || global.matchMedia?.('(display-mode: fullscreen)').matches || global.navigator.standalone === true;
  let promptEvent = null;
  let status = '';
  let busy = false;
  let installed = standalone();
  const isInstalled = () => installed || standalone();
  const locale = () => global.RetroI18n?.locale?.() === 'en' || (!global.RetroI18n && /^en(?:-|$)/i.test(global.document.documentElement.lang)) ? 'en' : 'zh-CN';
  const text = (key) => messages[locale()][key];
  function refresh() {
    for (const button of global.document.querySelectorAll('[data-retroone-install]')) {
      const label = text(isInstalled() ? 'installed' : 'install');
      if (button.textContent !== label) button.textContent = label;
      button.disabled = busy || isInstalled();
      button.setAttribute('aria-label', label);
    }
    for (const output of global.document.querySelectorAll('[data-retroone-install-status]')) {
      const message = status ? text(status) : '';
      if (output.textContent !== message) output.textContent = message;
    }
  }
  function show(key, button) {
    status = key;
    if (!global.document.querySelector('[data-retroone-install-status]') && button?.parentNode) {
      const output = global.document.createElement('p');
      output.setAttribute('data-retroone-install-status', '');
      output.setAttribute('role', 'status');
      button.insertAdjacentElement('afterend', output);
    }
    refresh();
  }
  global.document.addEventListener('click', async (event) => {
    const button = event.target.closest?.('[data-retroone-install]');
    if (!button || busy || isInstalled()) return;
    event.preventDefault();
    if (!global.isSecureContext) { show('insecure', button); return; }
    if (!promptEvent) { show('unavailable', button); return; }
    const pending = promptEvent;
    promptEvent = null;
    busy = true;
    show('installing', button);
    try {
      await pending.prompt();
      const result = await pending.userChoice;
      show(result?.outcome === 'accepted' ? 'ready' : 'cancelled', button);
    } catch (_) { show('error', button); }
    finally { busy = false; refresh(); }
  });
  global.addEventListener('beforeinstallprompt', (event) => {
    if (!global.isSecureContext || isInstalled()) return;
    event.preventDefault();
    promptEvent = event;
    status = 'ready';
    refresh();
  });
  global.addEventListener('appinstalled', () => { promptEvent = null; installed = true; status = 'ready'; refresh(); });
  const observer = new MutationObserver(refresh);
  observer.observe(global.document.documentElement, {attributes: true, attributeFilter: ['lang']});
  refresh();
  if (!global.isSecureContext || !('serviceWorker' in global.navigator)) return;
  global.navigator.serviceWorker.register('/service-worker.js', {scope: '/', updateViaCache: 'none'}).then((registration) => {
    const waiting = () => { if (registration.waiting && registration.active) { status = 'update'; refresh(); } };
    waiting();
    registration.addEventListener('updatefound', () => {
      registration.installing?.addEventListener('statechange', waiting);
    });
    // No controllerchange reload, activation command or implicit user navigation.
  }).catch(() => { status = 'error'; refresh(); });
})(window);
