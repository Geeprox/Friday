import SwiftUI
import WebKit

struct ConsoleView: NSViewRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.uiDelegate = context.coordinator
        view.load(URLRequest(url: url))
        return view
    }
    func updateNSView(_ view: WKWebView, context: Context) {}
    static func dismantleNSView(_ view: WKWebView, coordinator: Coordinator) {
        view.stopLoading()
        coordinator.windows.forEach { $0.close() }
    }

    final class Coordinator: NSObject, WKUIDelegate, NSWindowDelegate {
        var windows: [NSWindow] = []
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            guard windows.count < 2, let url = navigationAction.request.url, url.scheme == "http",
                  url.host == "127.0.0.1", url.port == 8860,
                  ["/", "/games"].contains(url.path) else { return nil }
            configuration.websiteDataStore = .nonPersistent()
            let game = WKWebView(frame: .zero, configuration: configuration)
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = "Friday · 游戏"
            window.isReleasedWhenClosed = false
            window.contentView = game
            window.delegate = self
            windows.append(window)
            window.center()
            window.makeKeyAndOrderFront(nil)
            return game
        }
        func windowWillClose(_ notification: Notification) {
            guard let window = notification.object as? NSWindow else { return }
            (window.contentView as? WKWebView)?.stopLoading()
            windows.removeAll { $0 === window }
        }
    }
}
