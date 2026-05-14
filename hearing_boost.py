import ctypes
import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSlider,
        QSystemTrayIcon,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:  # pragma: no cover - shown at runtime
    Qt = None
    QTimer = None
    QAction = None
    QIcon = None
    QApplication = None
    QCheckBox = None
    QComboBox = None
    QFrame = None
    QHBoxLayout = None
    QLabel = None
    QMainWindow = None
    QMenu = None
    QMessageBox = None
    QPushButton = None
    QScrollArea = None
    QSlider = None
    QSystemTrayIcon = None
    QTabWidget = None
    QVBoxLayout = None
    QWidget = None
    QT_IMPORT_ERROR = exc
else:
    QT_IMPORT_ERROR = None

QT_MAIN_WINDOW_BASE = QMainWindow if QMainWindow is not None else object

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
APP_VERSION = "1.1.0"
GITHUB_URL = "https://github.com/CatGPT-Sys32"
APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "hearing-boost-icon.svg"
CHECKMARK_ICON_PATH = Path(__file__).resolve().parent / "assets" / "checkbox-check.svg"
APO_DOWNLOAD_URL = "https://sourceforge.net/projects/equalizerapo/files/1.4.2/EqualizerAPO-x64-1.4.2.exe/download"
APO_INSTALLER_PATH = Path(__file__).resolve().parent / "installers" / "EqualizerAPO-latest.exe"
APO_CONFIG_CANDIDATES = [
    Path(r"C:\Program Files\EqualizerAPO\config\config.txt"),
    Path(r"C:\Program Files (x86)\EqualizerAPO\config\config.txt"),
]
BOOST_MARKER_BEGIN = "# Hearing Boost begin"
BOOST_MARKER_END = "# Hearing Boost end"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
UI_FONT = "Segoe UI" if IS_WINDOWS else "Cantarell"
UI_BG = "#000000"
UI_SURFACE = "#ffffff"
UI_TEXT = "#101010"
UI_MUTED = "#666660"
UI_LINE = "#deded8"
UI_ACCENT = "#000000"
UI_BUTTON_ACTIVE = "#eeeee8"
UI_BUTTON_PRESSED = "#e4e4dc"
UI_BORDER = "#9b9b94"


@dataclass
class ApoState:
    path: Path | None
    writable: bool
    message: str


@dataclass
class ApoSnapshot:
    path: Path
    original_text: str


@dataclass(frozen=True)
class AudioDevice:
    id: str
    name: str
    is_default: bool = False


@dataclass(frozen=True)
class AppVolumeSession:
    id: str
    name: str
    volume_percent: int
    max_percent: int
    detail: str = ""


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_apo_config() -> ApoState:
    if not IS_WINDOWS:
        return ApoState(
            path=None,
            writable=False,
            message="Using PulseAudio/PipeWire through pactl.",
        )

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
        message="Using Windows audio controls. Equalizer APO not found; boost is capped at 100%.",
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
    backend_name = "Windows"

    def __init__(self, device_id: str | None = None) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("Windows audio controls are only available on Windows.")
        if IMPORT_ERROR:
            raise RuntimeError(f"Missing audio dependency: {IMPORT_ERROR}")
        comtypes.CoInitialize()
        self.device = self._select_device(device_id)
        self.volume = getattr(self.device, "EndpointVolume", None)
        if self.volume is None:
            endpoint = self.device._dev.Activate(IAudioEndpointVolume._iid_, 23, None)
            self.volume = endpoint.QueryInterface(IAudioEndpointVolume)
        self._channel_count = int(self.volume.GetChannelCount())
        self._last_channels: tuple[float, ...] | None = None
        self.original_channels = self.read_channel_scalars()

    @staticmethod
    def list_devices() -> list[AudioDevice]:
        if not IS_WINDOWS or IMPORT_ERROR:
            return []

        default = AudioUtilities.GetSpeakers()
        default_id = str(getattr(default, "id", getattr(default, "FriendlyName", "default")))
        devices: list[AudioDevice] = []
        for device in AudioUtilities.GetAllDevices():
            name = str(getattr(device, "FriendlyName", "Playback device"))
            device_id = str(getattr(device, "id", name))
            state = str(getattr(device, "State", ""))
            if state and state not in {"1", "DEVICE_STATE_ACTIVE"}:
                continue
            devices.append(AudioDevice(id=device_id, name=name, is_default=device_id == default_id))

        if not devices:
            devices.append(AudioDevice(id=default_id, name=getattr(default, "FriendlyName", "Default playback device"), is_default=True))
        return devices

    @staticmethod
    def _select_device(device_id: str | None):
        if not device_id:
            return AudioUtilities.GetSpeakers()

        for device in AudioUtilities.GetAllDevices():
            candidate_id = str(getattr(device, "id", getattr(device, "FriendlyName", "")))
            if candidate_id == device_id:
                return device
        return AudioUtilities.GetSpeakers()

    @property
    def device_name(self) -> str:
        return getattr(self.device, "FriendlyName", "Default playback device")

    @property
    def channel_count(self) -> int:
        return self._channel_count

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


