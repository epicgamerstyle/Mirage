#!/usr/bin/env python3
"""
generate_userscript.py — Dynamic device fingerprint userscript generator

Reads a jorkspoofer profile (.conf) and generates device_spoof.user.js
with all fingerprint values matched to that device profile.

Usage:
    # From a local .conf file:
    python3 generate_userscript.py profiles/profiles_export/samsung-note20ultra.conf

    # From the active profile on a connected device:
    python3 generate_userscript.py --from-device

    # Specify Chrome version:
    python3 generate_userscript.py --chrome-version 109.0.5414.123 profile.conf

    # Write output to specific file:
    python3 generate_userscript.py -o device_spoof.user.js profile.conf
"""

import re
import os
import sys
import json
import math
import argparse
import subprocess
import shutil

# ═══════════════════════════════════════════════════════════════════════
# GPU WebGL parameter database
# ═══════════════════════════════════════════════════════════════════════

_GPU_PARAMS = {
    # ARM Mali family (Exynos, Tensor, Dimensity SoCs)
    "mali": {
        "MAX_TEXTURE_SIZE": 8192,
        "MAX_CUBE_MAP_TEXTURE_SIZE": 8192,
        "MAX_RENDERBUFFER_SIZE": 8192,
        "MAX_VIEWPORT_DIMS": [8192, 8192],
        "MAX_3D_TEXTURE_SIZE": 2048,
        "MAX_ARRAY_TEXTURE_LAYERS": 2048,
        "ALIASED_POINT_SIZE_RANGE": [1, 1024],
        "ALIASED_LINE_WIDTH_RANGE": [1, 1],
        "MAX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 96,
        "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_FRAGMENT_UNIFORM_VECTORS": 1024,
        "MAX_VERTEX_UNIFORM_VECTORS": 1024,
        "MAX_VERTEX_ATTRIBS": 16,
        "MAX_VERTEX_UNIFORM_COMPONENTS": 16384,
        "MAX_VERTEX_UNIFORM_BLOCKS": 16,
        "MAX_VERTEX_OUTPUT_COMPONENTS": 128,
        "MAX_VARYING_VECTORS": 32,
        "MAX_VARYING_COMPONENTS": 128,
        "MAX_FRAGMENT_UNIFORM_COMPONENTS": 16384,
        "MAX_FRAGMENT_UNIFORM_BLOCKS": 16,
        "MAX_FRAGMENT_INPUT_COMPONENTS": 128,
        "MAX_DRAW_BUFFERS": 8,
        "MAX_COLOR_ATTACHMENTS": 8,
        "MAX_SAMPLES": 16,
        "MAX_UNIFORM_BUFFER_BINDINGS": 72,
        "MAX_UNIFORM_BLOCK_SIZE": 65536,
        "MAX_COMBINED_UNIFORM_BLOCKS": 60,
        "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS": 507904,
        "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS": 507904,
        "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS": 128,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS": 4,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS": 4,
        "MIN_PROGRAM_TEXEL_OFFSET": -8,
        "MAX_PROGRAM_TEXEL_OFFSET": 7,
        "MAX_TEXTURE_LOD_BIAS": 16,
        "UNIFORM_BUFFER_OFFSET_ALIGNMENT": 16,
    },
    # Qualcomm Adreno 6xx (Snapdragon 690–888)
    "adreno6": {
        "MAX_TEXTURE_SIZE": 16384,
        "MAX_CUBE_MAP_TEXTURE_SIZE": 16384,
        "MAX_RENDERBUFFER_SIZE": 16384,
        "MAX_VIEWPORT_DIMS": [16384, 16384],
        "MAX_3D_TEXTURE_SIZE": 2048,
        "MAX_ARRAY_TEXTURE_LAYERS": 2048,
        "ALIASED_POINT_SIZE_RANGE": [1, 1024],
        "ALIASED_LINE_WIDTH_RANGE": [1, 1],
        "MAX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 72,
        "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_FRAGMENT_UNIFORM_VECTORS": 4096,
        "MAX_VERTEX_UNIFORM_VECTORS": 4096,
        "MAX_VERTEX_ATTRIBS": 16,
        "MAX_VERTEX_UNIFORM_COMPONENTS": 16384,
        "MAX_VERTEX_UNIFORM_BLOCKS": 14,
        "MAX_VERTEX_OUTPUT_COMPONENTS": 128,
        "MAX_VARYING_VECTORS": 32,
        "MAX_VARYING_COMPONENTS": 128,
        "MAX_FRAGMENT_UNIFORM_COMPONENTS": 16384,
        "MAX_FRAGMENT_UNIFORM_BLOCKS": 14,
        "MAX_FRAGMENT_INPUT_COMPONENTS": 128,
        "MAX_DRAW_BUFFERS": 8,
        "MAX_COLOR_ATTACHMENTS": 8,
        "MAX_SAMPLES": 4,
        "MAX_UNIFORM_BUFFER_BINDINGS": 60,
        "MAX_UNIFORM_BLOCK_SIZE": 65536,
        "MAX_COMBINED_UNIFORM_BLOCKS": 28,
        "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS": 507904,
        "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS": 507904,
        "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS": 128,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS": 4,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS": 4,
        "MIN_PROGRAM_TEXEL_OFFSET": -32,
        "MAX_PROGRAM_TEXEL_OFFSET": 31,
        "MAX_TEXTURE_LOD_BIAS": 16,
        "UNIFORM_BUFFER_OFFSET_ALIGNMENT": 4,
    },
    # Qualcomm Adreno 7xx (Snapdragon 8 Gen 1–3)
    "adreno7": {
        "MAX_TEXTURE_SIZE": 16384,
        "MAX_CUBE_MAP_TEXTURE_SIZE": 16384,
        "MAX_RENDERBUFFER_SIZE": 16384,
        "MAX_VIEWPORT_DIMS": [16384, 16384],
        "MAX_3D_TEXTURE_SIZE": 2048,
        "MAX_ARRAY_TEXTURE_LAYERS": 2048,
        "ALIASED_POINT_SIZE_RANGE": [1, 1024],
        "ALIASED_LINE_WIDTH_RANGE": [1, 1],
        "MAX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 72,
        "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_FRAGMENT_UNIFORM_VECTORS": 4096,
        "MAX_VERTEX_UNIFORM_VECTORS": 4096,
        "MAX_VERTEX_ATTRIBS": 16,
        "MAX_VERTEX_UNIFORM_COMPONENTS": 16384,
        "MAX_VERTEX_UNIFORM_BLOCKS": 14,
        "MAX_VERTEX_OUTPUT_COMPONENTS": 128,
        "MAX_VARYING_VECTORS": 32,
        "MAX_VARYING_COMPONENTS": 128,
        "MAX_FRAGMENT_UNIFORM_COMPONENTS": 16384,
        "MAX_FRAGMENT_UNIFORM_BLOCKS": 14,
        "MAX_FRAGMENT_INPUT_COMPONENTS": 128,
        "MAX_DRAW_BUFFERS": 8,
        "MAX_COLOR_ATTACHMENTS": 8,
        "MAX_SAMPLES": 4,
        "MAX_UNIFORM_BUFFER_BINDINGS": 60,
        "MAX_UNIFORM_BLOCK_SIZE": 131072,
        "MAX_COMBINED_UNIFORM_BLOCKS": 28,
        "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS": 507904,
        "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS": 507904,
        "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS": 128,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS": 4,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS": 4,
        "MIN_PROGRAM_TEXEL_OFFSET": -32,
        "MAX_PROGRAM_TEXEL_OFFSET": 31,
        "MAX_TEXTURE_LOD_BIAS": 16,
        "UNIFORM_BUFFER_OFFSET_ALIGNMENT": 4,
    },
}


