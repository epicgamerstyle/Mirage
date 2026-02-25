package com.lukesmirage.webinject;

import android.webkit.WebView;
import android.webkit.WebViewClient;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage.LoadPackageParam;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.IOException;
import java.util.Collections;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Mirage WebView JS Injector — LSPosed/Xposed module.
 *
 * Reads /data/local/tmp/device_spoof.user.js and injects it into every
 * WebView page load so that browser-visible fingerprint properties match
 * the active device profile.
 *
 * Hook strategy:
 *   1. WebView.setWebViewClient()    → dynamically hook each custom client
 *   2. WebViewClient.onPageStarted() → inject AFTER (early property overrides)
 *   3. WebViewClient.onPageFinished()→ inject BEFORE app code runs (safety net)
 *
 * The JS is cached in memory and reloaded only when the file changes.
 * A guard variable (__mirage_injected) prevents double-execution per page.
 */
public class WebViewInjector implements IXposedHookLoadPackage {

    private static final String TAG = "[MirageInject]";
    private static final String JS_PATH = "/data/local/tmp/device_spoof.user.js";

    /** Cached JS content (null = not loaded yet) */
    private static volatile String cachedJS = null;
    /** Last-modified timestamp of the JS file when we cached it */
    private static volatile long cachedMtime = 0;

    /** Track which WebViewClient subclasses we've already hooked */
    private static final Set<String> hookedClients =
            Collections.newSetFromMap(new ConcurrentHashMap<String, Boolean>());

    // ------------------------------------------------------------------ //
    //  Entry point — called once per process by LSPosed
    // ------------------------------------------------------------------ //