class LinuxAudio:
    backend_name = "Linux"

    def __init__(self, device_id: str | None = None) -> None:
        if not IS_LINUX:
            raise RuntimeError("Linux audio controls are only available on Linux.")
        if not shutil.which("pactl"):
            raise RuntimeError("pactl was not found. Install PulseAudio utilities or PipeWire Pulse compatibility.")
        self.sink_name = device_id or self._run_pactl("get-default-sink").strip()
        if not self.sink_name:
            raise RuntimeError("Could not determine the default audio output sink.")
        self._device_name = self._sink_display_name(self.sink_name)
        self._last_channels: tuple[float, ...] | None = None
        self._channel_count = 1
        self.original_channels = self.read_channel_scalars()
        self._channel_count = len(self.original_channels)

    @staticmethod
    def _run_pactl(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["pactl", *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("pactl was not found.") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"pactl failed: {message}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("pactl timed out.") from exc
        return completed.stdout

    @staticmethod
    def _list_sink_dicts() -> list[dict]:
        output = LinuxAudio._run_pactl("--format=json", "list", "sinks")
        try:
            sinks = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Could not parse pactl sink information.") from exc
        return sinks if isinstance(sinks, list) else []

    @staticmethod
    def list_devices() -> list[AudioDevice]:
        if not IS_LINUX or not shutil.which("pactl"):
            return []
        default_sink = LinuxAudio._run_pactl("get-default-sink").strip()
        devices: list[AudioDevice] = []
        for sink in LinuxAudio._list_sink_dicts():
            sink_id = str(sink.get("name") or "")
            if not sink_id:
                continue
            devices.append(
                AudioDevice(
                    id=sink_id,
                    name=str(sink.get("description") or sink_id),
                    is_default=sink_id == default_sink,
                )
            )
        return devices

    @staticmethod
    def _sink_display_name(sink_name: str) -> str:
        for sink in LinuxAudio._list_sink_dicts():
            if sink.get("name") == sink_name:
                return str(sink.get("description") or sink_name)
        return sink_name

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def channel_count(self) -> int:
        return self._channel_count

    def read_channel_scalars(self) -> tuple[float, ...]:
        output = self._run_pactl("get-sink-volume", self.sink_name)
        channels = [max(0.0, float(value) / 100.0) for value in re.findall(r"/\s*(\d+(?:\.\d+)?)%\s*/", output)]

        if not channels:
            raise RuntimeError("Could not read sink channel volumes.")
        self._channel_count = len(channels)
        return tuple(channels)

    def restore_original(self) -> None:
        self.restore_channels(self.original_channels)
        self._last_channels = self.original_channels

    def restore_channels(self, channels: tuple[float, ...]) -> None:
        self._set_channel_volumes(channels)

    def read_state(self) -> tuple[int, int]:
        channels = self.read_channel_scalars()
        if len(channels) < 2:
            return round(channels[0] * 100), 0

        left = max(0.0, channels[0])
        right = max(0.0, channels[1])
        loudest = max(left, right, 0.01)
        if left >= right:
            balance = -round((1.0 - (right / loudest)) * 100)
        else:
            balance = round((1.0 - (left / loudest)) * 100)
        return round(loudest * 100), balance

    def _set_channel_volumes(self, channels: tuple[float, ...]) -> None:
        volume_args = [f"{max(0.0, channel) * 100.0:.0f}%" for channel in channels]
        self._run_pactl("set-sink-volume", self.sink_name, *volume_args)

    def apply(self, boost_percent: float, balance_percent: float, apo_active: bool) -> tuple[float, float, float]:
        base = max(0.0, min(5.0, boost_percent / 100.0))
        balance = max(-100.0, min(100.0, balance_percent)) / 100.0

        left_gain = 1.0
        right_gain = 1.0
        if balance > 0:
            left_gain = 1.0 - balance
        elif balance < 0:
            right_gain = 1.0 + balance

        left = max(0.0, base * left_gain)
        right = max(0.0, base * right_gain)
        count = max(1, self.channel_count)
        channels = (left, right, *([base] * max(0, count - 2))) if count >= 2 else (base,)
        if self._last_channels and len(self._last_channels) == len(channels):
            if all(abs(old - new) < 0.002 for old, new in zip(self._last_channels, channels)):
                return base * 100.0, left * 100.0, right * 100.0

        previous_channels = self.read_channel_scalars()
        try:
            self._set_channel_volumes(channels)
        except Exception:
            self.restore_channels(previous_channels)
            self._last_channels = previous_channels
            raise

        self._last_channels = channels
        return base * 100.0, left * 100.0, right * 100.0


def list_audio_devices() -> list[AudioDevice]:
    if IS_WINDOWS:
        return WindowsAudio.list_devices()
    if IS_LINUX:
        return LinuxAudio.list_devices()
    return []


def create_audio_backend(device_id: str | None = None) -> WindowsAudio | LinuxAudio:
    if IS_WINDOWS:
        return WindowsAudio(device_id)
    if IS_LINUX:
        return LinuxAudio(device_id)
    raise RuntimeError(f"{sys.platform} is not supported yet.")


def _volume_percent_from_linux_volume(value) -> int | None:
    percentages: list[float] = []

    def collect(node) -> None:
        if isinstance(node, dict):
            percent = node.get("value_percent")
            if isinstance(percent, str):
                match = re.search(r"(\d+(?:\.\d+)?)%", percent)
                if match:
                    percentages.append(float(match.group(1)))
            for child in node.values():
                collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    collect(value)
    if not percentages:
        return None
    return round(max(percentages))


def _clean_linux_app_name(name: str, binary: str = "", app_id: str = "") -> str:
    raw = (name or binary or app_id).strip()
    lowered = raw.lower()
    if app_id == "com.brave.Browser" or binary == "brave" or lowered.startswith("brave"):
        return "Brave"
    if binary == "spotify" or lowered == "spotify":
        return "Spotify"
    if raw:
        return raw[:1].upper() + raw[1:] if raw.islower() else raw
    return "Audio stream"


def _linux_client_properties() -> dict[str, dict]:
    try:
        output = LinuxAudio._run_pactl("--format=json", "list", "clients")
        clients = json.loads(output)
    except Exception:
        return {}

    properties_by_client: dict[str, dict] = {}
    for client in clients if isinstance(clients, list) else []:
        index = client.get("index")
        properties = client.get("properties")
        if index is not None and isinstance(properties, dict):
            properties_by_client[str(index)] = properties
    return properties_by_client


def list_app_volume_sessions() -> list[AppVolumeSession]:
    if IS_WINDOWS:
        return list_windows_app_volume_sessions()
    if IS_LINUX:
        return list_linux_app_volume_sessions()
    return []


def set_app_volume_session(session_id: str, volume_percent: int) -> None:
    if IS_WINDOWS:
        set_windows_app_volume_session(session_id, volume_percent)
        return
    if IS_LINUX:
        set_linux_app_volume_session(session_id, volume_percent)
        return
    raise RuntimeError(f"{sys.platform} is not supported yet.")


def list_windows_app_volume_sessions() -> list[AppVolumeSession]:
    if not IS_WINDOWS or IMPORT_ERROR:
        return []

    comtypes.CoInitialize()
    sessions: list[AppVolumeSession] = []
    seen: set[str] = set()
    for session in AudioUtilities.GetAllSessions():
        volume = getattr(session, "SimpleAudioVolume", None)
        if volume is None:
            continue

        process = getattr(session, "Process", None)
        process_id = str(getattr(process, "pid", "") or getattr(session, "ProcessId", "") or "")
        display_name = str(getattr(session, "DisplayName", "") or "").strip()
        try:
            process_name = process.name() if process else ""
        except Exception:
            process_name = ""
        name = display_name or process_name or f"Audio session {process_id or len(sessions) + 1}"
        session_id = process_id or name
        if session_id in seen:
            continue
        seen.add(session_id)
        try:
            current = round(max(0.0, min(1.0, float(volume.GetMasterVolume()))) * 100.0)
        except Exception:
            continue
        sessions.append(
            AppVolumeSession(
                id=session_id,
                name=name,
                volume_percent=current,
                max_percent=500,
                detail="",
            )
        )
    return sorted(sessions, key=lambda item: item.name.lower())


def set_windows_app_volume_session(session_id: str, volume_percent: float) -> None:
    if not IS_WINDOWS or IMPORT_ERROR:
        raise RuntimeError("Windows app audio controls are not available.")

    comtypes.CoInitialize()
    target = max(0.0, min(1.0, float(volume_percent) / 100.0))
    matched = False
    for session in AudioUtilities.GetAllSessions():
        process = getattr(session, "Process", None)
        process_id = str(getattr(process, "pid", "") or getattr(session, "ProcessId", "") or "")
        display_name = str(getattr(session, "DisplayName", "") or "").strip()
        try:
            process_name = process.name() if process else ""
        except Exception:
            process_name = ""
        candidate_id = process_id or display_name or process_name
        if candidate_id == session_id and getattr(session, "SimpleAudioVolume", None):
            session.SimpleAudioVolume.SetMasterVolume(target, None)
            matched = True
    if not matched:
        raise RuntimeError("That Windows audio session is no longer active.")


def list_linux_app_volume_sessions() -> list[AppVolumeSession]:
    if not IS_LINUX or not shutil.which("pactl"):
        return []

    output = LinuxAudio._run_pactl("--format=json", "list", "sink-inputs")
    try:
        sink_inputs = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Could not parse pactl app volume information.") from exc

    client_properties = _linux_client_properties()
    grouped: dict[str, dict] = {}
    for item in sink_inputs if isinstance(sink_inputs, list) else []:
        index = item.get("index")
        if index is None:
            continue
        properties = dict(client_properties.get(str(item.get("client")), {}))
        stream_properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        properties.update(stream_properties)
        app_name = str(properties.get("application.name") or "").strip()
        binary = str(properties.get("application.process.binary") or "").strip()
        app_id = str(
            properties.get("application.id")
            or properties.get("pipewire.access.portal.app_id")
            or ""
        ).strip()
        group_key = (app_id or binary or app_name or str(index)).lower()
        current = _volume_percent_from_linux_volume(item.get("volume"))
        if current is None:
            continue

        group = grouped.setdefault(
            group_key,
            {
                "indexes": [],
                "name": _clean_linux_app_name(app_name, binary, app_id),
                "volume": current,
            },
        )
        group["indexes"].append(str(index))
        group["volume"] = max(int(group["volume"]), current)

    sessions: list[AppVolumeSession] = []
    for group in grouped.values():
        indexes = sorted(group["indexes"], key=lambda value: int(value) if value.isdigit() else value)
        sessions.append(
            AppVolumeSession(
                id="linux:" + "|".join(indexes),
                name=str(group["name"]),
                volume_percent=max(0, min(500, int(group["volume"]))),
                max_percent=500,
                detail="",
            )
        )
    return sorted(sessions, key=lambda item: item.name.lower())


def set_linux_app_volume_session(session_id: str, volume_percent: int) -> None:
    if not IS_LINUX or not shutil.which("pactl"):
        raise RuntimeError("Linux app audio controls are not available.")
    sink_inputs = session_id.removeprefix("linux:").split("|")
    for sink_input in [value for value in sink_inputs if value]:
        LinuxAudio._run_pactl("set-sink-input-volume", sink_input, f"{max(0, min(500, volume_percent))}%")


def make_separator() -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    line.setFrameShape(QFrame.Shape.HLine)
    return line


def app_icon():
    if QIcon is None or not APP_ICON_PATH.exists():
        return None
    icon = QIcon(str(APP_ICON_PATH))
    return None if icon.isNull() else icon


def open_external_url(url: str) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


class HearingBoostApp(QT_MAIN_WINDOW_BASE):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        icon = app_icon()
        if icon:
            self.setWindowIcon(icon)
        self.resize(500, 660)
        self.setMinimumSize(460, 620)

        self.audio: WindowsAudio | LinuxAudio | None = None
        self.apo = find_apo_config()
        self.last_apo_boost: int | None = None
        self.apo_snapshot: ApoSnapshot | None = None
        self.closing = False
        self.loading = False
        self.apply_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hearing-boost-audio")
        self.apply_future: Future | None = None
        self.pending_apply: tuple[int, int, bool] | None = None
        self.selected_device_id: str | None = None
        self.quit_requested = False
        self.tray_notice_shown = False
        self.app_volume_sliders: dict[str, QSlider] = {}
        self.app_volume_labels: dict[str, QLabel] = {}
        self.app_desired_volumes: dict[str, int] = {}
        self.app_volume_loading = False

        self.apply_timer = QTimer(self)
        self.apply_timer.setSingleShot(True)
        self.apply_timer.timeout.connect(self.apply_settings)
        self.apply_final = False

        self.result_timer = QTimer(self)
        self.result_timer.timeout.connect(self._poll_apply_result)
        self.result_timer.start(60)

        self._apply_style()
        self._build()
        self._build_tray()
        self._connect_audio()
        self._capture_apo_snapshot()
        self._schedule_apply()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget {{
                background: #000000;
                color: #f7f7f2;
                font-family: "{UI_FONT}";
                font-size: 10pt;
            }}
            QLabel#title {{
                color: #ffffff;
                font-size: 21pt;
                font-weight: 700;
            }}
            QLabel#caption, QLabel#sliderMark, QLabel#footer {{
                color: #8b8b84;
                font-size: 8.5pt;
            }}
            QLabel#credits {{
                color: #8b8b84;
                font-size: 8.5pt;
            }}
            QLabel#credits a {{
                color: #ffffff;
                text-decoration: none;
            }}
            QLabel#section {{
                color: #f4f4ef;
                font-size: 10pt;
            }}
            QLabel#metric {{
                color: #ffffff;
                font-size: 26pt;
                font-weight: 700;
            }}
            QLabel#balanceMetric {{
                color: #ffffff;
                font-size: 14pt;
                font-weight: 700;
            }}
            QLabel#status {{
                color: #ffffff;
                font-size: 10pt;
            }}
            QLabel#warning {{
                color: #ffcf66;
                font-size: 8.5pt;
            }}
            QLabel#appName {{
                color: #ffffff;
                font-size: 10pt;
                font-weight: 600;
            }}
            QLabel#appDetail {{
                color: #8b8b84;
                font-size: 8.5pt;
            }}
            QFrame#separator {{
                color: #222222;
                background: #222222;
                max-height: 1px;
                border: 0;
            }}
            QFrame#appRow {{
                border-bottom: 1px solid #202020;
            }}
            QTabWidget::pane {{
                border: 0;
                top: 10px;
            }}
            QTabBar::tab {{
                background: #050505;
                color: #ffffff;
                border: 1px solid #303030;
                padding: 8px 16px;
                margin-right: 6px;
            }}
            QTabBar::tab:selected {{
                background: #ffffff;
                color: #000000;
                border-color: #ffffff;
            }}
            QScrollArea {{
                border: 0;
            }}
            QPushButton {{
                background: #050505;
                color: #ffffff;
                border: 1px solid #3a3a3a;
                padding: 8px 14px;
                min-height: 18px;
            }}
            QPushButton:hover {{
                background: #151515;
                border-color: #686868;
            }}
            QPushButton:pressed {{
                background: #242424;
            }}
            QComboBox {{
                background: #050505;
                color: #ffffff;
                border: 1px solid #2d2d2d;
                padding: 7px 10px;
                min-height: 20px;
            }}
            QComboBox:hover {{
                border-color: #686868;
            }}
            QComboBox QAbstractItemView {{
                background: #050505;
                color: #ffffff;
                selection-background-color: #242424;
                border: 1px solid #333333;
            }}
            QCheckBox {{
                color: #ffffff;
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 13px;
                height: 13px;
                border: 1px solid #ffffff;
                background: #000000;
            }}
            QCheckBox::indicator:checked {{
                background: #ffffff;
                image: url("{CHECKMARK_ICON_PATH.as_posix()}");
            }}
            QSlider {{
                min-height: 30px;
                max-height: 30px;
            }}
            QSlider::groove:horizontal {{
                height: 2px;
                background: #333333;
            }}
            QSlider::sub-page:horizontal {{
                background: #ffffff;
            }}
            QSlider::add-page:horizontal {{
                background: #333333;
            }}
            QSlider::handle:horizontal {{
                background: #ffffff;
                border: 1px solid #ffffff;
                width: 14px;
                height: 14px;
                margin: -6px 0;
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #cfcfc8;
                border-color: #cfcfc8;
            }}
            """
        )

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(34, 30, 34, 28)
        layout.setSpacing(0)

        self.title_label = QLabel(APP_NAME)
        self.title_label.setObjectName("title")
        layout.addWidget(self.title_label)

        self.device_label = QLabel("Default playback device")
        self.device_label.setObjectName("caption")
        self.device_label.setWordWrap(True)
        layout.addWidget(self.device_label)

        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        layout.addWidget(self.device_combo)
        layout.addSpacing(24)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)

        main_tab = QWidget()
        self.main_layout = QVBoxLayout(main_tab)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.tabs.addTab(main_tab, "Main")
        self._build_main_tab(self.main_layout)

        app_tab = QWidget()
        self.app_layout = QVBoxLayout(app_tab)
        self.app_layout.setContentsMargins(0, 0, 0, 0)
        self.app_layout.setSpacing(0)
        self.tabs.addTab(app_tab, "App Management")
        self._build_app_tab(self.app_layout)

    def _build_main_tab(self, layout: QVBoxLayout) -> None:
        boost_header = QHBoxLayout()
        boost_header.setContentsMargins(0, 0, 0, 0)
        boost_title = QLabel("Boost")
        boost_title.setObjectName("section")
        boost_header.addWidget(boost_title)
        boost_header.addStretch(1)
        self.boost_label = QLabel("100%")
        self.boost_label.setObjectName("metric")
        boost_header.addWidget(self.boost_label)
        layout.addLayout(boost_header)

        self.boost_slider = QSlider(Qt.Orientation.Horizontal)
        self.boost_slider.setRange(0, 500)
        self.boost_slider.setValue(100)
        self.boost_slider.valueChanged.connect(self._on_boost_changed)
        self.boost_slider.sliderReleased.connect(self._on_slider_released)
        layout.addWidget(self.boost_slider)

        boost_marks = self._mark_row("0%", "500%")
        layout.addLayout(boost_marks)
        self.boost_warning_label = QLabel("")
        self.boost_warning_label.setObjectName("warning")
        layout.addWidget(self.boost_warning_label)
        layout.addSpacing(26)
        layout.addWidget(make_separator())
        layout.addSpacing(26)

        balance_header = QHBoxLayout()
        balance_header.setContentsMargins(0, 0, 0, 0)
        balance_title = QLabel("Balance")
        balance_title.setObjectName("section")
        balance_header.addWidget(balance_title)
        balance_header.addStretch(1)
        self.left_label = QLabel("L --")
        self.left_label.setObjectName("balanceMetric")
        self.right_label = QLabel("R --")
        self.right_label.setObjectName("balanceMetric")
        balance_header.addWidget(self.left_label)
        balance_header.addSpacing(14)
        balance_header.addWidget(self.right_label)
        layout.addLayout(balance_header)

        self.balance_slider = QSlider(Qt.Orientation.Horizontal)
        self.balance_slider.setRange(-100, 100)
        self.balance_slider.setValue(0)
        self.balance_slider.valueChanged.connect(self._on_balance_changed)
        self.balance_slider.sliderReleased.connect(self._on_slider_released)
        layout.addWidget(self.balance_slider)

        self.balance_value_label = QLabel("Center")
        self.balance_value_label.setObjectName("caption")
        self.balance_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.balance_value_label)
        layout.addLayout(self._mark_row("Left", "Right"))
        layout.addSpacing(26)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        self.center_button = QPushButton("Center balance")
        self.center_button.clicked.connect(self.center_balance)
        self.reset_button = QPushButton("Reset session")
        self.reset_button.clicked.connect(self.reset)
        controls.addWidget(self.center_button)
        controls.addStretch(1)
        controls.addWidget(self.reset_button)
        layout.addLayout(controls)
        layout.addSpacing(12)

        presets = QHBoxLayout()
        presets.setContentsMargins(0, 0, 0, 0)
        self.normal_preset_button = QPushButton("Normal")
        self.normal_preset_button.clicked.connect(lambda: self.apply_preset(100, 0))
        self.left_preset_button = QPushButton("Left ear")
        self.left_preset_button.clicked.connect(lambda: self.apply_preset(130, -35))
        self.right_preset_button = QPushButton("Right ear")
        self.right_preset_button.clicked.connect(lambda: self.apply_preset(130, 35))
        self.loud_preset_button = QPushButton("Loud")
        self.loud_preset_button.clicked.connect(lambda: self.apply_preset(150, 0))
        presets.addWidget(self.normal_preset_button)
        presets.addWidget(self.left_preset_button)
        presets.addWidget(self.right_preset_button)
        presets.addWidget(self.loud_preset_button)
        layout.addLayout(presets)
        layout.addSpacing(22)

        self.session_checkbox = QCheckBox("Restore original settings when closing")
        self.session_checkbox.setChecked(True)
        layout.addWidget(self.session_checkbox)
        layout.addSpacing(24)
        layout.addWidget(make_separator())
        layout.addSpacing(16)

        self.status_label = QLabel("Starting...")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.apo_label = QLabel(self.apo.message)
        self.apo_label.setObjectName("footer")
        self.apo_label.setWordWrap(True)
        layout.addWidget(self.apo_label)
        layout.addStretch(1)

        self.credits_label = QLabel(f'v{APP_VERSION}  |  by <a href="{GITHUB_URL}">CatGPT-Sys32</a>')
        self.credits_label.setObjectName("credits")
        self.credits_label.setOpenExternalLinks(False)
        self.credits_label.setTextFormat(Qt.TextFormat.RichText)
        self.credits_label.linkActivated.connect(open_external_url)
        layout.addWidget(self.credits_label)

        if IS_WINDOWS:
            layout.addSpacing(14)
            apo_controls = QHBoxLayout()
            self.install_button = QPushButton("Install APO")
            self.install_button.clicked.connect(self.install_apo)
            self.refresh_button = QPushButton("Refresh")
            self.refresh_button.clicked.connect(self.refresh_apo)
            apo_controls.addWidget(self.install_button)
            apo_controls.addWidget(self.refresh_button)
            apo_controls.addStretch(1)
            layout.addLayout(apo_controls)

    def _build_app_tab(self, layout: QVBoxLayout) -> None:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Running apps with audio")
        title.setObjectName("section")
        header.addWidget(title)
        header.addStretch(1)
        self.app_refresh_button = QPushButton("Refresh")
        self.app_refresh_button.clicked.connect(self.refresh_app_sessions)
        header.addWidget(self.app_refresh_button)
        layout.addLayout(header)
        layout.addSpacing(8)

        layout.addSpacing(10)

        self.app_scroll = QScrollArea()
        self.app_scroll.setWidgetResizable(True)
        self.app_scroll_content = QWidget()
        self.app_rows = QVBoxLayout(self.app_scroll_content)
        self.app_rows.setContentsMargins(0, 0, 0, 0)
        self.app_rows.setSpacing(0)
        self.app_scroll.setWidget(self.app_scroll_content)
        layout.addWidget(self.app_scroll, 1)

        self.app_status_label = QLabel("Select Refresh to scan active audio streams.")
        self.app_status_label.setObjectName("status")
        self.app_status_label.setWordWrap(True)
        layout.addSpacing(12)
        layout.addWidget(self.app_status_label)

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.tabText(index) != "App Management":
            return
        self._neutralize_global_boost()
        self.refresh_app_sessions()

    def _neutralize_global_boost(self) -> None:
        if self.boost_slider.value() == 100:
            return
        self.boost_slider.setValue(100)
        self._schedule_apply(0, final=True)

    def _clear_app_rows(self) -> None:
        while self.app_rows.count():
            item = self.app_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.app_volume_sliders.clear()
        self.app_volume_labels.clear()

    def refresh_app_sessions(self) -> None:
        self._neutralize_global_boost()
        self._clear_app_rows()
        self.app_volume_loading = True
        try:
            sessions = list_app_volume_sessions()
        except Exception as exc:
            self.app_status_label.setText(f"Could not read app audio sessions: {exc}")
            self.app_volume_loading = False
            return

        if not sessions:
            empty = QLabel("No active app audio streams found.")
            empty.setObjectName("caption")
            self.app_rows.addWidget(empty)
            self.app_rows.addStretch(1)
            self.app_status_label.setText("Start playback in an app, then refresh.")
            self.app_volume_loading = False
            return

        for session in sessions:
            if session.id not in self.app_desired_volumes:
                self.app_desired_volumes[session.id] = session.volume_percent
            self._add_app_session_row(session)
        self.app_rows.addStretch(1)
        self.app_status_label.setText(f"Found {len(sessions)} active audio stream{'s' if len(sessions) != 1 else ''}.")
        self.app_volume_loading = False

    def _add_app_session_row(self, session: AppVolumeSession) -> None:
        row = QFrame()
        row.setObjectName("appRow")
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 12, 0, 14)
        row_layout.setSpacing(7)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        name = QLabel(session.name)
        name.setObjectName("appName")
        name.setWordWrap(True)
        header.addWidget(name, 1)
        value = QLabel(f"{self.app_desired_volumes.get(session.id, session.volume_percent)}%")
        value.setObjectName("balanceMetric")
        header.addWidget(value)
        row_layout.addLayout(header)

        if session.detail:
            detail = QLabel(session.detail)
            detail.setObjectName("appDetail")
            detail.setWordWrap(True)
            row_layout.addWidget(detail)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, session.max_percent)
        slider.setValue(max(0, min(session.max_percent, self.app_desired_volumes.get(session.id, session.volume_percent))))
        slider.valueChanged.connect(lambda value, session_id=session.id: self._on_app_volume_changed(session_id, value))
        slider.sliderReleased.connect(lambda session_id=session.id: self._apply_app_volume(session_id))
        row_layout.addWidget(slider)
        row_layout.addLayout(self._mark_row("0%", f"{session.max_percent}%"))

        self.app_volume_sliders[session.id] = slider
        self.app_volume_labels[session.id] = value
        self.app_rows.addWidget(row)

    def _on_app_volume_changed(self, session_id: str, value: int) -> None:
        label = self.app_volume_labels.get(session_id)
        if label:
            label.setText(f"{value}%")
        if self.app_volume_loading:
            return
        if not IS_WINDOWS:
            self._neutralize_global_boost()
        self.app_desired_volumes[session_id] = value

    def _apply_app_volume(self, session_id: str) -> None:
        slider = self.app_volume_sliders.get(session_id)
        if slider is None:
            return
        self.app_desired_volumes[session_id] = slider.value()
        try:
            if IS_WINDOWS:
                self._apply_windows_effective_app_volumes()
            else:
                self._neutralize_global_boost()
                set_app_volume_session(session_id, slider.value())
        except Exception as exc:
            self.app_status_label.setText(f"Could not set app volume: {exc}")
            return
        self.app_status_label.setText(f"Applied app volume {slider.value()}%.")

    def _apply_windows_effective_app_volumes(self) -> None:
        active_ids = set(self.app_volume_sliders)
        desired = {
            session_id: max(0, min(500, volume))
            for session_id, volume in self.app_desired_volumes.items()
            if session_id in active_ids
        }
        if not desired:
            return

        backing_boost = max(100, max(desired.values()))
        if backing_boost > 100 and not (self.apo.path and self.apo.writable):
            raise RuntimeError("Effective app volume above 100% needs Equalizer APO. Install or refresh APO first.")

        self.apply_timer.stop()
        if self.apply_future and not self.apply_future.done():
            self.apply_future.result(timeout=2)
            self.apply_future = None

        if self.boost_slider.value() != 100:
            self.boost_slider.blockSignals(True)
            self.boost_slider.setValue(100)
            self.boost_slider.blockSignals(False)
            self._update_labels()

        self._apply_settings_blocking(backing_boost, self.balance_slider.value(), True)
        for session_id, effective_volume in desired.items():
            mixer_volume = 0.0 if backing_boost <= 0 else (effective_volume / backing_boost) * 100.0
            set_windows_app_volume_session(session_id, mixer_volume)

        self.left_label.setText("L 100%")
        self.right_label.setText("R 100%")
        self.status_label.setText(f"App Management backing boost: {backing_boost}%.")

    def _build_tray(self) -> None:
        self.tray = None
        if QSystemTrayIcon is None or not QSystemTrayIcon.isSystemTrayAvailable():
            return

        icon = app_icon()
        if not icon:
            return

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(f"{APP_NAME} {APP_VERSION}")
        menu = QMenu(self)
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.show)
        hide_action = QAction("Hide", self)
        hide_action.triggered.connect(self.hide)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(show_action)
        menu.addAction(hide_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def quit_from_tray(self) -> None:
        self.quit_requested = True
        self.close()

    def _mark_row(self, left: str, right: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        left_label = QLabel(left)
        left_label.setObjectName("sliderMark")
        right_label = QLabel(right)
        right_label.setObjectName("sliderMark")
        row.addWidget(left_label)
        row.addStretch(1)
        row.addWidget(right_label)
        return row

    def _connect_audio(self) -> None:
        try:
            devices = list_audio_devices()
            self.device_combo.blockSignals(True)
            self.device_combo.clear()
            for device in devices:
                self.device_combo.addItem(device.name, device.id)
                if device.is_default:
                    self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
            self.device_combo.blockSignals(False)
            if devices:
                self.selected_device_id = self.device_combo.currentData()
            self.audio = create_audio_backend(self.selected_device_id)
            current_volume, current_balance = self.audio.read_state()
            self.loading = True
            self.boost_slider.setValue(current_volume)
            self.balance_slider.setValue(current_balance)
            self.loading = False
            self._update_labels()
            self.device_label.setText(self.audio.device_name)
            self.status_label.setText("Ready.")
        except Exception as exc:
            self.status_label.setText(f"Could not access audio controls: {exc}")
            QMessageBox.critical(self, APP_NAME, str(exc))

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
        self._update_labels()
        if not self.loading:
            self._schedule_apply(160)

    def _on_balance_changed(self) -> None:
        self._update_labels()
        if not self.loading:
            self._schedule_apply(160)

    def _on_slider_released(self) -> None:
        self._schedule_apply(0, final=True)

    def _on_device_changed(self, _index=None) -> None:
        if self.loading:
            return
        self.selected_device_id = self.device_combo.currentData()
        if self.audio:
            try:
                self.audio.restore_original()
            except Exception:
                pass
        try:
            self.audio = create_audio_backend(self.selected_device_id)
            current_volume, current_balance = self.audio.read_state()
            self.loading = True
            self.boost_slider.setValue(current_volume)
            self.balance_slider.setValue(current_balance)
            self.loading = False
            self._update_labels()
            self.device_label.setText(self.audio.device_name)
            self.status_label.setText("Device changed.")
            self._schedule_apply(0, final=True)
        except Exception as exc:
            self.status_label.setText(f"Could not switch device: {exc}")

    def _schedule_apply(self, delay_ms: int = 140, final: bool = False) -> None:
        self.apply_final = self.apply_final or final
        self.apply_timer.start(delay_ms)

    def _update_labels(self) -> None:
        boost = self.boost_slider.value()
        balance = self.balance_slider.value()
        self.boost_label.setText(f"{boost}%")
        if boost >= 250:
            self.boost_warning_label.setText("High boost can clip or distort audio.")
        elif boost > 100:
            self.boost_warning_label.setText("Software amplification active.")
        else:
            self.boost_warning_label.setText("")
        if balance == 0:
            self.balance_value_label.setText("Center")
        else:
            self.balance_value_label.setText(f"{abs(balance)}% {'R' if balance > 0 else 'L'}")

    def apply_settings(self) -> None:
        if self.closing:
            return
        boost = self.boost_slider.value()
        balance = self.balance_slider.value()
        final_apply = self.apply_final
        self.apply_final = False
        self.boost_label.setText(f"{boost}%")

        if not self.audio:
            return

        command = (boost, balance, final_apply)
        if self.apply_future and not self.apply_future.done():
            self.pending_apply = command
            return

        self.status_label.setText("Applying...")
        self.apply_future = self.apply_executor.submit(self._apply_settings_blocking, boost, balance, final_apply)

    def _poll_apply_result(self) -> None:
        if not self.apply_future or not self.apply_future.done():
            return

        try:
            windows, left, right, apo_active = self.apply_future.result()
        except Exception as exc:
            self.status_label.setText(f"Failed to apply audio settings: {exc}")
            self.apply_future = None
        else:
            self.apply_future = None
            boost = self.boost_slider.value()
            self.left_label.setText(f"L {left:.0f}%")
            self.right_label.setText(f"R {right:.0f}%")
            backend_name = self.audio.backend_name if self.audio else "Audio"
            if apo_active:
                boost_note = f", APO +{db_for_boost(boost):.1f} dB"
            elif IS_LINUX and boost > 100:
                boost_note = ", software boost"
            else:
                boost_note = ""
            if IS_WINDOWS and boost > 100 and not apo_active:
                self.status_label.setText("Boost above 100% needs Equalizer APO. Applying 100% Windows volume instead.")
            else:
                self.status_label.setText(f"Applied {backend_name} {windows:.0f}%: L {left:.0f}% / R {right:.0f}%{boost_note}")

        if self.pending_apply:
            boost, balance, final_apply = self.pending_apply
            self.pending_apply = None
            self.status_label.setText("Applying...")
            self.apply_future = self.apply_executor.submit(self._apply_settings_blocking, boost, balance, final_apply)

    def _apply_settings_blocking(self, boost: int, balance: int, final_apply: bool = False) -> tuple[float, float, float, bool]:
        if not self.audio:
            raise RuntimeError("Audio is not connected.")

        apo_active = False
        if IS_WINDOWS and boost > 100:
            if self.apo.path and self.apo.writable:
                try:
                    if final_apply and self.last_apo_boost != boost:
                        update_apo_preamp(self.apo.path, boost)
                        self.last_apo_boost = boost
                    apo_active = self.last_apo_boost == boost
                except OSError as exc:
                    raise RuntimeError(f"Could not write Equalizer APO config: {exc}") from exc
            else:
                pass
        elif IS_WINDOWS and self.apo.path and self.apo.writable:
            try:
                if final_apply and self.last_apo_boost != 100:
                    update_apo_preamp(self.apo.path, 100)
                    self.last_apo_boost = 100
            except OSError:
                pass

        windows, left, right = self.audio.apply(boost, balance, apo_active)
        return windows, left, right, apo_active

    def center_balance(self) -> None:
        self.balance_slider.setValue(0)
        self._schedule_apply(0, final=True)

    def bump_balance(self, amount: int) -> None:
        self.balance_slider.setValue(max(-100, min(100, self.balance_slider.value() + amount)))
        self._schedule_apply(0, final=True)

    def apply_preset(self, boost: int, balance: int) -> None:
        self.boost_slider.setValue(boost)
        self.balance_slider.setValue(balance)
        self._schedule_apply(0, final=True)

    def reset(self) -> None:
        self.boost_slider.setValue(100)
        self.balance_slider.setValue(0)
        self._schedule_apply(0, final=True)

    def refresh_apo(self) -> None:
        self.apo = find_apo_config()
        self.apo_label.setText(self.apo.message)
        self.last_apo_boost = None
        self._capture_apo_snapshot()
        self._schedule_apply(0, final=True)

    def install_apo(self) -> None:
        try:
            APO_INSTALLER_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not APO_INSTALLER_PATH.exists():
                self.status_label.setText("Downloading Equalizer APO installer...")
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
            self.status_label.setText("Equalizer APO installer launched. Select your headset in Configurator, then reboot.")
        except Exception as exc:
            self.status_label.setText(f"Could not launch Equalizer APO installer: {exc}")

    def restore_session(self) -> None:
        if not self.session_checkbox.isChecked():
            return

        errors: list[str] = []
        self.apply_timer.stop()
        if self.apply_future and not self.apply_future.done():
            try:
                self.apply_future.result(timeout=2)
            except Exception as exc:
                errors.append(f"Pending apply: {exc}")

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
                errors.append(f"Audio: {exc}")

        if errors:
            QMessageBox.warning(self, APP_NAME, "Some settings could not be restored:\n" + "\n".join(errors))

    def closeEvent(self, event) -> None:
        if self.tray and not self.quit_requested:
            self.hide()
            if not self.tray_notice_shown:
                self.tray.showMessage(APP_NAME, "Still running in the tray.", QSystemTrayIcon.MessageIcon.Information, 2500)
                self.tray_notice_shown = True
            event.ignore()
            return

        self.closing = True
        self.restore_session()
        self.apply_executor.shutdown(wait=False, cancel_futures=True)
        if self.tray:
            self.tray.hide()
        event.accept()
        app = QApplication.instance()
        if app:
            QTimer.singleShot(0, app.quit)


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--minimized", action="store_true", help="Start hidden in the system tray.")
    args = parser.parse_args()

    if QT_IMPORT_ERROR:
        print(
            f"{APP_NAME} needs PySide6 for its native UI. Install it with: python3 -m pip install -r requirements.txt\n"
            f"Import error: {QT_IMPORT_ERROR}",
            file=sys.stderr,
        )
        return 1

    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    icon = app_icon()
    if icon:
        app.setWindowIcon(icon)
    window = HearingBoostApp()
    if args.minimized and window.tray:
        window.hide()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
