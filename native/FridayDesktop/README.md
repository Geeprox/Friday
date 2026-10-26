# Friday for macOS

The SwiftUI client uses a user-owned household service and a local Console.
See ../../docs/FRIDAY-DESKTOP-BUILD.md for a fresh source build and explicit install.
Runtime state lives in the current user's Application Support/Friday directory.
The development App references the selected local Python environment; it is not
an independently redistributable runtime. Building never launches the service.
