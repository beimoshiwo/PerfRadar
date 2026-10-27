# -*- coding: utf-8 -*-
"""CPU 采集器：``/proc/stat`` 差分算整体/每核占用率，并逐核读取 CPU 频率。

PerfDog 风格设计（对比 Kite 分簇更细、更通用）：
 - ``cpu_total``     : 整机 CPU 占用率(%)。
 - ``per_core``      : 每个核占用率 {核号: %}（可看单独某个核心）。
 - ``cpu_freq_mhz``  : 当前最大核频率(MHz)。
 - ``core_freq_mhz`` : 每个核频率 {核号: MHz}（逐核，不按大小核分簇）。
"""

from __future__ import annotations

import re

from .transport import AdbTransport


class CPUCollector:
    """CPU 采集器（免 root）。"""

    def __init__(self, transport: AdbTransport) -> None:
        self.transport = transport
        self._prev_total = None
        self._prev_cores: dict[int, list[int]] = {}

    # 一次调用同时读 /proc/stat 与逐核频率（2026-10-26 采样提速）：
    # 每个核一行 ``F<i> <kHz>`` 标记（取不到为空），按标记归属核号，杜绝错位；
    # mksh 无花括号展开，核号显式枚举 0-9（覆盖现有全部 Android 机型）。
    _FREQ_CORES = range(10)
    _BATCH_CMD = "; ".join(
        ["cat /proc/stat 2>/dev/null"]
        + ["v=$(cat /sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq 2>/dev/null); "
           "[ -z \"$v\" ] && v=$(cat /sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_cur_freq 2>/dev/null); "
           "echo \"F%d $v\"" % (i, i, i) for i in _FREQ_CORES])

    @staticmethod
    def _usage(prev, cur) -> float:
        if prev is None or len(prev) < 4 or len(cur) < 4:
            return 0.0
        dtotal = sum(cur) - sum(prev)
        didle = cur[3] - prev[3]
        if dtotal <= 0:
            return 0.0
        return max(0.0, min(100.0, (dtotal - didle) / dtotal * 100.0))

    @staticmethod
    def _parse(out) -> tuple:
        total = None
        cores: dict[int, list[int]] = {}
        for line in (out or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "cpu":
                total = [int(x) for x in parts[1:]]
            elif parts[0].startswith("cpu") and parts[0][3:].isdigit():
                idx = int(parts[0][3:])
                cores[idx] = [int(x) for x in parts[1:]]
        return total, cores

    def sample(self) -> dict:
        out = self.transport.shell(self._BATCH_CMD)
        stat_lines: list = []
        freqs: dict[int, int] = {}
        for line in (out or "").splitlines():
            m = re.match(r"^F(\d+)\s+(\d+)$", line.strip())
            if m:                                   # 核频率标记行
                freqs[int(m.group(1))] = int(m.group(2))
            else:                                   # /proc/stat 行
                stat_lines.append(line)
        cur_total, cur_cores = self._parse("\n".join(stat_lines))
        cpu_total = 0.0
        if self._prev_total is not None and cur_total:
            cpu_total = self._usage(self._prev_total, cur_total)
        per_core: dict[int, float] = {}
        for idx in cur_cores:
            if idx in self._prev_cores:
                per_core[idx] = round(self._usage(self._prev_cores[idx], cur_cores[idx]), 1)
            else:
                per_core[idx] = 0.0
        self._prev_total = cur_total
        self._prev_cores = cur_cores

        core_freq: dict = {}
        for idx in cur_cores:
            if idx in freqs:
                core_freq[idx] = round(freqs[idx] / 1000.0, 1)
        overall = max(core_freq.values()) if core_freq else None
        return {
            "cpu_total": round(cpu_total, 1),
            "per_core": per_core,
            "cpu_freq_mhz": round(overall, 1) if overall is not None else None,
            "core_freq_mhz": core_freq,
            "cores": len(cur_cores),
        }
