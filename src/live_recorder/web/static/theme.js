(function () {
  "use strict";
  var KEY = "lr-theme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "system";
    } catch (err) {
      return "system";
    }
  }

  function apply(pref) {
    var dark = pref === "dark" || (pref === "system" && (!media || media.matches));
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.setAttribute("data-theme-pref", pref);
  }

  apply(stored());

  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (stored() === "system") apply("system");
    });
  }

  window.LRTheme = {
    get: stored,
    set: function (pref) {
      try {
        if (pref === "system") window.localStorage.removeItem(KEY);
        else window.localStorage.setItem(KEY, pref);
      } catch (err) {
        /* 保存できない環境でも現在の表示だけは切り替える */
      }
      apply(pref);
    },
  };
})();
