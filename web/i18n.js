/* Small, explicit UI message catalog. Game metadata is never translated here. */
((global) => {
  'use strict';
  const supported = Object.freeze(['zh-CN', 'en']);
  const catalogs = Object.create(null);
  const attributes = ['aria-label', 'aria-description', 'title', 'placeholder', 'content'];
  let preference = 'zh-CN';

  function normalize(value) {
    return value === 'auto' || supported.includes(value) ? value : 'zh-CN';
  }
  function browserLocale() {
    const languages = global.navigator?.languages?.length ? global.navigator.languages : [global.navigator?.language || ''];
    for (const language of languages) {
      if (/^zh(?:-|$)/i.test(language)) return 'zh-CN';
      if (/^en(?:-|$)/i.test(language)) return 'en';
    }
    return 'zh-CN';
  }
  function locale() { return preference === 'auto' ? browserLocale() : preference; }
  function add(language, messages) {
    if (!supported.includes(language) || !messages || typeof messages !== 'object' || Array.isArray(messages)) throw new TypeError('Invalid language catalog');
    const next = Object.assign(Object.create(null), catalogs[language]);
    for (const [key, value] of Object.entries(messages)) {
      if (typeof value !== 'string') throw new TypeError('Messages must be strings');
      next[key] = value;
    }
    catalogs[language] = next;
  }
  function t(source, params = {}) {
    const key = String(source ?? '');
    const messages = catalogs[locale()];
    const message = messages && Object.hasOwn(messages, key) ? messages[key] : key;
    return message.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (placeholder, name) => Object.hasOwn(params, name) ? String(params[name] ?? '') : placeholder);
  }
  function apply(root = global.document) {
    if (!root?.querySelectorAll) return;
    const selector = ['[data-i18n]', ...attributes.map((name) => `[data-i18n-${name}]`)].join(',');
    const elements = [...(root.matches?.(selector) ? [root] : []), ...root.querySelectorAll(selector)];
    for (const element of elements) {
      if (element.hasAttribute('data-i18n')) element.textContent = t(element.getAttribute('data-i18n'));
      for (const name of attributes) {
        const key = element.getAttribute(`data-i18n-${name}`);
        if (key !== null) element.setAttribute(name, t(key));
      }
    }
    for (const picker of root.querySelectorAll('[data-language-picker]')) picker.value = preference;
    if (global.document?.documentElement) global.document.documentElement.lang = locale();
  }
  function setLocale(value) {
    preference = normalize(value);
    apply();
    return locale();
  }
  function number(value, options = {}) { return new Intl.NumberFormat(locale(), options).format(Number(value) || 0); }
  function date(value, options = {}) {
    const parsed = value instanceof Date ? value : new Date(value);
    return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat(locale(), options).format(parsed);
  }
  global.RetroI18n = Object.freeze({supported, normalize, getLocale: () => preference, locale, add, t, apply, setLocale, number, date});
})(typeof window !== 'undefined' ? window : globalThis);
