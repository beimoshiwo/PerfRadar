# -*- coding: utf-8 -*-
"""FPS 采集器（免 root）：SurfaceFlinger **前台应用渲染面**的帧时间戳，1 秒实时窗口。

三个关键修正（真机定位，前两个是用户报"FPS 与 MIUI 功耗检测对不上"的根因）：
1. **测错了层**（2026-10-24）：无参 ``dumpsys SurfaceFlinger --latency`` 取的是 SF 层列表
   第一个层——实测是 ``ImageWallpaper``（壁纸层），FPS 完全不对。必须先按前台
   包名从 ``--list`` 定位该应用的层。
2. **层名前缀匹配漏了新版格式**（2026-10-26，K40 Pro 安卓14 实测）：
   层名格式跨版本差异——旧设备是 ``SurfaceView - <pkg>/…``（带横线），
   安卓 12+ 是 ``SurfaceView[<pkg>/<activity>]#id`` 与
   ``SurfaceView[<pkg>/<activity>](BLAST)#id``（无横线，真实帧数据在 BLAST 变体）。
   旧代码只认带横线旧格式 → 匹配落空后退取**主窗口层**：Unity 游戏主窗口几乎不重绘
   （实测恒定 2.4fps），而游戏实际跑 ~90fps（BLAST 层 desired present 间隔 11.2ms），
   即用户看到的"对不上"。
   现改为**数据驱动选层**：候选层按 BLAST SurfaceView > SurfaceView > 主窗口 > 其他
   排序，逐层读延迟缓冲，选有效帧最多的层——自动跳过 ``0 0 0`` 失效层与低帧 UI 层。
3. **静止画面误报**：游戏暂停/加载时延迟缓冲保留旧时间戳，1 秒窗口仍按旧帧算出帧率。
   新增停滞检测：最新帧时间戳连续 1.5s 不前进 → FPS 记 0（画面确实没有在更新）。

FPS 口径：实际 present 时间戳（每行第 2 列；第 1 列 desired、第 3 列 frame-ready），
最新帧往前 1 秒窗口，按 帧数/跨度 计算，与 gameturbo 的实时口径一致。
"""

from __future__ import annotations

import time

from .appinfo import focus_window_cached
from .transport import AdbTransport


