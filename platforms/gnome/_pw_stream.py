"""
platforms/gnome/_pw_stream.py — PipeWire ctypes screen-capture client.

Linux/GNOME-specific. Connects to a Mutter ScreenCast PipeWire node and
calls `callback(r, g, b)` for each frame with the sampled colour.

Not imported directly — use platforms.get_capture_backend() instead.
"""

import ctypes
import mmap as _mmap
import os
import time
import threading

import numpy as np

# ---------------------------------------------------------------------------
# Load libpipewire
# ---------------------------------------------------------------------------

_pw = ctypes.CDLL("libpipewire-0.3.so.0")
_P = ctypes.c_void_p
_U32 = ctypes.c_uint32
_I32 = ctypes.c_int32
_I64 = ctypes.c_int64
_I = ctypes.c_int
_SZ = ctypes.c_size_t
_BOOL = ctypes.c_bool

# ---------------------------------------------------------------------------
# PipeWire constants
# ---------------------------------------------------------------------------

PW_DIRECTION_INPUT      = 0          # SPA_DIRECTION_INPUT
PW_DIRECTION_OUTPUT     = 1          # SPA_DIRECTION_OUTPUT
PW_STREAM_FLAG_NONE     = 0
PW_STREAM_FLAG_MAP_BUFFERS = (1 << 2)  # auto mmap DMA-BUF/MemFd → pointer
PW_VERSION_STREAM_EVENTS = 5
SPA_ID_INVALID          = 0xFFFFFFFF

SPA_DATA_Invalid = 0
SPA_DATA_MemPtr  = 1
SPA_DATA_MemFd   = 2
SPA_DATA_DmaBuf  = 3

# ---------------------------------------------------------------------------
# C structure definitions
# ---------------------------------------------------------------------------

class _SpaList(ctypes.Structure):
    pass
_SpaList._fields_ = [("next", ctypes.POINTER(_SpaList)),
                     ("prev", ctypes.POINTER(_SpaList))]

class _SpaCallbacks(ctypes.Structure):
    _fields_ = [("funcs", _P), ("data", _P)]

class SpaHook(ctypes.Structure):
    """spa_hook — opaque listener handle; must stay alive for listener lifetime."""
    _fields_ = [
        ("link",    _SpaList),
        ("cb",      _SpaCallbacks),
        ("priv",    _P * 4),
        ("removed", _P),
    ]

class _SpaChunk(ctypes.Structure):
    _fields_ = [("offset", _U32), ("size", _U32),
                ("stride", _I32), ("flags", _I32)]

class _SpaData(ctypes.Structure):
    _fields_ = [
        ("type",      _U32),
        ("flags",     _U32),
        ("fd",        _I64),
        ("mapoffset", _U32),
        ("maxsize",   _U32),
        ("data",      _P),           # mapped pointer (set by MAP_BUFFERS)
        ("chunk",     ctypes.POINTER(_SpaChunk)),
    ]

class _SpaBuffer(ctypes.Structure):
    _fields_ = [
        ("n_metas", _U32),
        ("n_datas", _U32),
        ("metas",   _P),
        ("datas",   ctypes.POINTER(_SpaData)),
    ]

class _PwBuffer(ctypes.Structure):
    _fields_ = [
        ("buffer",    ctypes.POINTER(_SpaBuffer)),
        ("user_data", _P),
        ("size",      ctypes.c_uint64),
        ("requested", ctypes.c_uint64),
    ]

# pw_stream_events — version 5 layout (PipeWire 1.x)
_CFUNC = ctypes.CFUNCTYPE
_StreamEventsFields = [
    ("version",       _U32),
    ("destroy",       _CFUNC(None, _P)),
    ("state_changed", _CFUNC(None, _P, _I, _I, ctypes.c_char_p)),
    ("control_info",  _CFUNC(None, _P, _U32, _P)),
    ("io_changed",    _CFUNC(None, _P, _U32, _P, _U32)),
    ("param_changed", _CFUNC(None, _P, _U32, _P)),
    ("add_buffer",    _CFUNC(None, _P, ctypes.POINTER(_PwBuffer))),
    ("remove_buffer", _CFUNC(None, _P, ctypes.POINTER(_PwBuffer))),
    ("process",       _CFUNC(None, _P)),
    ("drained",       _CFUNC(None, _P)),
    ("command",       _CFUNC(None, _P, _P)),
    ("trigger_done",  _CFUNC(None, _P)),
]

class _PwStreamEvents(ctypes.Structure):
    _fields_ = _StreamEventsFields

# ---------------------------------------------------------------------------
# libpipewire function prototypes
# ---------------------------------------------------------------------------

