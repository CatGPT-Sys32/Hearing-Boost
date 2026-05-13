import ctypes
import html
import math
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
except Exception as exc:  # pragma: no cover - shown in GUI at runtime
    comtypes = None
    AudioUtilities = None
    IAudioEndpointVolume = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


APP_NAME = "Hearing Boost"
APO_DOWNLOAD_URL = "https://sourceforge.net/projects/equalizerapo/files/1.4.2/EqualizerAPO-x64-1.4.2.exe/download"
APO_INSTALLER_PATH = Path(__file__).resolve().parent / "installers" / "EqualizerAPO-latest.exe"
APO_CONFIG_CANDIDATES = [
    Path(r"C:\Program Files\EqualizerAPO\config\config.txt"),
    Path(r"C:\Program Files (x86)\EqualizerAPO\config\config.txt"),
]
BOOST_MARKER_BEGIN = "# Hearing Boost begin"
BOOST_MARKER_END = "# Hearing Boost end"


@dataclass
class ApoState:
    path: Path | None
    writable: bool
    message: str


@dataclass
class ApoSnapshot:
    path: Path
    original_text: str


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_apo_config() -> ApoState:
    for path in APO_CONFIG_CANDIDATES:
        if path.exists():
            try:
                with path.open("a", encoding="utf-8"):
                    pass
            except OSError:
                return ApoState(
                    path=path,
                    writable=False,
                    message=f"Found Equalizer APO, but {path} is not writable. Run as admin for >100%.",
                )
            return ApoState(
                path=path,
                writable=True,
                message=f"Equalizer APO boost enabled via {path}",
            )

    return ApoState(
        path=None,
        writable=False,
        message="Equalizer APO not found. Windows volume and balance work; boost is capped at 100%.",
    )


def db_for_boost(boost_percent: float) -> float:
    if boost_percent <= 100:
        return 0.0
    return 20.0 * math.log10(boost_percent / 100.0)


def update_apo_preamp(path: Path, boost_percent: float) -> None:
    preamp_db = db_for_boost(boost_percent)
    block = f"{BOOST_MARKER_BEGIN}\nPreamp: {preamp_db:.2f} dB\n{BOOST_MARKER_END}\n"

    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    start = text.find(BOOST_MARKER_BEGIN)
    end = text.find(BOOST_MARKER_END)
    if start != -1 and end != -1 and end > start:
        end += len(BOOST_MARKER_END)
        new_text = text[:start] + block.rstrip("\n") + text[end:]
    else:
        separator = "" if not text or text.endswith("\n") else "\n"
        new_text = f"{text}{separator}\n{block}"

    path.write_text(new_text, encoding="utf-8")


def restore_apo_snapshot(snapshot: ApoSnapshot) -> None:
    snapshot.path.write_text(snapshot.original_text, encoding="utf-8")


def download_apo_installer(destination: Path) -> None:
    import urllib.request

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(APO_DOWNLOAD_URL, headers={"User-Agent": "Mozilla/5.0"})
    page = urllib.request.urlopen(request, timeout=30).read()
    match = re.search(
        rb'https://downloads\.sourceforge\.net/project/equalizerapo/1\.4\.2/EqualizerAPO-x64-1\.4\.2\.exe[^"\']+',
        page,
    )
    download_url = html.unescape(match.group(0).decode("utf-8")) if match else APO_DOWNLOAD_URL
    request = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()

    if not data.startswith(b"MZ"):
        raise RuntimeError("SourceForge did not return a Windows installer.")
    destination.write_bytes(data)