# ═══════════════════════════════════════════════════════════════════════
# Profile parsing
# ═══════════════════════════════════════════════════════════════════════

def parse_profile(conf_text: str) -> dict:
    """Parse a jorkspoofer .conf file into a key→value dict."""
    result = {}
    for line in conf_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^(\w+)="(.*)"$', line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def parse_profile_file(path: str) -> dict:
    """Read and parse a local .conf file."""
    with open(path, "r") as f:
        return parse_profile(f.read())


# ═══════════════════════════════════════════════════════════════════════
# Value derivation
# ═══════════════════════════════════════════════════════════════════════

def _lookup_gpu_family(gl_renderer: str) -> str:
    """Determine GPU parameter family from GL_RENDERER string."""
    r = gl_renderer.lower()
    if "mali" in r:
        return "mali"
    if "adreno" in r:
        m = re.search(r"adreno.*?(\d)\d{2}", r)
        if m and int(m.group(1)) >= 7:
            return "adreno7"
        return "adreno6"
    # PowerVR, Xclipse, etc. — fall back to Mali-like params
    return "mali"


def _device_memory(ram_gb: int) -> int:
    """Convert RAM GB to navigator.deviceMemory (power of 2, max 8)."""
    if ram_gb <= 0:
        return 4
    return min(8, max(1, 2 ** int(math.log2(ram_gb))))


def compute_config(profile: dict, chrome_version: str = "120.0.6099.230") -> dict:
    """
    Compute all values needed by the userscript from a parsed profile.

    Returns a flat dict suitable for JSON embedding in the JS.
    """
    model = profile.get("PROFILE_MODEL", "Pixel 7")
    manufacturer = profile.get("PROFILE_MANUFACTURER", "Google")
    android_ver = profile.get("PROFILE_BUILD_VERSION_RELEASE", "13")
    build_id = profile.get("PROFILE_BUILD_ID", "TQ3A.230901.001")

    screen_w = int(profile.get("PROFILE_SCREEN_WIDTH", "1080"))
    screen_h = int(profile.get("PROFILE_SCREEN_HEIGHT", "2400"))
    lcd_density = int(profile.get("PROFILE_LCD_DENSITY", "420"))

    gl_renderer = profile.get("PROFILE_GL_RENDERER", "Mali-G710 MC10")
    gl_vendor = profile.get("PROFILE_GL_VENDOR", "ARM")

    cpu_cores = int(profile.get("PROFILE_CPU_CORES", "8"))
    ram_gb = int(profile.get("PROFILE_RAM_GB", "8"))
    timezone = profile.get("PROFILE_TIMEZONE", "Europe/London")

    # Derived values
    dpr = round(lcd_density / 160, 3)
    viewport_w = round(screen_w / dpr)
    # Chrome on Android: status bar ~24dp + toolbar ~56dp + nav bar ~48dp = ~128dp
    viewport_h = round(screen_h / dpr) - 128
    avail_h = screen_h - round(24 * dpr)  # screen minus status bar

    device_mem = _device_memory(ram_gb)
    chrome_major = chrome_version.split(".")[0]

    ua = (
        f"Mozilla/5.0 (Linux; Android {android_ver}; {model} "
        f"Build/{build_id}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_version} Mobile Safari/537.36"
    )
    app_version = ua.replace("Mozilla/", "", 1)

    gpu_family = _lookup_gpu_family(gl_renderer)
    gpu_params = _GPU_PARAMS.get(gpu_family, _GPU_PARAMS["mali"])

    # Derive RNG seed from model+brand for unique but consistent
    # canvas/audio fingerprints per device profile. Different profiles
    # will produce different noise patterns, avoiding a detectable
    # "all users have identical canvas fingerprint" signal.
    seed_str = f"{model}:{manufacturer}:{gl_renderer}"
    rng_seed = 0
    for ch in seed_str:
        rng_seed = ((rng_seed << 5) - rng_seed + ord(ch)) & 0xFFFFFFFF
    if rng_seed == 0:
        rng_seed = 2752919560  # fallback

    return {
        "ua": ua,
        "appVersion": app_version,
        "model": model,
        "manufacturer": manufacturer,
        "android": android_ver,
        "buildId": build_id,
        "chromeVer": chrome_version,
        "chromeMajor": chrome_major,
        "screenW": screen_w,
        "screenH": screen_h,
        "availH": avail_h,
        "dpr": dpr,
        "vpW": viewport_w,
        "vpH": viewport_h,
        "glRenderer": gl_renderer,
        "glVendor": gl_vendor,
        "gpuParams": gpu_params,
        "cpuCores": cpu_cores,
        "deviceMem": device_mem,
        "ramGb": ram_gb,
        "timezone": timezone,
        "profileName": profile.get("PROFILE_NAME", "Unknown"),
        "rngSeed": rng_seed,
    }


# ═══════════════════════════════════════════════════════════════════════
# JS generation
# ═══════════════════════════════════════════════════════════════════════

# The inner JS is split into sections. Profile-dependent values are
# accessed via the P config object injected at the top.

