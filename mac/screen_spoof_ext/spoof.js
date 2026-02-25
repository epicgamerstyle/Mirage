// Device Screen Override — injected at document_start in MAIN world
// Overrides screen/window dimension properties to match spoofed device profile.
// Values are replaced at install time by cdp_injector.py.

(function () {
  "use strict";

  // ---- Spoofed values (replaced by cdp_injector.py) ----
  const SCREEN_W   = /*SCREEN_WIDTH*/1080;
  const SCREEN_H   = /*SCREEN_HEIGHT*/2400;
  const DPR        = /*DPR*/2.625;
  const INNER_W    = /*INNER_WIDTH*/412;
  const INNER_H    = /*INNER_HEIGHT*/915;
  const OUTER_W    = /*OUTER_WIDTH*/412;
  const OUTER_H    = /*OUTER_HEIGHT*/915;

  // ---- Helper: define a read-only getter on an object ----
  function spoof(obj, prop, value) {
    try {
      Object.defineProperty(obj, prop, {
        get: function () { return value; },
        configurable: true,
        enumerable: true,
      });
    } catch (_) { /* some props may be non-configurable */ }
  }

  // ---- Screen object ----
  var scr = window.screen || (window.screen = {});
  spoof(scr, "width",       SCREEN_W);
  spoof(scr, "height",      SCREEN_H);
  spoof(scr, "availWidth",  SCREEN_W);
  spoof(scr, "availHeight", SCREEN_H);
  spoof(scr, "colorDepth",  24);
  spoof(scr, "pixelDepth",  24);

  // ---- Window dimensions ----
  spoof(window, "innerWidth",  INNER_W);
  spoof(window, "innerHeight", INNER_H);
  spoof(window, "outerWidth",  OUTER_W);
  spoof(window, "outerHeight", OUTER_H);
  spoof(window, "devicePixelRatio", DPR);

  // ---- MediaQuery override (width/height media features) ----
  var origMatchMedia = window.matchMedia;
  if (origMatchMedia) {
    window.matchMedia = function (query) {
      // Rewrite device-width / device-height queries
      query = query
        .replace(/device-width\s*:\s*\d+px/g,  "device-width: " + SCREEN_W + "px")
        .replace(/device-height\s*:\s*\d+px/g, "device-height: " + SCREEN_H + "px");
      return origMatchMedia.call(window, query);
    };
  }
})();
