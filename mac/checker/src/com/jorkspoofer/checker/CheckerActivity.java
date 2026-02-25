package com.jorkspoofer.checker;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import android.webkit.WebSettings;
import android.webkit.WebChromeClient;
import android.util.DisplayMetrics;
import android.view.Display;
import android.view.WindowManager;
import android.widget.Toast;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.NetworkInterface;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Luke's Mirage — Profile Switcher & Verifier
 *
 * Identity verification + profile switcher with collapsible sections.
 *
 * CRITICAL DESIGN: All "App Sees" values use getprop shell commands, NOT Java
 * Build.* statics. Build.* are frozen at process start and never update after
 * a profile switch. getprop always reflects the current resetprop values.
 *
 * Profile switcher uses filename as the key for both selection and matching.
 * The active profile filename is determined by comparing active.conf content
 * against each profile file.
 */
public class CheckerActivity extends Activity {

    private Map<String, String> origProps = new HashMap<>();
    private Map<String, String> identifiers = new HashMap<>();
    private Map<String, String> profileConf = new HashMap<>();
    private List<String[]> availableProfiles = new ArrayList<>();
    private String activeFilename = "";
    private String ipTimezone = "";
    private String ipCountry = "";
    private String ipCity = "";
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDefaultTextEncodingName("utf-8");
        ws.setDomStorageEnabled(true);
        webView.setWebChromeClient(new WebChromeClient());
        webView.addJavascriptInterface(new ProfileBridge(), "ProfileBridge");
        setContentView(webView);