_JS_CORE = r"""
    // ══════════════════════════════════════════════════════════════
    // 0. CORE INFRASTRUCTURE
    // ══════════════════════════════════════════════════════════════

    var _origFns = new Map();
    function _storeOrig(fn, name) { _origFns.set(fn, name); }

    var _origToStr = Function.prototype.toString;
    var _newToStr = function() {
        if (_origFns.has(this)) {
            return 'function ' + _origFns.get(this) + '() { [native code] }';
        }
        return _origToStr.call(this);
    };
    Function.prototype.toString = _newToStr;
    _origFns.set(_newToStr, 'toString');

    var _s = P.rngSeed >>> 0;
    function _rng() { _s = (_s * 1664525 + 1013904223) >>> 0; return _s; }
    function _noiseF() { return ((_rng() / 0xFFFFFFFF) - 0.5) * 0.00008; }

    function _def(obj, prop, val) {
        try {
            var _getter = function() { return val; };
            _storeOrig(_getter, 'get ' + prop);
            Object.defineProperty(obj, prop, {
                get: _getter,
                configurable: false,
                enumerable: true,
            });
        } catch(e) {}
    }

    var _origPerfNow = Performance.prototype.now;

    // — navigator.webdriver = false —
    try {
        delete Navigator.prototype.webdriver;
        var _getWebdriver = function() { return false; };
        _storeOrig(_getWebdriver, 'get webdriver');
        Object.defineProperty(Navigator.prototype, 'webdriver', {
            get: _getWebdriver,
            configurable: true,
            enumerable: true,
        });
    } catch(e) {}

    // — Notification.permission = 'denied' —
    try {
        var _getNotifPerm = function() { return 'denied'; };
        _storeOrig(_getNotifPerm, 'get permission');
        Object.defineProperty(Notification, 'permission', {
            get: _getNotifPerm,
            configurable: true,
        });
    } catch(e) {}

    // — window.chrome —
    try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) {
            window.chrome.runtime = {
                connect: function() {},
                sendMessage: function() {},
            };
        }
        delete window.chrome.csi;
        delete window.chrome.loadTimes;
    } catch(e) {}

    // — SharedArrayBuffer polyfill —
    if (typeof SharedArrayBuffer === 'undefined') {
        Object.defineProperty(window, 'SharedArrayBuffer', {
            value: function SharedArrayBuffer(length) {
                return new ArrayBuffer(length);
            },
            writable: true,
            configurable: true,
            enumerable: false,
        });
        Object.defineProperty(SharedArrayBuffer, Symbol.hasInstance, {
            value: function(instance) {
                return instance instanceof ArrayBuffer;
            },
        });
    }
"""

_JS_NAVIGATOR = r"""
    // ══════════════════════════════════════════════════════════════
    // 1. NAVIGATOR
    // ══════════════════════════════════════════════════════════════

    var _nav = {
        userAgent: P.ua,
        appVersion: P.appVersion,
        platform: "Linux armv8l",
        hardwareConcurrency: P.cpuCores,
        deviceMemory: P.deviceMem,
        maxTouchPoints: 5,
        languages: Object.freeze(["en-US", 'en']),
        language: "en-US",
        vendor: 'Google Inc.',
        product: 'Gecko',
        productSub: '20030107',
        pdfViewerEnabled: false,
    };
    for (var _k in _nav) {
        _def(Navigator.prototype, _k, _nav[_k]);
    }

    // — navigator.connection —
    try {
        var _getEffType = function() { return '4g'; };
        var _getConnType = function() { return 'cellular'; };
        var _getDownlink = function() { return 10; };
        var _getRtt = function() { return 50; };
        var _getSaveData = function() { return false; };
        var _getOnchange = function() { return null; };
        _storeOrig(_getEffType, 'get effectiveType');
        _storeOrig(_getConnType, 'get type');
        _storeOrig(_getDownlink, 'get downlink');
        _storeOrig(_getRtt, 'get rtt');
        _storeOrig(_getSaveData, 'get saveData');
        _storeOrig(_getOnchange, 'get onchange');
        var _connP = { onchange: null };
        var _conn = Object.create(NetworkInformation.prototype, {
            effectiveType: { get: _getEffType, enumerable: true },
            type: { get: _getConnType, enumerable: true },
            downlink: { get: _getDownlink, enumerable: true },
            rtt: { get: _getRtt, enumerable: true },
            saveData: { get: _getSaveData, enumerable: true },
            onchange: { get: _getOnchange, set: function(v) { _connP.onchange = v; }, enumerable: true },
        });
        _def(Navigator.prototype, 'connection', _conn);
    } catch(e) {
        try {
            _def(Navigator.prototype, 'connection', {
                effectiveType: '4g', type: 'cellular', downlink: 10, rtt: 50,
                saveData: false, onchange: null,
                addEventListener: function() {}, removeEventListener: function() {},
                dispatchEvent: function() { return true; },
            });
        } catch(e2) {}
    }
"""

_JS_BATTERY = r"""
    // — navigator.getBattery() —
    try {
        var _battStartLevel = (60 + (_rng() % 30)) / 100;
        var _battStartTime = Date.now();
        var _battDischTime = Math.floor((_battStartLevel * 100) / 0.15 * 60);
        var _batt = {
            charging: false, chargingTime: Infinity,
            dischargingTime: _battDischTime, level: _battStartLevel,
            onchargingchange: null, onchargingtimechange: null,
            ondischargingtimechange: null, onlevelchange: null,
            addEventListener: function(type, cb) { this['on' + type] = cb; },
            removeEventListener: function(type) { this['on' + type] = null; },
            dispatchEvent: function() { return true; },
        };
        setInterval(function() {
            var elapsed = (Date.now() - _battStartTime) / 60000;
            var drained = elapsed * 0.15 / 100;
            var newLevel = Math.max(0.15, _battStartLevel - drained);
            if (newLevel <= 0.15 && !_batt.charging) {
                _batt.charging = true;
                _batt.chargingTime = 3600;
                _batt.dischargingTime = Infinity;
                if (_batt.onchargingchange) { try { _batt.onchargingchange(new Event('chargingchange')); } catch(e2){} }
            }
            if (newLevel !== _batt.level) {
                _batt.level = Math.round(newLevel * 100) / 100;
                if (!_batt.charging) {
                    _batt.dischargingTime = Math.max(0, Math.floor((_batt.level * 100) / 0.15 * 60));
                }
                if (_batt.onlevelchange) { try { _batt.onlevelchange(new Event('levelchange')); } catch(e2){} }
            }
        }, 30000);
        Navigator.prototype.getBattery = function() { return Promise.resolve(_batt); };
        _storeOrig(Navigator.prototype.getBattery, 'getBattery');
    } catch(e) {}
"""

