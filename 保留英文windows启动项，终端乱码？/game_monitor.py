#!/usr/bin/env python3
"""
Game Monitor - Stage 2

Monitors a target game process, records recent system/process/FPS/GPU samples,
collects Windows crash and driver events, and generates a diagnostic report
when the process exits.
"""

from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import datetime as dt
import html
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence

try:
    import psutil
except ImportError:  # pragma: no cover - exercised by user environment.
    psutil = None

try:
    import pynvml
except ImportError:  # pragma: no cover - optional NVIDIA telemetry.
    pynvml = None

try:
    import wmi
except ImportError:  # pragma: no cover - optional LibreHardwareMonitor telemetry.
    wmi = None


DEFAULT_CONFIG = {
    "games": [],
    "sample_interval_seconds": 2,
    "history_minutes": 5,
    "reports_dir": "reports",
    "presentmon": {
        "enabled": False,
        "executable_path": "",
        "csv_path": "",
        "arguments": [
            "--process_name",
            "{process_name}",
            "--output_file",
            "{csv_path}",
        ],
    },
    "hardware": {
        "enable_nvml": True,
        "enable_libre_hardware_monitor": True,
    },
    "windows_events": {
        "enabled": True,
        "max_events": 40,
        "lookback_minutes_when_start_unknown": 10,
    },
    "thresholds": {
        "system_memory_percent": 90,
        "process_cpu_percent": 85,
        "process_memory_mb": 8192,
        "disk_busy_percent": 80,
        "low_average_fps": 45,
        "low_1pct_fps": 30,
        "stutter_frame_time_ms": 50,
        "stutter_count": 5,
        "gpu_load_percent": 95,
        "gpu_temperature_c": 85,
        "gpu_vram_percent": 90,
        "gpu_power_percent": 95,
    },
}


@dataclasses.dataclass
class FrameStats:
    average_fps: Optional[float] = None
    one_percent_low_fps: Optional[float] = None
    point_one_percent_low_fps: Optional[float] = None
    average_frame_time_ms: Optional[float] = None
    p95_frame_time_ms: Optional[float] = None
    max_frame_time_ms: Optional[float] = None
    frame_count: int = 0
    stutter_count: int = 0


@dataclasses.dataclass
class GpuStats:
    source: str
    gpu_name: Optional[str] = None
    gpu_load_percent: Optional[float] = None
    gpu_temperature_c: Optional[float] = None
    gpu_memory_used_mb: Optional[float] = None
    gpu_memory_total_mb: Optional[float] = None
    gpu_power_w: Optional[float] = None
    gpu_power_limit_w: Optional[float] = None
    gpu_fan_percent: Optional[float] = None


@dataclasses.dataclass
class WindowsEvent:
    timestamp: str
    log_name: str
    provider: str
    event_id: int
    level: str
    message: str


@dataclasses.dataclass
class Sample:
    timestamp: str
    process_name: str
    pid: int
    process_cpu_percent: float
    process_memory_mb: float
    process_threads: int
    process_handles: Optional[int]
    system_cpu_percent: float
    system_memory_percent: float
    disk_busy_percent: Optional[float]
    average_fps: Optional[float] = None
    one_percent_low_fps: Optional[float] = None
    point_one_percent_low_fps: Optional[float] = None
    average_frame_time_ms: Optional[float] = None
    p95_frame_time_ms: Optional[float] = None
    max_frame_time_ms: Optional[float] = None
    frame_count: int = 0
    stutter_count: int = 0
    gpu_source: Optional[str] = None
    gpu_name: Optional[str] = None
    gpu_load_percent: Optional[float] = None
    gpu_temperature_c: Optional[float] = None
    gpu_memory_used_mb: Optional[float] = None
    gpu_memory_total_mb: Optional[float] = None
    gpu_power_w: Optional[float] = None
    gpu_power_limit_w: Optional[float] = None
    gpu_fan_percent: Optional[float] = None


def deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return json.loads(json.dumps(DEFAULT_CONFIG))

    with open(path, "r", encoding="utf-8") as file:
        user_config = json.load(file)
    return deep_merge(DEFAULT_CONFIG, user_config)


def require_psutil() -> None:
    if psutil is None:
        print("缺少依赖 psutil。请先运行：python3 -m pip install -r requirements.txt")
        sys.exit(2)


def normalize_names(names: Iterable[str]) -> List[str]:
    return [Path(name).name.lower() for name in names if name]


def find_process(process_names: Iterable[str]) -> Optional[Any]:
    wanted = set(normalize_names(process_names))
    if not wanted:
        return None

    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe_name = Path(proc.info.get("exe") or "").name.lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if name in wanted or exe_name in wanted or any(item in cmdline for item in wanted):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def select_game_profile(config: Dict[str, Any], process_names: Sequence[str]) -> Dict[str, Any]:
    wanted = set(normalize_names(process_names))
    for game in config.get("games", []):
        profile_names = set(normalize_names(game.get("process_names", [])))
        if wanted & profile_names:
            return game
    return {
        "name": "未命名游戏",
        "process_names": list(process_names),
    }


