# Reverse-Engineering a Cheap BLE RGB Lamp from Scratch

*A journey through Bluetooth sniffing, Android DEX bytecode, and Dalvik disassembly.*

---

## The Starting Point

It started with a simple question: I have a USB-powered RGB desk lamp. It glows. It has a little remote control. And according to the box, it speaks Bluetooth — controllable via an Android app called **MR Star**. Could I cut out the middleman and drive it directly from Linux?

The lamp advertised itself over BLE as `GATT--DEMO`, MAC address `FF:24:03:18:45:51`. That name alone — `GATT--DEMO` — screamed "generic firmware, probably used in a hundred different gadgets." The GATT service table was minimal: one service (`FFF0`) with two characteristics — `fff3` (write, labelled `"Commond"` in its descriptor, typo included) and `fff4` (notify, labelled `"Response"`). There was also a second service that turned out to be OTA firmware update territory. Simple enough on the surface.

---

## First Attempts: Throwing Standard Protocols at the Wall

The obvious first move was to try the well-known RGB lamp protocols. These lamps are usually running one of a handful of generic firmwares, and the commands are documented all over the internet:

- **ELK-BLEDOM** (9-byte format: `7E ... EF`) — nothing.
- **ZJ-MBL** (7-byte format: `56 RR GG BB 00 F0 AA`) — nothing.
- **Generic CC commands** (`CC 23 33` for power on, `CC 24 33` for off) — nothing.

The lamp sat there, indifferent. `btmon` confirmed the writes were actually being delivered to the device — the lamp was receiving our packets and ignoring every single one. So it wasn't a connection problem. The protocol was simply wrong.

---

## Getting the Ground Truth: Sniffing the Phone

If the app works, the protocol is in there somewhere. The plan: enable Android's HCI snoop log, connect MR Star to the lamp, do some things, pull the log.

This turned out to be more annoying than expected. The first attempt produced a `.btsnooz` file with filtering enabled — all the ACL payload data was stripped. Second attempt: disabled all filters. Third attempt: the phone wasn't recognised by `adb` because the cable was charge-only. Fourth attempt, finally with a data cable and unfiltered logging: a real capture.

Parsing the btsnoop with a custom Python script, we found connection handle `0x0041` which belonged to the lamp (identified by correlating the connection timestamp with when MR Star connected). The phone was sending one unique pattern to `fff3`:

```
bc 04 06
```

Just three bytes — repeated continuously. The problem: `orig_len=22` vs `inc_len=15`. The capture was *still* truncated. The ATT Write Command (`0x52`) header eats 3 bytes, leaving 12 for the payload, but the snooper was only capturing 12 bytes total, giving us only 3 bytes of actual payload. We were seeing `bc 04 06` and nothing else.

So the snoop told us the *start* of the command but not the rest. Not enough to work with directly — but it confirmed the start byte (`0xBC`) and the command type (`0x04`).

---

## Going Deeper: APK Extraction and DEX Analysis

If the snoop wouldn't give us the full packet, the app itself would. We pulled the APK from the phone:

```bash
adb shell pm path com.frok.mrstar
adb pull /data/app/.../base.apk /tmp/mrstar.apk
unzip /tmp/mrstar.apk -d /tmp/mrstar_apk/
```

`jadx` was installed to decompile it, but the binary had `@@HOMEBREW_PREFIX@@` placeholders that weren't substituted — a broken install. DNS was also resolving nothing (GitHub, Fedora mirrors, all dead). So we went lower-level: wrote a Python DEX parser from scratch.

The DEX format is well-documented. We parsed the string table, type table, method ID table, and class definitions manually. A scan for **fill-array-data payloads** — the DEX mechanism for initialising static byte arrays — found 13 arrays starting with `0xBC` and ending with `0x55`. These were the 5-byte commands:

