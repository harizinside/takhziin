// takhziin UI — minimal fetch helpers, no framework.
(function () {
  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return match ? decodeURIComponent(match[2]) : null;
  }

  // No-op for now; kept for future fetch-based features (run-now polling, etc.).
  window.takhziin = {
    getCookie,
    fetch: async function (url, opts) {
      opts = opts || {};
      opts.headers = Object.assign(
        { "Content-Type": "application/json" },
        opts.headers || {},
      );
      const r = await fetch(url, opts);
      if (!r.ok) {
        const t = await r.text();
        throw new Error("request failed: " + r.status + " " + t);
      }
      return r;
    },
  };
})();