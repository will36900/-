# 游戏监测 MVP

这是“游戏运行健康监测与崩溃诊断工具”的最小可用版本。

它当前支持：

- 按进程名识别正在运行的游戏
- 实时采集游戏进程 CPU、内存、线程数、句柄/文件描述符数量
- 采集系统 CPU、内存、磁盘忙碌度
- 保存最近几分钟采样数据
- 游戏进程退出后自动生成 JSON 和 HTML 诊断报告
- 基于规则输出可能原因和证据

## 安装

```bash
python3 -m pip install -r requirements.txt
```

## 快速运行

监控某个游戏进程名，例如 `Cyberpunk2077.exe`：

```bash
python3 game_monitor.py --process Cyberpunk2077.exe
```

使用配置文件：

```bash
python3 game_monitor.py --config config.example.json
```

只运行一次自检，确认依赖和采集能力：

```bash
python3 game_monitor.py --self-test
```

## 输出

报告会生成到：

```text
reports/
```

每次游戏退出后会生成：

- `*.json`：结构化诊断数据
- `*.html`：可读报告

## 当前限制

MVP 版不做注入、不读取游戏内存、不绕过反作弊系统。FPS、GPU 温度、显存、Windows 事件日志会在第二阶段通过 PresentMon、LibreHardwareMonitor、Windows Event Log 接入。