```python
bc 01 01 00 55  # power off
bc 01 01 01 55  # power on
bc 0f 01 00 55  # unknown
bc 0f 01 01 55  # unknown
bc 11 01 01 55  # mode 1
bc 11 01 02 55  # mode 2
bc 11 01 03 55  # mode 3
bc 11 01 04 55  # mode 4
bc 07 01 00 55  # unknown
bc 07 01 01 55  # unknown
bc 0c 01 01 55  # ???
```

We sent all of them to the lamp. Nothing happened for a while — then `bc 0c 01 01 55` made the lamp go into a **colour cycle mode**, throwing out every colour in sequence. First confirmed working command. The protocol was real.

But there was no static RGB colour command in those arrays. The colour-setting command had to be constructed dynamically.

---

## Reading Dalvik Bytecode

We scanned the entire `classes.dex` for the pattern `13 XX BC FF` — the Dalvik encoding of `const/16 vX, 0xBC` (loading the start byte as a constant). Found 17 occurrences. Each one was the beginning of a BLE command being assembled on the stack.

Using the method map (matching instruction offsets back to class definitions), we found the interesting ones were in:

- `AdjustFragment.sendColorByRGB()`
- `AdjustFragment.sendColorByMode1()`
- `AdjustFragment.sendColorById()`
- `VoiceUtils.sendRgbColor()`
- `MusicFragment.sendArgbColor()`

We wrote a register-tracing Dalvik disassembler in Python. The key insight came from tracing `sendColorByRGB()`:

```
new-array v4, 10, byte[]          # 10-byte command buffer
const/16 v5, 0xBC
aput-byte arr[0] = 0xBC           # start marker
aput-byte arr[1] = 4              # command type
aput-byte arr[2] = 6              # sub-command
... arithmetic ...
aput-byte arr[3] = H >> 8         # hue high byte
aput-byte arr[4] = H & 0xFF       # hue low byte
aput-byte arr[5] = S >> 8         # saturation high byte
aput-byte arr[6] = S & 0xFF       # saturation low byte
aput-byte arr[7] = 0
aput-byte arr[8] = 0
aput-byte arr[9] = 0x55           # end marker
invoke-virtual MyApplication.sendDataToBle()
```

The method called `RgbUtils.getHsvFromRgb()` first (method ID `0x61a5`), converting the user's picked RGB colour to HSV. Then it multiplied the saturation float by **1000.0** (the constant `0x447A0000` in IEEE 754) before doing a `float-to-int` cast, turning S=1.0 into the integer 1000. Hue came through as a direct `float-to-int` of the 0–360 degree value.

The final command format:

```
bc 04 06 [H >> 8] [H & 0xFF] [S*1000 >> 8] [S*1000 & 0xFF] 00 00 55
```

---

## The Mistakes

A few wrong turns worth noting:

**Wrong byte position for channel test.** When probing which byte controlled which colour, an early test assumed `bc 09 06 ff 00 00 00 00 00 55` would be red (first colour byte = red). It turned out to produce *white*. The red command was `bc 09 06 00 ff 00 00 00 00 00 55` — byte[4], not byte[3]. This was command type `0x09` (voice mode), which uses a completely different byte layout from the main colour command type `0x04`.

**Assuming command 0x09 used raw RGB.** `VoiceUtils.sendRgbColor()` uses command byte `0x09` but the bytes at positions 3–6 are *not* straight R, G, B, W. They're an interleaved mix of object field reads and HSV components. Testing byte[4]=0xFF gave red (H≈360°≈0°), and any position with byte=0xFF that wasn't the hue byte gave white (no saturation). Sweeping hue values through command `0x09` showed only white regardless of the value — it needs saturation set simultaneously, which we weren't doing.

**Thinking the btsnoop capture was complete.** The first few captures were either filtered (ACL payload stripped), truncated (only 3 bytes of the actual command visible), or belonged to other connected devices. It took four separate capture attempts to get anything useful, and even then the payload truncation meant we could only verify the start bytes.

