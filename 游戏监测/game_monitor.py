#!/usr/bin/env python3
"""
Game Monitor MVP

Monitors a target game process, records recent system/process samples, and
generates a diagnostic report when the process exits.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import html
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - exercised by user environment.
    psutil = None


DEFAULT_CONFIG = {
    "games": [],
    "sample_interval_seconds": 2,
    "history_minutes": 5,
    "thresholds": {
        "system_memory_percent": 90,
        "process_cpu_percent": 85,
        "process_memory_mb": 8192,
        "disk_busy_percent": 80,
    },
}


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


def load_config(path: Optional[str]) -> Dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if not path:
        return config

    with open(path, "r", encoding="utf-8") as file:
        user_config = json.load(file)

    for key, value in user_config.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def require_psutil() -> None:
    if psutil is None:
        print("缺少依赖 psutil。请先运行：python3 -m pip install -r requirements.txt")
        sys.exit(2)


def normalize_names(names: Iterable[str]) -> List[str]:
    return [name.lower() for name in names if name]


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


def capture_sample(proc: Any, disk_sampler: DiskBusySampler) -> Sample:
    with proc.oneshot():
        memory = proc.memory_info().rss / 1024 / 1024
        return Sample(
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
        )


def summarize(samples: List[Sample]) -> Dict[str, Any]:
    if not samples:
        return {}

    def max_of(field: str) -> Optional[float]:
        values = [getattr(sample, field) for sample in samples if getattr(sample, field) is not None]
        return max(values) if values else None

    def avg_of(field: str) -> Optional[float]:
        values = [getattr(sample, field) for sample in samples if getattr(sample, field) is not None]
        return round(sum(values) / len(values), 2) if values else None

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
    }


def analyze(samples: List[Sample], thresholds: Dict[str, Any]) -> List[Dict[str, str]]:
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

    if not findings:
        findings.append({
            "level": "info",
            "title": "未发现明显系统资源瓶颈",
            "evidence": "MVP 采集范围内 CPU、内存、磁盘未超过阈值。",
            "suggestion": "下一步应接入 FPS、GPU、显存、驱动事件和游戏日志进一步定位。",
        })
    return findings


def write_reports(process_names: List[str], samples: List[Sample], findings: List[Dict[str, str]]) -> Dict[str, str]:
    report_dir = Path("reports")
    report_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = report_dir / f"game-monitor-report-{stamp}"
    payload = {
        "target_process_names": process_names,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "machine": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
        },
        "summary": summarize(samples),
        "findings": findings,
        "samples": [dataclasses.asdict(sample) for sample in samples],
    }

    json_path = base.with_suffix(".json")
    html_path = base.with_suffix(".html")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    return {"json": str(json_path), "html": str(html_path)}


def render_html(payload: Dict[str, Any]) -> str:
    findings = "\n".join(
        f"<li><strong>{html.escape(item['title'])}</strong><br>"
        f"证据：{html.escape(item['evidence'])}<br>"
        f"建议：{html.escape(item['suggestion'])}</li>"
        for item in payload["findings"]
    )
    summary_rows = "\n".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in payload["summary"].items()
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>游戏监测诊断报告</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; line-height: 1.6; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 900px; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 10px; text-align: left; }}
    th {{ width: 260px; background: #f6f7f8; }}
    li {{ margin-bottom: 14px; }}
  </style>
</head>
<body>
  <h1>游戏监测诊断报告</h1>
  <p>生成时间：{html.escape(payload["generated_at"])}</p>
  <h2>摘要</h2>
  <table>{summary_rows}</table>
  <h2>可能原因</h2>
  <ol>{findings}</ol>
</body>
</html>
"""


def run_monitor(config: Dict[str, Any], process_names: List[str]) -> int:
    require_psutil()
    interval = float(config["sample_interval_seconds"])
    max_samples = max(1, int(config["history_minutes"] * 60 / interval))
    samples: Deque[Sample] = collections.deque(maxlen=max_samples)
    disk_sampler = DiskBusySampler()

    print(f"正在等待游戏进程：{', '.join(process_names)}")
    proc = None
    while proc is None:
        proc = find_process(process_names)
        if proc is None:
            time.sleep(interval)

    print(f"已发现游戏进程：{proc.name()} pid={proc.pid}")
    proc.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None)

    while True:
        try:
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                break
            sample = capture_sample(proc, disk_sampler)
            samples.append(sample)
            disk_text = "n/a" if sample.disk_busy_percent is None else f"{sample.disk_busy_percent}%"
            print(
                f"[{sample.timestamp}] CPU {sample.process_cpu_percent}% | "
                f"内存 {sample.process_memory_mb} MB | 系统内存 {sample.system_memory_percent}% | "
                f"磁盘 {disk_text}"
            )
            time.sleep(interval)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            break
        except psutil.AccessDenied:
            print("权限不足，无法继续读取该进程。请尝试以管理员权限运行。")
            break

    sample_list = list(samples)
    findings = analyze(sample_list, config["thresholds"])
    paths = write_reports(process_names, sample_list, findings)
    print("游戏进程已退出，诊断报告已生成：")
    print(f"- JSON: {paths['json']}")
    print(f"- HTML: {paths['html']}")
    return 0


def configured_processes(config: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for game in config.get("games", []):
        names.extend(game.get("process_names", []))
    return names


def self_test() -> int:
    require_psutil()
    print("psutil 可用")
    print(f"系统：{platform.platform()}")
    print(f"CPU 核心数：{os.cpu_count()}")
    print(f"系统 CPU：{psutil.cpu_percent(interval=0.2)}%")
    print(f"系统内存：{psutil.virtual_memory().percent}%")
    print("可读取进程列表：是")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="游戏运行健康监测 MVP")
    parser.add_argument("--process", action="append", help="要监控的游戏进程名，可重复传入")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--self-test", action="store_true", help="运行采集能力自检")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    config = load_config(args.config)
    process_names = args.process or configured_processes(config)
    if not process_names:
        print("请通过 --process 指定游戏进程名，或在配置文件中添加 games.process_names。")
        return 2
    return run_monitor(config, process_names)


if __name__ == "__main__":
    raise SystemExit(main())
