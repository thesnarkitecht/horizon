// Horizon.app: a native window onto the `horizon app` dashboard, which it starts and stops.
// Built by install.sh with: swiftc -O Horizon.swift -o Horizon.app/Contents/MacOS/Horizon
import Cocoa
import WebKit

class App: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    let url = URL(string: "http://127.0.0.1:7777")!

    func applicationDidFinishLaunching(_ n: Notification) {
        let p = Process()  // login shell so python3 and mlx tools are on the user's PATH
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "exec python3 ~/.horizon/bin/horizon.py app --no-open"]
        try? p.run()  // exits at once if a dashboard is already running; the window still connects to it
        server = p

        web = WKWebView()
        web.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 760),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Horizon"
        window.contentView = web
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.load(URLRequest(url: url))
    }

    // The server takes a moment to bind on first launch: retry until it answers.
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { w.load(URLRequest(url: self.url)) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ n: Notification) { server?.terminate() }  // runs keep going; only the dashboard stops
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