_pw.pw_init.restype = None
_pw.pw_init.argtypes = [ctypes.POINTER(_I), ctypes.POINTER(ctypes.c_char_p)]

_pw.pw_thread_loop_new.restype = _P
_pw.pw_thread_loop_new.argtypes = [ctypes.c_char_p, _P]

_pw.pw_thread_loop_get_loop.restype = _P
_pw.pw_thread_loop_get_loop.argtypes = [_P]

_pw.pw_thread_loop_start.restype = _I
_pw.pw_thread_loop_start.argtypes = [_P]

_pw.pw_thread_loop_stop.restype = None
_pw.pw_thread_loop_stop.argtypes = [_P]

_pw.pw_thread_loop_destroy.restype = None
_pw.pw_thread_loop_destroy.argtypes = [_P]

_pw.pw_thread_loop_lock.restype = None
_pw.pw_thread_loop_lock.argtypes = [_P]

_pw.pw_thread_loop_unlock.restype = None
_pw.pw_thread_loop_unlock.argtypes = [_P]

_pw.pw_thread_loop_wait.restype = None
_pw.pw_thread_loop_wait.argtypes = [_P]

_pw.pw_thread_loop_signal.restype = None
_pw.pw_thread_loop_signal.argtypes = [_P, _BOOL]

_pw.pw_context_new.restype = _P
_pw.pw_context_new.argtypes = [_P, _P, _SZ]

_pw.pw_context_destroy.restype = None
_pw.pw_context_destroy.argtypes = [_P]

_pw.pw_context_connect.restype = _P
_pw.pw_context_connect.argtypes = [_P, _P, _SZ]

_pw.pw_context_connect_fd.restype = _P
_pw.pw_context_connect_fd.argtypes = [_P, _I, _P, _SZ]

_pw.pw_core_disconnect.restype = _I
_pw.pw_core_disconnect.argtypes = [_P]

_pw.pw_core_create_object.restype = _P
_pw.pw_core_create_object.argtypes = [_P, ctypes.c_char_p, ctypes.c_char_p,
                                       _U32, _P, _SZ]

_pw.pw_properties_new_string.restype = _P
_pw.pw_properties_new_string.argtypes = [ctypes.c_char_p]

_pw.pw_properties_free.restype = None
_pw.pw_properties_free.argtypes = [_P]

_pw.pw_stream_new.restype = _P
_pw.pw_stream_new.argtypes = [_P, ctypes.c_char_p, _P]

_pw.pw_stream_destroy.restype = None
_pw.pw_stream_destroy.argtypes = [_P]

_pw.pw_stream_add_listener.restype = None
_pw.pw_stream_add_listener.argtypes = [_P, ctypes.POINTER(SpaHook),
                                        ctypes.POINTER(_PwStreamEvents), _P]

_pw.pw_stream_connect.restype = _I
_pw.pw_stream_connect.argtypes = [_P, _I, _U32, _U32,
                                   ctypes.POINTER(_P), _U32]

_pw.pw_stream_disconnect.restype = _I
_pw.pw_stream_disconnect.argtypes = [_P]

_pw.pw_stream_dequeue_buffer.restype = ctypes.POINTER(_PwBuffer)
_pw.pw_stream_dequeue_buffer.argtypes = [_P]

_pw.pw_stream_queue_buffer.restype = _I
_pw.pw_stream_queue_buffer.argtypes = [_P, ctypes.POINTER(_PwBuffer)]

_pw.pw_stream_update_params.restype = _I
_pw.pw_stream_update_params.argtypes = [_P, ctypes.POINTER(_P), _U32]

_pw.pw_stream_get_node_id.restype = _U32
_pw.pw_stream_get_node_id.argtypes = [_P]

_pw.pw_proxy_destroy.restype = None
_pw.pw_proxy_destroy.argtypes = [_P]

# ---------------------------------------------------------------------------
# PwCapture
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Region samplers
# ---------------------------------------------------------------------------