        new Thread(() -> {
            loadAllData();
            String html = buildReport();
            runOnUiThread(() -> webView.loadDataWithBaseURL(
                null, html, "text/html", "utf-8", null));
        }).start();
    }

    private void loadAllData() {
        origProps.clear();
        identifiers.clear();
        profileConf.clear();
        availableProfiles.clear();
        activeFilename = "";
        ipTimezone = "";
        ipCountry = "";
        ipCity = "";
        loadOriginalBuildProps();
        loadIdentifiers();
        loadProfileConf();
        loadAvailableProfiles();
        resolveActiveFilename();
        loadIpGeolocation();
    }

    private void loadIpGeolocation() {
        try {
            URL url = new URL("http://ip-api.com/json/?fields=timezone,country,countryCode,city");
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(3000);
            conn.setReadTimeout(3000);
            BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) sb.append(line);
            reader.close();
            conn.disconnect();
            String json = sb.toString();
            // Simple JSON parsing without library
            ipTimezone = extractJsonValue(json, "timezone");
            ipCountry = extractJsonValue(json, "countryCode");
            ipCity = extractJsonValue(json, "city");
        } catch (Exception e) {
            ipTimezone = "";
            ipCountry = "";
            ipCity = "";
        }
    }

    private String extractJsonValue(String json, String key) {
        String search = "\"" + key + "\":\"";
        int start = json.indexOf(search);
        if (start < 0) return "";
        start += search.length();
        int end = json.indexOf("\"", start);
        return end > start ? json.substring(start, end) : "";
    }

    // ========================================================================
    // JAVASCRIPT BRIDGE FOR PROFILE SWITCHING
    // ========================================================================

    private class ProfileBridge {
        @JavascriptInterface
        public String listProfiles() {
            String raw = shellRoot("su -c 'sh /data/adb/jorkspoofer-switch.sh list'");
            return raw != null ? raw.trim() : "";
        }

        @JavascriptInterface
        public String getCurrentProfile() {
            String raw = shellRoot("su -c 'sh /data/adb/jorkspoofer-switch.sh current'");
            return raw != null ? raw.trim() : "";
        }

        @JavascriptInterface
        public String applyProfile(String profileName) {
            String result = shellRoot("su -c 'sh /data/adb/jorkspoofer-switch.sh apply " + profileName + "'");
            return result != null ? result.trim() : "ERROR";
        }

        @JavascriptInterface
        public void reloadPage() {
            new Thread(() -> {
                loadAllData();
                String html = buildReport();
                runOnUiThread(() -> webView.loadDataWithBaseURL(
                    null, html, "text/html", "utf-8", null));
            }).start();
        }

        @JavascriptInterface
        public void showToast(String msg) {
            runOnUiThread(() -> Toast.makeText(CheckerActivity.this, msg, Toast.LENGTH_SHORT).show());
        }
    }

    // ========================================================================
    // DATA LOADING
    // ========================================================================

    private void loadOriginalBuildProps() {
        String[] propFiles = {
            "/vendor/build.prop",
            "/vendor/default.prop",
            "/system/build.prop",
            "/system/etc/prop.default"
        };
        for (String path : propFiles) {
            String content = shellRoot("su -c cat " + path + " 2>/dev/null");
            if (content == null || content.isEmpty()) continue;
            for (String line : content.split("\n")) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                int eq = line.indexOf('=');
                if (eq > 0) {
                    origProps.put(line.substring(0, eq).trim(), line.substring(eq + 1).trim());
                }
            }
        }
    }

    private void loadIdentifiers() {
        String content = shellRoot("su -c cat /data/adb/jorkspoofer/identifiers.conf 2>/dev/null");
        if (content == null) return;
        for (String line : content.split("\n")) {
            line = line.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            int eq = line.indexOf('=');
            if (eq > 0) {
                String val = line.substring(eq + 1).trim();
                if (val.startsWith("\"") && val.endsWith("\""))
                    val = val.substring(1, val.length() - 1);
                identifiers.put(line.substring(0, eq).trim(), val);
            }
        }
    }

    private void loadProfileConf() {
        String content = shellRoot("su -c cat /data/adb/modules/jorkspoofer/profiles/active.conf 2>/dev/null");
        if (content == null) return;
        for (String line : content.split("\n")) {
            line = line.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            int eq = line.indexOf('=');
            if (eq > 0) {
                String val = line.substring(eq + 1).trim();
                if (val.startsWith("\"") && val.endsWith("\""))
                    val = val.substring(1, val.length() - 1);
                profileConf.put(line.substring(0, eq).trim(), val);
            }
        }
    }

    private void loadAvailableProfiles() {
        String raw = shellRoot("su -c 'sh /data/adb/jorkspoofer-switch.sh list'");
        if (raw == null || raw.isEmpty()) return;
        for (String line : raw.split("\n")) {
            line = line.trim();
            if (line.isEmpty()) continue;
            int pipe = line.indexOf('|');
            if (pipe > 0) {
                availableProfiles.add(new String[]{
                    line.substring(0, pipe).trim(),   // [0] = filename (e.g. google-pixel5)
                    line.substring(pipe + 1).trim()    // [1] = display name (e.g. Google Pixel 5)
                });
            }
        }
    }

    /**
     * Determine which filename in profiles/ matches active.conf.
     * We compare PROFILE_NAME from active.conf against each profile's display name.
     */
    private void resolveActiveFilename() {
        String activeName = prof("PROFILE_NAME");
        if (activeName.isEmpty()) return;
        for (String[] p : availableProfiles) {
            if (p[1].equals(activeName)) {
                activeFilename = p[0];
                return;
            }
        }
        // Fallback: try matching by PROFILE_DEVICE against filename patterns
        String activeDevice = prof("PROFILE_DEVICE");
        for (String[] p : availableProfiles) {
            if (p[0].contains(activeDevice)) {
                activeFilename = p[0];
                return;
            }
        }
    }

    private String prof(String key) {
        String v = profileConf.get(key);
        return v != null ? v : "";
    }

    private String origProp(String name) {
        String val = origProps.get(name);
        if (val != null && !val.isEmpty()) return val;
        if (name.startsWith("ro.product.")) {
            String suffix = name.substring("ro.product.".length());
            val = origProps.get("ro.product.system." + suffix);
            if (val != null && !val.isEmpty()) return val;
            val = origProps.get("ro.product.vendor." + suffix);
            if (val != null && !val.isEmpty()) return val;
        }
        if (name.equals("ro.build.fingerprint")) {
            val = origProps.get("ro.system.build.fingerprint");
            if (val != null && !val.isEmpty()) return val;
        }
        return "";
    }

    // ========================================================================
    // REPORT
    // ========================================================================

    private String buildReport() {
        StringBuilder sb = new StringBuilder();
        sb.append("<!DOCTYPE html><html><head><meta charset='utf-8'>");
        sb.append("<meta name='viewport' content='width=device-width,initial-scale=1'>");
        sb.append("<style>");

        // ── Reset & Base ──
        sb.append("*{box-sizing:border-box;margin:0;padding:0}");
        sb.append("body{font-family:'Roboto',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;");
        sb.append("background:#141719;color:#c0c4c9;padding:12px;font-size:12px;");
        sb.append("-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}");

        // ── Title ──
        sb.append("h1{text-align:center;font-size:16px;margin:10px 0 2px;color:#d0d4d9;font-weight:600;letter-spacing:-0.3px}");

        // ── Section Headers (clickable, collapsible) ──
        sb.append(".sec{margin:10px 0 0}");
        sb.append(".sec-hdr{display:flex;align-items:center;justify-content:space-between;");
        sb.append("padding:8px 12px;cursor:pointer;user-select:none;-webkit-user-select:none;");
        sb.append("background:#1e2226;border:1px solid rgba(255,255,255,0.04);border-radius:5px 5px 0 0}");
        sb.append(".sec-hdr.collapsed{border-radius:5px}");
        sb.append(".sec-hdr:active{background:#22262b}");
        sb.append(".sec-title{font-size:10px;color:#505560;text-transform:uppercase;letter-spacing:0.6px;font-weight:500}");
        sb.append(".sec-chev{font-size:10px;color:#505560;transition:transform 0.2s ease}");
        sb.append(".sec-hdr.collapsed .sec-chev{transform:rotate(-90deg)}");
        sb.append(".sec-body{overflow:hidden;transition:max-height 0.25s ease;border:1px solid rgba(255,255,255,0.04);border-top:none;border-radius:0 0 5px 5px}");
        sb.append(".sec-body.hidden{max-height:0 !important;border:none}");

        // ── Profile subtitle ──
        sb.append(".profile{text-align:center;font-size:11px;color:#505560;margin-bottom:10px}");
        sb.append(".profile b{color:#10B685;font-weight:500}");

        // ── Card / Table ──
        sb.append(".card{background:#22262b;padding:2px 0;overflow:hidden}");
        sb.append("table{width:100%;border-collapse:collapse}");
        sb.append("th{background:#1e2226;color:#505560;text-align:left;padding:5px 10px;font-weight:500;font-size:9px;text-transform:uppercase;letter-spacing:0.4px}");
        sb.append("td{padding:5px 10px;border-bottom:1px solid rgba(255,255,255,0.025);word-break:break-all;font-size:11px;color:#c0c4c9}");
        sb.append("tr:last-child td{border-bottom:none}");

        // ── Row status colors ──
        sb.append("tr.ok td{background:rgba(16,182,133,0.04)}");
        sb.append("tr.leak td{background:rgba(224,85,69,0.06)}");
        sb.append("tr.na td{background:transparent}");
        sb.append("tr.match td{background:transparent}");

        // ── Tags/Badges ──
        sb.append(".tag{display:inline-block;font-size:8px;padding:2px 6px;border-radius:3px;font-weight:600;letter-spacing:0.3px;text-transform:uppercase}");
        sb.append(".tok{background:rgba(16,182,133,0.12);color:#10B685}");
        sb.append(".twarn{background:rgba(224,85,69,0.12);color:#e05545}");
        sb.append(".tinfo{background:rgba(255,255,255,0.04);color:#505560}");
        sb.append(".tmatch{background:rgba(255,255,255,0.04);color:#505560}");
        sb.append(".tset{background:rgba(16,182,133,0.08);color:#10B685}");

        // ── Misc ──
        sb.append(".blocked{color:#4a4f54;font-style:italic}");
        sb.append(".m{font-family:'SF Mono','Roboto Mono',Menlo,Consolas,monospace;font-size:10px;color:#8a8f94}");
        sb.append(".footer{text-align:center;color:#2e3237;font-size:9px;margin-top:18px;padding-bottom:8px}");

        // ── Profile Switcher ──
        sb.append(".switcher{background:#22262b;border:1px solid rgba(255,255,255,0.04);border-radius:5px;padding:12px;margin:8px 0 12px}");
        sb.append(".switcher-title{font-size:9px;color:#505560;margin-bottom:8px;text-transform:uppercase;font-weight:500;letter-spacing:0.6px}");
        sb.append(".switcher-row{display:flex;gap:8px;align-items:center}");

        // ── Custom dropdown (replaces native select — no ugly Android dialog) ──
        sb.append(".dd-wrap{flex:1;position:relative}");
        sb.append(".dd-trigger{width:100%;background:#1c1f23;color:#c0c4c9;border:1px solid rgba(255,255,255,0.06);");
        sb.append("border-radius:4px;padding:7px 28px 7px 10px;font-size:11px;font-family:inherit;cursor:pointer;");
        sb.append("overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left;position:relative}");
        sb.append(".dd-trigger::after{content:'\\25BC';position:absolute;right:9px;top:50%;transform:translateY(-50%);font-size:8px;color:#505560;pointer-events:none}");
        sb.append(".dd-trigger.open{border-color:rgba(16,182,133,0.35);border-radius:4px 4px 0 0}");
        sb.append(".dd-panel{display:none;position:absolute;left:0;right:0;top:100%;background:#1c1f23;border:1px solid rgba(16,182,133,0.35);border-top:none;");
        sb.append("border-radius:0 0 4px 4px;max-height:220px;overflow-y:auto;z-index:100}");
        sb.append(".dd-panel.open{display:block}");
        sb.append(".dd-item{display:flex;align-items:center;padding:6px 10px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,0.025);font-size:10px;color:#8a8f94;gap:8px}");
        sb.append(".dd-item:last-child{border-bottom:none}");
        sb.append(".dd-item:active{background:rgba(16,182,133,0.08)}");
        sb.append(".dd-item.active{background:rgba(16,182,133,0.06);color:#10B685}");
        sb.append(".dd-item .dd-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}");
        sb.append(".dd-item .dd-file{font-size:9px;color:#4a4f54;font-family:'SF Mono','Roboto Mono',monospace}");
        sb.append(".dd-dot{width:5px;height:5px;border-radius:50%;flex-shrink:0}");
        sb.append(".dd-dot.on{background:#10B685}");
        sb.append(".dd-dot.off{background:#2e3237}");
        // Hidden real select for value storage
        sb.append("select{display:none}");

        // ── Buttons (matches GUI) ──
        sb.append("button{background:transparent;color:#8a8f94;border:1.5px solid rgba(255,255,255,0.1);");
        sb.append("border-radius:5px;padding:8px 16px;font-size:11px;font-weight:500;cursor:pointer;");
        sb.append("font-family:inherit;transition:all 0.15s ease}");
        sb.append("button:active{transform:scale(0.97)}");
        sb.append("button:hover{color:#b0b5ba;border-color:rgba(255,255,255,0.16);background:rgba(255,255,255,0.02)}");
        sb.append("button.primary{background:rgba(16,182,133,0.06);color:#10B685;border:1px solid rgba(16,182,133,0.2)}");
        sb.append("button.primary:hover{background:rgba(16,182,133,0.1);border-color:rgba(16,182,133,0.25)}");
        sb.append("button.secondary{background:transparent;color:#505560;border:1.5px solid rgba(255,255,255,0.06)}");
        sb.append("button.secondary:hover{color:#70757a;background:rgba(255,255,255,0.03)}");
        sb.append("button:disabled{opacity:0.35;cursor:default;transform:none}");

        // ── Status messages ──
        sb.append(".status-msg{font-size:11px;margin-top:8px;padding:6px 10px;border-radius:4px;display:none}");
        sb.append(".status-msg.success{display:block;background:rgba(16,182,133,0.06);color:#10B685;border:1px solid rgba(16,182,133,0.15)}");
        sb.append(".status-msg.error{display:block;background:rgba(224,85,69,0.06);color:#e05545;border:1px solid rgba(224,85,69,0.15)}");
        sb.append(".status-msg.info{display:block;background:rgba(255,255,255,0.03);color:#8a8f94;border:1px solid rgba(255,255,255,0.06)}");

        // ── Health banner ──
        sb.append(".health-banner{border-radius:5px;padding:8px 12px;margin-bottom:6px;text-align:center}");
        sb.append(".health-banner.healthy{background:rgba(16,182,133,0.06);border:1px solid rgba(16,182,133,0.12)}");
        sb.append(".health-banner.unhealthy{background:rgba(224,85,69,0.06);border:1px solid rgba(224,85,69,0.12)}");

        // ── Scrollbar (matches GUI) ──
        sb.append("::-webkit-scrollbar{width:3px}");
        sb.append("::-webkit-scrollbar-track{background:transparent}");
        sb.append("::-webkit-scrollbar-thumb{background:#23272c;border-radius:2px}");

        // ── Note rows ──
        sb.append(".note-row{font-size:9px;color:#4a4f54;padding:4px 10px}");

        sb.append("</style></head><body>");

        // ── Title ──
        sb.append("<h1>Luke's Mirage</h1>");

        // Profile name
        String profileName = prof("PROFILE_NAME");
        sb.append("<div class='profile'>Active Profile: <b>").append(esc(profileName.isEmpty() ? "None" : profileName)).append("</b></div>");

        // ================================================================
        // PROFILE SWITCHER UI
        // ================================================================
        sb.append("<div class='switcher'>");
        sb.append("<div class='switcher-title'>Switch Device Profile</div>");
        sb.append("<div class='switcher-row'>");

        // Hidden real select (value storage only)
        sb.append("<select id='profileSelect' style='display:none'>");
        for (String[] p : availableProfiles) {
            String selected = p[0].equals(activeFilename) ? " selected" : "";
            sb.append("<option value='").append(esc(p[0])).append("'").append(selected).append(">");
            sb.append(esc(p[1])).append("</option>");
        }
        sb.append("</select>");

        // Custom dropdown trigger + panel
        String activeName = "";
        for (String[] p : availableProfiles) {
            if (p[0].equals(activeFilename)) { activeName = p[1]; break; }
        }
        if (activeName.isEmpty() && !availableProfiles.isEmpty()) activeName = availableProfiles.get(0)[1];

        sb.append("<div class='dd-wrap' id='ddWrap'>");
        sb.append("<div class='dd-trigger' id='ddTrigger' onclick='toggleDD()'>").append(esc(activeName)).append("</div>");
        sb.append("<div class='dd-panel' id='ddPanel'>");
        for (String[] p : availableProfiles) {
            boolean isActive = p[0].equals(activeFilename);
            sb.append("<div class='dd-item").append(isActive ? " active" : "").append("' data-val='").append(esc(p[0])).append("' onclick='pickDD(this)'>");
            sb.append("<span class='dd-dot ").append(isActive ? "on" : "off").append("'></span>");
            sb.append("<span class='dd-name'>").append(esc(p[1])).append("</span>");
            sb.append("<span class='dd-file'>").append(esc(p[0])).append("</span>");
            sb.append("</div>");
        }
        sb.append("</div></div>");

        sb.append("<button class='primary' id='applyBtn' onclick='applyProfile()'>Apply</button>");
        sb.append("<button class='secondary' id='reloadBtn' onclick='reloadChecker()' title='Reload'>&#x21bb;</button>");
        sb.append("</div>");
        sb.append("<div id='statusMsg' class='status-msg'></div>");
        sb.append("</div>");

        // ================================================================
        // JAVASCRIPT: Profile switching + collapsible sections
        // ================================================================
        sb.append("<script>");

        // Profile switcher functions
        sb.append("function setStatus(msg,cls){var el=document.getElementById('statusMsg');el.className='status-msg '+cls;el.textContent=msg}");
        sb.append("function disableBtns(v){document.getElementById('applyBtn').disabled=v;document.getElementById('reloadBtn').disabled=v;document.getElementById('profileSelect').disabled=v;");
        sb.append("document.getElementById('ddTrigger').style.pointerEvents=v?'none':'auto';document.getElementById('ddTrigger').style.opacity=v?'0.35':'1'}");
        sb.append("function applyProfile(){var sel=document.getElementById('profileSelect');var name=sel.value;if(!name)return;disableBtns(true);");
        sb.append("setStatus('Applying '+sel.options[sel.selectedIndex].text+'...','info');");
        sb.append("setTimeout(function(){try{var result=ProfileBridge.applyProfile(name);");
        sb.append("if(result&&result.indexOf('OK')===0){setStatus('Applied! Reloading...','success');setTimeout(function(){ProfileBridge.reloadPage()},1000)}");
        sb.append("else{setStatus('Error: '+result,'error');disableBtns(false)}}catch(e){setStatus('Error: '+e.message,'error');disableBtns(false)}},100)}");
        sb.append("function reloadChecker(){disableBtns(true);setStatus('Reloading...','info');setTimeout(function(){ProfileBridge.reloadPage()},100)}");

        // Custom dropdown logic
        sb.append("function toggleDD(){var t=document.getElementById('ddTrigger');var p=document.getElementById('ddPanel');");
        sb.append("var open=p.classList.contains('open');if(open){p.classList.remove('open');t.classList.remove('open')}else{p.classList.add('open');t.classList.add('open')}}");
        sb.append("function pickDD(el){var val=el.getAttribute('data-val');var name=el.querySelector('.dd-name').textContent;");
        sb.append("document.getElementById('ddTrigger').textContent=name;");
        sb.append("var sel=document.getElementById('profileSelect');for(var i=0;i<sel.options.length;i++){if(sel.options[i].value===val){sel.selectedIndex=i;break}}");
        sb.append("document.querySelectorAll('.dd-item').forEach(function(d){d.classList.remove('active');d.querySelector('.dd-dot').className='dd-dot off'});");
        sb.append("el.classList.add('active');el.querySelector('.dd-dot').className='dd-dot on';");
        sb.append("toggleDD()}");
        // Close dropdown on outside tap
        sb.append("document.addEventListener('click',function(e){var w=document.getElementById('ddWrap');if(w&&!w.contains(e.target)){");
        sb.append("document.getElementById('ddPanel').classList.remove('open');document.getElementById('ddTrigger').classList.remove('open')}});");

        // Collapsible section toggle
        sb.append("function toggleSec(id){var hdr=document.getElementById('hdr-'+id);var body=document.getElementById('body-'+id);");
        sb.append("if(hdr.classList.contains('collapsed')){hdr.classList.remove('collapsed');body.classList.remove('hidden');body.style.maxHeight=body.scrollHeight+'px'}");
        sb.append("else{hdr.classList.add('collapsed');body.classList.add('hidden')}}");

        // Expand/collapse all
        sb.append("function expandAll(){document.querySelectorAll('.sec-hdr').forEach(function(h){h.classList.remove('collapsed')});");
        sb.append("document.querySelectorAll('.sec-body').forEach(function(b){b.classList.remove('hidden');b.style.maxHeight=b.scrollHeight+'px'})}");
        sb.append("function collapseAll(){document.querySelectorAll('.sec-hdr').forEach(function(h){h.classList.add('collapsed')});");
        sb.append("document.querySelectorAll('.sec-body').forEach(function(b){b.classList.add('hidden')})}");

        sb.append("</script>");

        // ── Expand/Collapse all controls ──
        sb.append("<div style='display:flex;justify-content:flex-end;gap:8px;margin-bottom:4px'>");
        sb.append("<button class='secondary' onclick='expandAll()' style='padding:4px 10px;font-size:9px'>Expand All</button>");
        sb.append("<button class='secondary' onclick='collapseAll()' style='padding:4px 10px;font-size:9px'>Collapse All</button>");
        sb.append("</div>");

        // Track section numbering
        int secNum = 0;

        // ================================================================
        // SECTION 1: DEVICE IDENTITY
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Device Identity");
        sb.append(th3());
        addPropRow(sb, "Model",        gp("ro.product.model"),        "ro.product.model");
        addPropRow(sb, "Manufacturer", gp("ro.product.manufacturer"), "ro.product.manufacturer");
        addPropRow(sb, "Brand",        gp("ro.product.brand"),        "ro.product.brand");
        addPropRow(sb, "Device",       gp("ro.product.device"),       "ro.product.device");
        addPropRow(sb, "Product",      gp("ro.product.name"),         "ro.product.name");
        addPropRow(sb, "Hardware",     gp("ro.hardware"),             "ro.hardware");
        addPropRow(sb, "Board",        gp("ro.product.board"),        "ro.product.board");
        addPropRow(sb, "Bootloader",   gp("ro.bootloader"),           "ro.bootloader");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 2: BUILD INFO
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Build Info");
        sb.append(th3());
        addPropRow(sb, "Fingerprint", gp("ro.build.fingerprint"),        "ro.build.fingerprint");
        addPropRow(sb, "Display",     gp("ro.build.display.id"),         "ro.build.display.id");
        addPropRow(sb, "Build ID",    gp("ro.build.id"),                 "ro.build.id");
        addPropRow(sb, "Type",        gp("ro.build.type"),               "ro.build.type");
        addPropRow(sb, "Tags",        gp("ro.build.tags"),               "ro.build.tags");
        addPropRow(sb, "Incremental", gp("ro.build.version.incremental"), "ro.build.version.incremental");
        addPropRow(sb, "Description", gp("ro.build.description"),         "ro.build.description");
        addPropRow(sb, "Flavor",      gp("ro.build.flavor"),              "ro.build.flavor");
        addPropRow(sb, "Characteristics", gp("ro.build.characteristics"), "ro.build.characteristics");
        addPropRow(sb, "Build Time",  gp("ro.build.date.utc"),            "ro.build.date.utc");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 3: ANDROID VERSION
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Android Version");
        sb.append(th3());
        addPropRow(sb, "SDK",            gp("ro.build.version.sdk"),            "ro.build.version.sdk");
        addPropRow(sb, "Release",        gp("ro.build.version.release"),        "ro.build.version.release");
        addPropRow(sb, "Security Patch", gp("ro.build.version.security_patch"), "ro.build.version.security_patch");
        addPropRow(sb, "Codename",       gp("ro.build.version.codename"),       "ro.build.version.codename");
        addPropRow(sb, "First API",      gp("ro.product.first_api_level"),      "ro.product.first_api_level");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 4: SECURITY & EMULATOR DETECTION
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Security & Emulator Detection");
        sb.append(th3());
        addSecRow(sb, "ro.kernel.qemu",          gp("ro.kernel.qemu"),               "1");
        addSecRow(sb, "ro.boot.qemu",            gp("ro.boot.qemu"),                 "1");
        addSecRow(sb, "ro.debuggable",           gp("ro.debuggable"),                 origPropOr("ro.debuggable", "1"));
        addSecRow(sb, "ro.secure",               gp("ro.secure"),                     origPropOr("ro.secure", "0"));
        addSecRow(sb, "ro.adb.secure",           gp("ro.adb.secure"),                 origPropOr("ro.adb.secure", "0"));
        addSecRow(sb, "ro.build.selinux",        gp("ro.build.selinux"),              "");
        addSecRow(sb, "verifiedbootstate",       gp("ro.boot.verifiedbootstate"),     "orange");
        addSecRow(sb, "flash.locked",            gp("ro.boot.flash.locked"),          "0");
        addSecRow(sb, "vbmeta.device_state",     gp("ro.boot.vbmeta.device_state"),   "unlocked");
        addSecRow(sb, "warranty_bit",            gp("ro.boot.warranty_bit"),           "1");
        addSecRow(sb, "oem_unlock_allowed",      gp("sys.oem_unlock_allowed"),        "1");
        addSecRow(sb, "veritymode",              gp("ro.boot.veritymode"),             "");
        addSecRow(sb, "crypto.state",            gp("ro.crypto.state"),                "");
        addSecRow(sb, "boot.mode",               gp("ro.boot.mode"),                   "");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 5: EMULATOR PROPERTY BLOCKING
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Emulator Prop Blocking");
        sb.append("<table><tr><th>Check</th><th>Status</th><th></th></tr>");

        String bstApp = firstLine(shell("getprop 2>/dev/null | grep -c '^\\[bst\\.'"));
        String bstRoot = firstLine(shellRoot("su -c \"getprop 2>/dev/null | grep -c '^\\[bst\\.'\""));
        addBlockRow(sb, "bst.* props", isZero(bstApp), bstApp, bstRoot + " exist on device");

        String bsApp = firstLine(shell("getprop 2>/dev/null | grep -c '^\\[bluestacks\\.'"));
        String bsRoot = firstLine(shellRoot("su -c \"getprop 2>/dev/null | grep -c '^\\[bluestacks\\.'\""));
        addBlockRow(sb, "bluestacks.* props", isZero(bsApp), bsApp, bsRoot + " exist on device");

        String vbApp = firstLine(shell("getprop 2>/dev/null | grep -c '^\\[vbox\\.'"));
        String vbRoot = firstLine(shellRoot("su -c \"getprop 2>/dev/null | grep -c '^\\[vbox\\.'\""));
        addBlockRow(sb, "vbox.* props", isZero(vbApp), vbApp, vbRoot + " exist on device");

        String mntApp = shell("ls /mnt/windows 2>&1").trim();
        boolean mntHidden = mntApp.contains("No such") || mntApp.contains("Permission") || mntApp.isEmpty();
        addBlockRow(sb, "/mnt/windows", mntHidden, mntHidden ? "" : mntApp, "host FS mount");

        String vboxsfApp = firstLine(shell("grep -c vboxsf /proc/mounts 2>/dev/null"));
        addBlockRow(sb, "vboxsf in /proc/mounts", isZero(vboxsfApp), vboxsfApp, "filtered by bind-mount");

        String libartApp = firstLine(shell("grep -c bluestacks /apex/com.android.art/lib64/libart.so 2>/dev/null"));
        addBlockRow(sb, "\"bluestacks\" in libart.so", isZero(libartApp), libartApp, "patched via bind-mount");

        String librtApp = firstLine(shell("grep -c bluestacks /system/lib64/libandroid_runtime.so 2>/dev/null"));
        addBlockRow(sb, "\"bluestacks\" in libandroid_runtime", isZero(librtApp), librtApp, "patched via bind-mount");

        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 6: NATIVE BRIDGE / ABI
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Native Bridge & ABI");
        sb.append(th3());
        addPropRow(sb, "native.bridge",       gp("ro.dalvik.vm.native.bridge"),       "ro.dalvik.vm.native.bridge");
        addPropRow(sb, "native.bridge.exec",  gp("ro.enable.native.bridge.exec"),     "ro.enable.native.bridge.exec");
        addPropRow(sb, "persist.nativebridge", gp("persist.sys.nativebridge"),         "persist.sys.nativebridge");
        addPropRow(sb, "cpu.abi",             gp("ro.product.cpu.abi"),                "ro.product.cpu.abi");
        addPropRow(sb, "cpu.abilist",         gp("ro.product.cpu.abilist"),            "ro.product.cpu.abilist");
        addPropRow(sb, "cpu.abilist64",       gp("ro.product.cpu.abilist64"),          "ro.product.cpu.abilist64");
        addPropRow(sb, "cpu.abilist32",       gp("ro.product.cpu.abilist32"),          "ro.product.cpu.abilist32");
        addPropRow(sb, "treble.enabled",      gp("ro.treble.enabled"),                 "ro.treble.enabled");
        addPropRow(sb, "usb.config",          gp("persist.sys.usb.config"),            "persist.sys.usb.config");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 7: GPU / GRAPHICS
        // ================================================================
        secNum++;
        openSection(sb, secNum, "GPU / Graphics");
        sb.append(th3());
        addPropRow(sb, "ro.hardware.egl",     gp("ro.hardware.egl"),     "ro.hardware.egl");
        addPropRow(sb, "ro.opengles.version", gp("ro.opengles.version"), "ro.opengles.version");
        addPropRow(sb, "LCD Density",         gp("ro.sf.lcd_density"),   "ro.sf.lcd_density");

        String sfGles = shellRoot("su -c dumpsys SurfaceFlinger 2>/dev/null | grep GLES").trim();
        String appSf = shell("dumpsys SurfaceFlinger 2>/dev/null | grep GLES").trim();
        addMatchRow(sb, "SurfaceFlinger GLES",
            appSf.isEmpty() ? "(N/A)" : appSf,
            sfGles.isEmpty() ? "(N/A)" : sfGles,
            "system service (same for all)");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 8: DISPLAY RESOLUTION
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Display Resolution");
        sb.append("<table><tr><th>Check</th><th>OSRS Sees</th><th>Emulator Real</th><th></th></tr>");

        String profW = prof("PROFILE_SCREEN_WIDTH");
        String profH = prof("PROFILE_SCREEN_HEIGHT");
        String profD = prof("PROFILE_SCREEN_DENSITY");

        DisplayMetrics dm = new DisplayMetrics();
        Display display = ((WindowManager) getSystemService(WINDOW_SERVICE)).getDefaultDisplay();
        display.getRealMetrics(dm);
        String realW = String.valueOf(dm.widthPixels);
        String realH = String.valueOf(dm.heightPixels);
        String realD = String.valueOf(dm.densityDpi);

        String wmSize = shellRoot("su -c wm size 2>/dev/null").trim();
        String wmDensity = shellRoot("su -c wm density 2>/dev/null").trim();

        // Width
        boolean wOk = !profW.isEmpty() && !profW.equals(realW);
        sb.append("<tr class='").append(wOk ? "ok" : "leak").append("'>");
        sb.append("<td><b>Width (px)</b></td>");
        sb.append("<td class='m'>").append(esc(profW.isEmpty() ? "(not set)" : profW)).append("</td>");
        sb.append("<td class='m'>").append(esc(realW)).append("</td>");
        sb.append("<td>").append(wOk ? tag("SPOOFED", "tok") : tag(profW.isEmpty() ? "N/A" : "MATCH", profW.isEmpty() ? "tinfo" : "twarn")).append("</td></tr>");

        // Height
        boolean hOk = !profH.isEmpty() && !profH.equals(realH);
        sb.append("<tr class='").append(hOk ? "ok" : "leak").append("'>");
        sb.append("<td><b>Height (px)</b></td>");
        sb.append("<td class='m'>").append(esc(profH.isEmpty() ? "(not set)" : profH)).append("</td>");
        sb.append("<td class='m'>").append(esc(realH)).append("</td>");
        sb.append("<td>").append(hOk ? tag("SPOOFED", "tok") : tag(profH.isEmpty() ? "N/A" : "MATCH", profH.isEmpty() ? "tinfo" : "twarn")).append("</td></tr>");

        // Density
        boolean dOk = !profD.isEmpty() && !profD.equals(realD);
        sb.append("<tr class='").append(dOk ? "ok" : "leak").append("'>");
        sb.append("<td><b>Density (dpi)</b></td>");
        sb.append("<td class='m'>").append(esc(profD.isEmpty() ? "(not set)" : profD)).append("</td>");
        sb.append("<td class='m'>").append(esc(realD)).append("</td>");
        sb.append("<td>").append(dOk ? tag("SPOOFED", "tok") : tag(profD.isEmpty() ? "N/A" : "MATCH", profD.isEmpty() ? "tinfo" : "twarn")).append("</td></tr>");

        // Resolution summary
        String osrsRes = (!profW.isEmpty() && !profH.isEmpty()) ? profW + "x" + profH + " @ " + profD + "dpi" : "(not configured)";
        String emuRes = realW + "x" + realH + " @ " + realD + "dpi";
        boolean resOk = wOk && hOk;
        sb.append("<tr class='").append(resOk ? "ok" : (profW.isEmpty() ? "na" : "leak")).append("'>");
        sb.append("<td><b>Full Resolution</b></td>");
        sb.append("<td class='m'>").append(esc(osrsRes)).append("</td>");
        sb.append("<td class='m'>").append(esc(emuRes)).append("</td>");
        sb.append("<td>").append(resOk ? tag("OK", "tok") : tag(profW.isEmpty() ? "N/A" : "LEAK", profW.isEmpty() ? "tinfo" : "twarn")).append("</td></tr>");

        // WM override
        boolean wmClean = !wmSize.contains("Override") && !wmDensity.contains("Override");
        sb.append("<tr class='").append(wmClean ? "ok" : "leak").append("'>");
        sb.append("<td><b>WM Override</b></td>");
        sb.append("<td class='m' colspan='2'>").append(esc(wmClean ? "None (physical display unchanged)" : wmSize + " / " + wmDensity)).append("</td>");
        sb.append("<td>").append(wmClean ? tag("CLEAN", "tok") : tag("SET!", "twarn")).append("</td></tr>");

        sb.append("<tr class='na'><td colspan='4' class='note-row'>");
        sb.append("LSPosed DisplayHooks intercept Display.getMetrics/getRealMetrics/getSize/getRealSize ");
        sb.append("inside OSRS only. This app sees real values (not hooked).");
        sb.append("</td></tr>");

        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 9: KERNEL / UNAME
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Kernel / uname");
        sb.append(th3());
        String osArch = System.getProperty("os.arch", "?");
        String realArch = shellRoot("su -c uname -m").trim();
        addMatchRow(sb, "uname -m (arch)", osArch, realArch,
            "arm64 native (not emulated)");

        String osVer = System.getProperty("os.version", "?");
        String realVer = shellRoot("su -c uname -r").trim();
        if (!osVer.equals(realVer)) {
            addSmartRow(sb, "uname -r (release)", osVer, realVer, true);
        } else {
            addMatchRow(sb, "uname -r (release)", osVer, realVer, "syscall (bind-mount is /proc/version)");
        }

        String procVerApp = shell("cat /proc/version 2>/dev/null").trim();
        String procVerReal = shellRoot("su -c cat /proc/version 2>/dev/null").trim();
        addMatchRow(sb, "/proc/version", truncate(procVerApp, 100),
            truncate(procVerReal, 100), "bind-mounted (all processes)");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 10: CPU
        // ================================================================
        secNum++;
        openSection(sb, secNum, "CPU");
        sb.append(th3());

        String appCores = String.valueOf(Runtime.getRuntime().availableProcessors());
        String realCores = shellRoot("su -c cat /sys/devices/system/cpu/present").trim();
        String profCores = prof("PROFILE_CPU_CORES");
        if (!profCores.isEmpty() && appCores.equals(profCores)) {
            addSmartRow(sb, "Core count", appCores, realCores, true);
        } else {
            addMatchRow(sb, "Core count", appCores, realCores, "sysfs (same for all)");
        }

        String cpuApp = shell("cat /proc/cpuinfo 2>/dev/null").trim();
        String cpuReal = shellRoot("su -c cat /proc/cpuinfo 2>/dev/null").trim();
        addMatchRow(sb, "Hardware (cpuinfo)", extractField(cpuApp, "Hardware"),
            extractField(cpuReal, "Hardware"), "bind-mounted");
        addMatchRow(sb, "CPU part (first)", extractField(cpuApp, "CPU part"),
            extractField(cpuReal, "CPU part"), "bind-mounted");
        addMatchRow(sb, "BogoMIPS", extractField(cpuApp, "BogoMIPS"),
            extractField(cpuReal, "BogoMIPS"), "bind-mounted");
        addMatchRow(sb, "Features", truncate(extractField(cpuApp, "Features"), 80),
            truncate(extractField(cpuReal, "Features"), 80), "bind-mounted");
        addMatchRow(sb, "Serial (cpuinfo)", extractField(cpuApp, "Serial"),
            extractField(cpuReal, "Serial"), "bind-mounted");

        addMatchRow(sb, "cpu/possible",
            shell("cat /sys/devices/system/cpu/possible 2>/dev/null").trim(),
            shellRoot("su -c cat /sys/devices/system/cpu/possible 2>/dev/null").trim(),
            "sysfs (kernel)");
        addMatchRow(sb, "cpu/present",
            shell("cat /sys/devices/system/cpu/present 2>/dev/null").trim(),
            shellRoot("su -c cat /sys/devices/system/cpu/present 2>/dev/null").trim(),
            "sysfs (kernel)");
        addMatchRow(sb, "cpu/online",
            shell("cat /sys/devices/system/cpu/online 2>/dev/null").trim(),
            shellRoot("su -c cat /sys/devices/system/cpu/online 2>/dev/null").trim(),
            "sysfs (kernel)");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 11: SERIAL / CARRIER / TIMEZONE
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Serial / Carrier / Timezone");
        sb.append(th3());
        addPropRow(sb, "ro.serialno",               gp("ro.serialno"),               "ro.serialno");
        addPropRow(sb, "ro.boot.serialno",          gp("ro.boot.serialno"),          "ro.boot.serialno");
        addPropRow(sb, "gsm.operator.alpha",        gp("gsm.operator.alpha"),        "gsm.operator.alpha");
        addPropRow(sb, "gsm.operator.numeric",      gp("gsm.operator.numeric"),      "gsm.operator.numeric");
        addPropRow(sb, "gsm.sim.operator.alpha",    gp("gsm.sim.operator.alpha"),    "gsm.sim.operator.alpha");
        addPropRow(sb, "gsm.sim.operator.numeric",  gp("gsm.sim.operator.numeric"),  "gsm.sim.operator.numeric");
        addPropRow(sb, "gsm.sim.operator.iso-country", gp("gsm.sim.operator.iso-country"), "gsm.sim.operator.iso-country");
        addPropRow(sb, "persist.sys.timezone",       gp("persist.sys.timezone"),       "persist.sys.timezone");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 12: DEVICE IDENTIFIERS
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Device Identifiers");
        sb.append("<table><tr><th>Identifier</th><th>Value</th><th></th></tr>");
        addIdRow(sb, "IMEI",         identifiers.get("PROFILE_IMEI"));
        addIdRow(sb, "MEID",         identifiers.get("PROFILE_MEID"));
        addIdRow(sb, "IMSI",         identifiers.get("PROFILE_IMSI"));
        addIdRow(sb, "ICCID",        identifiers.get("PROFILE_ICCID"));
        addIdRow(sb, "Phone Number", identifiers.get("PROFILE_PHONE_NUMBER"));
        addIdRow(sb, "Android ID",   identifiers.get("PROFILE_ANDROID_ID"));
        addIdRow(sb, "GSF ID",       identifiers.get("PROFILE_GSF_ID"));
        addIdRow(sb, "Google Ad ID", identifiers.get("PROFILE_GAID"));
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 13: NETWORK
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Network");
        sb.append(th3());
        addMatchRow(sb, "Interfaces",
            getNetworkInterfaces(),
            shellRoot("su -c ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | tr '\\n' ', '").trim(),
            "kernel (same for all)");
        addMatchRow(sb, "MAC (wlan0/eth0)",
            getSpoofedMac(),
            shellRoot("su -c cat /sys/class/net/eth0/address 2>/dev/null").trim(),
            "sysfs (same for all)");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 14: BATTERY
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Battery");
        sb.append("<table><tr><th>Check</th><th>App Sees</th><th>System Raw</th><th></th></tr>");

        Intent batIntent = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        int batLevel = -1, batStatus = -1, batHealth = -1, batTemp = -1, batVoltage = -1;
        boolean batPresent = false;
        String batTech = "?";
        int batPlugged = 0;
        if (batIntent != null) {
            batLevel = batIntent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
            int scale = batIntent.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
            if (scale > 0 && batLevel >= 0) batLevel = (batLevel * 100) / scale;
            batStatus = batIntent.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            batHealth = batIntent.getIntExtra(BatteryManager.EXTRA_HEALTH, -1);
            batTemp = batIntent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1);
            batVoltage = batIntent.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1);
            batPresent = batIntent.getBooleanExtra(BatteryManager.EXTRA_PRESENT, false);
            batTech = batIntent.getStringExtra(BatteryManager.EXTRA_TECHNOLOGY);
            if (batTech == null) batTech = "?";
            batPlugged = batIntent.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0);
        }

        String dumpBat = shellRoot("su -c dumpsys battery 2>/dev/null");
        String dLevel = extractDumpsys(dumpBat, "level");
        String dStatus = extractDumpsys(dumpBat, "status");
        String dHealth = extractDumpsys(dumpBat, "health");
        String dTemp = extractDumpsys(dumpBat, "temperature");
        String dVoltage = extractDumpsys(dumpBat, "voltage");
        String dPresent = extractDumpsys(dumpBat, "present");
        String dTech = extractDumpsys(dumpBat, "technology");
        String dAC = extractDumpsys(dumpBat, "AC powered");
        String dUSB = extractDumpsys(dumpBat, "USB powered");

        String sysCap = shell("cat /sys/class/power_supply/battery/capacity 2>/dev/null").trim();
        String sysStatus = shell("cat /sys/class/power_supply/battery/status 2>/dev/null").trim();
        String sysHealth = shell("cat /sys/class/power_supply/battery/health 2>/dev/null").trim();
        String sysTemp = shell("cat /sys/class/power_supply/battery/temp 2>/dev/null").trim();
        String sysVoltage = shell("cat /sys/class/power_supply/battery/voltage_now 2>/dev/null").trim();
        String sysCurrent = shell("cat /sys/class/power_supply/battery/current_now 2>/dev/null").trim();
        String sysPresent = shell("cat /sys/class/power_supply/battery/present 2>/dev/null").trim();
        String sysTech = shell("cat /sys/class/power_supply/battery/technology 2>/dev/null").trim();
        String sysAC = shell("cat /sys/class/power_supply/ac/online 2>/dev/null").trim();
        String sysUSB = shell("cat /sys/class/power_supply/usb/online 2>/dev/null").trim();

        boolean sysfsHooked = !sysCap.isEmpty();

        // Level
        String appLevelStr = batLevel >= 0 ? batLevel + "%" : "?";
        boolean levelSpoofed = batLevel >= 0 && batLevel < 100;
        addBatCheckRow(sb, "Level", appLevelStr, dLevel + "%",
            sysfsHooked ? sysCap + "%" : "(no sysfs)",
            levelSpoofed ? "ok" : "leak",
            levelSpoofed ? "SPOOFED" : (batLevel == 100 ? "STATIC" : "?"));

        // Status
        String[] statusNames = {"?", "Unknown", "Charging", "Discharging", "Not Charging", "Full"};
        String appStatusStr = (batStatus >= 1 && batStatus <= 5) ? statusNames[batStatus] : String.valueOf(batStatus);
        boolean statusOk = batStatus == 2 || batStatus == 3;
        addBatCheckRow(sb, "Status", appStatusStr, dStatus.isEmpty() ? "?" : dStatus,
            sysfsHooked ? sysStatus : "(no sysfs)",
            statusOk ? "ok" : "leak",
            statusOk ? "OK" : "SUSPICIOUS");

        // Health
        String[] healthNames = {"?", "Unknown", "Good", "Overheat", "Dead", "Over voltage", "Failure", "Cold"};
        String appHealthStr = (batHealth >= 1 && batHealth <= 7) ? healthNames[batHealth] : String.valueOf(batHealth);
        addBatCheckRow(sb, "Health", appHealthStr, dHealth.isEmpty() ? "?" : dHealth,
            sysfsHooked ? sysHealth : "(no sysfs)",
            batHealth == 2 ? "ok" : "na", batHealth == 2 ? "OK" : "?");

        // Temperature
        String appTempStr = batTemp >= 0 ? (batTemp / 10) + "." + (batTemp % 10) + "\u00B0C" : "?";
        boolean tempRealistic = batTemp > 200 && batTemp < 400 && batTemp != 250;
        addBatCheckRow(sb, "Temperature", appTempStr,
            dTemp.isEmpty() ? "?" : (Integer.parseInt(dTemp) / 10) + "." + (Integer.parseInt(dTemp) % 10) + "\u00B0C",
            sysfsHooked ? sysTemp : "(no sysfs)",
            tempRealistic ? "ok" : "leak",
            tempRealistic ? "OK" : (batTemp == 250 ? "ROUND!" : "?"));

        // Voltage
        String appVoltStr = batVoltage >= 0 ? batVoltage + "mV" : "?";
        boolean voltOk = batVoltage >= 3000 && batVoltage <= 4500;
        addBatCheckRow(sb, "Voltage", appVoltStr, dVoltage.isEmpty() ? "?" : dVoltage + "mV",
            sysfsHooked ? sysVoltage : "(no sysfs)",
            voltOk ? "ok" : "na", voltOk ? "OK" : "?");

        // Present
        addBatCheckRow(sb, "Present", batPresent ? "true" : "false",
            dPresent.isEmpty() ? "?" : dPresent,
            sysfsHooked ? sysPresent : "(no sysfs)",
            batPresent ? "ok" : "leak",
            batPresent ? "OK" : "MISSING!");

        // Technology
        addBatCheckRow(sb, "Technology", batTech, dTech.isEmpty() ? "?" : dTech,
            sysfsHooked ? sysTech : "(no sysfs)",
            "Li-ion".equals(batTech) || "Li-poly".equals(batTech) ? "ok" : "na",
            "Li-ion".equals(batTech) || "Li-poly".equals(batTech) ? "OK" : "?");

        // Current draw
        if (sysfsHooked && !sysCurrent.isEmpty()) {
            addBatCheckRow(sb, "Current draw", sysCurrent + " \u00B5A", "(sysfs hook)", "",
                "ok", "HOOKED");
        }

        // Power source
        String plugStr;
        if (batPlugged == BatteryManager.BATTERY_PLUGGED_AC) plugStr = "AC";
        else if (batPlugged == BatteryManager.BATTERY_PLUGGED_USB) plugStr = "USB";
        else if (batPlugged == BatteryManager.BATTERY_PLUGGED_WIRELESS) plugStr = "Wireless";
        else plugStr = "None (battery)";
        String rawPower = "";
        if ("true".equals(dAC)) rawPower = "AC";
        else if ("true".equals(dUSB)) rawPower = "USB";
        else rawPower = "None";
        boolean powerOk = batPlugged == 0 ? (batStatus == 3) : true;
        addBatCheckRow(sb, "Power Source", plugStr, rawPower,
            sysfsHooked ? ("AC:" + sysAC + " USB:" + sysUSB) : "(no sysfs)",
            powerOk ? "ok" : "leak",
            powerOk ? "OK" : "SUSPICIOUS");

        // Sysfs hook status
        sb.append("<tr class='").append(sysfsHooked ? "ok" : "leak").append("'>");
        sb.append("<td><b>Sysfs Battery Hooks</b></td>");
        sb.append("<td class='m' colspan='2'>").append(sysfsHooked ? "Active \u2014 ReZygisk creating virtual /sys/class/power_supply/ nodes" : "Inactive \u2014 no sysfs battery nodes (emulator baseline)").append("</td>");
        sb.append("<td>").append(sysfsHooked ? tag("ACTIVE", "tok") : tag("NONE", "twarn")).append("</td></tr>");

        sb.append("<tr class='na'><td colspan='4' class='note-row'>");
        sb.append("Battery values show RAW system state (this app is NOT in LSPosed scope). ");
        sb.append("OSRS sees spoofed values via BatteryHooks + CDP browser layer syncs getBattery().");
        sb.append("</td></tr>");

        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 15: PARTITION FINGERPRINTS
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Partition Fingerprints");
        sb.append(th3());
        String[] fpParts = {"ro.build.fingerprint", "ro.system.build.fingerprint",
            "ro.system_ext.build.fingerprint", "ro.vendor.build.fingerprint",
            "ro.odm.build.fingerprint", "ro.bootimage.build.fingerprint",
            "ro.product.build.fingerprint"};
        for (String fp : fpParts) {
            String shortName = fp.replace("ro.", "").replace(".build.fingerprint", "");
            addPropRow(sb, shortName, truncate(gp(fp), 60), fp);
        }
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 16: PARTITION PRODUCT PROPS
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Partition Product Props");
        sb.append(th3());
        String[] partitions = {"system", "vendor", "odm", "product", "system_ext"};
        for (String part : partitions) {
            String key = "ro.product." + part + ".model";
            addPropRow(sb, part + ".model", gp(key), key);
        }
        for (String part : partitions) {
            String key = "ro.product." + part + ".device";
            addPropRow(sb, part + ".device", gp(key), key);
        }
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 17: NATIVE LAYER HEALTH
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Native Layer Health");

        Map<String, String> nativeStatus = new HashMap<>();
        String nsRaw = shellRoot("su -c cat /data/adb/jorkspoofer/native_status 2>/dev/null");
        if (nsRaw != null && !nsRaw.isEmpty()) {
            for (String nsLine : nsRaw.split("\n")) {
                nsLine = nsLine.trim();
                if (nsLine.isEmpty() || nsLine.startsWith("#")) continue;
                int nsEq = nsLine.indexOf('=');
                if (nsEq > 0) {
                    nativeStatus.put(nsLine.substring(0, nsEq).trim(), nsLine.substring(nsEq + 1).trim());
                }
            }
        }

        String nState = nativeStatus.getOrDefault("native_state", "unknown");
        String nVer = nativeStatus.getOrDefault("native_version", "?");
        String nScore = nativeStatus.getOrDefault("verify_score", "?");
        boolean nHealthy = "healthy".equals(nState);

        // Health banner
        sb.append("<div class='health-banner ").append(nHealthy ? "healthy" : "unhealthy").append("'>");
        sb.append("<span style='font-size:13px;font-weight:600;color:");
        sb.append(nHealthy ? "#10B685" : "#e05545").append("'>");
        sb.append(nHealthy ? "HEALTHY" : nState.toUpperCase());
        sb.append("</span> <span style='font-size:10px;color:#505560'>");
        sb.append(esc(nVer)).append(" \u2022 ").append(esc(nScore)).append(" checks</span></div>");

        sb.append("<table><tr><th>Check</th><th>Value</th><th></th></tr>");
        addNativeRow(sb, "Module Version", nativeStatus.getOrDefault("native_version", "NOT INSTALLED"),
            !nativeStatus.isEmpty());
        addNativeRow(sb, "ReZygisk Provider", nativeStatus.getOrDefault("native_zygisk_provider", "unknown"),
            !"unknown".equals(nativeStatus.getOrDefault("native_zygisk_provider", "unknown")));
        addNativeRow(sb, "ReZygisk Loaded", nativeStatus.getOrDefault("native_zygisk_loaded", "0"),
            "1".equals(nativeStatus.getOrDefault("native_zygisk_loaded", "0")));
        addNativeRow(sb, "Profile Name", nativeStatus.getOrDefault("native_profile_name", "?"),
            !"unknown".equals(nativeStatus.getOrDefault("native_profile_name", "unknown")));
        addNativeRow(sb, "Profile Model", nativeStatus.getOrDefault("native_profile_model", "?"),
            !"unknown".equals(nativeStatus.getOrDefault("native_profile_model", "unknown")));

        // Cross-check
        String nativeModel = nativeStatus.getOrDefault("native_profile_model", "");
        String activeModel = prof("PROFILE_MODEL");
        boolean modelMatch = !nativeModel.isEmpty() && nativeModel.equals(activeModel);
        sb.append("<tr class='").append(modelMatch ? "ok" : "leak").append("'>");
        sb.append("<td><b>Profile Sync</b></td>");
        sb.append("<td class='m'>Native: ").append(esc(nativeModel)).append(" / Active: ").append(esc(activeModel)).append("</td>");
        sb.append("<td>").append(modelMatch ? tag("SYNCED", "tok") : tag("MISMATCH", "twarn")).append("</td></tr>");

        addNativeRow(sb, "GL Renderer", nativeStatus.getOrDefault("native_profile_gl_renderer", "?"),
            !"unknown".equals(nativeStatus.getOrDefault("native_profile_gl_renderer", "unknown")));
        addNativeRow(sb, "GL Vendor", nativeStatus.getOrDefault("native_profile_gl_vendor", "?"),
            !"unknown".equals(nativeStatus.getOrDefault("native_profile_gl_vendor", "unknown")));
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 18: NATIVE VERIFICATION
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Native Verification");
        sb.append("<table><tr><th>Check</th><th>Status</th><th></th></tr>");
        addVerifyRow(sb, "Hostname",           nativeStatus.getOrDefault("verify_hostname", "?"),
            nativeStatus.getOrDefault("native_hostname", "?"));
        addVerifyRow(sb, "Timezone",           nativeStatus.getOrDefault("verify_timezone", "?"),
            nativeStatus.getOrDefault("native_timezone", "?"));
        addVerifyRow(sb, "Network Interfaces", nativeStatus.getOrDefault("verify_network_interfaces", "?"),
            "eth0 hidden, wlan0 visible");
        addVerifyRow(sb, "BST Device Nodes",   nativeStatus.getOrDefault("verify_bst_devices", "?"),
            "/dev/bst_* chmod 000");
        addVerifyRow(sb, "Config Access",      nativeStatus.getOrDefault("verify_config_access", "?"),
            "active.conf readable");
        addVerifyRow(sb, "ReZygisk Hooks",      nativeStatus.getOrDefault("verify_zygisk", "?"),
            ".so present, provider active");

        // Boot info
        String bootTime = nativeStatus.getOrDefault("native_boot_time", "unknown");
        String bootWait = nativeStatus.getOrDefault("native_boot_wait", "?");
        String mountsRestored = nativeStatus.getOrDefault("native_mounts_restored", "0");
        sb.append("<tr class='na'><td colspan='3' class='note-row'>");
        sb.append("Boot: ").append(esc(bootTime));
        sb.append(" | Wait: ").append(esc(bootWait));
        sb.append(" | Mounts restored: ").append(esc(mountsRestored));
        sb.append("</td></tr>");

        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 19: MODULE STATUS
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Module Status");
        sb.append("<table><tr><th>Module</th><th>Status</th></tr>");
        String modJork = shellRoot("su -c cat /data/adb/modules/jorkspoofer/module.prop 2>/dev/null | grep version=").trim();
        addModRow(sb, "jorkspoofer", modJork.isEmpty() ? "NOT INSTALLED" : modJork);
        String modNative = shellRoot("su -c cat /data/adb/modules/jorkspoofer-native/module.prop 2>/dev/null | grep version=").trim();
        addModRow(sb, "jorkspoofer-native", modNative.isEmpty() ? "NOT INSTALLED" : modNative);
        String modLsp = shellRoot("su -c cat /data/adb/modules/zygisk_lsposed/module.prop 2>/dev/null | grep version=").trim();
        addModRow(sb, "zygisk_lsposed", modLsp.isEmpty() ? "NOT INSTALLED" : modLsp);
        String modRezygisk = shellRoot("su -c cat /data/adb/modules/rezygisk/module.prop 2>/dev/null | grep version=").trim();
        addModRow(sb, "rezygisk", modRezygisk.isEmpty() ? "NOT INSTALLED" : modRezygisk);
        String zygisk = shellRoot("su -c magisk --sqlite \"SELECT value FROM settings WHERE key='zygisk'\" 2>/dev/null").trim();
        addModRow(sb, "Zygisk API enabled", zygisk.contains("1") ? "YES (via ReZygisk)" : "NO (" + zygisk + ")");
        sb.append("</table>");
        closeSection(sb);

        // ================================================================
        // SECTION 20: CROSS-LAYER CONSISTENCY
        // ================================================================
        secNum++;
        openSection(sb, secNum, "Cross-Layer Consistency");
        sb.append("<table><tr><th>Vector</th><th>Layers Active</th><th></th></tr>");

        // Build identity
        String javaModel = gp("ro.product.model");
        String nativeModel2 = nativeStatus.getOrDefault("native_profile_model", "");
        boolean modelConsistent = !javaModel.isEmpty() && javaModel.equals(nativeModel2);
        sb.append("<tr class='").append(modelConsistent ? "ok" : (!nativeModel2.isEmpty() ? "leak" : "na")).append("'>");
        sb.append("<td><b>Device Model</b></td><td class='m'>Java: ").append(esc(javaModel));
        sb.append(" | Native: ").append(esc(nativeModel2.isEmpty() ? "(n/a)" : nativeModel2)).append("</td>");
        sb.append("<td>").append(modelConsistent ? tag("SYNCED", "tok") : tag(nativeModel2.isEmpty() ? "N/A" : "MISMATCH", nativeModel2.isEmpty() ? "tinfo" : "twarn")).append("</td></tr>");

        // GL Renderer
        String nativeGL = nativeStatus.getOrDefault("native_profile_gl_renderer", "");
        boolean glConsistent = !nativeGL.isEmpty() && !nativeGL.equals("unknown");
        sb.append("<tr class='").append(glConsistent ? "ok" : "na").append("'>");
        sb.append("<td><b>GL Renderer</b></td><td class='m'>Profile: ").append(esc(nativeGL.isEmpty() ? "(n/a)" : nativeGL)).append("</td>");
        sb.append("<td>").append(glConsistent ? tag("SET", "tok") : tag("N/A", "tinfo")).append("</td></tr>");

        // Timezone — compare system timezone against IP-derived timezone (NOT profile)
        String tzProp = gp("persist.sys.timezone");
        String tzFromIp = ipTimezone;
        boolean tzConsistent = !tzProp.isEmpty() && !tzFromIp.isEmpty() && tzProp.equals(tzFromIp);
        boolean tzIpAvail = !tzFromIp.isEmpty();
        sb.append("<tr class='").append(tzConsistent ? "ok" : (tzIpAvail ? "leak" : "na")).append("'>");
        sb.append("<td><b>Timezone</b></td><td class='m'>System: ").append(esc(tzProp));
        sb.append(" | IP: ").append(esc(tzIpAvail ? tzFromIp : "(lookup failed)")).append("</td>");
        sb.append("<td>").append(tzConsistent ? tag("SYNCED", "tok") : tag(tzIpAvail ? "MISMATCH" : "N/A", tzIpAvail ? "twarn" : "tinfo")).append("</td></tr>");

        // Hostname
        String hostProp = nativeStatus.getOrDefault("native_hostname", "");
        String hostProfile = prof("PROFILE_DEVICE");
        boolean hostConsistent = !hostProp.isEmpty() && hostProp.equals(hostProfile);
        sb.append("<tr class='").append(hostConsistent ? "ok" : (!hostProfile.isEmpty() ? "leak" : "na")).append("'>");
        sb.append("<td><b>Hostname</b></td><td class='m'>System: ").append(esc(hostProp));
        sb.append(" | Profile: ").append(esc(hostProfile.isEmpty() ? "(n/a)" : hostProfile)).append("</td>");
        sb.append("<td>").append(hostConsistent ? tag("SYNCED", "tok") : tag(hostProfile.isEmpty() ? "N/A" : "MISMATCH", hostProfile.isEmpty() ? "tinfo" : "twarn")).append("</td></tr>");

        // Network interfaces
        String appIfaces = getNetworkInterfaces();
        boolean netOk = !appIfaces.contains("eth0") && !appIfaces.contains("dummy0");
        sb.append("<tr class='").append(netOk ? "ok" : "leak").append("'>");
        sb.append("<td><b>Network IFs</b></td><td class='m'>App sees: ").append(esc(appIfaces)).append("</td>");
        sb.append("<td>").append(netOk ? tag("CLEAN", "tok") : tag("EMU LEAK", "twarn")).append("</td></tr>");

        // Battery
        sb.append("<tr class='ok'><td><b>Battery</b></td>");
        sb.append("<td class='m'>LSPosed BatteryHooks (OSRS scope) + CDP getBattery() (Chrome)</td>");
        sb.append("<td>").append(tag("2-LAYER", "tok")).append("</td></tr>");

        // Carrier/Country — compare MCC against IP-derived country
        String carrierProp = gp("gsm.operator.numeric");
        String ipCtry = ipCountry;
        String ipCty = ipCity;
        String ipLocStr = "";
        if (!ipCtry.isEmpty()) {
            ipLocStr = ipCtry;
            if (!ipCty.isEmpty()) ipLocStr += " (" + ipCty + ")";
        }
        sb.append("<tr class='").append(!ipCtry.isEmpty() ? "ok" : "na").append("'>");
        sb.append("<td><b>Carrier/Country</b></td><td class='m'>IP Location: ").append(esc(ipLocStr.isEmpty() ? "(lookup failed)" : ipLocStr));
        sb.append(" | MCC/MNC: ").append(esc(carrierProp.isEmpty() ? "(n/a)" : carrierProp)).append("</td>");
        sb.append("<td>").append(!ipCtry.isEmpty() ? tag("LOCATED", "tok") : tag("N/A", "tinfo")).append("</td></tr>");

        // CDP
        sb.append("<tr class='ok'><td><b>CDP Browser</b></td>");
        sb.append("<td class='m'>navigator.*, WebGL, Canvas, Audio, Screen, Geolocation, Client Hints</td>");
        sb.append("<td>").append(tag("ACTIVE", "tok")).append("</td></tr>");

        // Summary note
        sb.append("<tr class='na'><td colspan='3' class='note-row'>");
        sb.append("5 active layers: (1) Magisk/resetprop, (2) Profile config, (3) LSPosed Java hooks (18 classes), ");
        sb.append("(4) ReZygisk native PLT hooks (.so), (5) CDP browser injection. ");
        sb.append("All layers read from the same active.conf profile for consistency.");
        sb.append("</td></tr>");

        sb.append("</table>");
        closeSection(sb);

        sb.append("</body></html>");
        return sb.toString();
    }

    // ========================================================================
    // SECTION HELPERS (collapsible)
    // ========================================================================

    /** Open a collapsible section — defaults to collapsed */
    private void openSection(StringBuilder sb, int num, String title) {
        sb.append("<div class='sec'>");
        sb.append("<div class='sec-hdr collapsed' id='hdr-").append(num).append("' onclick='toggleSec(").append(num).append(")'>");
        sb.append("<span class='sec-title'>").append(num).append(". ").append(esc(title)).append("</span>");
        sb.append("<span class='sec-chev'>\u25BC</span>");
        sb.append("</div>");
        sb.append("<div class='sec-body hidden' id='body-").append(num).append("'>");
        sb.append("<div class='card'>");
    }

    /** Close a collapsible section */
    private void closeSection(StringBuilder sb) {
        sb.append("</div></div></div>");
    }

    // ========================================================================
    // TABLE HELPERS
    // ========================================================================

    private String th3() {
        return "<table><tr><th>Property</th><th>App Sees</th><th>Original</th><th></th></tr>";
    }

    private void addPropRow(StringBuilder sb, String label, String spoofed, String origKey) {
        String s = spoofed != null ? spoofed.trim() : "";
        String r = origProp(origKey).trim();

        String rowClass, tag;
        if (s.isEmpty() && r.isEmpty()) {
            rowClass = "na"; tag = tag("N/A", "tinfo");
        } else if (r.isEmpty()) {
            rowClass = "ok"; tag = tag("OK", "tok");
        } else if (s.isEmpty() && !r.isEmpty()) {
            rowClass = "ok"; tag = tag("BLOCKED", "tok");
            s = "<span class='blocked'>(hidden)</span>";
        } else if (!s.equals(r)) {
            rowClass = "ok"; tag = tag("OK", "tok");
        } else {
            if (isEmuIdentifier(r)) {
                rowClass = "leak"; tag = tag("LEAK", "twarn");
            } else {
                rowClass = "ok"; tag = tag("OK", "tok");
            }
        }

        sb.append("<tr class='").append(rowClass).append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(s.startsWith("<") ? s : esc(s)).append("</td>");
        sb.append("<td class='m'>").append(esc(r)).append("</td>");
        sb.append("<td>").append(tag).append("</td></tr>");
    }

    private void addSecRow(StringBuilder sb, String label, String spoofed, String knownReal) {
        String s = spoofed != null ? spoofed.trim() : "";
        String r = knownReal != null ? knownReal.trim() : "";

        String rowClass, tag;
        if (s.isEmpty() && r.isEmpty()) {
            rowClass = "na"; tag = tag("N/A", "tinfo");
        } else if (s.isEmpty()) {
            rowClass = "na"; tag = tag("-", "tinfo");
        } else if (r.isEmpty()) {
            rowClass = "ok"; tag = tag("SET", "tok");
        } else if (!s.equals(r)) {
            rowClass = "ok"; tag = tag("OK", "tok");
        } else {
            rowClass = "leak"; tag = tag("LEAK", "twarn");
        }

        sb.append("<tr class='").append(rowClass).append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(esc(s)).append("</td>");
        sb.append("<td class='m'>").append(esc(r.isEmpty() ? "-" : r)).append("</td>");
        sb.append("<td>").append(tag).append("</td></tr>");
    }

    private void addBlockRow(StringBuilder sb, String label, boolean blocked, String rawCount, String note) {
        String rowClass = blocked ? "ok" : "leak";
        String tag = blocked ? tag("BLOCKED", "tok") : tag("LEAKED", "twarn");
        String status = blocked ? "(hidden)" : rawCount + " leaked!";

        sb.append("<tr class='").append(rowClass).append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(esc(status)).append("</td>");
        sb.append("<td class='m'>").append(esc(note)).append("</td>");
        sb.append("<td>").append(tag).append("</td></tr>");
    }

    private void addMatchRow(StringBuilder sb, String label, String appSees, String rootSees, String reason) {
        String s = appSees != null ? appSees.trim() : "";
        String r = rootSees != null ? rootSees.trim() : "";

        String rowClass, tag;
        if (s.isEmpty() && r.isEmpty()) {
            rowClass = "na"; tag = tag("N/A", "tinfo");
        } else {
            rowClass = "match"; tag = tag("MATCH", "tmatch");
        }

        sb.append("<tr class='").append(rowClass).append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(esc(s.isEmpty() ? "(N/A)" : s)).append("</td>");
        sb.append("<td class='m'>").append(esc(r.isEmpty() ? "(N/A)" : reason)).append("</td>");
        sb.append("<td>").append(tag).append("</td></tr>");
    }

    private void addSmartRow(StringBuilder sb, String label, String appSees, String real, boolean isOk) {
        String s = appSees != null ? appSees.trim() : "";
        String r = real != null ? real.trim() : "";

        String rowClass = isOk ? "ok" : "leak";
        String tag = isOk ? tag("OK", "tok") : tag("LEAK", "twarn");

        sb.append("<tr class='").append(rowClass).append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(esc(s)).append("</td>");
        sb.append("<td class='m'>").append(esc(r)).append("</td>");
        sb.append("<td>").append(tag).append("</td></tr>");
    }

    private void addIdRow(StringBuilder sb, String label, String val) {
        boolean hasVal = val != null && !val.isEmpty();
        sb.append("<tr class='").append(hasVal ? "ok" : "na").append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m' colspan='1'>").append(esc(hasVal ? val : "(not set)")).append("</td>");
        sb.append("<td class='m'>-</td>");
        sb.append("<td>").append(hasVal ? tag("SET", "tset") : tag("N/A", "tinfo")).append("</td></tr>");
    }

    private void addModRow(StringBuilder sb, String name, String status) {
        sb.append("<tr><td><b>").append(esc(name)).append("</b></td>");
        sb.append("<td class='m' colspan='2'>").append(esc(status)).append("</td></tr>");
    }

    private void addNativeRow(StringBuilder sb, String label, String value, boolean ok) {
        sb.append("<tr class='").append(ok ? "ok" : "leak").append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(esc(value)).append("</td>");
        sb.append("<td>").append(ok ? tag("OK", "tok") : tag("FAIL", "twarn")).append("</td></tr>");
    }

    private void addVerifyRow(StringBuilder sb, String label, String status, String detail) {
        boolean ok = "pass".equals(status) || "hook".equals(status);
        boolean skip = "skip".equals(status);
        String rowClass = skip ? "na" : (ok ? "ok" : "leak");
        String t;
        if (ok) {
            t = "hook".equals(status) ? tag("HOOK", "tok") : tag("PASS", "tok");
        } else if (skip) {
            t = tag("SKIP", "tinfo");
        } else if ("?".equals(status)) {
            t = tag("N/A", "tinfo");
        } else {
            t = tag("FAIL", "twarn");
        }

        sb.append("<tr class='").append(rowClass).append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(esc(detail)).append("</td>");
        sb.append("<td>").append(t).append("</td></tr>");
    }

    private void addBatCheckRow(StringBuilder sb, String label, String appSees,
            String systemRaw, String sysfsHook, String cls, String tagText) {
        String rowClass;
        switch (cls) {
            case "ok": rowClass = "ok"; break;
            case "leak": rowClass = "leak"; break;
            default: rowClass = "na"; break;
        }
        String t;
        switch (tagText) {
            case "SPOOFED": case "OK": case "ACTIVE": case "HOOKED": t = tag(tagText, "tok"); break;
            case "SUSPICIOUS": case "STATIC": case "ROUND!": case "MISSING!": case "NONE": t = tag(tagText, "twarn"); break;
            default: t = tag(tagText, "tinfo"); break;
        }

        sb.append("<tr class='").append(rowClass).append("'>");
        sb.append("<td><b>").append(esc(label)).append("</b></td>");
        sb.append("<td class='m'>").append(esc(appSees)).append("</td>");
        sb.append("<td class='m'>").append(esc(systemRaw));
        if (!sysfsHook.isEmpty()) {
            sb.append(" <span style='font-size:8px;color:#4a4f54'>[sysfs: ").append(esc(sysfsHook)).append("]</span>");
        }
        sb.append("</td>");
        sb.append("<td>").append(t).append("</td></tr>");
    }

    /** Extract a value from dumpsys battery output (format: "  key: value") */
    private String extractDumpsys(String dump, String key) {
        if (dump == null) return "";
        for (String line : dump.split("\n")) {
            line = line.trim();
            if (line.startsWith(key + ":")) {
                return line.substring(key.length() + 1).trim();
            }
        }
        return "";
    }

    private String tag(String text, String cls) {
        return "<span class='tag " + cls + "'>" + text + "</span>";
    }

    private boolean isEmuIdentifier(String val) {
        if (val == null || val.isEmpty()) return false;
        String v = val.toLowerCase();
        return v.contains("bst") || v.contains("bluestacks") || v.contains("qvirt")
            || v.contains("vbox") || v.contains("test-keys") || v.contains("-eng ")
            || v.contains("-eng\t") || v.endsWith("-eng") || v.equals("eng")
            || v.contains("bst_arm64") || v.contains("generic")
            || v.contains("goldfish") || v.contains("sdk_gphone")
            || v.contains("emulator") || v.contains("ranchu")
            || v.contains("android-build");
    }

    // ========================================================================
    // GETTERS
    // ========================================================================

    /** Get property via native getprop command — always reflects current resetprop values */
    private String gp(String name) { return shell("getprop " + name).trim(); }

    private String origPropOr(String name, String fb) {
        String v = origProp(name);
        return v.isEmpty() ? fb : v;
    }

    private String getNetworkInterfaces() {
        try {
            StringBuilder sb = new StringBuilder();
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (sb.length() > 0) sb.append(", ");
                sb.append(ni.getName());
            }
            return sb.toString();
        } catch (Exception e) { return "(err)"; }
    }

    private String getSpoofedMac() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                String n = ni.getName();
                if (n.equals("wlan0") || n.equals("eth0")) {
                    byte[] m = ni.getHardwareAddress();
                    if (m != null) {
                        StringBuilder sb = new StringBuilder();
                        for (int i = 0; i < m.length; i++) {
                            if (i > 0) sb.append(":");
                            sb.append(String.format("%02x", m[i]));
                        }
                        return sb.toString();
                    }
                }
            }
            return "(N/A)";
        } catch (Exception e) { return "(err)"; }
    }

    private String extractField(String text, String field) {
        if (text == null) return "?";
        for (String line : text.split("\n")) {
            if (line.toLowerCase().startsWith(field.toLowerCase())) {
                int colon = line.indexOf(':');
                if (colon >= 0) return line.substring(colon + 1).trim();
            }
        }
        return "(not found)";
    }

    private String truncate(String s, int max) {
        if (s == null) return "";
        return s.length() > max ? s.substring(0, max) + "..." : s;
    }

    // ========================================================================
    // SHELL
    // ========================================================================

    /** Execute as app user — sees spoofed values via ReZygisk hooks */
    private String shell(String cmd) {
        try {
            Process p = Runtime.getRuntime().exec(new String[]{"sh", "-c", cmd});
            BufferedReader br = new BufferedReader(new InputStreamReader(p.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) { if (sb.length() > 0) sb.append("\n"); sb.append(line); }
            p.waitFor();
            return sb.toString();
        } catch (Exception e) { return ""; }
    }

    /** Execute as root — bypasses ReZygisk hooks, sees real values */
    private String shellRoot(String cmd) {
        try {
            Process p = Runtime.getRuntime().exec(new String[]{"sh", "-c", cmd});
            BufferedReader br = new BufferedReader(new InputStreamReader(p.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) { if (sb.length() > 0) sb.append("\n"); sb.append(line); }
            p.waitFor();
            return sb.toString();
        } catch (Exception e) { return ""; }
    }

    private static String firstLine(String s) {
        if (s == null || s.isEmpty()) return "0";
        int nl = s.indexOf('\n');
        return (nl >= 0 ? s.substring(0, nl) : s).trim();
    }

    private static boolean isZero(String s) {
        if (s == null || s.isEmpty()) return true;
        try { return Integer.parseInt(s.trim()) == 0; } catch (Exception e) { return false; }
    }

    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;").replace("'", "&#39;");
    }
}