class WindowsAudio:
    def __init__(self) -> None:
        if IMPORT_ERROR:
            raise RuntimeError(f"Missing audio dependency: {IMPORT_ERROR}")
        comtypes.CoInitialize()
        self.device = AudioUtilities.GetSpeakers()
        self.volume = getattr(self.device, "EndpointVolume", None)
        if self.volume is None:
            endpoint = self.device._dev.Activate(IAudioEndpointVolume._iid_, 23, None)
            self.volume = endpoint.QueryInterface(IAudioEndpointVolume)
        self._last_channels: tuple[float, ...] | None = None
        self.original_channels = self.read_channel_scalars()

    @property
    def device_name(self) -> str:
        return getattr(self.device, "FriendlyName", "Default playback device")

    @property
    def channel_count(self) -> int:
        return int(self.volume.GetChannelCount())

    def read_channel_scalars(self) -> tuple[float, ...]:
        count = self.channel_count
        if count >= 2:
            return tuple(
                max(0.0, min(1.0, float(self.volume.GetChannelVolumeLevelScalar(channel))))
                for channel in range(count)
            )
        return (max(0.0, min(1.0, float(self.volume.GetMasterVolumeLevelScalar()))),)

    def restore_original(self) -> None:
        count = self.channel_count
        self.restore_channels(self.original_channels)
        self._last_channels = self.original_channels

    def restore_channels(self, channels: tuple[float, ...]) -> None:
        count = self.channel_count
        if count >= 2 and len(channels) >= 2:
            for channel, scalar in enumerate(channels[:count]):
                self.volume.SetChannelVolumeLevelScalar(channel, scalar, None)
        else:
            self.volume.SetMasterVolumeLevelScalar(channels[0], None)

    def read_state(self) -> tuple[int, int]:
        master = max(0.0, min(1.0, float(self.volume.GetMasterVolumeLevelScalar())))
        count = self.channel_count
        if count < 2:
            return round(master * 100), 0

        left = max(0.0, min(1.0, float(self.volume.GetChannelVolumeLevelScalar(0))))
        right = max(0.0, min(1.0, float(self.volume.GetChannelVolumeLevelScalar(1))))
        loudest = max(left, right, 0.01)
        if left >= right:
            balance = -round((1.0 - (right / loudest)) * 100)
        else:
            balance = round((1.0 - (left / loudest)) * 100)
        return round(loudest * 100), balance

    def apply(self, boost_percent: float, balance_percent: float, apo_active: bool) -> tuple[float, float, float]:
        windows_percent = min(boost_percent, 100.0)
        base = max(0.0, min(1.0, windows_percent / 100.0))
        balance = max(-100.0, min(100.0, balance_percent)) / 100.0

        left_gain = 1.0
        right_gain = 1.0
        if balance > 0:
            left_gain = 1.0 - balance
        elif balance < 0:
            right_gain = 1.0 + balance

        left = max(0.0, min(1.0, base * left_gain))
        right = max(0.0, min(1.0, base * right_gain))

        count = self.channel_count
        channels = (left, right, *([base] * max(0, count - 2))) if count >= 2 else (base,)
        if self._last_channels and len(self._last_channels) == len(channels):
            if all(abs(old - new) < 0.002 for old, new in zip(self._last_channels, channels)):
                return base * 100.0, left * 100.0, right * 100.0

        previous_channels = self.read_channel_scalars()
        try:
            if count >= 2:
                self.volume.SetChannelVolumeLevelScalar(0, left, None)
                self.volume.SetChannelVolumeLevelScalar(1, right, None)
                for channel in range(2, count):
                    self.volume.SetChannelVolumeLevelScalar(channel, base, None)
            else:
                self.volume.SetMasterVolumeLevelScalar(base, None)
        except Exception:
            self.restore_channels(previous_channels)
            self._last_channels = previous_channels
            raise

        self._last_channels = channels
        return base * 100.0, left * 100.0, right * 100.0