def _make_sampler(region: str):
    """
    Return a function  sample(arr) -> bgra_mean
    where arr has shape (h, w, 4) in BGRA order.

    region must be one of:
        top | bottom | left | right | border | full
    """
    S = 8  # spatial step — every 8th pixel along each axis

    region = region.lower()

    if region == "top":
        def sample(arr):
            h = arr.shape[0]
            bh = max(1, h // 5)
            return arr[:bh:S, ::S, :].mean(axis=(0, 1))

    elif region == "bottom":
        def sample(arr):
            h = arr.shape[0]
            bh = max(1, h // 5)
            return arr[-bh::S, ::S, :].mean(axis=(0, 1))

    elif region == "left":
        def sample(arr):
            w = arr.shape[1]
            bw = max(1, w // 5)
            return arr[::S, :bw:S, :].mean(axis=(0, 1))

    elif region == "right":
        def sample(arr):
            w = arr.shape[1]
            bw = max(1, w // 5)
            return arr[::S, -bw::S, :].mean(axis=(0, 1))

    elif region == "border":
        def sample(arr):
            h, w = arr.shape[:2]
            bh = max(1, h // 5)
            bw = max(1, w // 5)
            # Average four bands without a concatenate allocation
            return (
                arr[:bh:S,      ::S,    :].mean(axis=(0, 1))
                + arr[-bh::S,   ::S,    :].mean(axis=(0, 1))
                + arr[bh:-bh:S, :bw:S,  :].mean(axis=(0, 1))
                + arr[bh:-bh:S, -bw::S, :].mean(axis=(0, 1))
            ) * 0.25

    elif region == "full":
        def sample(arr):
            return arr[::S, ::S, :].mean(axis=(0, 1))

    else:
        raise ValueError(f"Unknown region {region!r}. "
                         "Use: top bottom left right border full")

    return sample


REGIONS = ["top", "bottom", "left", "right", "border", "full"]


_CAPTURE_INTERVAL = 1.0 / 20  # max 20 fps processed; queue back all others


class PwCapture:
    """
    Connects to Mutter ScreenCast PipeWire node `node_id` and calls
    `callback(r, g, b)` with each frame's average colour.

    `region` controls which part of the frame is sampled:
        top | bottom | left | right | border (default) | full

    Strategy:
      1. Connect stream with SPA_ID_INVALID (no target), creating our input node.
      2. After paused + 500 ms (port registration delay), create a graph link
         from the gnome-shell source node to our input node via link-factory.
      3. Frames flow once the link goes active.
      4. Rate-limited to _CAPTURE_INTERVAL — excess frames are queued back
         immediately without mmapping, reducing DMA-BUF pressure.
    """

    def __init__(self, node_id: int, callback, region: str = "border"):
        self._node_id    = node_id
        self._cb         = callback
        self._sample     = _make_sampler(region)
        self._loop       = None
        self._ctx        = None
        self._core       = None
        self._stream     = None
        self._link       = None        # pw_proxy* for the link
        self._hook       = SpaHook()
        self._events     = None        # keep alive (GC guard)
        self._error: str | None = None
        self._paused     = threading.Event()
        self._last_frame = 0.0         # monotonic time of last processed frame
        _pw.pw_init(None, None)

    # ------------------------------------------------------------------
    def start(self):
        self._loop = _pw.pw_thread_loop_new(b"lamp-capture", None)
        if not self._loop:
            raise RuntimeError("pw_thread_loop_new failed")

        spa_loop = _pw.pw_thread_loop_get_loop(self._loop)
        self._ctx = _pw.pw_context_new(spa_loop, None, 0)
        if not self._ctx:
            raise RuntimeError("pw_context_new failed")

        _pw.pw_thread_loop_start(self._loop)
        _pw.pw_thread_loop_lock(self._loop)

        try:
            self._core = _pw.pw_context_connect(self._ctx, None, 0)
            if not self._core:
                raise RuntimeError("pw_context_connect failed")

            props = _pw.pw_properties_new_string(
                b"media.type=Video "
                b"media.category=Capture "
                b"media.role=Screen "
                b"media.class=Stream/Input/Video"
            )
            self._stream = _pw.pw_stream_new(self._core, b"lamp-capture", props)
            if not self._stream:
                raise RuntimeError("pw_stream_new failed")

            ev = _PwStreamEvents()
            ev.version = PW_VERSION_STREAM_EVENTS

            _state_names = {0: "unconnected", 1: "connecting",
                            2: "paused", 3: "streaming", -1: "error"}

            @_CFUNC(None, _P, _I, _I, ctypes.c_char_p)
            def _on_state(data, old, new, err):
                print(f"[pw] stream: {_state_names.get(old,'?')} → "
                      f"{_state_names.get(new, str(new))}"
                      + (f" ({err.decode()})" if err else ""), flush=True)
                if new == -1:  # error
                    self._error = (err or b"unknown").decode()
                    _pw.pw_thread_loop_signal(self._loop, False)
                elif new == 2:  # paused
                    self._paused.set()

            @_CFUNC(None, _P, _U32, _P)
            def _on_param_changed(data, id, param):
                if param:
                    _pw.pw_stream_update_params(self._stream, None, 0)

            @_CFUNC(None, _P)
            def _on_process(data):
                pbuf = _pw.pw_stream_dequeue_buffer(self._stream)
                if not pbuf:
                    return
                try:
                    # Rate-limit: skip frames that arrive faster than our cap.
                    # Queue back immediately without touching the DMA-BUF.
                    now = time.monotonic()
                    if now - self._last_frame < _CAPTURE_INTERVAL:
                        return
                    self._last_frame = now

                    buf = pbuf.contents
                    spa = buf.buffer.contents
                    if spa.n_datas == 0:
                        return
                    d = spa.datas[0]
                    chunk = d.chunk.contents
                    size  = chunk.size
                    if size == 0:
                        return
                    stride = chunk.stride  # bytes per row
                    if stride <= 0:
                        return
                    w = stride // 4
                    h = size // stride
                    if w <= 0 or h <= 0:
                        return
                    mm: _mmap.mmap | None = None
                    try:
                        if d.type == SPA_DATA_DmaBuf:
                            fd = int(d.fd)
                            if fd < 0:
                                return
                            mm = _mmap.mmap(fd, d.maxsize,
                                            _mmap.MAP_SHARED, _mmap.PROT_READ)
                            # np.frombuffer on mmap — no bulk copy; numpy only
                            # page-faults the edge rows it actually reads.
                            arr = np.frombuffer(mm, dtype=np.uint8,
                                                count=size,
                                                offset=chunk.offset
                                                ).reshape(h, w, 4)
                        elif d.data:
                            raw = ctypes.string_at(d.data, size)
                            arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
                        else:
                            return
                        bgra = self._sample(arr)
                        b, g, r, _ = bgra
                        self._cb(int(r), int(g), int(b))
                    finally:
                        if mm is not None:
                            mm.close()
                except Exception:
                    pass
                finally:
                    _pw.pw_stream_queue_buffer(self._stream, pbuf)

            ev.state_changed  = _on_state
            ev.param_changed  = _on_param_changed
            ev.process        = _on_process

            # Keep ctypes callbacks alive
            self._events          = ev
            self._on_state        = _on_state
            self._on_param_changed = _on_param_changed
            self._on_process      = _on_process

            _pw.pw_stream_add_listener(
                self._stream,
                ctypes.byref(self._hook),
                ctypes.byref(ev),
                None,
            )

            # Connect without target — we'll link manually after port registration
            rc = _pw.pw_stream_connect(
                self._stream,
                PW_DIRECTION_INPUT,
                SPA_ID_INVALID,
                PW_STREAM_FLAG_NONE | PW_STREAM_FLAG_MAP_BUFFERS,
                None, 0,
            )
            if rc < 0:
                raise RuntimeError(f"pw_stream_connect failed: {rc}")

        finally:
            _pw.pw_thread_loop_unlock(self._loop)

        # Wait for paused state (our node and its input port are now registered)
        if not self._paused.wait(timeout=8):
            raise RuntimeError("stream did not reach paused state")

        if self._error:
            raise RuntimeError(f"stream error: {self._error}")

        # Give PipeWire ~500 ms to export our port to the global registry
        time.sleep(0.5)

        # Create the link: gnome-shell output → our input node
        _pw.pw_thread_loop_lock(self._loop)
        try:
            our_node = _pw.pw_stream_get_node_id(self._stream)
            link_props = _pw.pw_properties_new_string(
                f"link.output.node={self._node_id} "
                f"link.input.node={our_node}".encode()
            )
            self._link = _pw.pw_core_create_object(
                self._core,
                b"link-factory",
                b"PipeWire:Interface:Link",
                3,           # PW_VERSION_LINK
                link_props,  # pw_properties* is spa_dict*-compatible
                0,
            )
            print(f"[pw] linked node {self._node_id} → {our_node}", flush=True)
        finally:
            _pw.pw_thread_loop_unlock(self._loop)

    # ------------------------------------------------------------------
    def stop(self):
        loop = self._loop
        if not loop:
            return
        # Stop the thread first (must NOT hold the lock).
        # Once the thread has exited, objects can be destroyed lock-free.
        _pw.pw_thread_loop_stop(loop)
        if self._link:
            _pw.pw_proxy_destroy(self._link)
            self._link = None
        if self._stream:
            _pw.pw_stream_disconnect(self._stream)
            _pw.pw_stream_destroy(self._stream)
            self._stream = None
        if self._core:
            _pw.pw_core_disconnect(self._core)
            self._core = None
        if self._ctx:
            _pw.pw_context_destroy(self._ctx)
            self._ctx = None
        _pw.pw_thread_loop_destroy(loop)
        self._loop = None
