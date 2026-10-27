# -*- coding: utf-8 -*-
"""PerfDog 克隆 · 代码库包入口。

分层结构：
  · transport   —— ADB 通信层（设备发现 / shell / run / 离线 mock）
  · gpu / cpu / fps / memory / thermal / battery / logcat_gpu —— 各类采集器
  · session      —— 采样会话（并行采样 + 节奏控制 + 导出）
  · report       —— HTML 报告导出

产品（可运行程序）在 ``产品/PerfDog.app``：双击即开的实时 GUI 监控软件。
"""

from __future__ import annotations

from .transport import AdbTransport
from .gpu import GPUCollector
from .logcat_gpu import LogcatGPUCollector
from .cpu import CPUCollector
from .fps import FPSCollector
from .memory import MemoryCollector
from .thermal import ThermalCollector
from .battery import BatteryCollector
from .session import PerfSession, mock_demo
from .report import build_html_report, write_html

__all__ = [
    "AdbTransport", "GPUCollector", "LogcatGPUCollector", "CPUCollector",
    "FPSCollector", "MemoryCollector", "ThermalCollector", "BatteryCollector",
    "PerfSession", "mock_demo", "build_html_report", "write_html",
]

__version__ = "0.3.0"