_JS_CLIENT_HINTS = r"""
    // — navigator.userAgentData (Client Hints JS API) —
    try {
        var _uaBrands = Object.freeze([
            Object.freeze({ brand: 'Chromium', version: P.chromeMajor }),
            Object.freeze({ brand: 'Google Chrome', version: P.chromeMajor }),
            Object.freeze({ brand: 'Not_A Brand', version: '24' }),
        ]);
        var _uaFullVersionList = Object.freeze([
            Object.freeze({ brand: 'Chromium', version: P.chromeVer }),
            Object.freeze({ brand: 'Google Chrome', version: P.chromeVer }),
            Object.freeze({ brand: 'Not_A Brand', version: '24.0.0.0' }),
        ]);
        var _uaData = Object.create(null);
        var _getUABrands = function() { return _uaBrands; };
        var _getUAMobile = function() { return true; };
        var _getUAPlatform = function() { return 'Android'; };
        _storeOrig(_getUABrands, 'get brands');
        _storeOrig(_getUAMobile, 'get mobile');
        _storeOrig(_getUAPlatform, 'get platform');
        Object.defineProperties(_uaData, {
            brands: { get: _getUABrands, enumerable: true },
            mobile: { get: _getUAMobile, enumerable: true },
            platform: { get: _getUAPlatform, enumerable: true },
        });
        _uaData.getHighEntropyValues = function(hints) {
            return Promise.resolve({
                brands: _uaBrands,
                mobile: true,
                platform: 'Android',
                platformVersion: P.android,
                architecture: 'arm',
                bitness: '64',
                model: P.model,
                uaFullVersion: P.chromeVer,
                fullVersionList: _uaFullVersionList,
            });
        };
        _storeOrig(_uaData.getHighEntropyValues, 'getHighEntropyValues');
        _uaData.toJSON = function() {
            return { brands: _uaBrands, mobile: true, platform: 'Android' };
        };
        _storeOrig(_uaData.toJSON, 'toJSON');
        Object.defineProperty(_uaData, Symbol.toStringTag, {
            value: 'NavigatorUAData', configurable: true, writable: false, enumerable: false,
        });
        try {
            if (typeof NavigatorUAData !== 'undefined') {
                Object.setPrototypeOf(_uaData, NavigatorUAData.prototype);
            }
        } catch(e) {}
        _def(Navigator.prototype, 'userAgentData', _uaData);
    } catch(e) {}
"""

_JS_SCREEN = r"""
    // ══════════════════════════════════════════════════════════════
    // 2. SCREEN, DISPLAY & VIEWPORT
    // ══════════════════════════════════════════════════════════════

    var _scr = {
        width: P.screenW,
        height: P.screenH,
        availWidth: P.screenW,
        availHeight: P.availH,
        availTop: 0, availLeft: 0,
        colorDepth: 24, pixelDepth: 24,
    };
    for (var _sk in _scr) {
        _def(Screen.prototype, _sk, _scr[_sk]);
    }

    try { _def(Screen.prototype, 'isExtended', false); } catch(e) {}
    try { _def(window, 'devicePixelRatio', P.dpr); } catch(e) {}

    try {
        var _getOrientType = function() { return 'portrait-primary'; };
        var _getOrientAngle = function() { return 0; };
        _storeOrig(_getOrientType, 'get type');
        _storeOrig(_getOrientAngle, 'get angle');
        Object.defineProperty(ScreenOrientation.prototype, 'type', {
            get: _getOrientType, configurable: false,
        });
        Object.defineProperty(ScreenOrientation.prototype, 'angle', {
            get: _getOrientAngle, configurable: false,
        });
    } catch(e) {}

    // — Window viewport dimensions (CSS pixels) —
    var _vpW = P.vpW;
    var _vpH = P.vpH;
    try {
        var _getInnerW = function() { return _vpW; };
        var _getInnerH = function() { return _vpH; };
        var _getOuterW = function() { return _vpW; };
        var _getOuterH = function() { return _vpH; };
        _storeOrig(_getInnerW, 'get innerWidth');
        _storeOrig(_getInnerH, 'get innerHeight');
        _storeOrig(_getOuterW, 'get outerWidth');
        _storeOrig(_getOuterH, 'get outerHeight');
        Object.defineProperty(window, 'innerWidth', {
            get: _getInnerW, configurable: false, enumerable: true,
        });
        Object.defineProperty(window, 'innerHeight', {
            get: _getInnerH, configurable: false, enumerable: true,
        });
        Object.defineProperty(window, 'outerWidth', {
            get: _getOuterW, configurable: false, enumerable: true,
        });
        Object.defineProperty(window, 'outerHeight', {
            get: _getOuterH, configurable: false, enumerable: true,
        });
    } catch(e) {}

    // — document.documentElement.clientWidth/clientHeight —
    try {
        var _getDocCW = function() { return _vpW; };
        var _getDocCH = function() { return _vpH; };
        _storeOrig(_getDocCW, 'get clientWidth');
        _storeOrig(_getDocCH, 'get clientHeight');
        function _patchDocEl() {
            if (!document.documentElement) return false;
            try {
                Object.defineProperty(document.documentElement, 'clientWidth', {
                    get: _getDocCW, configurable: true,
                });
                Object.defineProperty(document.documentElement, 'clientHeight', {
                    get: _getDocCH, configurable: true,
                });
            } catch(e) {}
            return true;
        }
        if (!_patchDocEl()) {
            var _docObs = new MutationObserver(function(m, obs) {
                if (_patchDocEl()) obs.disconnect();
            });
            _docObs.observe(document, { childList: true });
        }
    } catch(e) {}

    // — visualViewport API —
    try {
        if (window.visualViewport) {
            var _getVVW = function() { return _vpW; };
            var _getVVH = function() { return _vpH; };
            var _getVVScale = function() { return 1; };
            var _getVVZero = function() { return 0; };
            _storeOrig(_getVVW, 'get width');
            _storeOrig(_getVVH, 'get height');
            _storeOrig(_getVVScale, 'get scale');
            _storeOrig(_getVVZero, 'get offsetLeft');
            Object.defineProperty(window.visualViewport, 'width', { get: _getVVW, configurable: false, enumerable: true });
            Object.defineProperty(window.visualViewport, 'height', { get: _getVVH, configurable: false, enumerable: true });
            Object.defineProperty(window.visualViewport, 'scale', { get: _getVVScale, configurable: false, enumerable: true });
            Object.defineProperty(window.visualViewport, 'offsetLeft', { get: _getVVZero, configurable: false, enumerable: true });
            Object.defineProperty(window.visualViewport, 'offsetTop', { get: _getVVZero, configurable: false, enumerable: true });
            Object.defineProperty(window.visualViewport, 'pageLeft', { get: _getVVZero, configurable: false, enumerable: true });
            Object.defineProperty(window.visualViewport, 'pageTop', { get: _getVVZero, configurable: false, enumerable: true });
        }
    } catch(e) {}

    // — matchMedia device-width/height rewrite —
    // (Further matchMedia hardening for pointer/hover in section 10)
    try {
        var _origMatchMedia = window.matchMedia.bind(window);
        window.matchMedia = function(query) {
            if (typeof query === 'string') {
                query = query.replace(/device-width/g, 'width').replace(/device-height/g, 'height');
            }
            return _origMatchMedia(query);
        };
        _storeOrig(window.matchMedia, 'matchMedia');
    } catch(e) {}
"""