class ValueSlider(tk.Frame):
    def __init__(
        self,
        master,
        variable: tk.DoubleVar,
        minimum: float,
        maximum: float,
        left_label: str,
        right_label: str,
        command=None,
        formatter=None,
        **kwargs,
    ) -> None:
        super().__init__(master, bg="#ffffff", **kwargs)
        self.variable = variable
        self.minimum = minimum
        self.maximum = maximum
        self.left_label = left_label
        self.right_label = right_label
        self.command = command
        self.formatter = formatter or (lambda value: str(round(value)))
        self.track_pad = 14
        self.track_y = 38
        self.thumb_radius = 6
        self.dragging = False

        self.canvas = tk.Canvas(
            self,
            height=62,
            bg="#ffffff",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="x", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.canvas.bind("<Button-1>", self._set_from_event)
        self.canvas.bind("<B1-Motion>", self._set_from_event)
        self.canvas.bind("<ButtonPress-1>", lambda _event: self._set_dragging(True))
        self.canvas.bind("<ButtonRelease-1>", lambda _event: self._set_dragging(False))
        self.variable.trace_add("write", lambda *_args: self.redraw())
        self.redraw()

    def _set_dragging(self, value: bool) -> None:
        self.dragging = value
        self.redraw()

    def _set_from_event(self, event) -> None:
        width = max(1, self.canvas.winfo_width())
        start = self.track_pad
        end = width - self.track_pad
        x = max(start, min(end, event.x))
        value = self.minimum + ((x - start) / (end - start)) * (self.maximum - self.minimum)
        self.variable.set(round(value))
        if self.command:
            self.command(value)
        self.redraw()

    def redraw(self) -> None:
        width = max(1, self.canvas.winfo_width())
        start = self.track_pad
        end = width - self.track_pad
        mid = (start + end) / 2
        value = max(self.minimum, min(self.maximum, float(self.variable.get())))
        x = start + ((value - self.minimum) / (self.maximum - self.minimum)) * (end - start)
        radius = 9 if self.dragging else self.thumb_radius

        self.canvas.delete("all")
        self.canvas.create_text(start, 11, text=self.left_label, anchor="w", fill="#575d66", font=("Segoe UI", 8))
        self.canvas.create_text(end, 11, text=self.right_label, anchor="e", fill="#575d66", font=("Segoe UI", 8))
        self.canvas.create_text(x, 58, text=self.formatter(value), anchor="s", fill="#111318", font=("Segoe UI Semibold", 9))
        self.canvas.create_line(start, self.track_y, end, self.track_y, fill="#d4d8de", width=1)
        for tick_x in (start, mid, end):
            self.canvas.create_line(tick_x, self.track_y - 2, tick_x, self.track_y + 2, fill="#aeb4bd", width=1)
        self.canvas.create_oval(
            x - radius,
            self.track_y - radius,
            x + radius,
            self.track_y + radius,
            fill="#148cff",
            outline="#148cff",
        )


class HearingBoostApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("500x610")
        self.minsize(480, 560)
        self.configure(bg="#f2f3f5")

        self.audio: WindowsAudio | None = None
        self.apo = find_apo_config()
        self.apply_after_id: str | None = None
        self.last_apo_boost: int | None = None
        self.apo_snapshot: ApoSnapshot | None = None
        self.closing = False
        self.worker_busy = False
        self.pending_apply: tuple[int, int] | None = None
        self.apply_queue: queue.Queue[tuple[int, int] | None] = queue.Queue()
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)

        self.boost_var = tk.DoubleVar(value=100)
        self.balance_var = tk.DoubleVar(value=0)
        self.session_only_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Starting...")
        self.device_var = tk.StringVar(value="Default playback device")
        self.left_var = tk.StringVar(value="L --")
        self.right_var = tk.StringVar(value="R --")
        self.apo_var = tk.StringVar(value=self.apo.message)

        self._configure_style()
        self._build()
        self._connect_audio()
        self._capture_apo_snapshot()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.worker_thread.start()
        self.after(100, self._poll_worker_results)
        self._schedule_apply()

    def _configure_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f3f5")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("TLabel", background="#f2f3f5", foreground="#17191c", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background="#ffffff", foreground="#17191c", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#ffffff", foreground="#111318", font=("Segoe UI Semibold", 16))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#666c75", font=("Segoe UI", 9))
        style.configure("Value.TLabel", background="#ffffff", foreground="#111318", font=("Segoe UI Semibold", 11))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6))
        style.configure("TCheckbutton", background="#ffffff", foreground="#17191c", font=("Segoe UI", 9))
        style.configure("Small.TButton", font=("Segoe UI", 9), padding=(8, 5))

    def _build(self) -> None:
        root = ttk.Frame(self, padding=20)
        root.pack(fill="both", expand=True)

        card = ttk.Frame(root, style="Card.TFrame", padding=22)
        card.pack(fill="both", expand=True)

        ttk.Label(card, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.device_var, style="Muted.TLabel", wraplength=430).pack(anchor="w", pady=(2, 20))

        boost_section = ttk.Frame(card, style="Card.TFrame")
        boost_section.pack(fill="x", pady=(0, 18))
        boost_header = ttk.Frame(boost_section, style="Card.TFrame")
        boost_header.pack(fill="x")
        ttk.Label(boost_header, text="Output boost", style="Card.TLabel").pack(side="left")
        self.boost_label = ttk.Label(boost_header, text="100%", style="Value.TLabel", width=7, anchor="e")
        self.boost_label.pack(side="right")
        ttk.Label(
            boost_section,
            text="Above 100% uses Equalizer APO. Higher values may clip.",
            style="Muted.TLabel",
            wraplength=430,
        ).pack(anchor="w", pady=(2, 4))
        self.boost_slider = ValueSlider(
            boost_section,
            variable=self.boost_var,
            minimum=0,
            maximum=500,
            left_label="0%",
            right_label="500%",
            formatter=lambda value: f"{round(value)}%",
            command=lambda _value: self._on_boost_changed(),
        )
        self.boost_slider.pack(fill="x")

        balance_section = ttk.Frame(card, style="Card.TFrame")
        balance_section.pack(fill="x", pady=(0, 14))
        balance_header = ttk.Frame(balance_section, style="Card.TFrame")
        balance_header.pack(fill="x")
        ttk.Label(balance_header, text="Left/right sound balance", style="Card.TLabel").pack(side="left")
        balance_values = ttk.Frame(balance_header, style="Card.TFrame")
        balance_values.pack(side="right")
        ttk.Label(balance_values, textvariable=self.left_var, style="Value.TLabel").pack(side="left")
        ttk.Label(balance_values, text="  /  ", style="Muted.TLabel").pack(side="left")
        ttk.Label(balance_values, textvariable=self.right_var, style="Value.TLabel").pack(side="left")
        ttk.Label(balance_section, text="Bias sound toward the ear that needs help.", style="Muted.TLabel").pack(anchor="w", pady=(2, 4))
        self.balance_slider = ValueSlider(
            balance_section,
            variable=self.balance_var,
            minimum=-100,
            maximum=100,
            left_label="Left",
            right_label="Right",
            formatter=lambda value: "Center" if round(value) == 0 else f"{abs(round(value))}% {'R' if value > 0 else 'L'}",
            command=lambda _value: self._on_balance_changed(),
        )
        self.balance_slider.pack(fill="x")

        controls = ttk.Frame(card, style="Card.TFrame")
        controls.pack(fill="x", pady=(0, 18))
        ttk.Button(controls, text="Center balance", style="Small.TButton", command=self.center_balance).pack(side="left")
        ttk.Button(controls, text="Reset session", style="Small.TButton", command=self.reset).pack(side="right")

        ttk.Checkbutton(
            card,
            text="Restore original settings when closing",
            variable=self.session_only_var,
            style="TCheckbutton",
        ).pack(anchor="w", pady=(0, 18))

        ttk.Separator(card).pack(fill="x", pady=(2, 12))
        ttk.Label(card, textvariable=self.status_var, style="Value.TLabel", wraplength=430).pack(anchor="w", pady=(0, 6))
        ttk.Label(card, textvariable=self.apo_var, style="Muted.TLabel", wraplength=430).pack(anchor="w")
        apo_controls = ttk.Frame(card, style="Card.TFrame")
        apo_controls.pack(fill="x", pady=(8, 0))
        ttk.Button(apo_controls, text="Install APO", style="Small.TButton", command=self.install_apo).pack(side="left")
        ttk.Button(apo_controls, text="Refresh", style="Small.TButton", command=self.refresh_apo).pack(side="left", padx=(8, 0))

    def _connect_audio(self) -> None:
        try:
            self.audio = WindowsAudio()
            current_volume, current_balance = self.audio.read_state()
            self.boost_var.set(current_volume)
            self.balance_var.set(current_balance)
            self.device_var.set(self.audio.device_name)
            self.status_var.set("Ready.")
        except Exception as exc:
            self.status_var.set(f"Could not access Windows audio controls: {exc}")
            messagebox.showerror(APP_NAME, str(exc))

    def _capture_apo_snapshot(self) -> None:
        if self.apo.path and self.apo.path.exists() and self.apo.writable:
            try:
                self.apo_snapshot = ApoSnapshot(
                    path=self.apo.path,
                    original_text=self.apo.path.read_text(encoding="utf-8", errors="ignore"),
                )
            except OSError:
                self.apo_snapshot = None

    def _on_boost_changed(self) -> None:
        boost = round(float(self.boost_var.get()))
        self.boost_label.configure(text=f"{boost}%")
        self._schedule_apply()

    def _on_balance_changed(self) -> None:
        self._schedule_apply()

    def _schedule_apply(self, delay_ms: int = 140) -> None:
        if self.apply_after_id:
            self.after_cancel(self.apply_after_id)
        self.apply_after_id = self.after(delay_ms, self.apply_settings)

    def apply_settings(self) -> None:
        if self.closing:
            return
        self.apply_after_id = None
        boost = round(float(self.boost_var.get()))
        balance = round(float(self.balance_var.get()))
        self.boost_label.configure(text=f"{boost}%")

        if not self.audio:
            return

        self.pending_apply = (boost, balance)
        if not self.worker_busy:
            self.worker_busy = True
            self.apply_queue.put(self.pending_apply)
            self.pending_apply = None
            self.status_var.set("Applying...")

    def _audio_worker(self) -> None:
        while True:
            command = self.apply_queue.get()
            if command is None:
                return

            while True:
                try:
                    newer = self.apply_queue.get_nowait()
                except queue.Empty:
                    break
                if newer is None:
                    return
                command = newer

            boost, balance = command
            try:
                result = self._apply_settings_blocking(boost, balance)
                self.result_queue.put(("applied", result))
            except Exception as exc:
                self.result_queue.put(("error", str(exc)))

    def _apply_settings_blocking(self, boost: int, balance: int) -> tuple[float, float, float, bool]:
        if not self.audio:
            raise RuntimeError("Windows audio is not connected.")

        apo_active = False
        if boost > 100:
            if self.apo.path and self.apo.writable:
                try:
                    if self.last_apo_boost != boost:
                        update_apo_preamp(self.apo.path, boost)
                        self.last_apo_boost = boost
                    apo_active = True
                except OSError as exc:
                    raise RuntimeError(f"Could not write Equalizer APO config: {exc}") from exc
            else:
                pass
        elif self.apo.path and self.apo.writable:
            try:
                if self.last_apo_boost != boost:
                    update_apo_preamp(self.apo.path, boost)
                    self.last_apo_boost = boost
            except OSError:
                pass

        windows, left, right = self.audio.apply(boost, balance, apo_active)
        return windows, left, right, apo_active

    def _poll_worker_results(self) -> None:
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                self.worker_busy = False
                if kind == "applied":
                    windows, left, right, apo_active = payload
                    self.left_var.set(f"L {left:.0f}%")
                    self.right_var.set(f"R {right:.0f}%")
                    boost = round(float(self.boost_var.get()))
                    boost_note = f", APO +{db_for_boost(boost):.1f} dB" if apo_active else ""
                    if boost > 100 and not apo_active:
                        self.status_var.set("Boost above 100% needs Equalizer APO. Applying 100% Windows volume instead.")
                    else:
                        self.status_var.set(f"Applied Windows {windows:.0f}%: L {left:.0f}% / R {right:.0f}%{boost_note}")
                else:
                    self.status_var.set(f"Failed to apply audio settings: {payload}")

                if self.pending_apply and not self.worker_busy:
                    self.worker_busy = True
                    self.apply_queue.put(self.pending_apply)
                    self.pending_apply = None
                    self.status_var.set("Applying...")
        except queue.Empty:
            pass

        if not self.closing:
            self.after(100, self._poll_worker_results)

    def center_balance(self) -> None:
        self.balance_var.set(0)
        self._schedule_apply(0)

    def bump_balance(self, amount: int) -> None:
        self.balance_var.set(max(-100, min(100, self.balance_var.get() + amount)))
        self._schedule_apply(0)

    def reset(self) -> None:
        self.boost_var.set(100)
        self.balance_var.set(0)
        self._schedule_apply(0)

    def refresh_apo(self) -> None:
        self.apo = find_apo_config()
        self.apo_var.set(self.apo.message)
        self.last_apo_boost = None
        self._capture_apo_snapshot()
        self._schedule_apply(0)

    def install_apo(self) -> None:
        try:
            APO_INSTALLER_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not APO_INSTALLER_PATH.exists():
                self.status_var.set("Downloading Equalizer APO installer...")
                self.update_idletasks()
                download_apo_installer(APO_INSTALLER_PATH)

            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f"Start-Process -FilePath '{APO_INSTALLER_PATH}' -Verb RunAs",
                ],
                shell=False,
            )
            self.status_var.set("Equalizer APO installer launched. Select your headset in Configurator, then reboot.")
        except Exception as exc:
            self.status_var.set(f"Could not launch Equalizer APO installer: {exc}")

    def restore_session(self) -> None:
        if not self.session_only_var.get():
            return

        errors: list[str] = []
        if self.apply_after_id:
            self.after_cancel(self.apply_after_id)
            self.apply_after_id = None

        if self.apo_snapshot:
            try:
                restore_apo_snapshot(self.apo_snapshot)
            except OSError as exc:
                errors.append(f"APO: {exc}")
        elif self.apo.path and self.apo.writable:
            try:
                update_apo_preamp(self.apo.path, 100)
            except OSError as exc:
                errors.append(f"APO: {exc}")

        if self.audio:
            try:
                self.audio.restore_original()
            except Exception as exc:
                errors.append(f"Windows audio: {exc}")

        if errors:
            messagebox.showwarning(APP_NAME, "Some settings could not be restored:\n" + "\n".join(errors))

    def on_close(self) -> None:
        self.closing = True
        self.apply_queue.put(None)
        self.restore_session()
        self.destroy()


def main() -> int:
    app = HearingBoostApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