def effective_thresholds(config: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    return deep_merge(config.get("thresholds", {}), profile.get("thresholds", {}))


def get_handle_count(proc: Any) -> Optional[int]:
    try:
        if hasattr(proc, "num_handles"):
            return int(proc.num_handles())
        if hasattr(proc, "num_fds"):
            return int(proc.num_fds())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None
    return None


class DiskBusySampler:
    def __init__(self) -> None:
        self.previous = None

    def sample(self) -> Optional[float]:
        try:
            current = psutil.disk_io_counters()
        except Exception:
            return None
        if current is None:
            return None
        if self.previous is None:
            self.previous = (time.monotonic(), current)
            return None

        previous_time, previous = self.previous
        now = time.monotonic()
        self.previous = (now, current)
        elapsed_ms = max((now - previous_time) * 1000, 1)
        busy_ms = (current.read_time - previous.read_time) + (current.write_time - previous.write_time)
        return round(min(100.0, max(0.0, busy_ms / elapsed_ms * 100)), 2)


def percentile(values: List[float], percent: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percent))))
    return ordered[index]


class PresentMonCsvSampler:
    """Reads frame data from a PresentMon CSV file.

    This keeps the monitor compatible with multiple PresentMon versions by
    looking for common FPS and frame-time column names instead of relying on a
    single schema.
    """

    FRAME_TIME_COLUMNS = (
        "msbetweenpresents",
        "msbetweenpresentsavg",
        "frametime",
        "frametimems",
        "frame_time_ms",
    )
    FPS_COLUMNS = ("fps", "instantfps", "presentrate")
    PROCESS_COLUMNS = ("processname", "application", "exename")

    def __init__(self, csv_path: str, process_name: Optional[str], stutter_ms: float) -> None:
        self.csv_path = Path(csv_path)
        self.process_name = (process_name or "").lower()
        self.stutter_ms = stutter_ms
        self.position = 0
        self.header: Optional[List[str]] = None
        self.pending = ""

    def sample(self) -> FrameStats:
        if not self.csv_path.exists():
            return FrameStats()

        try:
            with self.csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as file:
                file.seek(self.position)
                chunk = file.read()
                self.position = file.tell()
        except OSError:
            return FrameStats()

        if not chunk:
            return FrameStats()

        text = self.pending + chunk
        if not text.endswith("\n"):
            lines = text.splitlines()
            self.pending = lines.pop() if lines else text
        else:
            lines = text.splitlines()
            self.pending = ""

        if not lines:
            return FrameStats()

        if self.header is None:
            self.header = next(csv.reader([lines.pop(0)]), None)
            if not self.header:
                return FrameStats()

        fps_values: List[float] = []
        frame_times: List[float] = []
        normalized_header = [column.strip().lower().replace(" ", "") for column in self.header]

        for row in csv.reader(lines):
            if len(row) < len(self.header):
                continue
            if self.process_name and not self._row_matches_process(row, normalized_header):
                continue
            fps, frame_time = self._extract_row_metrics(row, normalized_header)
            if frame_time is not None and frame_time > 0:
                frame_times.append(frame_time)
                fps_values.append(1000.0 / frame_time)
            elif fps is not None and fps > 0:
                fps_values.append(fps)
                frame_times.append(1000.0 / fps)

        return build_frame_stats(fps_values, frame_times, self.stutter_ms)

    def _row_matches_process(self, row: List[str], normalized_header: List[str]) -> bool:
        for column_name in self.PROCESS_COLUMNS:
            if column_name in normalized_header:
                value = row[normalized_header.index(column_name)].lower()
                return self.process_name in value or Path(self.process_name).stem in value
        return True

    def _extract_row_metrics(
        self,
        row: List[str],
        normalized_header: List[str],
    ) -> tuple[Optional[float], Optional[float]]:
        fps = None
        frame_time = None
        for column_name in self.FPS_COLUMNS:
            if column_name in normalized_header:
                fps = parse_float(row[normalized_header.index(column_name)])
                break
        for column_name in self.FRAME_TIME_COLUMNS:
            if column_name in normalized_header:
                frame_time = parse_float(row[normalized_header.index(column_name)])
                break
        return fps, frame_time


