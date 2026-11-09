import AppKit
import WebKit
import Darwin

// One-time first-party website authorization. No JS injection, token in argv,
// credential logging, browser-cookie extraction or client-window automation.
final class LoginApp: NSObject, NSApplicationDelegate, WKHTTPCookieStoreObserver, WKNavigationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var label: NSTextField!
    var saved = false
    let destination: URL
    init(destination: URL) { self.destination = destination }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        config.websiteDataStore.httpCookieStore.add(self)
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 1120,height: 820), styleMask: [.titled,.closable,.resizable,.miniaturizable], backing: .buffered, defer: false)
        window.title = "Friday KTV · 网易云官方网页授权"
        window.delegate = self
        let content = NSView()
        label = NSTextField(wrappingLabelWithString: "请在下方网易云官方网站登录。成功后，仅将下载所需会话保存到这台 Mac。")
        label.font = .systemFont(ofSize: 14)
        label.translatesAutoresizingMaskIntoConstraints = false
        web.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(label); content.addSubview(web)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            label.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            label.topAnchor.constraint(equalTo: content.topAnchor, constant: 16),
            label.heightAnchor.constraint(equalToConstant: 44),
            web.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 12),
            web.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            web.bottomAnchor.constraint(equalTo: content.bottomAnchor)])
        window.contentView = content; window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.load(URLRequest(url: URL(string: "https://music.163.com/")!))
    }
    func cookiesDidChange(in cookieStore: WKHTTPCookieStore) {
        guard !saved else { return }
        cookieStore.getAllCookies { cookies in
            let names: Set<String> = ["MUSIC_U", "__csrf", "NMTID"]
            let own = cookies.filter { ($0.domain == "music.163.com" || $0.domain == ".music.163.com") && names.contains($0.name) }
            guard own.contains(where: { $0.name == "MUSIC_U" && !$0.value.isEmpty }) else { return }
            do {
                let payload: [String: Any] = ["cookie": own.map { "\($0.name)=\($0.value)" }.joined(separator: "; "),
                    "created": Int(Date().timeIntervalSince1970), "authorization": "official_website"]
                let data = try JSONSerialization.data(withJSONObject: payload)
                try data.write(to: self.destination, options: .atomic)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: self.destination.path)
                self.saved = true
                self.label.stringValue = "已完成本机授权。此窗口可以关闭，点歌后将由后台接口下载。"
                // Close only this short-lived authorization helper; Friday service stays running.
                DispatchQueue.main.asyncAfter(deadline: .now()+2) { NSApp.terminate(nil) }
            } catch { self.label.stringValue = "会话未保存，请关闭窗口并检查本机目录权限。" }
        }
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.targetFrame?.isMainFrame != false, let url = navigationAction.request.url {
            let host = url.host ?? ""
            guard url.scheme == "https", host == "music.163.com" || host == "passport.163.com" || host == "reg.163.com" else { decisionHandler(.cancel); return }
        }
        decisionHandler(.allow)
    }
    func windowWillClose(_ notification: Notification) { NSApp.terminate(nil) }
}
umask(0o077)
guard let argument = CommandLine.arguments.dropFirst().first(where: { $0.hasSuffix("/session.json") }) else { exit(2) }
let path = URL(fileURLWithPath: argument).standardizedFileURL
guard path.lastPathComponent == "session.json", FileManager.default.fileExists(atPath: path.deletingLastPathComponent().path) else { exit(2) }
let app = NSApplication.shared
let delegate = LoginApp(destination: path)
app.delegate = delegate
app.run()
