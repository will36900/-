#!/usr/bin/env python3
"""
Friendly terminal launcher for Game Monitor.

This file intentionally keeps the experience simple: users can double-click a
launcher script, choose a menu item, and avoid memorizing command-line flags.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import game_monitor


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
EXAMPLE_CONFIG_PATH = APP_DIR / "config.example.json"


def clear_screen() -> None:
    os.system("cls" if platform.system() == "Windows" else "clear")


def pause(message: str = "按 Enter 返回菜单...") -> None:
    input(f"\n{message}")


def title() -> None:
    print("=" * 58)
    print(" 游戏监测工具")
    print("=" * 58)
    print("用于监控游戏进程、记录卡顿/崩溃线索，并生成诊断报告。")
    print()


def ensure_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        return game_monitor.load_config(str(CONFIG_PATH))
    if EXAMPLE_CONFIG_PATH.exists():
        config = game_monitor.load_config(str(EXAMPLE_CONFIG_PATH))
    else:
        config = game_monitor.load_config(None)
    save_config(config)
    return config


def save_config(config: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def input_default(prompt: str, default: str = "") -> str:
    if default:
        value = input(f"{prompt} [{default}]: ").strip()
        return value or default
    return input(f"{prompt}: ").strip()


def choose_game(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    games = config.get("games", [])
    if not games:
        print("还没有游戏档案。请先选择“添加游戏档案”。")
        return None

    print("请选择要监控的游戏：")
    for index, game in enumerate(games, start=1):
        names = ", ".join(game.get("process_names", [])) or "未填写进程名"
        print(f"{index}. {game.get('name', '未命名游戏')}  ({names})")
    print("0. 返回")

    choice = input("\n输入编号: ").strip()
    if choice == "0":
        return None
    try:
        index = int(choice) - 1
    except ValueError:
        print("请输入列表里的数字。")
        return None
    if index < 0 or index >= len(games):
        print("没有这个编号。")
        return None
    return games[index]


def start_monitor(config: Dict[str, Any]) -> None:
    clear_screen()
    title()
    game = choose_game(config)
    if not game:
        pause()
        return

    process_names = game.get("process_names", [])
    if not process_names:
        print("这个游戏档案没有进程名。请编辑配置后再试。")
        pause()
        return

    print()
    print("接下来请先打开游戏。如果游戏已经运行，程序会自动识别。")
    print("监控开始后，保持这个窗口打开即可。游戏退出后会自动生成报告。")
    print()
    input("准备好了按 Enter 开始监控...")
    game_monitor.run_monitor(config, process_names)
    pause("报告已生成。按 Enter 返回菜单...")


def running_process_choices() -> List[Dict[str, Any]]:
    game_monitor.require_psutil()
    choices: List[Dict[str, Any]] = []
    current_pid = os.getpid()
    for proc in game_monitor.psutil.process_iter(["pid", "name", "exe", "username"]):
        try:
            name = proc.info.get("name") or ""
            exe = proc.info.get("exe") or ""
            if not name or proc.info.get("pid") == current_pid:
                continue
            memory_mb = proc.memory_info().rss / 1024 / 1024
            if memory_mb < 80:
                continue
            choices.append({
                "pid": proc.info.get("pid"),
                "name": name,
                "exe": exe,
                "memory_mb": round(memory_mb, 1),
            })
        except Exception:
            continue
    choices.sort(key=lambda item: item["memory_mb"], reverse=True)
    return choices[:30]


def monitor_running_process(config: Dict[str, Any]) -> None:
    clear_screen()
    title()
    print("从正在运行的程序中选择")
    print()
    print("请先启动游戏，然后在下面列表中选择它。通常游戏会排在靠前位置。")
    print()

    try:
        choices = running_process_choices()
    except SystemExit:
        pause()
        return

    if not choices:
        print("没有找到合适的运行中程序。请先启动游戏，或使用“添加游戏档案”。")
        pause()
        return

    for index, item in enumerate(choices, start=1):
        exe_text = item["exe"] or "路径不可读"
        print(f"{index:2d}. {item['name']}  pid={item['pid']}  内存={item['memory_mb']} MB")
        print(f"    {exe_text}")
    print(" 0. 返回")
    print()

    choice = input("输入编号: ").strip()
    if choice == "0":
        return
    try:
        index = int(choice) - 1
    except ValueError:
        print("请输入列表里的数字。")
        pause()
        return
    if index < 0 or index >= len(choices):
        print("没有这个编号。")
        pause()
        return

    selected = choices[index]
    process_name = selected["name"]
    print()
    print(f"将监控：{process_name}")
    save_answer = input("是否把它保存成游戏档案？输入 y 保存，直接 Enter 跳过: ").strip().lower()
    if save_answer == "y":
        default_name = Path(process_name).stem
        game_name = input_default("游戏名称", default_name)
        config.setdefault("games", []).append({
            "name": game_name,
            "process_names": [process_name],
        })
        save_config(config)
        print("已保存游戏档案。")
    print()
    input("按 Enter 开始监控...")
    game_monitor.run_monitor(config, [process_name])
    pause("报告已生成。按 Enter 返回菜单...")


def add_game_profile(config: Dict[str, Any]) -> None:
    clear_screen()
    title()
    print("添加游戏档案")
    print()
    print("提示：进程名通常像 Cyberpunk2077.exe、eldenring.exe、Starfield.exe。")
    print("如果不知道进程名，可以先启动游戏，再打开任务管理器查看。")
    print()

    name = input_default("游戏名称")
    process_text = input_default("游戏进程名，多个用逗号分隔")
    if not name or not process_text:
        print("游戏名称和进程名都不能为空。")
        pause()
        return

    process_names = [item.strip() for item in process_text.split(",") if item.strip()]
    low_fps = input_default("认为偏低的平均 FPS 阈值", "45")
    low_1pct = input_default("认为卡顿明显的 1% Low FPS 阈值", "30")

    try:
        low_fps_value = float(low_fps)
        low_1pct_value = float(low_1pct)
    except ValueError:
        print("FPS 阈值需要填写数字。")
        pause()
        return

    config.setdefault("games", []).append({
        "name": name,
        "process_names": process_names,
        "thresholds": {
            "low_average_fps": low_fps_value,
            "low_1pct_fps": low_1pct_value,
        },
    })
    save_config(config)
    print(f"\n已添加：{name}")
    pause()


def list_profiles(config: Dict[str, Any]) -> None:
    clear_screen()
    title()
    games = config.get("games", [])
    if not games:
        print("还没有游戏档案。")
    else:
        for index, game in enumerate(games, start=1):
            print(f"{index}. {game.get('name', '未命名游戏')}")
            print(f"   进程名：{', '.join(game.get('process_names', []))}")
            thresholds = game.get("thresholds", {})
            if thresholds:
                print(f"   自定义阈值：{thresholds}")
            print()
    pause()


def configure_presentmon(config: Dict[str, Any]) -> None:
    clear_screen()
    title()
    print("配置 FPS/帧时间采集")
    print()
    print("如果你还没有 PresentMon，可以先跳过。只是不采集 FPS，其他监控仍然可用。")
    print("Windows 游戏电脑上推荐填写 PresentMon.exe 的完整路径。")
    print()

    current = config.get("presentmon", {}).get("executable_path", "")
    path = input_default("PresentMon.exe 路径，留空表示不自动启动", current)
    config.setdefault("presentmon", {})["executable_path"] = path
    config["presentmon"]["enabled"] = bool(path)
    save_config(config)

    if path:
        print("\n已启用 PresentMon 自动启动。")
    else:
        print("\n未启用 PresentMon。")
    pause()


def run_self_test(config: Dict[str, Any]) -> None:
    clear_screen()
    title()
    game_monitor.self_test(config)
    pause()


def open_reports_folder(config: Dict[str, Any]) -> None:
    reports_dir = APP_DIR / str(config.get("reports_dir") or "reports")
    reports_dir.mkdir(exist_ok=True)
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(reports_dir))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", str(reports_dir)], check=False)
        else:
            subprocess.run(["xdg-open", str(reports_dir)], check=False)
        print(f"已打开报告文件夹：{reports_dir}")
    except Exception as exc:
        print(f"无法自动打开文件夹，请手动打开：{reports_dir}")
        print(f"原因：{exc}")
    pause()


def show_help() -> None:
    clear_screen()
    title()
    print("推荐使用流程：")
    print()
    print("1. 不知道进程名：先启动游戏，再选择“从正在运行的程序中选择”。")
    print("2. 知道进程名：选择“添加游戏档案”，填写 eldenring.exe 这类名称。")
    print("3. 选择“开始监控已保存游戏”。")
    print("4. 游戏崩溃或退出后，打开报告文件夹查看 HTML 报告。")
    print()
    print("说明：")
    print("- 不配置 PresentMon 也能监控 CPU、内存、磁盘、进程退出。")
    print("- 配置 PresentMon 后，可以增加 FPS、1% Low、帧时间、卡顿检测。")
    print("- Windows 上会尝试读取系统崩溃事件和显卡驱动异常事件。")
    print("- 本工具不注入游戏、不绕过反作弊。")
    pause()


def menu() -> None:
    while True:
        config = ensure_config()
        clear_screen()
        title()
        print("请选择：")
        print("1. 开始监控已保存游戏")
        print("2. 从正在运行的程序中选择")
        print("3. 添加游戏档案")
        print("4. 查看已有游戏档案")
        print("5. 配置 FPS/帧时间采集")
        print("6. 运行自检")
        print("7. 打开报告文件夹")
        print("8. 使用说明")
        print("0. 退出")
        print()
        choice = input("输入编号后按 Enter: ").strip()

        if choice == "1":
            start_monitor(config)
        elif choice == "2":
            monitor_running_process(config)
        elif choice == "3":
            add_game_profile(config)
        elif choice == "4":
            list_profiles(config)
        elif choice == "5":
            configure_presentmon(config)
        elif choice == "6":
            run_self_test(config)
        elif choice == "7":
            open_reports_folder(config)
        elif choice == "8":
            show_help()
        elif choice == "0":
            print("已退出。")
            return
        else:
            print("请输入 0 到 8 之间的数字。")
            pause()


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\n已退出。")
        sys.exit(0)