_JS_WEBGL = r"""
    // ══════════════════════════════════════════════════════════════
    // 3. WEBGL — Renderer/Vendor + Numeric Parameters
    // ══════════════════════════════════════════════════════════════

    var _glR = P.glRenderer;
    var _glV = P.glVendor;
    var _gpuP = P.gpuParams;

    var _glParamMap = {
        0x0D33: 'MAX_TEXTURE_SIZE',
        0x851C: 'MAX_CUBE_MAP_TEXTURE_SIZE',
        0x84E8: 'MAX_RENDERBUFFER_SIZE',
        0x0D3A: 'MAX_VIEWPORT_DIMS',
        0x8073: 'MAX_3D_TEXTURE_SIZE',
        0x88FF: 'MAX_ARRAY_TEXTURE_LAYERS',
        0x846D: 'ALIASED_POINT_SIZE_RANGE',
        0x846E: 'ALIASED_LINE_WIDTH_RANGE',
        0x8872: 'MAX_TEXTURE_IMAGE_UNITS',
        0x8B4D: 'MAX_COMBINED_TEXTURE_IMAGE_UNITS',
        0x8B4C: 'MAX_VERTEX_TEXTURE_IMAGE_UNITS',
        0x8DFD: 'MAX_FRAGMENT_UNIFORM_VECTORS',
        0x8DFE: 'MAX_VERTEX_UNIFORM_VECTORS',
        0x8869: 'MAX_VERTEX_ATTRIBS',
        0x8DFC: 'MAX_VARYING_VECTORS',
        0x8824: 'MAX_DRAW_BUFFERS',
        0x8CDF: 'MAX_COLOR_ATTACHMENTS',
        0x8D57: 'MAX_SAMPLES',
    };

    function _wrapGL(proto) {
        if (!proto || !proto.getParameter) return;
        var _orig = proto.getParameter;
        proto.getParameter = function(p) {
            if (p === 0x9246) return _glR;
            if (p === 0x9245) return _glV;
            if (p === 0x1F01) return _glR;
            if (p === 0x1F00) return _glV;
            var _key = _glParamMap[p];
            if (_key && _gpuP.hasOwnProperty(_key)) {
                var _val = _gpuP[_key];
                if (Array.isArray(_val)) {
                    if (_key.indexOf('RANGE') >= 0 || _key.indexOf('WIDTH') >= 0) {
                        return new Float32Array(_val);
                    }
                    return new Int32Array(_val);
                }
                return _val;
            }
            return _orig.call(this, p);
        };
        _storeOrig(proto.getParameter, 'getParameter');
    }
    try { _wrapGL(WebGLRenderingContext.prototype); } catch(e) {}
    try { _wrapGL(WebGL2RenderingContext.prototype); } catch(e) {}
"""

_JS_CANVAS = r"""
    // ══════════════════════════════════════════════════════════════
    // 4. CANVAS FINGERPRINT NOISE
    // ══════════════════════════════════════════════════════════════

    try {
        var _origToDU = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function() {
            try {
                var ctx = this.getContext && this.getContext('2d');
                if (ctx && this.width > 0 && this.height > 0 && this.width < 4000) {
                    var img = ctx.getImageData(0, 0, Math.min(this.width, 64), Math.min(this.height, 64));
                    var d = img.data;
                    for (var i = 0; i < d.length; i += 4) {
                        d[i] = (d[i] + (_rng() & 1)) & 0xFF;
                    }
                    ctx.putImageData(img, 0, 0);
                }
            } catch(ex) {}
            return _origToDU.apply(this, arguments);
        };
        _storeOrig(HTMLCanvasElement.prototype.toDataURL, 'toDataURL');
    } catch(e) {}

    try {
        var _origGID = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function() {
            var data = _origGID.apply(this, arguments);
            try {
                var d = data.data;
                for (var i = 0; i < d.length; i += 4) {
                    d[i] = (d[i] + (_rng() & 1)) & 0xFF;
                }
            } catch(ex) {}
            return data;
        };
        _storeOrig(CanvasRenderingContext2D.prototype.getImageData, 'getImageData');
    } catch(e) {}
"""

_JS_AUDIO = r"""
    // ══════════════════════════════════════════════════════════════
    // 5. AUDIO FINGERPRINT NOISE
    // ══════════════════════════════════════════════════════════════

    try {
        var _origSR = OfflineAudioContext.prototype.startRendering;
        OfflineAudioContext.prototype.startRendering = function() {
            return _origSR.call(this).then(function(buf) {
                try {
                    var ch = buf.getChannelData(0);
                    for (var i = 0; i < ch.length; i += 100) {
                        ch[i] += _noiseF();
                    }
                } catch(ex) {}
                return buf;
            });
        };
        _storeOrig(OfflineAudioContext.prototype.startRendering, 'startRendering');
    } catch(e) {}
"""

_JS_SPEECH = r"""
    // ══════════════════════════════════════════════════════════════
    // 6. SPEECHSYNTHESIS
    // ══════════════════════════════════════════════════════════════

    try {
        var _fakeVoices = null;
        function _buildVoices() {
            if (_fakeVoices) return _fakeVoices;
            var _vd = [
                ['Google US English', 'en-US', true],
                ['Google UK English Female', 'en-GB', false],
                ['Google UK English Male', 'en-GB', false],
            ];
            _fakeVoices = _vd.map(function(d) {
                return Object.freeze({
                    name: d[0], lang: d[1], default: d[2],
                    localService: true, voiceURI: d[0],
                });
            });
            Object.freeze(_fakeVoices);
            return _fakeVoices;
        }
        SpeechSynthesis.prototype.getVoices = function() { return _buildVoices(); };
        _storeOrig(SpeechSynthesis.prototype.getVoices, 'getVoices');
        setTimeout(function() {
            try {
                if (window.speechSynthesis) {
                    window.speechSynthesis.dispatchEvent(new Event('voiceschanged'));
                }
            } catch(e) {}
        }, 50);
    } catch(e) {}
"""

_JS_TIMING = r"""
    // ══════════════════════════════════════════════════════════════
    // 7. TIMING MITIGATION
    // ══════════════════════════════════════════════════════════════

    try {
        Performance.prototype.now = function() {
            return Math.round(_origPerfNow.call(this) * 10) / 10;
        };
        _storeOrig(Performance.prototype.now, 'now');
    } catch(e) {}
"""

