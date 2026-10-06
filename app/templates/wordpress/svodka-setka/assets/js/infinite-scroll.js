/**
 * Svodka Setka Infinite Scroll — Dual-Mode (Single Story Stream + Feed Stream)
 *
 * Zero dependencies, IntersectionObserver-based, Yandex Metrika (106439629) and
 * GA4 virtual pageview tracking, automatic URL history synchronization.
 */
(function (window, document) {
  'use strict';

  var DEFAULTS = {
    feedSelector: '#svodka-feed',
    sentinelSelector: '#svodka-sentinel',
    postSelector: '.svodka-post',
    archiveItemSelector: 'article.svodka-feed-item, aside.setka-tg-card, aside.svodka-promo-card',
    nextUrlAttribute: 'data-next-url',
    remotePostSelector: 'article.svodka-post',
    remoteSentinelSelector: '#svodka-sentinel',
    rootMargin: '700px 0px',
    maxArticles: 25,
    maxErrors: 3,
    metrikaCounterId: 106439629,
    debug: false
  };

  var config = Object.assign({}, DEFAULTS, window.SVODKA_INFINITE_SCROLL_CONFIG || {});

  function trackVirtualPageview(url, title) {
    try {
      if (typeof window.ym === 'function' && config.metrikaCounterId) {
        window.ym(config.metrikaCounterId, 'hit', url, { title: title });
      }
    } catch (e) {}

    try {
      if (typeof window.gtag === 'function') {
        window.gtag('event', 'page_view', {
          page_location: url,
          page_title: title
        });
      }
    } catch (e) {}
  }

  function InfiniteScroll() {
    this.config = config;
    this.feed = document.querySelector(this.config.feedSelector);
    this.sentinel = document.querySelector(this.config.sentinelSelector);
    this.mode = this.feed ? (this.feed.getAttribute('data-stream-mode') || 'single') : 'single';
    this.observer = null;
    this.titleObserver = null;
    this.controller = null;
    this.loading = false;
    this.loadedCount = 0;
    this.errorCount = 0;
    this.seenUrls = Object.create(null);

    this.handleIntersect = this.handleIntersect.bind(this);
    this.handleVisibleArticle = this.handleVisibleArticle.bind(this);
  }

  InfiniteScroll.prototype.start = function () {
    if (!this.feed || !this.sentinel || typeof window.IntersectionObserver !== 'function') {
      return false;
    }

    var current = window.location.pathname + window.location.search;
    this.seenUrls[current] = true;

    // Hide fallback pagination links on archive stream when JS Infinite Scroll is active
    var pagination = document.querySelector('.setka-pagination');
    if (pagination) {
      pagination.style.display = 'none';
    }

    this.observer = new window.IntersectionObserver(this.handleIntersect, {
      root: null,
      rootMargin: this.config.rootMargin,
      threshold: 0
    });
    this.observer.observe(this.sentinel);

    if (this.mode === 'single') {
      this.titleObserver = new window.IntersectionObserver(this.handleVisibleArticle, {
        root: null,
        rootMargin: '-45% 0px -45% 0px',
        threshold: 0
      });
      var initial = this.feed.querySelectorAll(this.config.postSelector);
      for (var i = 0; i < initial.length; i += 1) {
        this.titleObserver.observe(initial[i]);
      }
    }

    return true;
  };

  InfiniteScroll.prototype.handleIntersect = function (entries) {
    for (var i = 0; i < entries.length; i += 1) {
      if (entries[i].isIntersecting) {
        this.loadNext();
        return;
      }
    }
  };

  InfiniteScroll.prototype.handleVisibleArticle = function (entries) {
    for (var i = 0; i < entries.length; i += 1) {
      var entry = entries[i];
      if (!entry.isIntersecting) {
        continue;
      }
      var el = entry.target;
      var url = el.getAttribute('data-post-url');
      var title = el.getAttribute('data-post-title') || document.title;
      if (!url) {
        continue;
      }
      var currentPath = window.location.pathname + window.location.search;
      if (url === currentPath) {
        continue;
      }
      try {
        window.history.pushState({ svodkaUrl: url }, title, url);
      } catch (e) {
        continue;
      }
      document.title = title;
      if (!this.seenUrls[url]) {
        this.seenUrls[url] = true;
        trackVirtualPageview(url, title);
      }
    }
  };

  InfiniteScroll.prototype.loadNext = function () {
    var self = this;
    if (this.loading || this.loadedCount >= this.config.maxArticles) {
      return;
    }
    var nextUrl = this.sentinel.getAttribute(this.config.nextUrlAttribute);
    if (!nextUrl || this.seenUrls[nextUrl]) {
      this.stopLoading();
      return;
    }

    this.loading = true;
    this.controller = typeof window.AbortController === 'function' ? new window.AbortController() : null;

    window.fetch(nextUrl, {
      credentials: 'same-origin',
      signal: this.controller ? this.controller.signal : undefined,
      headers: { 'X-Requested-With': 'svodka-infinite-scroll' }
    })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error('HTTP ' + resp.status);
        }
        return resp.text();
      })
      .then(function (html) {
        self.append(html, nextUrl);
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') {
          return;
        }
        self.errorCount += 1;
        if (self.errorCount >= self.config.maxErrors) {
          self.stopLoading();
        }
      })
      .then(function () {
        self.loading = false;
        self.controller = null;
      });
  };

  InfiniteScroll.prototype.append = function (html, sourceUrl) {
    var parsed;
    try {
      parsed = new window.DOMParser().parseFromString(html, 'text/html');
    } catch (e) {
      return;
    }

    if (this.mode === 'archive') {
      var items = parsed.querySelectorAll('#svodka-feed > *');
      if (!items.length) {
        this.stopLoading();
        return;
      }
      for (var i = 0; i < items.length; i += 1) {
        this.feed.appendChild(document.importNode(items[i], true));
      }
      this.loadedCount += 1;
      this.seenUrls[sourceUrl] = true;
      trackVirtualPageview(sourceUrl, document.title);
    } else {
      var incoming = parsed.querySelector(this.config.remotePostSelector);
      if (!incoming) {
        this.stopLoading();
        return;
      }
      if (!incoming.getAttribute('data-post-url')) {
        incoming.setAttribute('data-post-url', sourceUrl);
      }
      var adopted = document.importNode(incoming, true);
      this.feed.appendChild(adopted);
      this.loadedCount += 1;
      if (this.titleObserver) {
        this.titleObserver.observe(adopted);
      }
    }

    var remoteSentinel = parsed.querySelector(this.config.remoteSentinelSelector);
    var followingUrl = remoteSentinel ? remoteSentinel.getAttribute(this.config.nextUrlAttribute) : null;
    if (followingUrl && !this.seenUrls[followingUrl]) {
      this.sentinel.setAttribute(this.config.nextUrlAttribute, followingUrl);
      if (remoteSentinel.textContent) {
        this.sentinel.textContent = remoteSentinel.textContent;
      }
    } else {
      this.stopLoading();
    }
  };

  InfiniteScroll.prototype.stopLoading = function () {
    if (this.observer) {
      this.observer.disconnect();
      this.observer = null;
    }
    if (this.sentinel) {
      this.sentinel.style.display = 'none';
    }
  };

  function init() {
    var instance = new InfiniteScroll();
    instance.start();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})(window, document);
