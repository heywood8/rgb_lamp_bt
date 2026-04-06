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
