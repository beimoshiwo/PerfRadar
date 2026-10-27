# -*- coding: utf-8 -*-
"""logcat GPU 频率采集器（免 root 真机通道：``adb logcat ThermalEngine``）。

在真机（小米5s / MIUI 11 / Android 8 / 无 root / SELinux Enforcing）实测确认：
  - ``adb logcat -s ThermalEngine`` 可由普通 shell 读取；
  - thermal-engine 周期打印 ``[thermal-debug] gpu freq is 401800000`` → 当前频率(Hz)；
  - 热控上限行 ``ACTION: GPU - Setting GPU[0] to 624000000`` → 频率上限(Hz)。

与 :class:`perfdog.gpu.GPUCollector` 并列，统一 ``sample() -> dict``，只负责频率兜底。
线程安全（共享状态用锁保护）。
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from subprocess import DEVNULL, PIPE

from .transport import AdbTransport


class LogcatGPUCollector:
    """通过 ``adb logcat`` 免 root 实时读取 GPU 频率（sysfs 被封时的兜底通道）。

    基线监听 ``ThermalEngine`` / ``thermal-debug``（小米系实测有效：
    小米5s 打 ``[thermal-debug] gpu freq is <Hz>``，小米6 每秒打
    ``ACTION: GPU - Setting GPU[0] to <Hz>``）。

    不同 ROM/机型的打点 tag 与文案各不相同，故首次启动时先做一次**宽扫描**：
    在最近日志里找「含 gpu 且带频率量级」的行，自动把它的 tag 加入监听列表，
    未知机型（红米5a / 其它品牌）无需逐个适配即可生效。
    """

    RE_CURRENT = re.compile(r"gpu freq is (\d+)")
    RE_MAX = re.compile(r"Setting GPU\[(\d+)\] to (\d+)")
    # 小米8 / 安卓10 起改用热控动作行报频率：`Action gpu value 596000000`
    RE_ACTION = re.compile(r"(?i)action\s+gpu\s+value\s+(\d+)")
    # 宽扫描用：任何含 gpu 且带频率量级的写法（Hz / MHz 两种口径）
    RE_FREQ_HZ = re.compile(r"(?i)gpu[^\d\n]{0,24}(\d{7,10})\b")
    RE_FREQ_MHZ = re.compile(r"(?i)gpu[^\d\n]{0,24}(\d{2,4})\s*MHz")
    # 排除明显不是频率的 GPU 数值（显存/缓冲字节数也会是 9 位数）
    RE_NOT_FREQ = re.compile(
        r"(?i)(mem|memory|size|byte|alloc|buffer|heap|vram|gpu_mem)")
    # logcat 行格式因 -v 而异，两种都要认：
    #   -v time:        MM-DD hh:mm:ss.mmm I/tag( pid): text
    #   -v threadtime:  MM-DD hh:mm:ss.mmm  pid  tid I tag: text
    RE_TAG_TIME = re.compile(
        r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+[VDIWEF]/([^\s(:]+)"
        r"\(\s*\d+\):\s*(.*)$")
    RE_TAG_THREAD = re.compile(
        r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+[VDIWEF]\s+"
        r"([^\s:]+)\s*:\s*(.*)$")

    # 基线 tag（各 ROM 通用度最高）+ 自动发现上限
    BASE_TAGS = ("ThermalEngine", "thermal-debug")
    MAX_EXTRA_TAGS = 6
    # 频率数据新鲜度上限（秒）。小米系只在**频率变化时**打点：小米6 游戏中每秒
    # 打、空闲时不打；小米8(安卓10) 仅温度触发热控时打一次，之后几十分钟不再打。
    # 故既要靠初值让刚连上就有数，也要在长时间无更新后判定为陈旧（不谎报实时值）。
    FRESH_TTL = 180.0
    # 频率合理区间（超出即视为误匹配，如 GPU 内存字节数）
    HZ_MIN, HZ_MAX = 5 * 10 ** 7, 3 * 10 ** 9
    MHZ_MIN, MHZ_MAX = 50, 3000

    _MOCK_FREQS_MHZ = [510.0, 401.8, 315.0]

    def __init__(self, transport: AdbTransport) -> None:
        self.transport = transport
        self.adb_bin = transport.adb_bin
        self.serial = transport.serial
        self.current_freq_hz: int | None = None
        self.max_freq_hz: int | None = None
        self.extra_tags: list = []          # 宽扫描自动发现的 tag
        self.discovered_tag: str | None = None
        self._scanned = False
        self._primed = False
        self._last_hit = 0.0            # 最后一次解析到频率的时间（monotonic）
        self._lock = threading.Lock()
        self._running = False
        self._tail_thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._mock_idx = 0

    @classmethod
    def _extract_hz(cls, text: str):
        """从一行日志里提取 GPU 频率(Hz)；非频率行返回 None。"""
        # 注意：每条精确正则都必须校验频率区间——小米8(安卓10) 的
        # ``Setting GPU[0] to 2`` / ``to 0`` 里的 2/0 是**频率档位号**不是频率，
        # 不校验会把 0 当成当前频率写进去（用户看到 GPU 频率恒为 0 的根因）。
        m = cls.RE_CURRENT.search(text)
        if m:
            v = int(m.group(1))
            if cls.HZ_MIN <= v <= cls.HZ_MAX:
                return v
        m = cls.RE_ACTION.search(text)
        if m:
            v = int(m.group(1))
            if cls.HZ_MIN <= v <= cls.HZ_MAX:
                return v
        m = cls.RE_MAX.search(text)
        if m:
            v = int(m.group(2))
            if cls.HZ_MIN <= v <= cls.HZ_MAX:
                return v
        m = cls.RE_FREQ_HZ.search(text)
        if m and not cls.RE_NOT_FREQ.search(text):
            v = int(m.group(1))
            if cls.HZ_MIN <= v <= cls.HZ_MAX:
                return v
        m = cls.RE_FREQ_MHZ.search(text)
        if m:
            v = int(m.group(1))
            if cls.MHZ_MIN <= v <= cls.MHZ_MAX:
                return v * 10 ** 6
        return None

    @classmethod
    def _split_line(cls, line: str):
        """拆出 (tag, text)；非日志正文行返回 None。"""
        for rx in (cls.RE_TAG_TIME, cls.RE_TAG_THREAD):
            m = rx.match(line)
            if m:
                return m.group(1), m.group(2)
        return None

    def _scan_freq_tags(self) -> list:
        """宽扫描最近日志，找出真正在打 GPU 频率的 tag（适配未知机型）。"""
        try:
            # 设备侧精准过滤后再回传（全缓冲 grep 要几秒，故在后台线程跑）：
            # ① 小米6 的 SELinux audit 每秒刷几十行，取最近 N 行会把真正的 GPU
            #    打点挤出窗口（频率只在变化时打点）；
            # ② 游戏加速每秒打 "getGpuRate called"（无数字），靠「gpu 之后某处出现
            #    7+ 位数字」过滤掉——注意不能用纯数字过滤，行内 pid 本身就是 5 位数字。
            out = self.transport.shell(
                "logcat -d -v time | grep -iE "
                "'gpu.*[0-9]{7,}|gpu.*[0-9]{2,4} *MHz' | head -80")
        except Exception:
            return []
        found: list = []
        for line in (out or "").splitlines():
            sp = self._split_line(line)
            if not sp:
                continue
            tag, text = sp
            if self._extract_hz(text) is None:
                continue
            if tag not in self.BASE_TAGS and tag not in found:
                found.append(tag)
        return found[:self.MAX_EXTRA_TAGS]

    def _ensure_tail(self) -> None:
        if self.transport.mock:
            return
        if self._running:
            return
        self._running = True
        # 先从历史缓冲取最后一条频率作初值——否则刚连上要等到下一次频率变化
        # 才有数（小米系只在变化时打点，空闲时可能几十秒才一次）。
        if not self._primed:
            self._primed = True
            self._prime_from_history()
        # 再用基线 tag 立刻开始监听（带 -T，只取此刻之后的新日志）
        self._start_proc(list(self.BASE_TAGS))
        if not self._scanned:
            # 宽扫描全缓冲要数秒，放后台跑；发现新 tag 后自动带上并重启监听
            self._scanned = True
            threading.Thread(target=self._async_scan, daemon=True).start()

    def _async_scan(self) -> None:
        try:
            found = self._scan_freq_tags()
        except Exception:
            return
        new = [t for t in found if t not in self.extra_tags]
        if not new or not self._running:
            return
        self.extra_tags += new
        self.discovered_tag = new[0]
        self._start_proc(list(self.BASE_TAGS) + self.extra_tags)

    def _start_proc(self, tags: list) -> None:
        """(重新)启动 logcat 监听：会先终止旧进程。"""
        old = self._proc
        if old is not None:
            try:
                old.terminate()
            except Exception:
                pass
        try:
            cmd = [self.adb_bin]
            if self.serial:
                cmd += ["-s", self.serial]
            cmd += ["logcat", "-v", "time", "-s"]
            cmd += tags
            # 只取启动时刻之后的日志：否则会把缓冲里几十分钟前的旧值
            # 当成“当前频率”一直显示（小米8 尤其明显：只有热控时才打点）。
            ts = self._device_ts()
            if ts:
                cmd += ["-T", ts + ".000"]
            proc = subprocess.Popen(cmd, stdout=PIPE, stderr=DEVNULL, text=True)
        except Exception:
            self._running = False
            self._proc = None
            return
        self._proc = proc
        self._tail_thread = threading.Thread(target=self._tail_loop, args=(proc,),
                                             daemon=True)
        self._tail_thread.start()

    def _tail_loop(self, proc) -> None:
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                if not self._running or proc is not self._proc:
                    break
                self._parse_line(line)
        except Exception:
            pass
        finally:
            # 只有“当前进程”结束时才清状态，旧进程（被扫描重启替换）退出不误清
            if proc is self._proc:
                self._running = False

    def _parse_line(self, line: str) -> None:
        # 统一走 _extract_hz：精确正则（含区间校验）+ 未知文案兜底，
        # 保证任何机型的“档位号/温度值”都不会被误当成频率。
        if "gpu" not in line.lower():
            return
        val = self._extract_hz(line)
        if val is None:
            return
        with self._lock:
            self._last_hit = time.monotonic()
            self.current_freq_hz = val
            if self.max_freq_hz is None or val > self.max_freq_hz:
                self.max_freq_hz = val

    def _device_ts(self):
        """取设备当前时间（MM-DD hh:mm:ss），用于 logcat -T 只取新日志。"""
        try:
            out = (self.transport.shell("date '+%m-%d %H:%M:%S'") or "").strip()
        except Exception:
            return None
        line = out.splitlines()[-1].strip() if out else ""
        return line or None

    def _prime_from_history(self) -> None:
        """启动时从历史缓冲取最后一条频率作初值（时间戳记为“现在”）。

        这样刚连上就有数；若之后一直没有新日志，FRESH_TTL 到期后会判定为陈旧
        并返回 None——不会把几十分钟前的旧值一直当实时值显示。
        """
        try:
            out = self.transport.shell(
                "logcat -d -v time -s %s | tail -40" % " ".join(self.BASE_TAGS))
        except Exception:
            return
        for line in reversed((out or "").splitlines()):
            if "gpu" not in line.lower():
                continue
            val = self._extract_hz(line)
            if val is None:
                continue
            with self._lock:
                self.current_freq_hz = val
                if self.max_freq_hz is None or val > self.max_freq_hz:
                    self.max_freq_hz = val
                self._last_hit = time.monotonic()
            return

    def sample(self) -> dict:
        if self.transport.mock:
            return self._mock_sample()
        self._ensure_tail()
        with self._lock:
            cur = self.current_freq_hz
            mx = self.max_freq_hz
            hit = self._last_hit
        # 新鲜度判定：超过 TTL 没有新日志就当作无数据，避免把旧值当实时值
        if cur is not None and hit and (time.monotonic() - hit > self.FRESH_TTL):
            cur = None
            mx = None
        if cur is not None:
            src = "logcat:ThermalEngine"
            if self.discovered_tag:
                src += "+" + self.discovered_tag
            return {
                "gpu_freq_mhz": round(cur / 1e6, 1),
                "gpu_max_freq_mhz": round(mx / 1e6, 1) if mx is not None else None,
                "gpu_freq_source": src,
                "gpu_freq_available": True,
            }
        return {
            "gpu_freq_mhz": None, "gpu_max_freq_mhz": None,
            "gpu_freq_source": None, "gpu_freq_available": False,
        }

    def _mock_sample(self) -> dict:
        idx = self._mock_idx % len(self._MOCK_FREQS_MHZ)
        self._mock_idx += 1
        mhz = self._MOCK_FREQS_MHZ[idx]
        return {
            "gpu_freq_mhz": mhz, "gpu_max_freq_mhz": None,
            "gpu_freq_source": "mock", "gpu_freq_available": True,
        }

    def push_freq(self, hz: int | None) -> None:
        with self._lock:
            self.current_freq_hz = hz

    def start(self) -> None:
        self._ensure_tail()

    def stop(self) -> None:
        self._running = False
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            self._proc = None