class PresentMonProcess:
    def __init__(self, config: Dict[str, Any], process_name: str, reports_dir: Path) -> None:
        self.config = config
        self.process_name = process_name
        self.reports_dir = reports_dir
        self.process: Optional[subprocess.Popen[Any]] = None
        self.csv_path: Optional[Path] = None

    def start(self) -> Optional[str]:
        presentmon = self.config.get("presentmon", {})
        executable = presentmon.get("executable_path") or ""
        if not presentmon.get("enabled") or not executable:
            return presentmon.get("csv_path") or None

        exe_path = Path(executable)
        if not exe_path.exists():
            print(f"PresentMon 路径不存在，已跳过启动：{exe_path}")
            return presentmon.get("csv_path") or None

        self.reports_dir.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.csv_path = self.reports_dir / f"presentmon-{Path(self.process_name).stem}-{stamp}.csv"
        args = [
            item.format(process_name=self.process_name, csv_path=str(self.csv_path))
            for item in presentmon.get("arguments", [])
        ]

        try:
            self.process = subprocess.Popen(
                [str(exe_path), *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"PresentMon 已启动，CSV：{self.csv_path}")
            return str(self.csv_path)
        except OSError as exc:
            print(f"PresentMon 启动失败，已跳过 FPS 采集：{exc}")
            return presentmon.get("csv_path") or None

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


def parse_float(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def build_frame_stats(fps_values: List[float], frame_times: List[float], stutter_ms: float) -> FrameStats:
    if not fps_values and not frame_times:
        return FrameStats()

    fps_values = [value for value in fps_values if value > 0]
    frame_times = [value for value in frame_times if value > 0]
    low_1_index = max(1, int(len(fps_values) * 0.01)) if fps_values else 0
    low_01_index = max(1, int(len(fps_values) * 0.001)) if fps_values else 0
    ordered_fps = sorted(fps_values)

    return FrameStats(
        average_fps=round(sum(fps_values) / len(fps_values), 2) if fps_values else None,
        one_percent_low_fps=round(sum(ordered_fps[:low_1_index]) / low_1_index, 2) if low_1_index else None,
        point_one_percent_low_fps=round(sum(ordered_fps[:low_01_index]) / low_01_index, 2) if low_01_index else None,
        average_frame_time_ms=round(sum(frame_times) / len(frame_times), 2) if frame_times else None,
        p95_frame_time_ms=round(percentile(frame_times, 0.95), 2) if frame_times else None,
        max_frame_time_ms=round(max(frame_times), 2) if frame_times else None,
        frame_count=len(fps_values) or len(frame_times),
        stutter_count=sum(1 for value in frame_times if value >= stutter_ms),
    )


class NvmlSampler:
    def __init__(self) -> None:
        self.available = False
        self.handle = None
        if pynvml is None:
            return
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.available = True
        except Exception:
            self.available = False

    def sample(self) -> Optional[GpuStats]:
        if not self.available or self.handle is None:
            return None
        try:
            memory = pynvml.nvmlDeviceGetMemoryInfo(self.handle)
            utilization = pynvml.nvmlDeviceGetUtilizationRates(self.handle)
            name = pynvml.nvmlDeviceGetName(self.handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            power_w = None
            power_limit_w = None
            fan_percent = None
            try:
                power_w = pynvml.nvmlDeviceGetPowerUsage(self.handle) / 1000
                power_limit_w = pynvml.nvmlDeviceGetEnforcedPowerLimit(self.handle) / 1000
            except Exception:
                pass
            try:
                fan_percent = float(pynvml.nvmlDeviceGetFanSpeed(self.handle))
            except Exception:
                pass
            return GpuStats(
                source="NVML",
                gpu_name=str(name),
                gpu_load_percent=round(float(utilization.gpu), 2),
                gpu_temperature_c=round(float(pynvml.nvmlDeviceGetTemperature(self.handle, pynvml.NVML_TEMPERATURE_GPU)), 2),
                gpu_memory_used_mb=round(memory.used / 1024 / 1024, 2),
                gpu_memory_total_mb=round(memory.total / 1024 / 1024, 2),
                gpu_power_w=round(power_w, 2) if power_w is not None else None,
                gpu_power_limit_w=round(power_limit_w, 2) if power_limit_w is not None else None,
                gpu_fan_percent=fan_percent,
            )
        except Exception:
            return None


class LibreHardwareMonitorSampler:
    def __init__(self) -> None:
        self.available = False
        self.connection = None
        if platform.system() != "Windows" or wmi is None:
            return
        try:
            self.connection = wmi.WMI(namespace="root\\LibreHardwareMonitor")
            self.connection.Sensor()
            self.available = True
        except Exception:
            self.available = False

    def sample(self) -> Optional[GpuStats]:
        if not self.available or self.connection is None:
            return None
        try:
            sensors = self.connection.Sensor()
        except Exception:
            return None

        stats = GpuStats(source="LibreHardwareMonitor")
        for sensor in sensors:
            name = str(getattr(sensor, "Name", "") or "")
            sensor_type = str(getattr(sensor, "SensorType", "") or "")
            hardware_name = str(getattr(sensor, "HardwareName", "") or "")
            value = parse_float(getattr(sensor, "Value", None))
            if value is None:
                continue
            target = f"{hardware_name} {name}".lower()
            if "gpu" not in target and "graphics" not in target and "video" not in target:
                continue
            stats.gpu_name = stats.gpu_name or hardware_name or None
            if sensor_type == "Load" and ("core" in name.lower() or "gpu" in name.lower()):
                stats.gpu_load_percent = round(value, 2)
            elif sensor_type == "Temperature" and stats.gpu_temperature_c is None:
                stats.gpu_temperature_c = round(value, 2)
            elif sensor_type == "SmallData" and "memory used" in name.lower():
                stats.gpu_memory_used_mb = round(value, 2)
            elif sensor_type == "SmallData" and "memory total" in name.lower():
                stats.gpu_memory_total_mb = round(value, 2)
            elif sensor_type == "Power" and stats.gpu_power_w is None:
                stats.gpu_power_w = round(value, 2)
            elif sensor_type == "Control" and "fan" in name.lower():
                stats.gpu_fan_percent = round(value, 2)
        if any(value is not None for value in dataclasses.asdict(stats).values() if value != "LibreHardwareMonitor"):
            return stats
        return None


class HardwareSampler:
    def __init__(self, config: Dict[str, Any]) -> None:
        hardware = config.get("hardware", {})
        self.samplers = []
        if hardware.get("enable_nvml", True):
            nvml_sampler = NvmlSampler()
            if nvml_sampler.available:
                self.samplers.append(nvml_sampler)
        if hardware.get("enable_libre_hardware_monitor", True):
            lhm_sampler = LibreHardwareMonitorSampler()
            if lhm_sampler.available:
                self.samplers.append(lhm_sampler)

    def sample(self) -> Optional[GpuStats]:
        merged: Optional[GpuStats] = None
        for sampler in self.samplers:
            stats = sampler.sample()
            if stats is None:
                continue
            if merged is None:
                merged = stats
            else:
                for field in dataclasses.fields(GpuStats):
                    if getattr(merged, field.name) is None and getattr(stats, field.name) is not None:
                        setattr(merged, field.name, getattr(stats, field.name))
                if stats.source not in merged.source:
                    merged.source = f"{merged.source}+{stats.source}"
        return merged

    def capability_text(self) -> str:
        if not self.samplers:
            return "未发现可用 GPU 增强采集器"
        return ", ".join(type(sampler).__name__ for sampler in self.samplers)


def capture_sample(
    proc: Any,
    disk_sampler: DiskBusySampler,
    fps_sampler: Optional[PresentMonCsvSampler],
    hardware_sampler: HardwareSampler,
) -> Sample:
    frame_stats = fps_sampler.sample() if fps_sampler else FrameStats()
    gpu_stats = hardware_sampler.sample()
    with proc.oneshot():
        memory = proc.memory_info().rss / 1024 / 1024
        sample = Sample(
            timestamp=dt.datetime.now().isoformat(timespec="seconds"),
            process_name=proc.name(),
            pid=proc.pid,
            process_cpu_percent=round(proc.cpu_percent(interval=None), 2),
            process_memory_mb=round(memory, 2),
            process_threads=proc.num_threads(),
            process_handles=get_handle_count(proc),
            system_cpu_percent=round(psutil.cpu_percent(interval=None), 2),
            system_memory_percent=round(psutil.virtual_memory().percent, 2),
            disk_busy_percent=disk_sampler.sample(),
            **dataclasses.asdict(frame_stats),
        )
    if gpu_stats is not None:
        sample.gpu_source = gpu_stats.source
        sample.gpu_name = gpu_stats.gpu_name
        sample.gpu_load_percent = gpu_stats.gpu_load_percent
        sample.gpu_temperature_c = gpu_stats.gpu_temperature_c
        sample.gpu_memory_used_mb = gpu_stats.gpu_memory_used_mb
        sample.gpu_memory_total_mb = gpu_stats.gpu_memory_total_mb
        sample.gpu_power_w = gpu_stats.gpu_power_w
        sample.gpu_power_limit_w = gpu_stats.gpu_power_limit_w
        sample.gpu_fan_percent = gpu_stats.gpu_fan_percent
    return sample


def numeric_values(samples: List[Sample], field: str) -> List[float]:
    return [
        float(getattr(sample, field))
        for sample in samples
        if getattr(sample, field) is not None
    ]


def summarize(samples: List[Sample]) -> Dict[str, Any]:
    if not samples:
        return {}

    def max_of(field: str) -> Optional[float]:
        values = numeric_values(samples, field)
        return round(max(values), 2) if values else None

    def min_of(field: str) -> Optional[float]:
        values = numeric_values(samples, field)
        return round(min(values), 2) if values else None

    def avg_of(field: str) -> Optional[float]:
        values = numeric_values(samples, field)
        return round(sum(values) / len(values), 2) if values else None

    fps_samples = [sample for sample in samples if sample.frame_count > 0]
    return {
        "started_at": samples[0].timestamp,
        "ended_at": samples[-1].timestamp,
        "sample_count": len(samples),
        "max_process_cpu_percent": max_of("process_cpu_percent"),
        "avg_process_cpu_percent": avg_of("process_cpu_percent"),
        "max_process_memory_mb": max_of("process_memory_mb"),
        "max_system_cpu_percent": max_of("system_cpu_percent"),
        "max_system_memory_percent": max_of("system_memory_percent"),
        "max_disk_busy_percent": max_of("disk_busy_percent"),
        "avg_fps": avg_of("average_fps"),
        "min_1pct_low_fps": min_of("one_percent_low_fps"),
        "min_01pct_low_fps": min_of("point_one_percent_low_fps"),
        "max_frame_time_ms": max_of("max_frame_time_ms"),
        "max_p95_frame_time_ms": max_of("p95_frame_time_ms"),
        "total_stutter_count": sum(sample.stutter_count for sample in samples),
        "fps_sample_count": len(fps_samples),
        "max_gpu_load_percent": max_of("gpu_load_percent"),
        "max_gpu_temperature_c": max_of("gpu_temperature_c"),
        "max_gpu_memory_used_mb": max_of("gpu_memory_used_mb"),
        "gpu_memory_total_mb": max_of("gpu_memory_total_mb"),
        "max_gpu_power_w": max_of("gpu_power_w"),
        "gpu_power_limit_w": max_of("gpu_power_limit_w"),
        "gpu_sources": sorted({sample.gpu_source for sample in samples if sample.gpu_source}),
    }


def has_event(events: List[WindowsEvent], keywords: Sequence[str]) -> Optional[WindowsEvent]:
    lowered = [keyword.lower() for keyword in keywords]
    for event in events:
        text = f"{event.provider} {event.message}".lower()
        if any(keyword in text for keyword in lowered):
            return event
    return None


def analyze(samples: List[Sample], thresholds: Dict[str, Any], events: List[WindowsEvent]) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    if not samples:
        return findings

    summary = summarize(samples)
    if summary.get("max_system_memory_percent", 0) >= thresholds["system_memory_percent"]:
        findings.append({
            "level": "high",
            "title": "系统内存压力过高",
            "evidence": f"系统内存峰值达到 {summary['max_system_memory_percent']}%。",
            "suggestion": "关闭后台程序，降低纹理质量，或增加物理内存。",
        })

    if summary.get("max_process_memory_mb", 0) >= thresholds["process_memory_mb"]:
        findings.append({
            "level": "medium",
            "title": "游戏进程内存占用较高",
            "evidence": f"游戏进程内存峰值达到 {summary['max_process_memory_mb']} MB。",
            "suggestion": "检查高清材质包、MOD、浏览器和录制软件占用。",
        })

    if summary.get("max_process_cpu_percent", 0) >= thresholds["process_cpu_percent"]:
        findings.append({
            "level": "medium",
            "title": "游戏 CPU 占用接近瓶颈",
            "evidence": f"游戏进程 CPU 峰值达到 {summary['max_process_cpu_percent']}%。",
            "suggestion": "降低人群/物理/阴影等 CPU 相关设置，关闭后台任务。",
        })

    max_disk = summary.get("max_disk_busy_percent")
    if max_disk is not None and max_disk >= thresholds["disk_busy_percent"]:
        findings.append({
            "level": "medium",
            "title": "磁盘忙碌度异常",
            "evidence": f"磁盘忙碌度峰值达到 {max_disk}%。",
            "suggestion": "确认游戏安装在 SSD，暂停下载、杀毒扫描和大型文件复制。",
        })

    avg_fps = summary.get("avg_fps")
    min_1pct = summary.get("min_1pct_low_fps")
    if avg_fps is not None and avg_fps <= thresholds["low_average_fps"]:
        findings.append({
            "level": "medium",
            "title": "平均 FPS 偏低",
            "evidence": f"采集窗口内平均 FPS 约为 {avg_fps}。",
            "suggestion": "降低分辨率、光追、阴影、体积雾或抗锯齿等级，并检查 GPU 占用是否已接近满载。",
        })
    if min_1pct is not None and min_1pct <= thresholds["low_1pct_fps"]:
        findings.append({
            "level": "high",
            "title": "1% Low FPS 过低",
            "evidence": f"最低 1% Low FPS 约为 {min_1pct}，说明帧时间波动明显。",
            "suggestion": "优先检查显存、内存分页、磁盘加载、后台录制和 CPU 单核瓶颈。",
        })

    total_stutter = summary.get("total_stutter_count", 0)
    if total_stutter >= thresholds["stutter_count"]:
        findings.append({
            "level": "high",
            "title": "检测到明显卡顿",
            "evidence": f"超过 {thresholds['stutter_frame_time_ms']} ms 的帧时间事件累计 {total_stutter} 次。",
            "suggestion": "查看报告中的采样时间线，重点对照显存占用、磁盘忙碌度和 CPU 峰值。",
        })

    max_gpu_load = summary.get("max_gpu_load_percent")
    max_gpu_temp = summary.get("max_gpu_temperature_c")
    used_vram = summary.get("max_gpu_memory_used_mb")
    total_vram = summary.get("gpu_memory_total_mb")
    if max_gpu_load is not None and max_gpu_load >= thresholds["gpu_load_percent"] and avg_fps is not None:
        findings.append({
            "level": "medium",
            "title": "GPU 可能处于性能瓶颈",
            "evidence": f"GPU 负载峰值 {max_gpu_load}%，平均 FPS {avg_fps}。",
            "suggestion": "降低 GPU 压力较大的画质项，如分辨率、光追、阴影、反射和抗锯齿。",
        })
    if max_gpu_temp is not None and max_gpu_temp >= thresholds["gpu_temperature_c"]:
        findings.append({
            "level": "high",
            "title": "GPU 温度过高",
            "evidence": f"GPU 温度峰值达到 {max_gpu_temp}°C。",
            "suggestion": "检查机箱风道、显卡风扇曲线和灰尘；若伴随频率下降，可能出现温度降频。",
        })
    if used_vram is not None and total_vram:
        vram_percent = used_vram / total_vram * 100
        if vram_percent >= thresholds["gpu_vram_percent"]:
            findings.append({
                "level": "high",
                "title": "显存占用接近上限",
                "evidence": f"显存峰值约 {used_vram} MB / {total_vram} MB，约 {vram_percent:.1f}%。",
                "suggestion": "降低纹理质量、分辨率、光追材质包或高清材质 MOD。",
            })

    if max_gpu_load is not None and max_gpu_load < 70 and summary.get("max_process_cpu_percent", 0) >= thresholds["process_cpu_percent"]:
        findings.append({
            "level": "medium",
            "title": "疑似 CPU 瓶颈",
            "evidence": f"游戏 CPU 峰值 {summary['max_process_cpu_percent']}%，但 GPU 负载峰值仅 {max_gpu_load}%。",
            "suggestion": "降低 CPU 相关设置，关闭后台任务；如果游戏支持，尝试 DX12/Vulkan 或调整线程相关选项。",
        })

    driver_event = has_event(events, ["display driver", "nvlddmkm", "amdkmdag", "atikmdag", "dxgi", "tdr"])
    if driver_event:
        findings.append({
            "level": "high",
            "title": "发现显卡驱动或显示子系统异常事件",
            "evidence": f"{driver_event.timestamp} {driver_event.provider} Event {driver_event.event_id}：{driver_event.message[:180]}",
            "suggestion": "优先检查显卡驱动版本、显卡/显存超频、电源供电和温度；必要时回滚到稳定驱动。",
        })

    app_event = has_event(events, ["application error", "windows error reporting", "faulting application", "崩溃", "错误应用程序"])
    if app_event:
        findings.append({
            "level": "high",
            "title": "发现应用崩溃事件",
            "evidence": f"{app_event.timestamp} {app_event.provider} Event {app_event.event_id}：{app_event.message[:180]}",
            "suggestion": "结合事件中的故障模块检查游戏文件完整性、MOD、覆盖层、运行库和显卡驱动。",
        })

    if not findings:
        findings.append({
            "level": "info",
            "title": "未发现明显资源瓶颈",
            "evidence": "采集范围内 CPU、内存、磁盘、FPS/GPU 增强指标未超过阈值，或增强采集器未启用。",
            "suggestion": "如果问题仍然存在，请启用 PresentMon、NVML 或 LibreHardwareMonitor 后重新采集。",
        })
    return findings


def query_windows_events(
    start_time: Optional[dt.datetime],
    process_names: Sequence[str],
    config: Dict[str, Any],
) -> List[WindowsEvent]:
    event_config = config.get("windows_events", {})
    if platform.system() != "Windows" or not event_config.get("enabled", True):
        return []

    if start_time is None:
        minutes = int(event_config.get("lookback_minutes_when_start_unknown", 10))
        start_time = dt.datetime.now() - dt.timedelta(minutes=minutes)

    max_events = int(event_config.get("max_events", 40))
    keywords = [Path(name).stem.lower() for name in process_names]
    keywords.extend(["display", "nvlddmkm", "amdkmdag", "atikmdag", "dxgi", "tdr", "application error", "windows error reporting"])

    script = rf"""
$start = [datetime]::Parse("{start_time.isoformat()}")
$events = Get-WinEvent -FilterHashtable @{{LogName=@("Application","System"); Level=@(1,2,3); StartTime=$start}} -ErrorAction SilentlyContinue |
  Select-Object -First {max_events} TimeCreated,LogName,ProviderName,Id,LevelDisplayName,Message
$events | ConvertTo-Json -Compress
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if not completed.stdout.strip():
        return []

    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict):
        raw_events = [raw]
    elif isinstance(raw, list):
        raw_events = raw
    else:
        return []

    events: List[WindowsEvent] = []
    for item in raw_events:
        message = str(item.get("Message") or "").replace("\r", " ").replace("\n", " ")
        provider = str(item.get("ProviderName") or "")
        combined = f"{provider} {message}".lower()
        if not any(keyword and keyword in combined for keyword in keywords):
            continue
        events.append(WindowsEvent(
            timestamp=str(item.get("TimeCreated") or ""),
            log_name=str(item.get("LogName") or ""),
            provider=provider,
            event_id=int(item.get("Id") or 0),
            level=str(item.get("LevelDisplayName") or ""),
            message=message[:1000],
        ))
    return events[:max_events]


def write_reports(
    process_names: List[str],
    profile: Dict[str, Any],
    samples: List[Sample],
    findings: List[Dict[str, str]],
    events: List[WindowsEvent],
    config: Dict[str, Any],
    capabilities: Dict[str, Any],
) -> Dict[str, str]:
    report_dir = Path(config.get("reports_dir") or "reports")
    report_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(ch for ch in profile.get("name", "game") if ch.isalnum() or ch in ("-", "_")) or "game"
    base = report_dir / f"game-monitor-{safe_name}-{stamp}"
    payload = {
        "stage": 2,
        "target_process_names": process_names,
        "game_profile": profile,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "machine": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
        },
        "capabilities": capabilities,
        "summary": summarize(samples),
        "findings": findings,
        "windows_events": [dataclasses.asdict(event) for event in events],
        "samples": [dataclasses.asdict(sample) for sample in samples],
    }

    json_path = base.with_suffix(".json")
    html_path = base.with_suffix(".html")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    return {"json": str(json_path), "html": str(html_path)}


def render_html(payload: Dict[str, Any]) -> str:
    findings = "\n".join(
        f"<li class=\"{html.escape(item['level'])}\"><strong>{html.escape(item['title'])}</strong><br>"
        f"证据：{html.escape(item['evidence'])}<br>"
        f"建议：{html.escape(item['suggestion'])}</li>"
        for item in payload["findings"]
    )
    summary_rows = "\n".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in payload["summary"].items()
    )
    event_rows = "\n".join(
        f"<tr><td>{html.escape(str(event['timestamp']))}</td>"
        f"<td>{html.escape(str(event['log_name']))}</td>"
        f"<td>{html.escape(str(event['provider']))}</td>"
        f"<td>{html.escape(str(event['event_id']))}</td>"
        f"<td>{html.escape(str(event['level']))}</td>"
        f"<td>{html.escape(str(event['message'][:280]))}</td></tr>"
        for event in payload["windows_events"]
    )
    sample_rows = "\n".join(
        f"<tr><td>{html.escape(sample['timestamp'])}</td>"
        f"<td>{html.escape(str(sample.get('average_fps')))}</td>"
        f"<td>{html.escape(str(sample.get('one_percent_low_fps')))}</td>"
        f"<td>{html.escape(str(sample.get('max_frame_time_ms')))}</td>"
        f"<td>{html.escape(str(sample.get('stutter_count')))}</td>"
        f"<td>{html.escape(str(sample.get('process_cpu_percent')))}</td>"
        f"<td>{html.escape(str(sample.get('system_memory_percent')))}</td>"
        f"<td>{html.escape(str(sample.get('gpu_load_percent')))}</td>"
        f"<td>{html.escape(str(sample.get('gpu_temperature_c')))}</td>"
        f"<td>{html.escape(str(sample.get('gpu_memory_used_mb')))}</td></tr>"
        for sample in payload["samples"][-120:]
    )
    capability_rows = "\n".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in payload["capabilities"].items()
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>游戏监测诊断报告</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; line-height: 1.6; color: #1f2933; }}
    table {{ border-collapse: collapse; width: 100%; margin: 10px 0 24px; }}
    th, td {{ border: 1px solid #dde2e8; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ width: 260px; background: #f5f7fa; }}
    li {{ margin-bottom: 14px; }}
    .high strong {{ color: #b42318; }}
    .medium strong {{ color: #b54708; }}
    .info strong {{ color: #175cd3; }}
    .muted {{ color: #667085; }}
    .samples td {{ font-size: 13px; }}
  </style>
</head>
<body>
  <h1>游戏监测诊断报告</h1>
  <p class="muted">生成时间：{html.escape(payload["generated_at"])} ｜ 阶段：Stage {payload["stage"]}</p>
  <h2>游戏档案</h2>
  <table>
    <tr><th>名称</th><td>{html.escape(str(payload["game_profile"].get("name")))}</td></tr>
    <tr><th>进程</th><td>{html.escape(", ".join(payload["target_process_names"]))}</td></tr>
  </table>
  <h2>采集能力</h2>
  <table>{capability_rows}</table>
  <h2>摘要</h2>
  <table>{summary_rows}</table>
  <h2>可能原因</h2>
  <ol>{findings}</ol>
  <h2>Windows 事件</h2>
  <table>
    <tr><th>时间</th><th>日志</th><th>来源</th><th>ID</th><th>级别</th><th>消息</th></tr>
    {event_rows or '<tr><td colspan="6">未采集到相关事件，或当前系统不支持 Windows Event Log 采集。</td></tr>'}
  </table>
  <h2>最近采样</h2>
  <table class="samples">
    <tr><th>时间</th><th>FPS</th><th>1% Low</th><th>最大帧时间</th><th>卡顿数</th><th>进程 CPU</th><th>系统内存</th><th>GPU 负载</th><th>GPU 温度</th><th>显存 MB</th></tr>
    {sample_rows}
  </table>
</body>
</html>
"""


def run_monitor(config: Dict[str, Any], process_names: List[str]) -> int:
    require_psutil()
    interval = float(config["sample_interval_seconds"])
    max_samples = max(1, int(config["history_minutes"] * 60 / interval))
    reports_dir = Path(config.get("reports_dir") or "reports")
    samples: Deque[Sample] = collections.deque(maxlen=max_samples)
    disk_sampler = DiskBusySampler()
    profile = select_game_profile(config, process_names)
    thresholds = effective_thresholds(config, profile)
    hardware_sampler = HardwareSampler(config)

    print(f"正在等待游戏进程：{', '.join(process_names)}")
    proc = None
    while proc is None:
        proc = find_process(process_names)
        if proc is None:
            time.sleep(interval)

    start_time = dt.datetime.now()
    process_name = proc.name()
    print(f"已发现游戏进程：{process_name} pid={proc.pid}")
    print(f"游戏档案：{profile.get('name', '未命名游戏')}")
    print(f"GPU 增强采集：{hardware_sampler.capability_text()}")
    proc.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None)

    presentmon_process = PresentMonProcess(config, process_name, reports_dir)
    csv_path = presentmon_process.start()
    fps_sampler = PresentMonCsvSampler(
        csv_path,
        process_name,
        float(thresholds["stutter_frame_time_ms"]),
    ) if csv_path else None
    if fps_sampler:
        print(f"FPS/帧时间采集 CSV：{csv_path}")
    else:
        print("FPS/帧时间采集未启用。可在 config 中配置 presentmon.csv_path 或 executable_path。")

    try:
        while True:
            try:
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    break
                sample = capture_sample(proc, disk_sampler, fps_sampler, hardware_sampler)
                samples.append(sample)
                disk_text = "n/a" if sample.disk_busy_percent is None else f"{sample.disk_busy_percent}%"
                fps_text = "n/a" if sample.average_fps is None else f"{sample.average_fps} FPS"
                gpu_text = "n/a"
                if sample.gpu_load_percent is not None or sample.gpu_temperature_c is not None:
                    gpu_text = f"{sample.gpu_load_percent or 'n/a'}% / {sample.gpu_temperature_c or 'n/a'}°C"
                print(
                    f"[{sample.timestamp}] FPS {fps_text} | CPU {sample.process_cpu_percent}% | "
                    f"内存 {sample.process_memory_mb} MB | 系统内存 {sample.system_memory_percent}% | "
                    f"磁盘 {disk_text} | GPU {gpu_text}"
                )
                time.sleep(interval)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                break
            except psutil.AccessDenied:
                print("权限不足，无法继续读取该进程。请尝试以管理员权限运行。")
                break
    finally:
        presentmon_process.stop()

    sample_list = list(samples)
    events = query_windows_events(start_time, process_names, config)
    findings = analyze(sample_list, thresholds, events)
    capabilities = {
        "presentmon_csv": csv_path or "未启用",
        "gpu_samplers": hardware_sampler.capability_text(),
        "windows_events": "启用" if platform.system() == "Windows" and config.get("windows_events", {}).get("enabled", True) else "未启用或非 Windows",
    }
    paths = write_reports(process_names, profile, sample_list, findings, events, config, capabilities)
    print("游戏进程已退出，诊断报告已生成：")
    print(f"- JSON: {paths['json']}")
    print(f"- HTML: {paths['html']}")
    return 0


def configured_processes(config: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for game in config.get("games", []):
        names.extend(game.get("process_names", []))
    return names


def self_test(config: Optional[Dict[str, Any]] = None) -> int:
    require_psutil()
    config = config or json.loads(json.dumps(DEFAULT_CONFIG))
    hardware_sampler = HardwareSampler(config)
    print("psutil 可用")
    print(f"系统：{platform.platform()}")
    print(f"CPU 核心数：{os.cpu_count()}")
    print(f"系统 CPU：{psutil.cpu_percent(interval=0.2)}%")
    print(f"系统内存：{psutil.virtual_memory().percent}%")
    print("可读取进程列表：是")
    print(f"NVML 依赖：{'可用' if pynvml is not None else '未安装'}")
    print(f"LibreHardwareMonitor WMI 依赖：{'可用' if wmi is not None else '未安装'}")
    print(f"GPU 增强采集器：{hardware_sampler.capability_text()}")
    print(f"Windows Event Log：{'可采集' if platform.system() == 'Windows' else '当前不是 Windows，跳过'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="游戏运行健康监测 Stage 2")
    parser.add_argument("--process", action="append", help="要监控的游戏进程名，可重复传入")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--fps-csv", help="PresentMon 已生成或正在生成的 CSV 路径")
    parser.add_argument("--self-test", action="store_true", help="运行采集能力自检")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.fps_csv:
        config["presentmon"]["csv_path"] = args.fps_csv

    if args.self_test:
        return self_test(config)

    process_names = args.process or configured_processes(config)
    if not process_names:
        print("请通过 --process 指定游戏进程名，或在配置文件中添加 games.process_names。")
        return 2
    return run_monitor(config, process_names)


if __name__ == "__main__":
    raise SystemExit(main())