_JS_WEBRTC = r"""
    // ══════════════════════════════════════════════════════════════
    // 8. WEBRTC + HARDENING
    // ══════════════════════════════════════════════════════════════

    try {
        var _origRTC = window.RTCPeerConnection;
        if (_origRTC) {
            window.RTCPeerConnection = function(config) {
                if (!config) config = {};
                config.iceServers = [];
                return new _origRTC(config);
            };
            window.RTCPeerConnection.prototype = _origRTC.prototype;
        }
    } catch(e) {}
"""

_JS_TIMEZONE = r"""
    // ══════════════════════════════════════════════════════════════
    // 9. TIMEZONE OVERRIDE
    // ══════════════════════════════════════════════════════════════

    try {
        var _targetTZ = P.timezone;

        // Compute the correct UTC offset for the target timezone (DST-aware)
        var _computeTZOffset = function() {
            var now = new Date();
            var utcStr = now.toLocaleString('en-US', { timeZone: 'UTC' });
            var tzStr = now.toLocaleString('en-US', { timeZone: _targetTZ });
            return (new Date(utcStr) - new Date(tzStr)) / 60000;
        };
        var _tzOffset = _computeTZOffset();

        // Override Date.prototype.getTimezoneOffset
        var _origGetTZO = Date.prototype.getTimezoneOffset;
        Date.prototype.getTimezoneOffset = function() { return _tzOffset; };
        _storeOrig(Date.prototype.getTimezoneOffset, 'getTimezoneOffset');

        // Override Intl.DateTimeFormat to inject our timezone
        var _origDTF = Intl.DateTimeFormat;
        var _newDTF = function(locales, options) {
            if (!options) options = {};
            if (!options.timeZone) options.timeZone = _targetTZ;
            return new _origDTF(locales, options);
        };
        _newDTF.prototype = _origDTF.prototype;
        _newDTF.supportedLocalesOf = _origDTF.supportedLocalesOf;
        Intl.DateTimeFormat = _newDTF;
        _storeOrig(_newDTF, 'DateTimeFormat');

        // Patch resolvedOptions on existing format instances
        var _origResolved = _origDTF.prototype.resolvedOptions;
        _origDTF.prototype.resolvedOptions = function() {
            var opts = _origResolved.call(this);
            opts.timeZone = _targetTZ;
            return opts;
        };
        _storeOrig(_origDTF.prototype.resolvedOptions, 'resolvedOptions');

        // Refresh offset every 30 min (DST transitions)
        setInterval(function() { _tzOffset = _computeTZOffset(); }, 1800000);
    } catch(e) {}
"""

