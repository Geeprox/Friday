package local.tv.camera;

import android.app.Activity;
import android.net.Uri;
import android.view.View;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayInputStream;

/** 在已配对电视上承载同源游戏舞台。 */
final class GameScreen {
    final WebView view;
    private final Uri origin;

    GameScreen(Activity activity, String base, String key, LoadFailure onFailure) {
        origin = Uri.parse(base);
        view = new WebView(activity);
        view.setBackgroundColor(0xfff7f6f1);
        view.setFocusable(true);
        view.setOverScrollMode(View.OVER_SCROLL_NEVER);
        view.getSettings().setJavaScriptEnabled(true);
        view.getSettings().setAllowFileAccess(false);
        view.getSettings().setAllowContentAccess(false);
        view.getSettings().setJavaScriptCanOpenWindowsAutomatically(false);
        view.getSettings().setSupportMultipleWindows(false);
        view.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView webView, String url) {
                return !sameOrigin(Uri.parse(url));
            }

            @Override public boolean shouldOverrideUrlLoading(WebView webView, WebResourceRequest request) {
                return !sameOrigin(request.getUrl());
            }

            @Override public WebResourceResponse shouldInterceptRequest(WebView webView, WebResourceRequest request) {
                if (sameOrigin(request.getUrl())) return null;
                return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream(new byte[0]));
            }

            @Override public boolean onRenderProcessGone(WebView webView, RenderProcessGoneDetail detail) {
                onFailure.accept(webView);
                return true;
            }

            @Override public void onReceivedError(WebView webView, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) onFailure.accept(webView);
            }

            @Override public void onReceivedHttpError(WebView webView, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame()) onFailure.accept(webView);
            }
        });
        view.loadUrl(origin.buildUpon().path("/games").appendQueryParameter("tv", "1")
                .appendQueryParameter("k", key).build().toString());
    }

    interface LoadFailure {
        void accept(WebView view);
    }

    private boolean sameOrigin(Uri uri) {
        return origin.getScheme().equals(uri.getScheme())
                && origin.getHost().equals(uri.getHost()) && origin.getPort() == uri.getPort();
    }

    void close() {
        view.onPause();
        view.stopLoading();
        view.destroy();
    }
}
