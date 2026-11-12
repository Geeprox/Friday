# LG TV menu bar for macOS

A standalone macOS 14+ menu bar utility for the LG webOS TV you choose. It uses
local webOS SSAP for power-off and Wake-on-LAN for power-on. An optional local
audio component maps native macOS volume controls to the TV's hardware volume.
It does not start Friday, Home Assistant, cameras, models, or other household services.
It deliberately runs without a Dock icon. Closing settings keeps the menu item
and wake observer running; Quit stops both.

## Native system volume

HDMI commonly exposes no writable volume control. Enable **系统音量** in settings:
install the bundled **LG TV 音量** component using macOS Installer, explicitly
grant TV audio control on the TV, select the matching HDMI audio output, then
enable **使用系统声音滑块控制电视**. The existing power-only credential is retained
until the new pairing succeeds. The feature initially reads the TV's actual volume;
it never initializes the TV from a cached Mac volume.

The system output becomes **LG TV 音量**. The standard Sound slider and media keys
write its HAL controls; the app coalesces these into `audio/setVolume` / `setMute`
and reads the TV back. Separate `getVolume` / `getStatus` subscriptions update
the native controls after remote changes. Reported updates do not generate new
commands. Switching to built-in speakers or headphones leaves their controls to
macOS. Selecting the bound HDMI output reactivates the bridge while enabled.
Each adjustment burst uses a fresh write connection. An interrupted absolute
volume/mute command can reconnect once and retain the newest queued adjustment.

Only stereo Float32 at 44.1/48 kHz is supported. Multichannel/encoded passthrough
is rejected. PCM is forwarded unchanged to HDMI; volume is adjusted in the TV,
not by software gain. Audio stays in a bounded memory ring and is never stored,
logged, sent over the network, or read from a physical microphone. macOS may
request audio-input permission because the app reads its own virtual loopback
input. The private aggregate contains only that input and the selected HDMI
device, whose input-channel count must be zero. macOS microphone permission is
requested before constructing the route. This does not start an AVCapture
session or select a physical input.

The HAL plug-in is derived from Apple's licensed sample and performs no client
HAL or networking calls. A user-session forwarder uses a private aggregate with
HDMI as its clock and drift compensation on the virtual device. A separate pipe
watchdog restores output after app failure. Normal quit/disable restores HDMI
only if the user has not selected another output. Connection loss makes controls
unavailable and attempts to restore the physical output; volume requests never
wake the TV. The aggregate uses concatenated mode (`stacked=0`); on the tested
HAL, `stacked=1` produced an output-only multi-output device without a virtual
input. Construction verifies membership, two input/four output channels, format
and channel order before selection. Failed configurations do not retry in
response to hardware events from their own temporary aggregate.

The local installer contains a signed universal HAL bundle. Installing or removing
it requires an administrator and restarts Core Audio, briefly interrupting all
audio. It touches only `LGTVAudio.driver`; existing third-party components remain
unchanged. The companion `Uninstall-LGTVVolume.pkg` is bundled in the app's
`Contents/Resources` and built under `.artifacts/lg-tv-menu/audio/`. Disable the
bridge before uninstalling. Previous versions are retained locally under
`/Library/Application Support/LGTVMenu/AudioBackups/` when upgrading. Local
packages are not notarized distribution releases.

Synthetic tests exercise driver intent/report isolation, volume/mute gates,
unchanged PCM, overwritten ring frames, and aggregate channel routing. Those
checks do not establish audible playback, native-slider behavior, remote sync,
or sleep/wake behavior; those require separate installed-device acceptance.

## Build and use

```sh
./native/LGTVMenu/script/build_and_run.sh --test
./native/LGTVMenu/script/build_and_run.sh --build-only
python3 native/LGTVMenu/script/install_local_app.py
./native/LGTVMenu/script/build_and_run.sh --verify
```

Builds stage `.artifacts/lg-tv-menu/LG TV.app`. No dependencies are downloaded.
The zero-argument local installer validates signatures and retains a rollback
copy. It does not change pairing or install the HAL component. Its receipts stay
under ignored artifacts.
For durable login startup, put the bundle in Applications, launch that copy,
then enable **登录时启动 LG TV**. macOS may require approval in Login Items or
Local Network settings. Signing defaults to ad hoc. For stable local updates,
set `LGTV_SIGNING_IDENTITY` to an existing Apple-issued signing identity or pin it
in the ignored `.artifacts/lg-tv-menu/signing-identity` file. The local bundle is
not notarized. Changing the signing identity may require explicit re-pairing.

1. Keep the TV on and on the same LAN. Open **电视设置…**, discover it, and
   explicitly select the correct TV. Multiple TVs are never selected for you.
   Manual private IPv4 entry is available if SSDP discovery is blocked.
2. Click **配对电视**, then use the TV remote to allow the pairing prompt.
   Pairing does not send a power command. Encrypted webOS port 3001 is the
   default; the explicit compatibility option uses unencrypted port 3000.
3. The app attempts to read the active network interface MAC. If unavailable,
   enter it locally from TV network settings. Recheck it when switching between
   Ethernet and Wi-Fi. Reserve the TV's IP in your router to avoid DHCP changes.
4. Enable the TV's network power-on setting. Depending on model, this is
   **TV On With Mobile / Turn On via Wi-Fi**, **LG Connect Apps**, or
   **Support → IP control settings → Wake on LAN**. Some models require Ethernet.
5. Test **关闭电视**, then **打开电视**. Finally sleep and wake the Mac while
   the app stays running. Check the actual TV picture for each step.

