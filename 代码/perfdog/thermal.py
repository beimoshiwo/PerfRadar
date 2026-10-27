# -*- coding: utf-8 -*-
"""温度采集器。

两条通路（按设备自动选择）：
1. ``/sys/class/thermal/thermal_zone*/{type,temp}``（Android ≤9，一次批量读）；
2. ``dumpsys thermalservice``（Android 10+ 的 ThermalService，真机验证
   Mate 10/EMUI：sysfs_thermal 在 shell 域被整体拒绝，ThermalService 仍可读）。

单位归一化（本机 MI 5s 实测，各热区单位并不统一）：
  · ``bms`` / ``pm8994_tz``  毫度  32200 → 32.2℃
  · ``tsens_tz_sensor*``     十分度 496   → 49.6℃
  · ``msm_therm`` / ``pa_therm`` 度   40    → 40℃
按量级自动判定，避免 0.4℃ / 496℃ 这类离谱值。
ThermalService 通路的值本身是 ℃，无需换算。
"""

from __future__ import annotations

import re

from .transport import AdbTransport


class ThermalCollector:
    """温度采集器（免 root）。"""

    # 逐区配对（type=temp 单行输出），跳过不可读/空区 —— 避免「两次 glob 行数不一致」导致的错位
    # （实测小米8 有 73 个热区，其中个别 temp 在 shell 域读不到，按行号对齐会整体错位）。
    SAMPLE_CMD = (
        "for z in /sys/class/thermal/thermal_zone*; do "
        "t=$(cat $z/type 2>/dev/null); v=$(cat $z/temp 2>/dev/null); "
        "[ -n \"$t\" ] && [ -n \"$v\" ] && echo \"$t=$v\"; done"
    )
    THERMAL_SERVICE_CMD = "dumpsys thermalservice"

    # CPU/GPU 片上传感器关键字（tsens_tz_sensor* / cpu-*-*-* / 麒麟 cluster*）
    CPU_ZONE_MARKERS = ("tsens", "cpu", "cluster")
    # SoC 封装温候选区名（高通 msm_therm；麒麟 system_h；其他平台按序回退）
    SOC_PKG_ZONES = ("msm_therm", "cpu-0-0-usr", "cpu-1-0-usr", "soc_tz",
                     "system_h", "soc")
    # GPU 温度候选区名（精确名）；另支持前缀 gpu（如 gpu0-usr / gpu1-usr）见 _derive
    GPU_ZONE_NAMES = ("gpu", "gpu-thermal", "gpu_therm")

    RE_TS_TEMP = re.compile(
        r"Temperature\{mValue=([\d.]+),\s*mType=(\d+),\s*mName=([^,\s]+)")

    def __init__(self, transport: AdbTransport) -> None:
        self.transport = transport
        self._svc_fallback = False    # sysfs 热区不可读 → 记住走 ThermalService，
                                      # 不再每样空跑一遍被 SELinux 拒的 sysfs（省 ~1 次调用）

    @staticmethod
    def _to_c(v: int) -> float:
        """热区原始值 → ℃（按量级判定单位）。"""
        if v > 10000:        # 毫度
            return v / 1000.0
        if v > 200:          # 十分度
            return v / 10.0
        return float(v)      # 度

    @classmethod
    def _derive(cls, zones: dict) -> tuple:
        """由 {区名: ℃} 派生 (最高温, CPU最高, SoC封装, GPU温度)。"""
        max_temp = cpu_max = soc_pkg = gpu_temp = None
        for name, tc in zones.items():
            # 合理性过滤：部分 thermal_zone 实为电压/电流传感器（vbat/ibat/soc/pa_therm），
            # 被误当温度换算出 429℃/-400℃，此类值直接跳过聚合（通用、不写死排除名单）。
            if not (-40.0 <= tc <= 125.0):
                continue
            if max_temp is None or tc > max_temp:
                max_temp = tc
            if any(mk in name for mk in cls.CPU_ZONE_MARKERS):
                cpu_max = tc if cpu_max is None else max(cpu_max, tc)
            if soc_pkg is None and name in cls.SOC_PKG_ZONES:
                soc_pkg = tc
            # GPU 区：精确名（gpu/gpu-thermal/gpu_therm）或前缀 gpu（如 gpu0-usr/gpu1-usr）；
            # 排除虚拟上限区（gpu-virt-max-step 等）。取实际传感器最高温。
            is_gpu = (name in cls.GPU_ZONE_NAMES) or (
                name.startswith("gpu") and "virt" not in name and "max-step" not in name)
            if is_gpu:
                gpu_temp = tc if gpu_temp is None else max(gpu_temp, tc)
        r = lambda v: round(v, 1) if v is not None else None
        return r(max_temp), r(cpu_max), r(soc_pkg), r(gpu_temp)

    @classmethod
    def _parse(cls, out) -> tuple:
        """解析 sysfs 逐区输出（``name=temp`` 单行）→ (zones, max, cpu_max, soc_pkg, gpu_temp)。"""
        zones: dict = {}
        if not out:
            return {}, None, None, None, None
        for line in out.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            name, _, val = line.partition("=")
            name = name.strip()
            try:
                t = int(val.strip())
            except ValueError:
                continue
            zones[name] = round(cls._to_c(t), 1)
        if not zones:
            return {}, None, None, None, None
        max_temp, cpu_max, soc_pkg, gpu_temp = cls._derive(zones)
        return zones, max_temp, cpu_max, soc_pkg, gpu_temp

    @classmethod
    def _parse_service(cls, out) -> tuple:
        """解析 dumpsys thermalservice → 同上（值本身就是 ℃）。"""
        zones: dict = {}
        for m in cls.RE_TS_TEMP.finditer(out or ""):
            try:
                zones[m.group(3)] = round(float(m.group(1)), 1)
            except ValueError:
                continue
        if not zones:
            return {}, None, None, None, None
        max_temp, cpu_max, soc_pkg, gpu_temp = cls._derive(zones)
        return zones, max_temp, cpu_max, soc_pkg, gpu_temp

    def sample(self) -> dict:
        if not self._svc_fallback:
            out = self.transport.shell(self.SAMPLE_CMD)
            zones, max_temp, cpu_max, soc_pkg, gpu_temp = self._parse(out)
            if max_temp is not None:
                return {
                    "zones": zones,
                    "max_temp_c": max_temp,
                    "soc_cpu_max_c": cpu_max,     # CPU 最高结温
                    "soc_pkg_c": soc_pkg,         # SoC 封装/板级温
                    "gpu_temp_c": gpu_temp,       # GPU 温度
                }
            if out and out.strip():
                # sysfs 有输出但解析不出有效热区（安卓 10+ SELinux 拒读的典型现象）
                # → 记住走 ThermalService；输出为空（传输抖动）不记住，下轮重试
                self._svc_fallback = True
        # ThermalService 通路（安卓 10+）
        zones, max_temp, cpu_max, soc_pkg, gpu_temp = self._parse_service(
            self.transport.shell(self.THERMAL_SERVICE_CMD))
        return {
            "zones": zones,
            "max_temp_c": max_temp,
            "soc_cpu_max_c": cpu_max,     # CPU 最高结温
            "soc_pkg_c": soc_pkg,         # SoC 封装/板级温
            "gpu_temp_c": gpu_temp,       # GPU 温度
        }
