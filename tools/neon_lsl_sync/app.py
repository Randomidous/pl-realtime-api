"""Sync neon device.

Neon <-> LSL Sync Tool
======================

A small standalone GUI app that:

1. Discovers all Neon (Pupil Labs) devices on the local network.
2. Lets the user pick one and connect to it.
3. Once connected (and minimized), periodically pushes a synchronization
   marker to:
      - an LSL outlet (so any LSL-recording software, e.g. LabRecorder,
        can align its own timeline to Neon), and
      - the Neon recording itself via `send_event` (only actually saved
        if the Companion app is recording).

Built on top of `pupil_labs.realtime_api.simple` and `pylsl`.

Packaging notes (PyInstaller):
  - `pylsl` needs the native `liblsl` shared library bundled explicitly;
    it is not picked up automatically by PyInstaller's import analysis.
  - Device discovery uses `zeroconf`/`asyncio` under the hood; these have
    known PyInstaller quirks (missing hidden imports for zeroconf's
    platform-specific backends) -- test the frozen exe's discovery step
    specifically, not just that it launches.
  - See README.md in this folder for the full build recipe.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from pupil_labs.realtime_api.simple import Device, discover_devices

try:
    import pylsl
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pylsl is required. Install it with: pip install pylsl") from exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("neon_lsl_sync")

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
DISCOVERY_SEARCH_SECONDS = 5.0
SYNC_INTERVAL_SECONDS = 1.0
TIME_OFFSET_REFRESH_SECONDS = 120.0
RECONNECT_BACKOFF_SECONDS = 5.0
STATUS_POLL_SECONDS = 1.0


@dataclasses.dataclass
class DeviceChoice:
    """A discovered device plus the info needed to reconnect to it later."""

    device: Device
    address: str
    port: int
    label: str


def _describe_device(device: Device) -> str:
    serial = device.serial_number_glasses or "unknown serial"
    return f"{device.phone_name}  ({serial})  @ {device.phone_ip}"


# --------------------------------------------------------------------------
# Background worker: owns the connected Device, the LSL outlet, and the
# once-a-second sync loop. Runs entirely off the Tk main thread and reports
# back to the GUI via a thread-safe queue.
# --------------------------------------------------------------------------
class SyncWorker:
    def __init__(self, choice: DeviceChoice, event_queue: queue.Queue[tuple]):
        self._choice = choice
        self._device = choice.device
        self._queue = event_queue
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._counter = 0
        self._clock_offset_ns = 0
        self._outlet: pylsl.StreamOutlet | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        try:
            self._device.close()
        except Exception:
            logger.exception("Error while closing device connection")

    # -- internal ----------------------------------------------------------

    def _emit(self, kind: str, payload: object = None) -> None:
        self._queue.put((kind, payload))

    def _make_outlet(self) -> pylsl.StreamOutlet:
        serial = self._device.serial_number_glasses or "unknown"
        source_id = f"neon-sync-{serial}"
        info = pylsl.StreamInfo(
            name="NeonSyncMarkers",
            type="Markers",
            channel_count=1,
            nominal_srate=pylsl.IRREGULAR_RATE,
            channel_format=pylsl.cf_string,
            source_id=source_id,
        )
        desc = info.desc()
        desc.append_child_value("manufacturer", "Pupil Labs")
        desc.append_child_value("serial_number", str(serial))
        return pylsl.StreamOutlet(info)

    def _refresh_time_offset(self) -> None:
        try:
            estimate = self._device.estimate_time_offset()
        except Exception:
            logger.exception("Time offset estimation failed")
            return
        if estimate is None:
            # Companion app too old to support the time-echo protocol.
            self._clock_offset_ns = 0
            return
        self._clock_offset_ns = round(estimate.time_offset_ms.mean * 1_000_000)
        self._emit("offset", estimate.time_offset_ms.mean)

    def _reconnect(self) -> bool:
        """Try to open a fresh Device connection to the same address/port."""
        with contextlib.suppress(Exception):
            self._device.close()
        try:
            self._device = Device(self._choice.address, self._choice.port)
            self._outlet = self._make_outlet()
            self._refresh_time_offset()
            return True  # noqa: TRY300
        except Exception:
            logger.exception("Reconnect attempt failed")
            return False

    def _run(self) -> None:
        try:
            self._outlet = self._make_outlet()
            self._refresh_time_offset()
        except Exception:
            logger.exception("Failed to set up LSL outlet / initial time offset")
            self._emit("fatal", "Could not create the LSL outlet.")
            return

        self._emit("connected", None)
        last_offset_refresh = time.monotonic()
        next_tick = time.monotonic()

        while not self._stop_event.is_set():
            now = time.monotonic()

            if now - last_offset_refresh >= TIME_OFFSET_REFRESH_SECONDS:
                self._refresh_time_offset()
                last_offset_refresh = time.monotonic()

            if now >= next_tick:
                self._send_sync_sample()
                next_tick = now + SYNC_INTERVAL_SECONDS

            self._stop_event.wait(timeout=0.1)

    def _send_sync_sample(self) -> None:
        self._counter += 1
        label = f"lsl_sync_{self._counter}"
        lsl_now = pylsl.local_clock()

        try:
            self._outlet.push_sample([label], timestamp=lsl_now)
        except Exception:
            logger.exception("Failed to push LSL sample")
            self._emit("status", "LSL push failed - see log")
            return

        try:
            client_now_ns = time.time_ns()
            neon_timestamp_ns = client_now_ns - self._clock_offset_ns
            self._device.send_event(label, event_timestamp_unix_ns=neon_timestamp_ns)
            self._emit("status", f"synced #{self._counter}: {label}")
        except Exception:
            logger.warning("send_event failed, attempting reconnect", exc_info=True)
            self._emit("status", "Neon unreachable, reconnecting...")
            if self._reconnect():
                self._emit("status", "Reconnected to Neon.")
            else:
                self._emit(
                    "status",
                    f"Reconnect failed, retrying in {RECONNECT_BACKOFF_SECONDS:.0f}s",
                )
                self._stop_event.wait(timeout=RECONNECT_BACKOFF_SECONDS)

    def is_recording(self) -> bool | None:
        """Best-effort peek at whether the Companion app is recording.

        Uses a "private" attribute (`_status`) because the simple API does
        not expose a public is_recording property; treat this as
        best-effort UI info, not something to build hard logic on.
        """
        status = getattr(self._device, "_status", None)
        recording = getattr(status, "recording", None)
        return recording is not None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Neon LSL Sync")
        self.geometry("480x360")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._event_queue: queue.Queue[tuple] = queue.Queue()
        self._worker: SyncWorker | None = None
        self._choices: list[DeviceChoice] = []

        self._container = ttk.Frame(self, padding=16)
        self._container.pack(fill="both", expand=True)

        self._build_discovery_screen()
        self.after(100, self._poll_queue)

    # -- screen 1: discover + pick -----------------------------------------

    def _build_discovery_screen(self) -> None:
        for child in self._container.winfo_children():
            child.destroy()

        ttk.Label(
            self._container, text="Find a Neon device", font=("", 14, "bold")
        ).pack(anchor="w")

        self._discovery_status = tk.StringVar(value="Press Search to scan the network.")
        ttk.Label(self._container, textvariable=self._discovery_status).pack(
            anchor="w", pady=(4, 8)
        )

        list_frame = ttk.Frame(self._container)
        list_frame.pack(fill="both", expand=True)
        self._device_listbox = tk.Listbox(list_frame, height=8)
        self._device_listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self._device_listbox.yview
        )
        scrollbar.pack(side="right", fill="y")
        self._device_listbox.configure(yscrollcommand=scrollbar.set)

        button_row = ttk.Frame(self._container)
        button_row.pack(fill="x", pady=(8, 0))
        ttk.Button(button_row, text="Search", command=self._start_discovery).pack(
            side="left"
        )
        self._connect_button = ttk.Button(
            button_row, text="Connect", command=self._connect_selected, state="disabled"
        )
        self._connect_button.pack(side="left", padx=(8, 0))

        ip_frame = ttk.Frame(self._container)
        ip_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(ip_frame, text="Or connect directly by IP:").pack(anchor="w")
        entry_row = ttk.Frame(ip_frame)
        entry_row.pack(fill="x", pady=(4, 0))
        self._ip_entry = ttk.Entry(entry_row)
        self._ip_entry.pack(side="left", fill="x", expand=True)
        ttk.Label(entry_row, text=":").pack(side="left", padx=2)
        self._port_entry = ttk.Entry(entry_row, width=6)
        self._port_entry.insert(0, "8080")
        self._port_entry.pack(side="left")
        ttk.Button(entry_row, text="Connect", command=self._connect_by_ip).pack(
            side="left", padx=(8, 0)
        )

    def _start_discovery(self) -> None:
        self._discovery_status.set("Searching...")
        self._device_listbox.delete(0, tk.END)
        self._connect_button.configure(state="disabled")
        self._choices = []
        threading.Thread(target=self._discover_thread, daemon=True).start()

    def _discover_thread(self) -> None:
        try:
            devices = discover_devices(DISCOVERY_SEARCH_SECONDS)
        except Exception:
            logger.exception("Discovery failed")
            self._event_queue.put(("discovery_error", "Discovery failed - see log"))
            return
        choices = [
            DeviceChoice(
                device=d,
                address=d.address,
                port=d.port,
                label=_describe_device(d),
            )
            for d in devices
        ]
        self._event_queue.put(("discovery_done", choices))

    def _on_discovery_done(self, choices: list[DeviceChoice]) -> None:
        self._choices = choices
        if not choices:
            self._discovery_status.set(
                "No devices found. Make sure the Companion app is open and "
                "your computer is on the same network, then try again."
            )
            return
        self._discovery_status.set(
            f"Found {len(choices)} device(s). Select one and press Connect."
        )
        for choice in choices:
            self._device_listbox.insert(tk.END, choice.label)
        self._connect_button.configure(state="normal")

    def _connect_selected(self) -> None:
        selection = self._device_listbox.curselection()
        if not selection:
            return
        choice = self._choices[selection[0]]
        self._begin_connection(choice)

    def _connect_by_ip(self) -> None:
        address = self._ip_entry.get().strip()
        port_text = self._port_entry.get().strip()
        if not address or not port_text.isdigit():
            self._discovery_status.set("Enter a valid IP address and port.")
            return
        try:
            device = Device(address=address, port=int(port_text))
        except Exception:
            logger.exception("Direct connection failed")
            self._discovery_status.set("Couldn't reach a device at that address/port.")
            return
        choice = DeviceChoice(
            device=device,
            address=address,
            port=int(port_text),
            label=_describe_device(device),
        )
        self._begin_connection(choice)

    def _begin_connection(self, choice: DeviceChoice) -> None:
        self._build_status_screen(choice)
        self._worker = SyncWorker(choice, self._event_queue)
        self._worker.start()

    # -- screen 2: connected / status / minimize ----------------------------

    def _build_status_screen(self, choice: DeviceChoice) -> None:
        for child in self._container.winfo_children():
            child.destroy()

        ttk.Label(self._container, text="Connected", font=("", 14, "bold")).pack(
            anchor="w"
        )
        ttk.Label(self._container, text=choice.label).pack(anchor="w", pady=(2, 12))

        self._sync_status = tk.StringVar(value="Starting sync loop...")
        ttk.Label(self._container, textvariable=self._sync_status, wraplength=420).pack(
            anchor="w"
        )

        self._offset_status = tk.StringVar(value="Clock offset: estimating...")
        ttk.Label(self._container, textvariable=self._offset_status).pack(
            anchor="w", pady=(4, 0)
        )

        self._recording_status = tk.StringVar(
            value="Neon recording: unknown (events are only saved while recording)"
        )
        ttk.Label(
            self._container, textvariable=self._recording_status, wraplength=420
        ).pack(anchor="w", pady=(4, 12))

        button_row = ttk.Frame(self._container)
        button_row.pack(fill="x", pady=(8, 0))
        ttk.Button(button_row, text="Minimize", command=self.iconify).pack(side="left")
        ttk.Button(button_row, text="Disconnect", command=self._disconnect).pack(
            side="left", padx=(8, 0)
        )

        self.after(int(STATUS_POLL_SECONDS * 1000), self._poll_recording_state)

    def _poll_recording_state(self) -> None:
        if self._worker is None:
            return
        recording = self._worker.is_recording()
        if recording is True:
            self._recording_status.set(
                "Neon recording: ACTIVE (sync events are being saved)"
            )
        elif recording is False:
            self._recording_status.set(
                "Neon recording: not running (sync events are NOT being saved to Neon)"
            )
        else:
            self._recording_status.set("Neon recording: unknown")
        self.after(int(STATUS_POLL_SECONDS * 1000), self._poll_recording_state)

    def _disconnect(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        self._build_discovery_screen()

    # -- queue plumbing ------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._event_queue.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_event(self, kind: str, payload: object) -> None:
        if kind == "discovery_done":
            self._on_discovery_done(payload)  # type: ignore[arg-type]
        elif kind == "discovery_error":
            self._discovery_status.set(str(payload))
        elif kind == "connected":
            self._sync_status.set("Sync loop running.")
        elif kind == "status":
            self._sync_status.set(str(payload))
        elif kind == "offset":
            self._offset_status.set(f"Clock offset: {payload:.2f} ms")
        elif kind == "fatal":
            self._sync_status.set(f"Fatal error: {payload}")

    def _on_close(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