**jadx install.** Homebrew installed jadx but left `@@HOMEBREW_PREFIX@@` tokens in the wrapper script unsubstituted, making it non-functional. Wasted time debugging the tool rather than the target. The Python DEX parser turned out to be more informative anyway.

---

## Verification

Once the format was clear, three commands confirmed it:

```python
# Red:   H=0°,   S=100%  →  S*1000=1000=0x03E8
bc 04 06 00 00 03 e8 00 00 55

# Green: H=120°, S=100%
bc 04 06 00 78 03 e8 00 00 55

# Blue:  H=240°, S=100%
bc 04 06 00 f0 03 e8 00 00 55
```

The lamp turned red, then green, then blue. Done.

---

## What We Built

`lamp.py` — a command-line controller:

```bash
python3 lamp.py red
python3 lamp.py rgb 255 80 0
python3 lamp.py hsv 200 80
python3 lamp.py cycle
python3 lamp.py off
```

It converts RGB input to HSV internally via `colorsys`, builds the 10-byte command, connects to the lamp over BLE using `bleak`, and sends it. Total dependency: just `bleak`.

The whole thing — from "I have a lamp" to working colour control — required: one broken USB cable, four btsnoop capture attempts, one broken jadx install, a custom DEX string parser, a custom fill-array-data scanner, a register-tracing Dalvik disassembler, and a lot of patience with byte positions.

---

## Part Two: Ambient Mode

With the protocol cracked, the obvious next step was ambient lighting: read the average colour of the screen and continuously update the lamp to match. Simple idea. The implementation turned into its own debugging odyssey.

### First Attempt: `mss`

`mss` is a minimal cross-platform screen capture library that calls `XGetImage()` under the hood. It works fine on X11. On GNOME under Wayland — which is what this machine runs — it produces:

```
mss.exception.ScreenShotError: XGetImage() failed
```

The GNOME compositor doesn't expose its framebuffer through XWayland. So mss was out, and so was anything else going through X11.

### The Right Path: Mutter ScreenCast API

GNOME exposes a private D-Bus API — `org.gnome.Mutter.ScreenCast` — that lets you capture the screen without a user-visible dialog (unlike the XDG portal, which pops up a picker every time). The flow:

1. `CreateSession` → get a session object path
2. `RecordMonitor` on the session → get a stream object path
3. Subscribe to `PipeWireStreamAdded` on the stream
4. Call `Start` on the session
5. Wait for the signal → receive a PipeWire `node_id`

That `node_id` is a node in the PipeWire graph being pushed to by gnome-shell. You then connect your own PipeWire client to it as a consumer.

GStreamer's `pipewiresrc` element should theoretically do the consumer side, but it consistently failed on this system (`PipeWire 1.4.10`, `GStreamer 1.26.11`) with `target not found` errors regardless of how the target-object property was set. So we went lower: ctypes bindings directly against `libpipewire-0.3.so`.

### PipeWire via ctypes

`pw_capture.py` implements a PipeWire stream consumer without WirePlumber or any higher-level middleware:

1. `pw_init()`, `pw_thread_loop_new()`, `pw_context_new()`, `pw_core_connect()`
2. Create stream with `pw_stream_new()` and properties:
   ```
   media.type=Video
   media.category=Capture
   media.role=Screen
   media.class=Stream/Input/Video
   ```
   The `media.class` matters: without it, the stream registers as `Stream/Output/Video` in the graph, and link creation fails silently.
3. `pw_stream_connect()` with `SPA_ID_INVALID` as the target (no AUTOCONNECT), then wait for the `paused` state event.
4. After a 500 ms delay (for port registration to propagate in the global graph), call `pw_core_create_object("link-factory", ...)` to wire gnome-shell's output node to our input node.

The 500 ms delay is load-bearing. Without it, the link-factory call says "unknown input port (null)" because our node's ports haven't shown up in the global graph yet.

### The Direction Bug

The single hardest bug to find: the original code had `PW_DIRECTION_INPUT = 1`. PipeWire's SPA headers define:

