# -*- coding: utf-8 -*-
"""内存采集器：被监控应用（前台窗口包名）的 ``/proc/<pid>/status`` 四条线，
另附系统内存 ``/proc/meminfo``（CSV 记录与兜底）。

Kite/PerfDog 风格的应用内存四指标（全部免 root，shell 域可直接读）：
  · ``alloc_mb``      申请内存      VmSize  当前申请的虚拟内存
  · ``alloc_peak_mb`` 峰值申请内存  VmPeak
  · ``rss_mb``        物理内存      VmRSS   当前实际占用的物理内存
  · ``rss_peak_mb``   峰值物理内存  VmHWM
"""

from __future__ import annotations

from .appinfo import focus_window_cached
from .transport import AdbTransport


class MemoryCollector:
    """内存采集器（免 root）。

    优先采集前台应用的进程内存；取不到应用（无窗口焦点/进程刚退出）时，
    自动回退为系统已用内存（MemTotal − MemAvailable）。
    """

    def __init__(self, transport: AdbTransport, pkg: str | None = None) -> None:
        self.transport = transport
        self.pkg = pkg
        self.last_activity: str | None = None   # 当前焦点 Activity 组件（供 API 线索）

    # ---- 前台应用 ----
    def foreground_pkg(self) -> str | None:
        """当前焦点窗口的包名（经共享缓存，与 FPS 采集器共用同一次探测）。"""
        pkg, activity = focus_window_cached(self.transport)
        if pkg:
            self.last_activity = activity
            return pkg
        return self.pkg

    def _pid_of(self, pkg: str | None) -> str | None:
        """包名 → PID（单次 pidof；供外部查询用，sample() 内部已在批量命令里拿到）。"""
        if not pkg:
            return None
        out = self.transport.shell("pidof %s" % pkg)
        nums = [x for x in (out or "").split() if x.isdigit()]
        return nums[0] if nums else None

    def sample(self) -> dict:
        # 采样提速（2026-10-26）：pidof + /proc/meminfo + /proc/<pid>/status 合并为
        # **一次** adb 调用——应用内存四指标从 2~5 次调用降到 1 次（缓存未命中时）。
        # 'PID <n>' 标记行回显进程号；三段输出的键名互不冲突，单遍解析。
        pkg = self.foreground_pkg()
        if pkg:
            cmd = ("P=$(pidof %s 2>/dev/null | cut -d' ' -f1); "
                   "cat /proc/meminfo 2>/dev/null; "
                   "[ -n \"$P\" ] && cat /proc/$P/status 2>/dev/null; "
                   "echo \"PID $P\"" % pkg)
        else:
            cmd = "cat /proc/meminfo"
        out = self.transport.shell(cmd)
        total = avail = None
        vals: dict = {}
        pid = None
        for line in (out or "").splitlines():
            parts = line.split()
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            key = parts[0]
            if key == "MemTotal:":
                total = int(parts[1])
            elif key == "MemAvailable:":
                avail = int(parts[1])
            elif key in ("VmSize:", "VmPeak:", "VmRSS:", "VmHWM:"):
                vals[key] = int(parts[1])
            elif key == "PID":
                pid = parts[1]
        used = (total - avail) if (total and avail is not None) else None
        mb = lambda kb: round(kb / 1024.0, 1) if kb else None
        app_mem = None
        if vals:
            app_mem = {
                "alloc_mb": mb(vals.get("VmSize:")),          # 申请内存
                "alloc_peak_mb": mb(vals.get("VmPeak:")),     # 峰值申请内存
                "rss_mb": mb(vals.get("VmRSS:")),             # 当前物理内存占用
                "rss_peak_mb": mb(vals.get("VmHWM:")),        # 峰值物理内存占用
            }
        return {
            "mem_total_mb": round(total / 1024, 1) if total else None,
            "mem_used_mb": round(used / 1024, 1) if used is not None else None,
            "mem_available_mb": round(avail / 1024, 1) if avail is not None else None,
            "app_pkg": pkg,
            "app_pid": int(pid) if pid else None,
            "app_mem": app_mem,
        }
