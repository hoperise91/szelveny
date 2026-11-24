// Prints how macOS rates the current connection: "online|offline normal|expensive unlimited|low-data".
// Expensive = iPhone Personal Hotspot (set by macOS); low-data = Low Data Mode on that Wi-Fi network.
// Used by weekly-fetch.sh to skip large downloads on hotspots.
import Foundation
import Network
let monitor = NWPathMonitor()
let done = DispatchSemaphore(value: 0)
monitor.pathUpdateHandler = { path in
    print(path.status == .satisfied ? "online" : "offline",
          path.isExpensive ? "expensive" : "normal",
          path.isConstrained ? "low-data" : "unlimited")
    done.signal()
}
monitor.start(queue: .global())
_ = done.wait(timeout: .now() + 5)