```c
SPA_DIRECTION_INPUT  = 0
SPA_DIRECTION_OUTPUT = 1
```

With direction=1 our stream created OUTPUT ports. The link-factory tried to connect gnome-shell (output) → our node (output) and couldn't. The stream reached `paused` and stayed there. Frames never arrived. There was no error — just silence.

Fixing one constant (`PW_DIRECTION_INPUT = 0`) unblocked everything.

### DMA-BUF Frames

Mutter sends frames as DMA-BUF (SPA_DATA_DmaBuf, type=3). `PW_STREAM_FLAG_MAP_BUFFERS` only maps MemPtr/MemFd buffers — it does nothing for DMA-BUF. The buffer's `data` pointer is null; `fd` is a DMA-BUF file descriptor.

The fix: `mmap.mmap(int(d.fd), d.maxsize, MAP_SHARED, PROT_READ)` in Python's `mmap` module. Then wrap it with `np.frombuffer(mm, dtype=np.uint8, count=size, offset=chunk.offset).reshape(h, w, 4)` for zero-copy numpy access. The initial version used `bytes(mm[offset:offset+size])` which copied 14 MB per frame and capped throughput at 1.5 fps.

### Edge Sampling and Smoothing

Two UX improvements:

**Edge-only sampling.** If the lamp sits behind the monitor, averaging the full screen mixes background colours with the dominant content, producing muddy results. Instead, only the outer 20% border is sampled (top band, bottom band, left strip, right strip), with spatial step=8 to further reduce the pixel count. Result: the lamp colour tracks what's at the periphery of your vision, not what's in the centre.

**LERP smoothing.** Without damping, a single bright flash — an explosion in a game, a white loading screen — would snap the lamp to white and back. An exponential moving average (`alpha * new + (1-alpha) * prev`) smooths transitions. Higher alpha = faster response.

### Performance

After the mmap fix and edge-only sampling: ~4–5 fps from the capture side. The bottleneck is GPU DMA-BUF synchronisation overhead, not numpy. At 5 fps the lamp updates every 200 ms, which is faster than the human eye tracks slow colour drifts. BLE writes go out at up to 20 Hz (every 50 ms), so the lamp never lags behind the computed colour.

### CLI Modes

`lamp_ambient.py` takes a mode flag to trade responsiveness for stability:

```bash
python3 lamp_ambient.py --live     # alpha=0.80, 50 ms BLE poll, no dead zone
python3 lamp_ambient.py --fast     # alpha=0.50, 100 ms, 3° dead zone
python3 lamp_ambient.py --regular  # alpha=0.25, 300 ms, 8° dead zone (default)
python3 lamp_ambient.py --slow     # alpha=0.10, 1 s,   15° dead zone
```

The dead zone suppresses BLE writes when hue/saturation shifts are below a threshold, preventing the lamp from flickering in response to small colour noise during normal desk work.

Total for part two: one Wayland dead end, one GStreamer dead end, a ctypes PipeWire client written from scratch, one swapped enum constant that cost most of a debugging session, and a mmap fix that recovered 3× the frame rate.

---

## Part Three: Making It Usable

Running `python3 lamp_ambient.py --live` from a terminal every time you want ambient lighting is fine for testing. It is not fine as a daily workflow. Part three is about turning the script into something you can operate without opening a terminal.

### System Tray Indicator

The plan: a persistent indicator icon in the GNOME status bar with a menu. Click it, pick a mode, done. The machine already had `appindicatorsupport@rgcjonas.gmail.com` installed (the GNOME Shell extension that makes AppIndicator icons appear in the top bar) and `libayatana-appindicator` available as a Python GObject binding. That's everything needed.

`lamp_tray.py` uses `AyatanaAppIndicator3` with GTK3. It spawns `lamp_ambient.py` as a subprocess, writes its PID to a file, and manages the lifecycle: start, stop, switch mode, restart on region change.

A `.desktop` file goes into `~/.local/share/applications/` for the app launcher and `~/.config/autostart/` so it starts on login.