_JS_ADDITIONAL = r"""
    // ══════════════════════════════════════════════════════════════
    // 10. ADDITIONAL SPOOFS
    // ══════════════════════════════════════════════════════════════

    // — MediaDevices.enumerateDevices() —
    try {
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            var _fakeDevices = [
                { deviceId: 'default', kind: 'audioinput', label: 'Bottom Microphone', groupId: 'mic0' },
                { deviceId: 'cam0', kind: 'videoinput', label: 'camera2 0, facing back', groupId: 'cam0' },
                { deviceId: 'cam1', kind: 'videoinput', label: 'camera2 1, facing front', groupId: 'cam1' },
                { deviceId: 'default', kind: 'audiooutput', label: 'Earpiece', groupId: 'spk0' },
                { deviceId: 'spk1', kind: 'audiooutput', label: 'Speaker', groupId: 'spk1' },
            ];
            _fakeDevices = _fakeDevices.map(function(d) {
                var obj = Object.create(MediaDeviceInfo.prototype);
                Object.defineProperties(obj, {
                    deviceId: { get: function() { return d.deviceId; }, enumerable: true },
                    kind: { get: function() { return d.kind; }, enumerable: true },
                    label: { get: function() { return d.label; }, enumerable: true },
                    groupId: { get: function() { return d.groupId; }, enumerable: true },
                    toJSON: { value: function() { return d; } },
                });
                return obj;
            });
            navigator.mediaDevices.enumerateDevices = function() {
                return Promise.resolve(_fakeDevices);
            };
            _storeOrig(navigator.mediaDevices.enumerateDevices, 'enumerateDevices');
        }
    } catch(e) {}

    // — navigator.permissions.query() —
    try {
        if (navigator.permissions && navigator.permissions.query) {
            var _origPermQuery = navigator.permissions.query.bind(navigator.permissions);
            var _permMap = {
                'geolocation': 'granted', 'notifications': 'denied',
                'camera': 'prompt', 'microphone': 'prompt',
                'accelerometer': 'granted', 'gyroscope': 'granted',
                'magnetometer': 'granted', 'midi': 'prompt',
                'clipboard-read': 'prompt', 'clipboard-write': 'granted',
            };
            navigator.permissions.query = function(desc) {
                var name = desc && desc.name;
                if (name && _permMap[name] !== undefined) {
                    return Promise.resolve({ state: _permMap[name], onchange: null,
                        addEventListener: function(){}, removeEventListener: function(){},
                        dispatchEvent: function(){ return true; } });
                }
                return _origPermQuery(desc);
            };
            _storeOrig(navigator.permissions.query, 'query');
        }
    } catch(e) {}

    // — navigator.storage.estimate() —
    try {
        if (navigator.storage && navigator.storage.estimate) {
            navigator.storage.estimate = function() {
                var totalGB = 128;
                var usedGB = 1.2 + (_rng() % 50) / 10;
                return Promise.resolve({
                    quota: totalGB * 1024 * 1024 * 1024,
                    usage: Math.floor(usedGB * 1024 * 1024 * 1024),
                    usageDetails: {},
                });
            };
            _storeOrig(navigator.storage.estimate, 'estimate');
        }
    } catch(e) {}

    // — WebGL extensions —
    try {
        var _dExts = ['WEBGL_debug_renderer_info', 'WEBGL_debug_shaders',
            'EXT_disjoint_timer_query', 'EXT_disjoint_timer_query_webgl2'];
        var _mExts = ['EXT_color_buffer_half_float', 'OES_texture_half_float_linear',
            'WEBGL_compressed_texture_astc', 'WEBGL_compressed_texture_etc',
            'EXT_texture_filter_anisotropic'];
        function _patchGLExts(proto) {
            var _origGetExts = proto.getSupportedExtensions;
            if (!_origGetExts) return;
            proto.getSupportedExtensions = function() {
                var exts = _origGetExts.call(this);
                if (!exts) return exts;
                exts = exts.filter(function(e) { return _dExts.indexOf(e) === -1; });
                for (var i = 0; i < _mExts.length; i++) {
                    if (exts.indexOf(_mExts[i]) === -1) exts.push(_mExts[i]);
                }
                return exts;
            };
        }
        if (window.WebGLRenderingContext) _patchGLExts(WebGLRenderingContext.prototype);
        if (window.WebGL2RenderingContext) _patchGLExts(WebGL2RenderingContext.prototype);
    } catch(e) {}

    // — navigator.plugins / mimeTypes (empty on mobile Chrome) —
    try {
        var _emptyPlugins = Object.create(PluginArray.prototype);
        Object.defineProperty(_emptyPlugins, 'length', {
            get: function() { return 0; }, enumerable: true,
        });
        _emptyPlugins.item = function() { return null; };
        _emptyPlugins.namedItem = function() { return null; };
        _emptyPlugins.refresh = function() {};
        _storeOrig(_emptyPlugins.item, 'item');
        _storeOrig(_emptyPlugins.namedItem, 'namedItem');
        _storeOrig(_emptyPlugins.refresh, 'refresh');
        _def(Navigator.prototype, 'plugins', _emptyPlugins);
    } catch(e) {}
    try {
        var _emptyMimes = Object.create(MimeTypeArray.prototype);
        Object.defineProperty(_emptyMimes, 'length', {
            get: function() { return 0; }, enumerable: true,
        });
        _emptyMimes.item = function() { return null; };
        _emptyMimes.namedItem = function() { return null; };
        _storeOrig(_emptyMimes.item, 'item');
        _storeOrig(_emptyMimes.namedItem, 'namedItem');
        _def(Navigator.prototype, 'mimeTypes', _emptyMimes);
    } catch(e) {}

    // — Touch events (must exist on mobile) —
    try {
        if (!('ontouchstart' in window)) window.ontouchstart = null;
        if (!('ontouchend' in window)) window.ontouchend = null;
        if (!('ontouchmove' in window)) window.ontouchmove = null;
        if (!('ontouchcancel' in window)) window.ontouchcancel = null;
    } catch(e) {}

    // — Mobile orientation (deprecated but still checked by fingerprinters) —
    try {
        if (!('orientation' in window)) {
            Object.defineProperty(window, 'orientation', {
                get: function() { return 0; }, configurable: true, enumerable: true,
            });
        }
        if (!('onorientationchange' in window)) {
            window.onorientationchange = null;
        }
    } catch(e) {}

    // — Block desktop-only APIs —
    try { if (navigator.bluetooth) _def(Navigator.prototype, 'bluetooth', undefined); } catch(e) {}
    try { if (navigator.usb) _def(Navigator.prototype, 'usb', undefined); } catch(e) {}
    try { if (navigator.serial) _def(Navigator.prototype, 'serial', undefined); } catch(e) {}
    try { if (navigator.hid) _def(Navigator.prototype, 'hid', undefined); } catch(e) {}
    try {
        if (Navigator.prototype.getGamepads) {
            Navigator.prototype.getGamepads = function() { return []; };
            _storeOrig(Navigator.prototype.getGamepads, 'getGamepads');
        }
    } catch(e) {}
    try {
        Navigator.prototype.vibrate = function() { return true; };
        _storeOrig(Navigator.prototype.vibrate, 'vibrate');
    } catch(e) {}

    // — matchMedia hardening for mobile-specific media features —
    // Override pointer/hover queries to report mobile values
    try {
        var _origMM2 = window.matchMedia;
        if (_origMM2) {
            window.matchMedia = function(query) {
                var result = _origMM2.call(window, query);
                // Mobile: pointer is coarse, no hover
                if (/\(\s*pointer\s*:\s*fine\s*\)/.test(query)) {
                    try { Object.defineProperty(result, 'matches', { get: function() { return false; } }); } catch(e) {}
                }
                if (/\(\s*pointer\s*:\s*coarse\s*\)/.test(query)) {
                    try { Object.defineProperty(result, 'matches', { get: function() { return true; } }); } catch(e) {}
                }
                if (/\(\s*hover\s*:\s*hover\s*\)/.test(query)) {
                    try { Object.defineProperty(result, 'matches', { get: function() { return false; } }); } catch(e) {}
                }
                if (/\(\s*hover\s*:\s*none\s*\)/.test(query)) {
                    try { Object.defineProperty(result, 'matches', { get: function() { return true; } }); } catch(e) {}
                }
                if (/\(\s*any-pointer\s*:\s*fine\s*\)/.test(query)) {
                    try { Object.defineProperty(result, 'matches', { get: function() { return false; } }); } catch(e) {}
                }
                if (/\(\s*any-hover\s*:\s*hover\s*\)/.test(query)) {
                    try { Object.defineProperty(result, 'matches', { get: function() { return false; } }); } catch(e) {}
                }
                return result;
            };
            _storeOrig(window.matchMedia, 'matchMedia');
        }
    } catch(e) {}

    // — Protect against property enumeration detection —
    // Ensure our overrides don't show up as own properties on navigator
    try {
        var _origHOP = Object.prototype.hasOwnProperty;
        var _navProtoKeys = ['userAgent', 'appVersion', 'platform',
            'hardwareConcurrency', 'deviceMemory', 'maxTouchPoints',
            'languages', 'language', 'vendor', 'product', 'productSub',
            'pdfViewerEnabled', 'connection', 'plugins', 'mimeTypes',
            'webdriver', 'userAgentData'];
        Object.prototype.hasOwnProperty = function(prop) {
            if (this === navigator && _navProtoKeys.indexOf(prop) !== -1) {
                return false;
            }
            return _origHOP.call(this, prop);
        };
        _storeOrig(Object.prototype.hasOwnProperty, 'hasOwnProperty');
    } catch(e) {}
"""


def _build_inner_js(config: dict) -> str:
    """Build the complete inner JS (the spoofing code) referencing config P."""
    config_json = json.dumps(config, separators=(",", ":"))

    parts = [
        "(function() {",
        "    'use strict';",
        "",
        f"    // Profile: {config['profileName']} ({config['model']})",
        f"    var P = {config_json};",
        "",
        _JS_CORE,
        _JS_NAVIGATOR,
        _JS_BATTERY,
        _JS_CLIENT_HINTS,
        _JS_SCREEN,
        _JS_WEBGL,
        _JS_CANVAS,
        _JS_AUDIO,
        _JS_SPEECH,
        _JS_TIMING,
        _JS_WEBRTC,
        _JS_TIMEZONE,
        _JS_ADDITIONAL,
        "",
        "})();",
    ]
    return "\n".join(parts)