    @Override
    public void handleLoadPackage(LoadPackageParam lpparam) throws Throwable {
        XposedBridge.log(TAG + " Loaded in: " + lpparam.packageName);

        // ----- Hook WebView.setWebViewClient() -----
        // Discover every concrete WebViewClient subclass at runtime and
        // hook its onPageStarted / onPageFinished dynamically.
        try {
            XposedHelpers.findAndHookMethod(
                    WebView.class,
                    "setWebViewClient",
                    WebViewClient.class,
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                            WebViewClient client = (WebViewClient) param.args[0];
                            if (client != null) {
                                hookClientClass(client.getClass());
                            }
                        }
                    }
            );
        } catch (Throwable t) {
            XposedBridge.log(TAG + " setWebViewClient hook failed: " + t);
        }

        // ----- Hook base WebViewClient.onPageStarted (early inject) -----
        try {
            XposedHelpers.findAndHookMethod(
                    WebViewClient.class,
                    "onPageStarted",
                    WebView.class,
                    String.class,
                    android.graphics.Bitmap.class,
                    new EarlyInjectHook("onPageStarted-base")
            );
        } catch (Throwable t) {
            XposedBridge.log(TAG + " base onPageStarted hook failed: " + t);
        }

        // ----- Hook base WebViewClient.onPageFinished (before app code) -----
        try {
            XposedHelpers.findAndHookMethod(
                    WebViewClient.class,
                    "onPageFinished",
                    WebView.class,
                    String.class,
                    new LateInjectHook("onPageFinished-base")
            );
        } catch (Throwable t) {
            XposedBridge.log(TAG + " base onPageFinished hook failed: " + t);
        }

        // ----- Hook WebView constructors to ensure JS is enabled -----
        hookWebViewConstructors();

        XposedBridge.log(TAG + " Hooks installed for: " + lpparam.packageName);
    }

    // ------------------------------------------------------------------ //
    //  WebView constructor hooks — ensure JavaScript is enabled
    // ------------------------------------------------------------------ //

    private void hookWebViewConstructors() {
        // 1-arg constructor: WebView(Context)
        try {
            XposedHelpers.findAndHookConstructor(
                    WebView.class,
                    android.content.Context.class,
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                            ((WebView) param.thisObject).getSettings().setJavaScriptEnabled(true);
                        }
                    }
            );
        } catch (Throwable ignored) {}

        // 2-arg constructor: WebView(Context, AttributeSet)
        try {
            XposedHelpers.findAndHookConstructor(
                    WebView.class,
                    android.content.Context.class,
                    android.util.AttributeSet.class,
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                            ((WebView) param.thisObject).getSettings().setJavaScriptEnabled(true);
                        }
                    }
            );
        } catch (Throwable ignored) {}

        // 3-arg constructor: WebView(Context, AttributeSet, int)
        try {
            XposedHelpers.findAndHookConstructor(
                    WebView.class,
                    android.content.Context.class,
                    android.util.AttributeSet.class,
                    int.class,
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                            ((WebView) param.thisObject).getSettings().setJavaScriptEnabled(true);
                        }
                    }
            );
        } catch (Throwable ignored) {}
    }

    // ------------------------------------------------------------------ //
    //  Dynamically hook concrete WebViewClient subclasses
    // ------------------------------------------------------------------ //

    private void hookClientClass(Class<?> clazz) {
        String name = clazz.getName();
        if (name.equals("android.webkit.WebViewClient")) return; // base already hooked
        if (!hookedClients.add(name)) return;                    // already hooked

        // Hook onPageStarted if the subclass overrides it
        try {
            XposedHelpers.findAndHookMethod(
                    clazz,
                    "onPageStarted",
                    WebView.class,
                    String.class,
                    android.graphics.Bitmap.class,
                    new EarlyInjectHook("onPageStarted-" + simpleName(name))
            );
        } catch (NoSuchMethodError ignored) {
            // Subclass doesn't override → base hook covers it
        } catch (Throwable t) {
            XposedBridge.log(TAG + " onPageStarted hook failed for " + name + ": " + t);
        }

        // Hook onPageFinished if the subclass overrides it
        try {
            XposedHelpers.findAndHookMethod(
                    clazz,
                    "onPageFinished",
                    WebView.class,
                    String.class,
                    new LateInjectHook("onPageFinished-" + simpleName(name))
            );
        } catch (NoSuchMethodError ignored) {
            // Subclass doesn't override → base hook covers it
        } catch (Throwable t) {
            XposedBridge.log(TAG + " onPageFinished hook failed for " + name + ": " + t);
        }

        XposedBridge.log(TAG + " Hooked client: " + name);
    }

    // ------------------------------------------------------------------ //
    //  Hook implementations
    // ------------------------------------------------------------------ //

    /**
     * Fires AFTER onPageStarted returns — sets up property overrides early,
     * before page content JS executes.
     */
    private static class EarlyInjectHook extends XC_MethodHook {
        private final String label;

        EarlyInjectHook(String label) {
            this.label = label;
        }

        @Override
        protected void afterHookedMethod(MethodHookParam param) throws Throwable {
            injectJS(param, label);
        }
    }

    /**
     * Fires BEFORE onPageFinished runs — ensures spoofing is in place
     * before the app's own onPageFinished code reads browser values.
     */
    private static class LateInjectHook extends XC_MethodHook {
        private final String label;

        LateInjectHook(String label) {
            this.label = label;
        }

        @Override
        protected void beforeHookedMethod(MethodHookParam param) throws Throwable {
            injectJS(param, label);
        }
    }

    /** Shared injection logic */
    private static void injectJS(XC_MethodHook.MethodHookParam param, String label) {
        try {
            WebView webView = (WebView) param.args[0];

            String js = loadJS();
            if (js == null || js.isEmpty()) return;

            // Wrap with guard to prevent double-injection per page
            String wrapped =
                    "if(!window.__mirage_injected){" +
                    "window.__mirage_injected=true;" +
                    js +
                    "}";

            webView.evaluateJavascript(wrapped, null);
            XposedBridge.log(TAG + " Injected (" + label + ")");
        } catch (Throwable t) {
            XposedBridge.log(TAG + " Inject failed (" + label + "): " + t);
        }
    }

    // ------------------------------------------------------------------ //
    //  JS file loading with caching
    // ------------------------------------------------------------------ //

    /**
     * Read and cache the JS file.  Returns cached content unless the file
     * has been modified since the last read.
     */
    private static String loadJS() {
        try {
            File f = new File(JS_PATH);
            if (!f.exists() || !f.canRead()) return null;

            long mtime = f.lastModified();
            // Return cache if file hasn't changed
            if (cachedJS != null && mtime == cachedMtime) return cachedJS;

            // Read the file
            StringBuilder sb = new StringBuilder((int) f.length());
            BufferedReader br = new BufferedReader(new FileReader(f), 16384);
            try {
                char[] buf = new char[8192];
                int n;
                while ((n = br.read(buf)) != -1) {
                    sb.append(buf, 0, n);
                }
            } finally {
                br.close();
            }

            String content = sb.toString();

            // Strip UserScript header block if present
            int endTag = content.indexOf("// ==/UserScript==");
            if (endTag >= 0) {
                int newline = content.indexOf('\n', endTag);
                if (newline >= 0) {
                    content = content.substring(newline + 1);
                }
            }

            content = content.trim();
            cachedMtime = mtime;
            cachedJS = content;

            XposedBridge.log(TAG + " Loaded JS: " + content.length() + " chars");
            return content;
        } catch (IOException e) {
            XposedBridge.log(TAG + " Failed to read JS: " + e.getMessage());
            return null;
        }
    }

    // ------------------------------------------------------------------ //
    //  Utility
    // ------------------------------------------------------------------ //

    private static String simpleName(String fqcn) {
        int dot = fqcn.lastIndexOf('.');
        return dot >= 0 ? fqcn.substring(dot + 1) : fqcn;
    }
}