### The RadioMenuItem Trap

The first menu implementation used `Gtk.RadioMenuItem` for the mode selector — radio buttons being the natural fit for mutually exclusive options. It looked right in the menu. It did nothing when clicked.

The bug: GTK's RadioMenuItem silently starts with the first item in the group pre-checked. Clicking an already-active RadioMenuItem does not fire the `activate` signal. So clicking "Live" — the first mode, and the one most likely to be clicked first — never triggered anything. Clicking "Fast" or "Regular" worked, because selecting them fired activate on the newly active item. But "Live" was a dead button.

Switched to `Gtk.CheckMenuItem` with `set_draw_as_radio(True)` for the visual and manual mutual exclusion via `handler_block_by_func`. Every click is now explicit and tracked regardless of prior state.

### Region Selection

The next obvious question: which part of the screen should the lamp track? The border average that was hardcoded made sense for a lamp sitting behind the monitor, but a lamp off to the side should probably track the nearest edge. "Full screen" is useful if the lamp is overhead.

Added a `--region` parameter to `lamp_ambient.py`:

```bash
python3 lamp_ambient.py --live --region right    # right edge only
python3 lamp_ambient.py --slow --region full     # whole screen, slow tracking
```

Options: `top`, `bottom`, `left`, `right`, `border` (default — all four edges), `full`.

The region is implemented as a closure built at startup — `_make_sampler(region)` returns a function that takes the numpy frame array and returns the mean BGRA. No per-frame branching. The tray menu got a second radio section so you can switch region without touching the terminal.

### Smoothing Rethought

The original mode design had each mode control three things: how fast the LERP tracked new frames (`alpha`), how often BLE writes went out (`ble_sleep`), and a hue dead zone that suppressed small changes. The idea was that "slow" mode would write to the lamp infrequently and ignore small shifts, making it calm for desk work.

The result was that all modes except `--live` had visibly choppy transitions. A slow BLE write rate means the lamp steps between colours in visible jumps rather than gliding.

Rethought: all modes use the same fast BLE write rate (50 ms) and no dead zone. The only thing that differs is `alpha`:

| Mode    | Alpha | Effective lag |
|---------|-------|---------------|
| live    | 0.80  | ~0.4 s        |
| fast    | 0.50  | ~1 s          |
| regular | 0.20  | ~3 s          |
| slow    | 0.05  | ~12 s         |

The lamp always moves smoothly. The mode controls how quickly it chases the screen colour — not how often it moves.

### The Orphan Process Problem

The first time the tray was used in anger it immediately broke: the lamp stayed white and BLE wouldn't connect. The log showed the new `lamp_ambient.py` stuck at `[ble] connecting...` indefinitely.

The reason: an older `lamp_ambient.py` process — started before `--region` was added, from a previous tray session — was still running and holding the BLE connection. The lamp was happily receiving white-screen commands from a process the tray didn't know existed.

Fix: write `/tmp/lamp_ambient.pid` on each subprocess start. When `_stop_proc()` is called — and at tray startup — read and kill whatever PID is in that file before starting anything new. This covers processes from crashed or restarted tray sessions.

### Pending State

Starting the lamp involves connecting to BLE, which takes 2–5 seconds. Stopping it means sending SIGTERM and waiting for the process to wind down. During both of these, the old icon (moon = on, sun = off) was wrong: it claimed a state that wasn't true yet.

`lamp_ambient.py` now writes a status to `/tmp/lamp_ambient.status`: `connecting` when the BLE attempt starts, `connected` when it succeeds, `off` when the process exits. The tray polls this file every 500 ms.

While the status is `connecting` (or while `_stop_proc()` is executing), the tray shows `content-loading-symbolic` and runs a braille spinner animation in the label at 10 fps. When `connected` is read, the icon switches to the moon. The transition is visible and immediate rather than silent and ambiguous.

---

## Part Four: Protocol Archaeology

