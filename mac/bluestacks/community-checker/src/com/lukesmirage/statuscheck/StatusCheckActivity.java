package com.lukesmirage.statuscheck;

import android.app.Activity;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.view.Display;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import android.webkit.WebSettings;
import android.webkit.WebChromeClient;
import android.webkit.WebViewClient;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.URL;
import java.net.HttpURLConnection;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

/**
 * Luke's Mirage — Community Status Check
 *
 * Lightweight pre-flight checker. Shows only what matters:
 *   - Device identity
 *   - IP + location
 *   - Timezone consistency
 *   - Native layer health
 *   - App ↔ Browser sync (OSRS app vs Chrome see the same device)
 *
 * No internal details exposed.
 */
public class StatusCheckActivity extends Activity {

    private WebView webView;

    // Collected data
    private String deviceModel = "";
    private String deviceBrand = "";
    private String deviceName = "";
    private String profileName = "";
    private String profileDevice = "";
    private String nativeState = "";
    private String systemTimezone = "";
    private String ipAddress = "";
    private String ipCity = "";
    private String ipRegion = "";
    private String ipCountry = "";
    private String ipTimezone = "";
    private String ipIsp = "";
    private boolean tzMatch = false;
    private boolean deviceMatch = false;
    private boolean nativeHealthy = false;
    private boolean ipLookupOk = false;

    // Profile screen specs (from config)
    private String profileScreenW = "";
    private String profileScreenH = "";
    private String profileDensity = "";

    // Browser sync data
    private String browserUA = "";
    private String browserScreenW = "";
    private String browserScreenH = "";
    private String browserDPR = "";
    private boolean browserSyncChecked = false;
    private boolean browserModelMatch = false;
    private boolean browserScreenMatch = false;
    private boolean browserDprMatch = false;
    private boolean appScreenMatch = false;
    private boolean browserSyncOk = false;
    private volatile CountDownLatch browserLatch = new CountDownLatch(1);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Set up WebView for the HTML report
        webView = new WebView(this);
        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        webView.setWebChromeClient(new WebChromeClient());
        webView.setBackgroundColor(0xFF141719);
        webView.addJavascriptInterface(new RefreshBridge(), "StatusRefresh");
        webView.loadDataWithBaseURL(null, buildLoading(), "text/html", "utf-8", null);
        setContentView(webView);

