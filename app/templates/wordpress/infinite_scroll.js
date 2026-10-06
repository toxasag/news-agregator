/**
 * SvodkaPress infinite scroll — vanilla JS, zero dependencies.
 *
 * Drop this into a WordPress theme and enqueue it on single-post views. It loads
 * the next article when the reader reaches the end of the current one, updates the
 * address bar, and reports a virtual pageview to Yandex Metrika and GA4.
 *
 * Why the pieces are the way they are:
 *
 * - **IntersectionObserver, not a scroll handler.** A `scroll` listener fires
 *   dozens of times per second and forces layout on every call to read
 *   `getBoundingClientRect`. The observer is evaluated off the main thread and
 *   fires once when the sentinel crosses the margin, which is the difference
 *   between a smooth scroll and a janky one on a mid-range phone.
 *
 * - **`rootMargin` starts the fetch early.** The next article begins loading
 *   several hundred pixels before the sentinel is visible, so it is usually in
 *   place by the time the reader arrives.
 *
 * - **Analytics fire on the URL change, not on the fetch.** A virtual pageview sent
 *   when loading starts would count articles the reader never reached. The hit is
 *   sent when an article actually becomes the current one, which is what makes the
 *   session depth number in Metrika mean something.
 *
 * - **Everything is torn down.** `destroy()` disconnects the observer, aborts any
 *   in-flight fetch and removes the listeners. A single-page theme that swaps views
 *   without a reload would otherwise leak an observer per navigation, and the leaked
 *   ones keep firing against detached nodes.
 *
 * Markup contract, all overridable through `window.SVODKA_INFINITE_SCROLL_CONFIG`:
 *
 *   <div id="svodka-feed">
 *     <article class="svodka-post" data-post-url="…" data-post-title="…"></article>
 *   </div>
 *   <div id="svodka-sentinel" data-next-url="/next-article/"></div>
 */