With ambient mode working day-to-day, curiosity won out: the original DEX scan had found 13 commands starting with `bc` that we only partially understood. Time to send each one to the lamp and document exactly what it does — including the ones marked dangerous.

The method: establish a baseline (lamp showing solid red via `bc 04 06 00 00 03 e8 00 00 55`), send one command, observe, recover, move on. Recovery after a cycling mode: send the colour command directly — no power cycle needed. Recovery after the lamp turns off: power on (`bc 01 01 01 55`) followed by a colour command. Recovery after deep sleep: physical button press only.

### The Confirmed Command Table

| Command | Effect | Recoverable? |
|---------|--------|--------------|
| `bc 01 01 01 55` | Power on — no visible change if already on | N/A |
| `bc 01 01 00 55` | Power off — clean, BLE stays connectable | Yes — power on + colour |
| `bc 04 06 [H>>8] [H&ff] [S*10>>8] [S*10&ff] 00 00 55` | Set colour (HSV) | N/A — this is the colour command |
| `bc 0f 01 01 55` | Persistent colour cycling mode | Yes — colour command exits it immediately |
| `bc 07 01 01 55` | Persistent cycling mode (different pattern) | Yes — colour command |
| `bc 11 01 01 55` | Persistent cycling mode | Yes — colour command |
| `bc 11 01 02 55` | Turns lamp off | Yes — power on + colour |
| `bc 11 01 03 55` | Flashes once, then turns off | Yes — power on + colour |
| `bc 11 01 04 55` | Persistent cycling mode (yet another pattern) | Yes — colour command |
| `bc 05 06 00 00 00 00 00 00 55` | **Deep sleep** — BLE stays connectable, lamp ignores all commands | **No — physical button only** |
| `bc 0c 01 01 55` | No effect | N/A |
| `bc 0c 01 02 55` | No effect | N/A |
| `bc 04 05 [any bytes] 55` | No effect | N/A |

### Key Findings

**`bc 04 06` is the master reset.** Every cycling mode — `0x0f`, `0x07`, `0x11/01`, `0x11/04` — exits immediately when a colour command arrives. You never need power-cycle to recover from a cycling mode. This is why the ambient script works after a physical button press drops the lamp into its default cycle mode: the first colour write our script sends breaks the cycle.

**`0x11` is a mode selector.** Byte[3] is the argument: `01` = cycling, `02` = off, `03` = flash-then-off, `04` = different cycling. There are likely more values (`05`, `06`, ...) that we haven't tested — probably more lighting effects. The pattern matches the app's "mode" UI.

**`bc 05 06 ...` is deep sleep.** This is the only command that requires a physical button press to recover. The lamp remains connectable over BLE — `BleakClient` connects, writes go through without errors — but the lamp hardware ignores everything. It is, effectively, off with no software wake path. Do not send this command in any automated context.

**`bc 0c` and `bc 04 05` are inert.** Whatever these do in the firmware (if anything), sending them produces no visible change to an already-on lamp. They may be read commands, configuration commands that require specific arguments, or dead code.

**Bytes 7–8 of the colour command are unused.** Several tests were made with non-zero values at positions 7 and 8 of `bc 04 06 ...`. The lamp's colour did not change. These bytes appear to be padding.

**`bc 09 06 ...` uses a different layout.** The voice-mode command (used by the app's microphone feature) puts colour information at different byte positions and uses raw RGB-adjacent encoding rather than HSV. It's not useful for our purposes since `bc 04 06` is more ergonomic and fully understood.

### The One Real Casualty

Early in testing — before the systematic approach — an accidental sequence put the lamp into a state where all commands were silently accepted but nothing happened: BLE connected fine, writes completed with no errors, and the lamp sat there dark. That session required a physical button press to recover.

The culprit was `bc 05 06` (deep sleep), triggered during an experimental command sequence. We initially suspected `bc 04 05` because it appeared nearby in the session, but systematic retesting showed `bc 04 05` to be completely inert. The deep sleep command is the only true hazard in the command set.