def generate_userscript(profile: dict, chrome_version: str = "120.0.6099.230") -> str:
    """
    Generate the complete device_spoof.user.js file content.

    Args:
        profile: dict from parse_profile()
        chrome_version: full Chrome version string

    Returns:
        Complete .user.js file content as a string.
    """
    config = compute_config(profile, chrome_version)
    inner_js = _build_inner_js(config)

    # Escape for embedding as a JS string literal (onclick attribute)
    inner_js_literal = json.dumps(inner_js)

    profile_name = config["profileName"]
    model = config["model"]

    return f"""// ==UserScript==
// @name         Device Fingerprint Spoof
// @version      2.0
// @description  Dynamic fingerprint override — {profile_name} ({model})
// @match        *://*/*
// @run-at       document-start
// ==/UserScript==

// Generated by generate_userscript.py from active device profile.
// Cromite 144+ blocks <script> tag injection from the isolated world.
// Use div.click() + onclick attribute for synchronous MAIN world execution.

(function() {{
    var _p = {inner_js_literal};
    var _d = document.createElement('div');
    _d.setAttribute('onclick', _p);
    (document.documentElement || document).appendChild(_d);
    _d.click();
    _d.remove();
}})();
"""


# ═══════════════════════════════════════════════════════════════════════
# ADB integration
# ═══════════════════════════════════════════════════════════════════════

def _find_adb():
    """Locate the adb binary (same logic as randomize_instances)."""
    # Check common BlueStacks locations
    for candidate in [
        "/Applications/BlueStacks.app/Contents/MacOS/hd-adb",
        os.path.expanduser("~/Library/BlueStacks_nxt/adb"),
        "/Applications/BlueStacks.app/Contents/MacOS/adb",
        shutil.which("adb") or "",
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "adb"


def read_active_profile_from_device(
    adb_exe: str = "", serial: str = "localhost:5575"
) -> dict:
    """Read the active profile .conf from a running BlueStacks instance."""
    if not adb_exe:
        adb_exe = _find_adb()

    # Ensure connected
    subprocess.run(
        [adb_exe, "connect", serial],
        capture_output=True, timeout=5,
    )

    cp = subprocess.run(
        [adb_exe, "-s", serial, "shell",
         "su -c 'cat /data/local/tmp/jorkspoofer_active.conf'"],
        capture_output=True, text=True, timeout=10,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        raise RuntimeError(
            f"Failed to read active profile: {(cp.stdout + cp.stderr).strip()}"
        )
    return parse_profile(cp.stdout)


def detect_chrome_version(
    adb_exe: str = "", serial: str = "localhost:5575"
) -> str:
    """Detect installed Chrome version on device."""
    if not adb_exe:
        adb_exe = _find_adb()

    cp = subprocess.run(
        [adb_exe, "-s", serial, "shell",
         "dumpsys package com.android.chrome | grep versionName"],
        capture_output=True, text=True, timeout=10,
    )
    if cp.returncode == 0 and cp.stdout.strip():
        m = re.search(r"versionName=(\S+)", cp.stdout)
        if m:
            return m.group(1)
    return "120.0.6099.230"  # sensible default


def push_userscript_to_device(
    js_content: str,
    adb_exe: str = "",
    serial: str = "localhost:5575",
    device_path: str = "/data/local/tmp/device_spoof.user.js",
) -> bool:
    """Push generated userscript to device."""
    if not adb_exe:
        adb_exe = _find_adb()

    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False
    ) as f:
        f.write(js_content)
        tmp_path = f.name

    try:
        cp = subprocess.run(
            [adb_exe, "-s", serial, "push", tmp_path, device_path],
            capture_output=True, text=True, timeout=15,
        )
        if cp.returncode != 0:
            print(f"WARNING: push failed: {cp.stderr.strip()}", file=sys.stderr)
            return False

        # Make readable
        subprocess.run(
            [adb_exe, "-s", serial, "shell",
             f"su -c 'chmod 0644 {device_path}'"],
            capture_output=True, timeout=5,
        )
        return True
    finally:
        os.unlink(tmp_path)


def generate_and_push(
    adb_exe: str = "",
    serial: str = "localhost:5575",
    chrome_version: str = "",
    output_local: str = "",
) -> str:
    """
    Read active profile from device, generate userscript, push to device.

    Returns the profile name that was used.
    """
    if not adb_exe:
        adb_exe = _find_adb()

    profile = read_active_profile_from_device(adb_exe, serial)
    profile_name = profile.get("PROFILE_NAME", "Unknown")

    if not chrome_version:
        chrome_version = detect_chrome_version(adb_exe, serial)

    js = generate_userscript(profile, chrome_version)

    # Push to device
    push_userscript_to_device(js, adb_exe, serial)

    # Optionally write local copy
    if output_local:
        with open(output_local, "w") as f:
            f.write(js)
        print(f"Written: {output_local}")

    print(f"Userscript generated for: {profile_name} (Chrome {chrome_version})")
    return profile_name


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Generate device_spoof.user.js from a jorkspoofer profile"
    )
    parser.add_argument(
        "profile", nargs="?",
        help="Path to a .conf profile file (omit for --from-device)",
    )
    parser.add_argument(
        "--from-device", action="store_true",
        help="Read active profile from connected device via adb",
    )
    parser.add_argument(
        "--chrome-version", default="",
        help="Chrome version string (default: auto-detect or 120.0.6099.230)",
    )
    parser.add_argument(
        "-o", "--output", default="device_spoof.user.js",
        help="Output file path (default: device_spoof.user.js)",
    )
    parser.add_argument(
        "--push", action="store_true",
        help="Also push to device via adb",
    )
    parser.add_argument(
        "--serial", default="localhost:5575",
        help="ADB serial (default: localhost:5575)",
    )
    args = parser.parse_args()

    chrome_ver = args.chrome_version or "120.0.6099.230"

    if args.from_device:
        adb_exe = _find_adb()
        profile = read_active_profile_from_device(adb_exe, args.serial)
        if not args.chrome_version:
            chrome_ver = detect_chrome_version(adb_exe, args.serial)
    elif args.profile:
        profile = parse_profile_file(args.profile)
    else:
        parser.error("Provide a profile path or use --from-device")
        return

    js = generate_userscript(profile, chrome_ver)

    # Write output
    with open(args.output, "w") as f:
        f.write(js)

    name = profile.get("PROFILE_NAME", "Unknown")
    model = profile.get("PROFILE_MODEL", "?")
    print(f"Generated {args.output} for: {name} ({model}), Chrome {chrome_ver}")

    if args.push:
        push_userscript_to_device(js, _find_adb(), args.serial)
        print("Pushed to device.")


if __name__ == "__main__":
    main()