(function (window, document) {
  'use strict';

  /** @const Defaults. Every value is overridable so a theme need not fork this file. */
  var DEFAULTS = {
    feedSelector: '#svodka-feed',
    sentinelSelector: '#svodka-sentinel',
    postSelector: '.svodka-post',
    // The attribute on the sentinel holding the next article's URL. When it is
    // absent or empty the feed has ended.
    nextUrlAttribute: 'data-next-url',
    // Selector for the article element inside a fetched document.
    remotePostSelector: 'article.svodka-post',
    // Selector for the next sentinel inside a fetched document.
    remoteSentinelSelector: '#svodka-sentinel',
    // Start fetching this far before the sentinel enters the viewport.
    rootMargin: '600px 0px',
    // Hard cap so a runaway feed cannot append forever and exhaust memory on a
    // long session.
    maxArticles: 20,
    // Consecutive failures tolerated before the observer gives up. Retrying
    // indefinitely against a 500 would hammer the origin from every open tab.
    maxErrors: 3,
    metrikaCounterId: null,
    debug: false
  };

  var config = Object.assign({}, DEFAULTS, window.SVODKA_INFINITE_SCROLL_CONFIG || {});

  function log() {
    if (!config.debug || typeof window.console === 'undefined') {
      return;
    }
    window.console.log.apply(window.console, ['[svodka-scroll]'].concat([].slice.call(arguments)));
  }

  /**
   * Report a virtual pageview to whichever analytics stacks are present.
   *
   * Both calls are guarded and wrapped: an ad blocker leaves `window.ym`
   * undefined, and a broken analytics snippet must not stop the reader from
   * scrolling. Analytics is the least important thing on the page.
   */
  function trackVirtualPageview(url, title) {
    try {
      if (typeof window.ym === 'function' && config.metrikaCounterId) {
        // Metrika needs the counter id and treats `hit` as a full pageview, which
        // is what an infinite-scroll article should count as.
        window.ym(config.metrikaCounterId, 'hit', url, { title: title });
        log('metrika hit', url);
      }
    } catch (error) {
      log('metrika failed', error);
    }

    try {
      if (typeof window.gtag === 'function') {
        // GA4 expects `page_view` with an explicit location, because it cannot
        // observe a `pushState` on its own.
        window.gtag('event', 'page_view', {
          page_location: url,
          page_title: title
        });
        log('gtag page_view', url);
      }
    } catch (error) {
      log('gtag failed', error);
    }
  }

  function InfiniteScroll(options) {
    this.config = Object.assign({}, config, options || {});
    this.feed = document.querySelector(this.config.feedSelector);
    this.sentinel = document.querySelector(this.config.sentinelSelector);
    this.observer = null;
    this.controller = null;
    this.loading = false;
    this.loadedCount = 0;
    this.errorCount = 0;
    this.destroyed = false;
    this.seenUrls = Object.create(null);
    this.onScroll = null;

    // Bound once so `removeEventListener` can actually remove them: a fresh bound
    // function is a different object and would not be removed.
    this.handleIntersect = this.handleIntersect.bind(this);
    this.handleVisibleArticle = this.handleVisibleArticle.bind(this);
  }

  InfiniteScroll.prototype.start = function () {
    if (this.destroyed) {
      return false;
    }
    if (!this.feed || !this.sentinel) {
      log('missing feed or sentinel; not starting');
      return false;
    }
    if (typeof window.IntersectionObserver !== 'function') {
      // No polyfill and no scroll fallback: on a browser this old the reader still
      // gets the article and its normal "next post" link, which is a working page.
      log('IntersectionObserver unsupported; infinite scroll disabled');
      return false;
    }

    // The first article is already on screen and was counted by the page's own
    // analytics snippet, so it is recorded as seen without a virtual hit.
    var current = window.location.pathname + window.location.search;
    this.seenUrls[current] = true;

    this.observer = new window.IntersectionObserver(this.handleIntersect, {
      root: null,
      rootMargin: this.config.rootMargin,
      threshold: 0
    });
    this.observer.observe(this.sentinel);

    // A second observer reports which article is currently on screen, which is what
    // drives the URL and the analytics hit. Keeping it separate from the loader
    // means a fetch failure does not stop the address bar from tracking the reader.
    this.titleObserver = new window.IntersectionObserver(this.handleVisibleArticle, {
      root: null,
      // Fire when an article's midpoint reaches the middle of the viewport: that is
      // when a reader would say they are "on" it.
      rootMargin: '-45% 0px -45% 0px',
      threshold: 0
    });
    var initial = this.feed.querySelectorAll(this.config.postSelector);
    for (var index = 0; index < initial.length; index += 1) {
      this.titleObserver.observe(initial[index]);
    }

    log('started');
    return true;
  };

  InfiniteScroll.prototype.handleIntersect = function (entries) {
    for (var index = 0; index < entries.length; index += 1) {
      if (entries[index].isIntersecting) {
        this.loadNext();
        return;
      }
    }
  };

  /**
   * Update the address bar and report the article the reader is looking at.
   *
   * `pushState` rather than `replaceState`: the reader's back button should walk
   * back through the articles they scrolled past, which is what they expect after
   * scrolling through five of them.
   */
  InfiniteScroll.prototype.handleVisibleArticle = function (entries) {
    for (var index = 0; index < entries.length; index += 1) {
      var entry = entries[index];
      if (!entry.isIntersecting) {
        continue;
      }
      var element = entry.target;
      var url = element.getAttribute('data-post-url');
      var title = element.getAttribute('data-post-title') || document.title;
      if (!url) {
        continue;
      }
      var currentPath = window.location.pathname + window.location.search;
      if (url === currentPath) {
        continue;
      }
      try {
        window.history.pushState({ svodkaUrl: url }, title, url);
      } catch (error) {
        // A cross-origin or malformed URL throws; the reader keeps scrolling.
        log('pushState failed', error);
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
    if (this.destroyed || this.loading) {
      return;
    }
    if (this.loadedCount >= this.config.maxArticles) {
      log('article cap reached; stopping');
      this.stopLoading();
      return;
    }
    var nextUrl = this.sentinel.getAttribute(this.config.nextUrlAttribute);
    if (!nextUrl) {
      log('no next url; feed ended');
      this.stopLoading();
      return;
    }

    this.loading = true;
    // AbortController lets `destroy()` cancel an in-flight request. Without it a
    // navigation mid-fetch would resolve against a detached DOM.
    this.controller = typeof window.AbortController === 'function'
      ? new window.AbortController()
      : null;

    window.fetch(nextUrl, {
      credentials: 'same-origin',
      signal: this.controller ? this.controller.signal : undefined,
      headers: { 'X-Requested-With': 'svodka-infinite-scroll' }
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('HTTP ' + response.status);
        }
        return response.text();
      })
      .then(function (html) {
        self.append(html, nextUrl);
      })
      .catch(function (error) {
        if (error && error.name === 'AbortError') {
          return;
        }
        self.errorCount += 1;
        log('load failed', error);
        if (self.errorCount >= self.config.maxErrors) {
          // Retrying forever against a broken origin would hammer it from every
          // open tab.
          log('error cap reached; stopping');
          self.stopLoading();
        }
      })
      .then(function () {
        self.loading = false;
        self.controller = null;
      });
  };

  /**
   * Append a fetched article and advance the sentinel.
   *
   * The response is parsed with `DOMParser` rather than assigned to `innerHTML`:
   * parsing an inert document does not execute scripts or fire loads, and only the
   * one element we asked for is adopted into the live page.
   */
  InfiniteScroll.prototype.append = function (html, sourceUrl) {
    if (this.destroyed) {
      return;
    }
    var parsed;
    try {
      parsed = new window.DOMParser().parseFromString(html, 'text/html');
    } catch (error) {
      log('parse failed', error);
      return;
    }

    var incoming = parsed.querySelector(this.config.remotePostSelector);
    if (!incoming) {
      log('no article in the response; stopping');
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

    // Advance the sentinel to the next article's URL, or clear it to end the feed.
    var remoteSentinel = parsed.querySelector(this.config.remoteSentinelSelector);
    var followingUrl = remoteSentinel
      ? remoteSentinel.getAttribute(this.config.nextUrlAttribute)
      : null;
    if (followingUrl) {
      this.sentinel.setAttribute(this.config.nextUrlAttribute, followingUrl);
      // Move the sentinel below the appended article so it can trigger again.
      this.feed.parentNode.appendChild(this.sentinel);
    } else {
      this.sentinel.removeAttribute(this.config.nextUrlAttribute);
      this.stopLoading();
    }
    log('appended', sourceUrl, 'total', this.loadedCount);
  };

  /** Stop loading more articles but leave the URL tracking running. */
  InfiniteScroll.prototype.stopLoading = function () {
    if (this.observer) {
      this.observer.disconnect();
      this.observer = null;
    }
  };

  /**
   * Tear everything down.
   *
   * Called on unmount. Without this a theme that swaps views without a reload
   * leaks one observer pair per navigation, and the leaked observers keep firing
   * callbacks against detached nodes — which shows up as a page that gets slower
   * the longer a reader browses.
   */
  InfiniteScroll.prototype.destroy = function () {
    this.destroyed = true;
    this.stopLoading();
    if (this.titleObserver) {
      this.titleObserver.disconnect();
      this.titleObserver = null;
    }
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
    if (this.onScroll) {
      window.removeEventListener('scroll', this.onScroll);
      this.onScroll = null;
    }
    this.seenUrls = Object.create(null);
    log('destroyed');
  };

  var instance = null;

  function init() {
    if (instance) {
      return instance;
    }
    instance = new InfiniteScroll();
    instance.start();
    return instance;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }

  // A bfcache restore or a real navigation must not leave observers attached.
  window.addEventListener('pagehide', function () {
    if (instance) {
      instance.destroy();
      instance = null;
    }
  });

  // Exported so a theme can control the lifecycle explicitly and so the behaviour
  // is testable without a browser.
  window.SvodkaInfiniteScroll = {
    InfiniteScroll: InfiniteScroll,
    init: init,
    trackVirtualPageview: trackVirtualPageview,
    getInstance: function () {
      return instance;
    },
    destroy: function () {
      if (instance) {
        instance.destroy();
        instance = null;
      }
    }
  };
})(window, document);