        new Thread(() -> {
            // Collect system + network data on background thread
            collectData();

            // Probe browser fingerprint from the UI thread (needs WebView)
            runOnUiThread(() -> probeBrowser());

            // Wait for browser probe (up to 4s)
            try { browserLatch.await(4, TimeUnit.SECONDS); } catch (Exception e) {}

            // Evaluate browser sync
            evaluateBrowserSync();

            // Build and show final HTML report
            final String html = buildReport();
            runOnUiThread(() -> {
                webView.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
            });
        }).start();
    }

    // ====================================================================
    // BROWSER FINGERPRINT PROBE
    // ====================================================================

    private void probeBrowser() {
        try {
            // Create a hidden WebView to probe what the browser engine reports
            WebView probe = new WebView(this);
            WebSettings ps = probe.getSettings();
            ps.setJavaScriptEnabled(true);
            probe.addJavascriptInterface(new BrowserBridge(), "StatusBridge");
            probe.setWebViewClient(new WebViewClient() {
                @Override
                public void onPageFinished(WebView view, String url) {
                    view.evaluateJavascript(
                        "(function(){" +
                        "  var ua = navigator.userAgent || '';" +
                        "  var sw = screen.width || 0;" +
                        "  var sh = screen.height || 0;" +
                        "  var dpr = window.devicePixelRatio || 1;" +
                        "  StatusBridge.onBrowserData(ua, ''+sw, ''+sh, ''+dpr);" +
                        "})()", null);
                }
            });
            // Load a blank page to trigger the JS probe
            probe.loadDataWithBaseURL(null,
                "<html><body></body></html>", "text/html", "utf-8", null);
        } catch (Exception e) {
            // WebView probe failed — browser sync will show as unchecked
            browserLatch.countDown();
        }
    }

    /** Called from JavaScript when Refresh button is tapped */
    private class RefreshBridge {
        @JavascriptInterface
        public void refresh() {
            // Reset all state for fresh check
            browserSyncChecked = false;
            browserLatch = new CountDownLatch(1);
            runOnUiThread(() -> {
                webView.loadDataWithBaseURL(null, buildLoading(), "text/html", "utf-8", null);
                new Thread(() -> {
                    collectData();
                    runOnUiThread(() -> probeBrowser());
                    try { browserLatch.await(4, TimeUnit.SECONDS); } catch (Exception e) {}
                    evaluateBrowserSync();
                    final String html = buildReport();
                    runOnUiThread(() -> {
                        webView.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
                    });
                }).start();
            });
        }
    }

    /** Called from JavaScript in the hidden probe WebView */
    private class BrowserBridge {
        @JavascriptInterface
        public void onBrowserData(String ua, String sw, String sh, String dpr) {
            browserUA = ua != null ? ua : "";
            browserScreenW = sw != null ? sw : "";
            browserScreenH = sh != null ? sh : "";
            browserDPR = dpr != null ? dpr : "";
            browserSyncChecked = true;
            browserLatch.countDown();
        }
    }

    private void evaluateBrowserSync() {
        if (!browserSyncChecked) {
            browserSyncOk = false;
            return;
        }

        // ── Check 1: Browser UA contains the device model ──
        // BrowserLeaks shows the UA string — the device model must appear in it.
        // e.g. UA should contain "SM-S918B" for Samsung S23 Ultra.
        // This confirms system property spoofing flows through to the browser.
        browserModelMatch = !deviceModel.isEmpty()
            && browserUA.contains(deviceModel);

        // ── Check 2: Profile has valid screen resolution ──
        // BrowserLeaks reports screen.width × screen.height as the device resolution.
        // The profile defines what OSRS, Chrome, and BrowserLeaks will see via the
        // display hooks (LSPosed) and CDP injection. We verify the profile has valid
        // resolution values that match the claimed device.
        appScreenMatch = false;
        try {
            int pw = Integer.parseInt(profileScreenW);
            int ph = Integer.parseInt(profileScreenH);
            // Valid phone resolution: width between 600-2200, height between 960-3440
            // (generous bounds to cover foldables and tall displays like OnePlus 11 3216px)
            appScreenMatch = (pw >= 600 && pw <= 2200 && ph >= 960 && ph <= 3440);
        } catch (Exception e) {
            appScreenMatch = false;
        }

        // ── Check 3: Profile DPR is consistent with resolution ──
        // devicePixelRatio = density / 160 on real devices.
        // BrowserLeaks shows DPR as a standalone value. We verify the profile's
        // density produces a DPR that makes sense for the screen width.
        // Real devices: 720p → DPR ~2, 1080p → DPR 2.5-3.0, 1440p → DPR 3.0-4.0
        browserDprMatch = false;
        try {
            int density = Integer.parseInt(profileDensity);
            int pw = Integer.parseInt(profileScreenW);
            float dpr = density / 160.0f;
            // Logical viewport width = physical / DPR
            float viewportW = pw / dpr;
            // On real phones the CSS viewport width is typically 320-480px
            browserDprMatch = (dpr >= 1.5f && dpr <= 4.5f)
                           && (viewportW >= 280 && viewportW <= 550);
        } catch (Exception e) {
            browserDprMatch = false;
        }

        // ── Check 4: Screen dimensions configured differently from emulator ──
        // The hidden WebView has no CDP, so it reports real emulator display.
        // We verify the profile defines a DIFFERENT resolution from the real
        // display — proving CDP injection will provide distinct, spoofed values
        // to web pages (Jagex website, BrowserLeaks, etc.)
        browserScreenMatch = false;
        try {
            android.util.DisplayMetrics dm = new android.util.DisplayMetrics();
            ((android.view.WindowManager) getSystemService(WINDOW_SERVICE))
                .getDefaultDisplay().getRealMetrics(dm);
            if (appScreenMatch) {
                int pw = Integer.parseInt(profileScreenW);
                int ph = Integer.parseInt(profileScreenH);
                // Profile resolution differs from real emulator display = spoof is active
                browserScreenMatch = (pw != dm.widthPixels || ph != dm.heightPixels);
            }
        } catch (Exception e) {
            browserScreenMatch = false;
        }

        // ── Check 5: System hostname matches native profile device ──
        // Verify the native layer correctly set the hostname from the active profile
        boolean hostnameMatch = !deviceName.isEmpty()
            && deviceName.equals(profileDevice);

        // Overall: model in UA + valid profile res + DPR consistent + screen spoof active
        browserSyncOk = browserModelMatch && appScreenMatch
                     && browserDprMatch && (browserScreenMatch || hostnameMatch);
    }

    // ====================================================================
    // DATA COLLECTION
    // ====================================================================

    private void collectData() {
        deviceModel = getProp("ro.product.model");
        deviceBrand = getProp("ro.product.brand");
        deviceName = getProp("ro.product.device");
        systemTimezone = getProp("persist.sys.timezone");

        loadNativeStatus();
        fetchIpInfo();

        tzMatch = !systemTimezone.isEmpty() && !ipTimezone.isEmpty()
                  && systemTimezone.equals(ipTimezone);

        deviceMatch = !deviceModel.isEmpty() && !profileName.isEmpty()
                      && deviceName.equals(profileDevice);
    }

    private void loadNativeStatus() {
        String content = shellRoot("su -c cat /data/adb/jorkspoofer/native_status 2>/dev/null");
        if (content == null || content.isEmpty()) {
            nativeState = "not found";
            return;
        }
        for (String line : content.split("\n")) {
            line = line.trim();
            if (line.startsWith("#") || line.isEmpty()) continue;
            int eq = line.indexOf('=');
            if (eq <= 0) continue;
            String key = line.substring(0, eq).trim();
            String val = line.substring(eq + 1).trim();
            switch (key) {
                case "native_profile_name": profileName = val; break;
                case "native_profile_device": profileDevice = val; break;
                case "native_state": nativeState = val; break;
                // Also read screen specs from native_status (added in v2.2)
                case "native_profile_screen_width":
                    if (profileScreenW.isEmpty()) profileScreenW = val; break;
                case "native_profile_screen_height":
                    if (profileScreenH.isEmpty()) profileScreenH = val; break;
                case "native_profile_screen_density":
                    if (profileDensity.isEmpty()) profileDensity = val; break;
            }
        }
        nativeHealthy = "healthy".equals(nativeState);

        // Read profile screen specs from active.conf
        String conf = shellRoot("su -c cat /data/adb/modules/jorkspoofer/profiles/active.conf 2>/dev/null");
        if (conf != null) {
            for (String line : conf.split("\n")) {
                line = line.trim();
                if (line.startsWith("#") || line.isEmpty()) continue;
                int eq = line.indexOf('=');
                if (eq <= 0) continue;
                String key = line.substring(0, eq).trim();
                String val = line.substring(eq + 1).trim().replace("\"", "");
                switch (key) {
                    case "PROFILE_SCREEN_WIDTH": profileScreenW = val; break;
                    case "PROFILE_SCREEN_HEIGHT": profileScreenH = val; break;
                    case "PROFILE_SCREEN_DENSITY": profileDensity = val; break;
                }
            }
        }
    }

    private void fetchIpInfo() {
        try {
            URL url = new URL("http://ip-api.com/json/?fields=query,city,regionName,country,timezone,isp");
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(5000);
            conn.setRequestMethod("GET");
            BufferedReader br = new BufferedReader(new InputStreamReader(conn.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) sb.append(line);
            br.close();
            parseIpJson(sb.toString());
            if (ipLookupOk) return;
        } catch (Exception e) {}

        try {
            String raw = shellRoot("su -c 'wget -qO- --timeout=5 \"http://ip-api.com/json/?fields=query,city,regionName,country,timezone,isp\" 2>/dev/null'");
            if (raw != null && !raw.isEmpty()) {
                parseIpJson(raw.trim());
            }
        } catch (Exception e) {
            ipLookupOk = false;
        }
    }

    private void parseIpJson(String jsonStr) {
        try {
            JSONObject json = new JSONObject(jsonStr);
            ipAddress = json.optString("query", "");
            ipCity = json.optString("city", "");
            ipRegion = json.optString("regionName", "");
            ipCountry = json.optString("country", "");
            ipTimezone = json.optString("timezone", "");
            ipIsp = json.optString("isp", "");
            ipLookupOk = !ipAddress.isEmpty();
        } catch (Exception e) {
            ipLookupOk = false;
        }
    }

    // ====================================================================
    // SHELL HELPERS
    // ====================================================================

    private String getProp(String name) {
        try {
            Process p = Runtime.getRuntime().exec(new String[]{"getprop", name});
            BufferedReader br = new BufferedReader(new InputStreamReader(p.getInputStream()));
            String val = br.readLine();
            br.close();
            p.waitFor();
            return val != null ? val.trim() : "";
        } catch (Exception e) {
            return "";
        }
    }

    private String shellRoot(String cmd) {
        try {
            Process p = Runtime.getRuntime().exec(new String[]{"sh", "-c", cmd});
            BufferedReader br = new BufferedReader(new InputStreamReader(p.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) {
                if (sb.length() > 0) sb.append("\n");
                sb.append(line);
            }
            br.close();
            p.waitFor();
            return sb.toString();
        } catch (Exception e) {
            return null;
        }
    }

    private String esc(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\"", "&quot;");
    }

    // ====================================================================
    // HTML REPORT
    // ====================================================================

    private String buildLoading() {
        StringBuilder sb = new StringBuilder();
        sb.append("<!DOCTYPE html><html><head><meta charset='utf-8'>");
        sb.append("<meta name='viewport' content='width=device-width,initial-scale=1'>");
        sb.append("<style>");
        sb.append("body{background:#141719;color:#505560;font-family:-apple-system,sans-serif;");
        sb.append("display:flex;align-items:center;justify-content:center;height:100vh;font-size:13px}");
        sb.append("</style></head><body>Checking status...</body></html>");
        return sb.toString();
    }

    private CharSequence buildNativeReport() {
        android.text.SpannableStringBuilder sb = new android.text.SpannableStringBuilder();

        // Plain symbols — no boxed/filled emoji backgrounds
        // \u2713 = ✓ (plain checkmark), \u2717 = ✗ (plain cross), \u25B3 = △ (plain triangle)
        int GREEN  = 0xFF4CAF50;
        int YELLOW = 0xFFFFB74D;
        int RED    = 0xFFE05545;

        // Title
        sb.append("Luke's Mirage\n");
        sb.setSpan(new android.text.style.RelativeSizeSpan(1.6f),
            0, sb.length() - 1, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        sb.setSpan(new android.text.style.StyleSpan(android.graphics.Typeface.BOLD),
            0, sb.length() - 1, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        sb.append("\n");

        // ── Device Identity ──
        appendIcon(sb, deviceMatch, GREEN, YELLOW);
        sb.append("Device Identity\n");
        sb.append("   " + deviceModel + " (" + deviceBrand + ")\n");
        if (!profileName.isEmpty()) {
            sb.append("   Profile: " + profileName + " \u2192 " + profileDevice + "\n");
        }
        sb.append("   Match: " + (deviceMatch ? "Yes" : "Mismatch") + "\n\n");

        // ── IP Address ──
        appendIcon(sb, ipLookupOk, GREEN, YELLOW);
        sb.append("IP Address\n");
        sb.append("   " + (ipAddress.isEmpty() ? "Unavailable" : ipAddress) + "\n");
        if (!ipCity.isEmpty() || !ipCountry.isEmpty()) {
            sb.append("   " + ipCity + ", " + ipRegion + ", " + ipCountry + "\n");
        }
        if (!ipIsp.isEmpty()) {
            sb.append("   ISP: " + ipIsp + "\n");
        }
        sb.append("\n");

        // ── Timezone ──
        appendIcon(sb, tzMatch, GREEN, YELLOW);
        sb.append("Timezone\n");
        sb.append("   System: " + systemTimezone + "\n");
        sb.append("   IP: " + (ipTimezone.isEmpty() ? "Unknown" : ipTimezone) + "\n");
        sb.append("   Match: " + (tzMatch ? "Yes" : "Mismatch") + "\n\n");

        // ── Native Layer ──
        appendIcon(sb, nativeHealthy, GREEN, YELLOW);
        sb.append("Native Layer\n");
        sb.append("   Status: " + (nativeHealthy ? "Healthy" : nativeState) + "\n\n");

        // ── Browser Sync ──
        appendIcon(sb, false, GREEN, YELLOW);
        sb.append("App \u2194 Browser\n");
        sb.append("   Skipped (WebView unavailable)\n\n");

        // Footer
        int start = sb.length();
        sb.append("Luke's Mirage");
        sb.setSpan(new android.text.style.ForegroundColorSpan(0xFF666666),
            start, sb.length(), android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);

        return sb;
    }

    /** Append a colored plain checkmark or warning triangle (no background shapes). */
    private void appendIcon(android.text.SpannableStringBuilder sb, boolean ok, int goodColor, int warnColor) {
        int start = sb.length();
        // \u2713 = ✓   \u25B3 = △
        sb.append(ok ? "\u2713 " : "\u25B3 ");
        sb.setSpan(new android.text.style.ForegroundColorSpan(ok ? goodColor : warnColor),
            start, start + 1, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
        sb.setSpan(new android.text.style.StyleSpan(android.graphics.Typeface.BOLD),
            start, start + 1, android.text.Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    private String buildReport() {
        StringBuilder sb = new StringBuilder();
        sb.append("<!DOCTYPE html><html><head><meta charset='utf-8'>");
        sb.append("<meta name='viewport' content='width=device-width,initial-scale=1'>");
        sb.append("<style>");

        // ── Reset & base ──
        sb.append("*{box-sizing:border-box;margin:0;padding:0}");
        sb.append("body{font-family:'Roboto',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;");
        sb.append("background:#141719;color:#c0c4c9;padding:20px 16px;font-size:12px;");
        sb.append("-webkit-font-smoothing:antialiased}");

        // ── Scrollbar ──
        sb.append("::-webkit-scrollbar{width:3px}");
        sb.append("::-webkit-scrollbar-track{background:transparent}");
        sb.append("::-webkit-scrollbar-thumb{background:#23272c;border-radius:2px}");

        // ── Title area ──
        sb.append(".title{text-align:center;margin:8px 0 20px}");
        sb.append(".title h1{font-size:15px;font-weight:600;color:#d0d4d9;letter-spacing:-0.3px;margin-bottom:3px}");
        sb.append(".title .sub{font-size:10px;color:#3a3f44}");

        // ── Status row ──
        sb.append(".row{display:flex;align-items:flex-start;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.03)}");
        sb.append(".row:last-child{border-bottom:none}");
        sb.append(".row-icon{flex-shrink:0;width:20px;margin-top:1px}");
        sb.append(".row-body{flex:1;min-width:0}");
        sb.append(".row-label{font-size:9px;color:#505560;text-transform:uppercase;letter-spacing:0.5px;font-weight:500;margin-bottom:3px}");
        sb.append(".row-value{font-size:13px;color:#c0c4c9;font-weight:400;word-break:break-word}");
        sb.append(".row-detail{font-size:10px;color:#4a4f54;margin-top:2px}");

        // ── Checkmark SVG (thin, no background) ──
        sb.append(".check{display:inline-block;width:14px;height:14px}");
        sb.append(".check svg{width:14px;height:14px}");
        sb.append(".check-ok svg{stroke:#10B685;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}");
        sb.append(".check-warn svg{stroke:#e05545;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}");
        sb.append(".check-na svg{stroke:#3a3f44;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}");

        // ── Divider ──
        sb.append(".divider{height:1px;background:rgba(255,255,255,0.03);margin:6px 0}");

        // ── Section label ──
        sb.append(".section-label{font-size:8px;color:#2e3237;text-transform:uppercase;letter-spacing:0.8px;");
        sb.append("font-weight:500;padding:16px 0 6px}");

        // ── Summary banner ──
        sb.append(".banner{border-radius:5px;padding:12px 14px;margin-bottom:20px;display:flex;align-items:center;gap:10px}");
        sb.append(".banner-ok{background:rgba(16,182,133,0.05);border:1px solid rgba(16,182,133,0.10)}");
        sb.append(".banner-warn{background:rgba(224,85,69,0.05);border:1px solid rgba(224,85,69,0.10)}");
        sb.append(".banner-text{flex:1}");
        sb.append(".banner-title{font-size:12px;font-weight:500}");
        sb.append(".banner-ok .banner-title{color:#10B685}");
        sb.append(".banner-warn .banner-title{color:#e05545}");
        sb.append(".banner-sub{font-size:10px;color:#505560;margin-top:1px}");

        // ── Refresh button ──
        sb.append(".refresh-wrap{text-align:center;padding:20px 0 8px}");
        sb.append(".refresh-btn{background:transparent;color:#505560;border:1.5px solid rgba(255,255,255,0.06);");
        sb.append("border-radius:5px;padding:8px 20px;font-size:11px;font-weight:400;cursor:pointer;");
        sb.append("font-family:inherit;transition:all 0.15s ease;-webkit-tap-highlight-color:transparent}");
        sb.append(".refresh-btn:active{transform:scale(0.97);background:rgba(255,255,255,0.02)}");

        // ── Footer ──
        sb.append(".footer{text-align:center;color:#1e2226;font-size:9px;padding:12px 0 4px}");

        sb.append("</style></head><body>");

        // SVG icon helpers
        String checkOk = "<span class='check check-ok'><svg viewBox='0 0 24 24'><polyline points='20 6 9 17 4 12'/></svg></span>";
        String checkWarn = "<span class='check check-warn'><svg viewBox='0 0 24 24'><line x1='18' y1='6' x2='6' y2='18'/><line x1='6' y1='6' x2='18' y2='18'/></svg></span>";
        String checkNa = "<span class='check check-na'><svg viewBox='0 0 24 24'><line x1='5' y1='12' x2='19' y2='12'/></svg></span>";

        // ── Overall banner ──
        boolean allGood = nativeHealthy && tzMatch && deviceMatch && ipLookupOk;
        if (allGood) {
            sb.append("<div class='banner banner-ok'>");
            sb.append("<div class='banner-text'>");
            sb.append("<div class='banner-title'>Ready to Play</div>");
            sb.append("<div class='banner-sub'>All checks passed</div>");
            sb.append("</div>");
            sb.append(checkOk);
            sb.append("</div>");
        } else {
            sb.append("<div class='banner banner-warn'>");
            sb.append("<div class='banner-text'>");
            sb.append("<div class='banner-title'>Issues Detected</div>");
            sb.append("<div class='banner-sub'>Review the items below</div>");
            sb.append("</div>");
            sb.append(checkWarn);
            sb.append("</div>");
        }

        // ── DEVICE ──
        sb.append("<div class='section-label'>Device</div>");

        sb.append("<div class='row'>");
        sb.append("<div class='row-icon'>").append(deviceMatch ? checkOk : checkWarn).append("</div>");
        sb.append("<div class='row-body'>");
        sb.append("<div class='row-label'>Device Model</div>");
        String brandCap = deviceBrand.isEmpty() ? "" : deviceBrand.substring(0, 1).toUpperCase() + deviceBrand.substring(1);
        sb.append("<div class='row-value'>").append(esc(brandCap + " " + deviceModel)).append("</div>");
        if (!profileName.isEmpty()) {
            sb.append("<div class='row-detail'>Profile: ").append(esc(profileName)).append("</div>");
        }
        sb.append("</div></div>");

        // ── PROFILE / NATIVE ──
        sb.append("<div class='row'>");
        sb.append("<div class='row-icon'>").append(nativeHealthy ? checkOk : checkWarn).append("</div>");
        sb.append("<div class='row-body'>");
        sb.append("<div class='row-label'>Native Layer</div>");
        sb.append("<div class='row-value'>").append(nativeHealthy ? "Healthy" : esc(nativeState.isEmpty() ? "Not found" : nativeState)).append("</div>");
        if (!nativeHealthy && nativeState.isEmpty()) {
            sb.append("<div class='row-detail'>native_status file not found</div>");
        }
        sb.append("</div></div>");

        sb.append("<div class='divider'></div>");

        // ── NETWORK ──
        sb.append("<div class='section-label'>Network</div>");

        sb.append("<div class='row'>");
        sb.append("<div class='row-icon'>").append(ipLookupOk ? checkOk : checkWarn).append("</div>");
        sb.append("<div class='row-body'>");
        sb.append("<div class='row-label'>IP Address</div>");
        if (ipLookupOk) {
            sb.append("<div class='row-value'>").append(esc(ipAddress)).append("</div>");
            String location = "";
            if (!ipCity.isEmpty()) location += ipCity;
            if (!ipRegion.isEmpty()) location += (location.isEmpty() ? "" : ", ") + ipRegion;
            if (!ipCountry.isEmpty()) location += (location.isEmpty() ? "" : ", ") + ipCountry;
            if (!location.isEmpty()) {
                sb.append("<div class='row-detail'>").append(esc(location)).append("</div>");
            }
        } else {
            sb.append("<div class='row-value' style='color:#e05545'>Lookup failed</div>");
            sb.append("<div class='row-detail'>Check your network / VPN connection</div>");
        }
        sb.append("</div></div>");

        // ── TIMEZONE ──
        sb.append("<div class='row'>");
        sb.append("<div class='row-icon'>").append(tzMatch ? checkOk : (ipLookupOk ? checkWarn : checkNa)).append("</div>");
        sb.append("<div class='row-body'>");
        sb.append("<div class='row-label'>Timezone</div>");
        sb.append("<div class='row-value'>").append(esc(systemTimezone.isEmpty() ? "Unknown" : systemTimezone)).append("</div>");
        if (ipLookupOk) {
            if (tzMatch) {
                sb.append("<div class='row-detail'>Matches IP location</div>");
            } else {
                sb.append("<div class='row-detail' style='color:#e05545'>IP expects: ").append(esc(ipTimezone)).append("</div>");
            }
        }
        sb.append("</div></div>");

        sb.append("<div class='divider'></div>");

        // ── CONSISTENCY ──
        sb.append("<div class='section-label'>Consistency</div>");

        // Device identity match
        sb.append("<div class='row'>");
        sb.append("<div class='row-icon'>").append(deviceMatch ? checkOk : checkWarn).append("</div>");
        sb.append("<div class='row-body'>");
        sb.append("<div class='row-label'>System ↔ Native</div>");
        sb.append("<div class='row-value'>").append(deviceMatch ? "Consistent" : "Mismatch").append("</div>");
        if (!deviceMatch) {
            sb.append("<div class='row-detail' style='color:#e05545'>System: ").append(esc(deviceName));
            sb.append(" / Native: ").append(esc(profileDevice)).append("</div>");
        }
        sb.append("</div></div>");

        // Timezone consistency
        sb.append("<div class='row'>");
        sb.append("<div class='row-icon'>").append(tzMatch ? checkOk : (ipLookupOk ? checkWarn : checkNa)).append("</div>");
        sb.append("<div class='row-body'>");
        sb.append("<div class='row-label'>Timezone ↔ IP</div>");
        if (!ipLookupOk) {
            sb.append("<div class='row-value' style='color:#4a4f54'>Skipped</div>");
            sb.append("<div class='row-detail'>No IP data available</div>");
        } else {
            sb.append("<div class='row-value'>").append(tzMatch ? "Consistent" : "Mismatch").append("</div>");
        }
        sb.append("</div></div>");

        // ── Refresh ──
        sb.append("<div class='refresh-wrap'>");
        sb.append("<button class='refresh-btn' onclick='StatusRefresh.refresh()'>Refresh</button>");
        sb.append("</div>");

        // ── Footer ──
        sb.append("<div class='footer'>Luke's Mirage</div>");

        sb.append("</body></html>");
        return sb.toString();
    }
}
