# Driver source

Derived from Apple’s “Creating an Audio Server Driver Plug-in” sample (2024).

Source: https://docs-assets.developer.apple.com/published/430ad6501f6f/CreatingAnAudioServerDriverPlugIn.zip

Archive SHA-256: `860334dfa9e83f2afb749a890ca5a79fc7d3d572d4e116ff9242bec458be4cb0`

Apple’s permissive license is retained in APPLE-LICENSE.txt. Changes add a bounded
in-memory stereo loopback, TV volume intent/report properties, a unique bundle
identity, and output-only default-device eligibility. No client HAL calls,
network traffic, files, credentials or physical microphone capture occur in the
driver. The user app performs physical HDMI forwarding and TV control.