class FPSCollector:
    """FPS 采集器（免 root，帧时间戳 + 1 秒实时窗口 + 数据驱动选层）。"""

    _SENT = 9223372036854775807       # INT64_MAX 哨兵（该帧尚未 present）
    _WINDOW_NS = 1_000_000_000        # 实时窗口：最新帧往前 1 秒
    _LAYER_TTL = 2.0                  # 层定位缓存秒数（应用切换检测周期）
    _STATIC_NS = 1.5                  # 最新帧停滞阈值：超过则画面静止，FPS 记 0
    _FULL_MIN = 100                   # 有效帧达此值即活跃渲染面（延迟缓冲约 128 帧）

    def __init__(self, transport: AdbTransport, pkg: str | None = None) -> None:
        self.transport = transport
        self.pkg = pkg
        self.last_pkg: str | None = None
        self._layer: str | None = None
        self._layer_ts: float = 0.0
        self._prev_newest: int | None = None   # 上一采样的最新 present 时间戳
        self._newest_since: float = 0.0        # 最新帧首次出现的 monotonic 时刻

    # ---- 前台包名 / 层定位 ----
    def _foreground_pkg(self) -> str | None:
        # 复用 appinfo.focus_window（经共享缓存，与内存采集器共用同一次探测）
        pkg, _activity = focus_window_cached(self.transport)
        if pkg:
            self.last_pkg = pkg
        return pkg

    def _latency(self, layer: str | None) -> str:
        if not layer:
            return ""
        return self.transport.shell(
            "dumpsys SurfaceFlinger --latency '%s'" % layer)

    @classmethod
    def _valid_count(cls, out: str | None) -> int:
        """延迟缓冲中有效帧数（0 < 实际 present < 哨兵）；``0 0 0`` 空槽不计。"""
        if not out:
            return 0
        n = 0
        for l in out.strip().splitlines()[1:]:
            parts = l.split()
            if len(parts) < 2:
                continue
            try:
                a = int(parts[1])
            except ValueError:
                continue
            if 0 < a < cls._SENT:
                n += 1
        return n

    def _resolve_layer(self, pkg: str | None) -> str | None:
        """数据驱动选层：按渲染面优先级逐层验证帧数据，选有效帧最多的层。

        候选层优先级：BLAST SurfaceView > SurfaceView > 主窗口 > 其他含包名层。
        逐层读延迟缓冲统计有效帧数，取最多者（并列取靠前）——失效层（全 0）、
        低帧 UI 层会被自动跳过；某个层缓冲装满（≥_FULL_MIN）即可提前判定，
        不必读完全部候选。层失效/应用切换时由 sample() 清缓存重定位。
        """
        if not pkg:
            return None
        out = self.transport.shell("dumpsys SurfaceFlinger --list")
        layers = [l.strip() for l in (out or "").splitlines() if l.strip()]
        cands = [l for l in layers if pkg in l]
        if not cands:
            return None
        sv = [l for l in cands
              if l.startswith("SurfaceView")
              and not l.startswith(("Background", "Bounds"))]
        sv_blast = [l for l in sv if "(BLAST)" in l]
        sv_plain = [l for l in sv if "(BLAST)" not in l]
        main = [l for l in cands if l.startswith(pkg + "/")]
        rest = [l for l in cands
                if l not in sv_blast and l not in sv_plain and l not in main]
        best: str | None = None
        best_n = 0
        for l in sv_blast + sv_plain + main + rest:
            n = self._valid_count(self._latency(l))
            if n > best_n:
                best, best_n = l, n
            if best_n >= self._FULL_MIN:
                break
        return best

    # ---- FPS 计算 ----
    @classmethod
    def _calc_fps(cls, out: str | None) -> tuple:
        """1 秒实时窗口：只统计最新帧往前 1 秒内的帧，按 帧数/跨度 得实时帧率。

        返回 ``(fps, newest_ts)``；无有效帧时 fps 为 None（newest 仍尽量返回）。
        """
        if not out:
            return None, None
        lines = out.strip().splitlines()
        if len(lines) < 2:
            return None, None
        actuals = []
        for l in lines[1:]:
            parts = l.split()
            if len(parts) < 2:
                continue
            try:
                a = int(parts[1])          # 第 2 列 = 实际 present 时间
            except ValueError:
                continue
            if 0 < a < cls._SENT:          # 跳过未 present 帧与 0 0 0 空槽
                actuals.append(a)
        if len(actuals) < 2:
            return None, None
        newest = max(actuals)
        win = sorted({a for a in actuals if newest - a <= cls._WINDOW_NS})
        if len(win) < 2:
            return None, newest
        span = win[-1] - win[0]
        if span <= 0:
            return None, newest
        return round((len(win) - 1) * 1e9 / span, 1), newest

    def sample(self) -> dict:
        layer = self._layer
        stale_layer = layer
        if layer is None or time.monotonic() - self._layer_ts >= self._LAYER_TTL:
            pkg = self._foreground_pkg() or self.pkg
            layer = self._resolve_layer(pkg)
            self._layer = layer
            self._layer_ts = time.monotonic()
            if layer != stale_layer:
                # 换层（应用切换/层重建）：停滞检测状态作废，避免跨层误判
                self._prev_newest = None
                self._newest_since = 0.0
        fps, newest = self._calc_fps(self._latency(layer))
        if newest is not None:
            now = time.monotonic()
            if self._prev_newest is not None and newest <= self._prev_newest:
                # 无新帧：缓冲里是旧时间戳。停滞超过阈值 → 画面静止
                # （暂停/加载/结算界面），FPS 记 0，而不是拿旧帧率糊在曲线上
                if now - self._newest_since >= self._STATIC_NS:
                    fps = 0.0
            else:
                self._newest_since = now
            self._prev_newest = newest
        if fps is None and layer is not None:
            # 层失效（应用切换/层重建）→ 清缓存，下次重新定位
            self._layer = None
            self._layer_ts = 0.0
        return {"fps": fps, "fps_layer": layer}
