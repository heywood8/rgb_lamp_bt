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