After pairing and identifying a valid MAC, **随 Mac 唤醒** is enabled for the
behavior requested by this utility. It can be disabled immediately in the menu.
Login startup is separate and opt in. Auto power-on requires an observed Mac
sleep, system wake and screen wake; launching, switching user sessions, waking
only the display, and a dark wake do not trigger it. Notifications are deduplicated.
Retries are bounded and wait for the local network to recover. Manual power-off
cancels pending wake work and suppresses that wake cycle until the next sleep.
The app does not put the Mac to sleep. The separate automatic power-off option
below handles actual display sleep, system sleep and lock-screen transitions.
Wake handling runs in the existing logged-in user session while its screen is
locked; it does not wait for an unlock notification. It cannot run before the
first login after a restart. WOL itself does not require the stored pairing key.

### Turn off with display sleep, system sleep or the lock screen

Enable **息屏、睡眠或锁屏时关闭电视** in settings. This separate option requires
a paired TV with a valid network MAC so the display can also be restored.
A lock-screen notification schedules power-off after two seconds. Unlocking
during that delay cancels it. Screen-saver video is tracked separately through
its start/stop events: a security-lock notification while the saver is playing
does not close the TV, and a saver start cancels a pending lock-only shutdown.
This covers either notification order. When the saver stops at a still-locked
password screen, the normal two-second lock delay resumes. Returning directly
to the unlocked desktop cancels it. Merely launching the app does not request
power-off.

Actual display sleep overrides screen-saver playback and requests power-off.
System sleep does likewise through IORegisterForSystemPower. A sleep
acknowledgement allows up to four seconds for the existing local TV command,
then always permits sleep, including on connection failure or an unresponsive
network callback. No sleep denial, persistent assertion or network retry keeps
the Mac awake. Duplicate display/system/lock events share one off request.

After this automatic power-off, new keyboard/mouse activity or an unlock
requests WOL, including while the password screen is still locked. The app
queries only IOHIDSystem's elapsed idle time twice a second while awaiting the
user's return from that automatic shutdown. It installs no event tap, reads no keys or pointer coordinates, and
does not request Accessibility or Input Monitoring. It ignores the initial
shortcut release and deduplicates lock, display and system-wake notifications.
Manual power-off overrides the pending restoration. The existing sleep/wake
option remains separate. Quit stops observers and releases any pending sleep
acknowledgement. Screen-saver playback, real lock, display sleep, system sleep
and audio after restoration each need installed-device physical acceptance.

The lock/unlock notification names are used by
[Hammerspoon's screen-lock watcher](https://github.com/Hammerspoon/hammerspoon/blob/master/extensions/caffeinate/libcaffeinate_watcher.m).
They are not a documented Apple API contract. Installed lock, pre-password
restoration, and post-lock audio behavior require physical acceptance separately
from synthetic policy tests.

## MAC, SDDP and pairing

The MAC field can stay empty for pairing and power-off. It is needed only for
Wake-on-LAN. Pairing attempts to read it from the active TV network interface;
fill it in locally afterward if that read is unavailable.

SDDP is Control4's Simple Device Discovery Protocol. It is distinct from SSDP,
which this app uses for automatic webOS discovery. Manual IP pairing uses neither
discovery protocol. The TV's SDDP switch is not a requirement for this utility.
See [Control4's protocol description](https://www.control4.com/for/partners).

Network wake settings allow the TV to retain wake capability in standby. A TV
pairing prompt still requires the Mac to reach the webOS control interface first.
Device isolation or firewall rules can prevent this even when the IP is correct.

## Privacy and verification

The TV pairing key is stored only in the current user's macOS Keychain. Target
identity, private IP, active NIC MAC, certificate pin and preferences use a local
app preferences. They are not written to source, test receipts or logs. TLS
trust on first use occurs only during explicit pairing; later connections pin
that certificate. Each session checks the saved webOS device UUID before using
the saved key or sending a power request. Rediscovery or explicit re-pairing is
needed after address/certificate changes. Removing pairing deletes the current
key locally; revoke old authorizations separately in TV settings if desired.
Keychain work runs on a bounded background queue without password dialogs. If
credentials are unavailable while locked, WOL retries still run; authenticated
state verification remains unavailable until access is restored or re-pairing.
Explicit re-pairing stores credentials in a fresh account, independent of the TV's
discovery identity, and verifies readback before saving the new configuration.
Previous credential items are retained as recovery data. Older configurations
continue using their original account until explicitly re-paired.

Automatic power-on sends UDP 9 to the selected TV IP and its subnet broadcast.
Each socket binds to the matching LAN interface and source address; it never
falls back to an unbound socket or sends a power-toggle command. This also
addresses [a first-hand LG C4 / Mac unicast wake report](https://www.reddit.com/r/OLED_Gaming/comments/1fr2w6e/).
Interface selection and local send results appear in the macOS unified log
without addresses, MACs, pairing keys or device IDs.
Successful UDP send, an SSAP acknowledgment, and a TV's reported `Active` state
are separate from physical acceptance. Offline status does not prove power-off.
The settings panel reports uncertainty explicitly. Tests use synthetic protocol
messages plus loopback-only UDP transport checks and do not discover, pair,
wake, shut down, or otherwise operate a TV.
Send/receive deadlines return independently of URLSession cancellation callbacks.
Tests also cover a callback that never returns, parent cancellation and late replies.

Protocol references: [LG network power-on settings](https://www.lg.com/us/support/help-library/lg-tv-how-to-set-up-the-lg-thinq-app-on-your-lg-smart-tv--20152745625356),
[Home Assistant webOS documentation](https://www.home-assistant.io/integrations/webostv/),
and the [aiowebostv SSAP implementation](https://github.com/home-assistant-libs/aiowebostv).
This utility has no runtime dependency on those projects and is not an LG product.
