# -*- coding: utf-8 -*-
"""GPU 采集器（免 root 方案核心，诚实反映设备真实能力）。

免 root 读取 GPU 的真实方案（小米5s / MIUI 11 / Android 8 / 无 root / SELinux
Enforcing 真机实测验证）：

  · **shell 域**（u:r:shell:s0）：读 kgsl 节点被 SELinux ``sysfs_kgsl`` 拒绝（Permission
    denied）。这很正常，**不代表读不到 GPU** —— 只是 shell 域被锁。

  · **app 域**（untrusted_app，通过 ``run-as <debuggable pkg>`` 获得）：可以读取 kgsl 节点：
      - ``gpu_busy_percentage``：**首选**，直接返回瞬时占用率（如 ``55 %``），无需差分、
        不受 DVFS 下 gpubusy 计数重置影响，每个采样点都有稳定值。
      - ``gpubusy`` ：累计 busy / idle 计数 → 差分算占用率（首选节点不可用时的兜底）。
      - ``gpuclk``  ：当前频率（单位 Hz，需 /1e6 转 MHz）。
    因此本采集器在发现一个 debuggable 第三方应用后，即可**免 root** 读取 GPU 占用率与频率。

  · **logcat 通道**（ThermalEngine ``[thermal-debug] gpu freq is <Hz>``）：普通 shell 可读，
    作为频率的兜底来源（当 app 域/ shell 域频率都不可读时）。

  · **root(su) 通道**（Android 9+ 读 kgsl 的唯一可靠通路）：经 ``su`` 读 kgsl/devfreq。
    频率/最高频采用**通用发现**——先试 kgsl 直接文件(``gpuclk``/``gpu_freq``/``max_gpuclk``)，
    再通配扫描 ``devfreq`` 节点（机型名不写死，自动适配 b00000 / 5000000 等），不按机型单独适配。

安卓 14（HyperOS / 骁龙888 K40 Pro）免 root 能力边界（2026-10-25 真机穷尽实测）：
  · 可读：``gpubusy``、``max_gpuclk``、``gpu_model``。
    ``gpu_busy_percentage`` 在该机型 shell/app 域均被拒——免 root busy 走 gpubusy。
  · **``gpubusy`` 是「轮询窗口刷新型」而非累计计数**（2026-10-25 受控实测）：
    数值阶跃式替换（约 100-600ms 一跳，窗口内恒定），两计数器同步跳变，
    和近似恒定（≈1.85M ≈ 96ms @19.2MHz ALWAYS-ON 计数时钟），GPU 全空闲被
    电源折叠时清零（``0 0``）。⇒ **单次采样比值 busy/(busy+idle) 即瞬时占用率**；
    差分法在该节点上会因计数器回退得到负增量/丢样，仅对「累计型」旧内核节点适用
    （见 ``_compute_usage`` 自适应算法：差分优先，检测到计数器回退即切换比值）。
    实测对照：QQ飞车菜单 41-45%，回桌面空闲 0-19%。
  · ``max_gpuclk`` 是**动态频率上限**（不是封顶！）：响应负载/热预算动态调整——
    待机限到最低频点 315MHz，轻负载 ~608MHz，游戏加速/高负载拉满到芯片峰值
    840MHz（= 极客湾口径的 Adreno660 峰值）。
    实测：空闲连读 315MHz；QQ飞车运行中连读 5 次均 840MHz。工具据此分别记录
    「当前上限」(gpu_max_freq_mhz，随状态变) 与「实测峰值」(gpu_max_peak_mhz，粘滞最高)。
    另：``/sys/kernel/gpu/gpu_max_clock``（小米 vendor 兼容节点）可读，值 = max_gpuclk/1e6
    的别名；``/sys/kernel/gpu`` 下其余节点与 devfreq 目录全部 Permission denied。
  · 不可读：当前频率（``gpuclk`` / devfreq ``cur_freq`` / 真实设备路径同标签全被拒）；
    本机没有可用于 run-as 的 debuggable 应用，run-as 通路不存在；
    perfetto(traced) 单事件穷举实测（30s 独占 trace，对照 cpu_frequency 625KB）：
    kgsl/gpu_frequency、devfreq/devfreq_monitor、dcvsh/dcvsh_freq、power/bw_hwmon_*、
    rpmh/rpmh_send_msg 全部 SELinux 拒启（空 trace 1.5KB 元数据）；可启用的
    clk/clk_set_rate（仅 UFS 等时钟，GPU 走 GMU/RPMh 不在通用 clk 框架）、
    power/cpu_frequency（仅 CPU）、gpu_mem/gpu_mem_total（每进程显存，可用作指标）
    均不携带 GPU 频率；5.4 内核无 power/gpu_frequency 事件；
    logcat ThermalEngine 在 HyperOS user 版完全静默；cmd gpu 仅 vkjson；
    atrace/直写 tracefs 被拒；/proc/kgsl 不存在。
    ⇒ **当前频率免 root 在此机型不可得**，可靠途径只有 root。
    （注：安卓 8 机型上曾靠 run-as 进 debuggable 应用的 app 域读 ``gpuclk``——那是安卓8
     系统层能力；但**不存在**所谓的"装个 com.fukebench7 debuggable 助手"方案，那是误解。）

占用率算法（自适应，兼容两种内核节点语义）：
    首选直接读 ``gpu_busy_percentage``；
    否则读 ``gpubusy`` 的 (busy, idle)：
      · 累计型节点（旧内核，计数单调递增）：差分 usage=(b2-b1)/((b2-b1)+(i2-i1))×100；
      · 窗口刷新型节点（K40 Pro 安卓14：阶跃替换/回退/清零）：
        usage=b/(b+i)×100（单次比值即瞬时占用率；b+i=0 表示 GPU 空闲，记 0%）。
    检测到计数器回退即锁定比值模式（该设备此后恒用比值）；
    连续两次以内回退视为累计型节点的偶发重置，仍走差分（仅当次用比值兜底）。
    若节点读失败（非数字），``gpu_usage`` 返回 ``None``（曲线留空，不伪造 0%）。
"""

from __future__ import annotations

import re
import time

from .transport import AdbTransport
from .logcat_gpu import LogcatGPUCollector


class GPUCollector:
    """GPU 采集器：统一 ``sample() -> dict`` 接口，支持 shell 域 / run-as app 域 读取。"""

    BUSY_NODE = "/sys/class/kgsl/kgsl-3d0/gpubusy"
    BUSY_PCT_NODE = "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage"
    GPU_MODEL_NODE = "/sys/class/kgsl/kgsl-3d0/gpu_model"
    FREQ_NODES = [
        "/sys/class/kgsl/kgsl-3d0/gpuclk",
        "/sys/class/kgsl/kgsl-3d0/devfreq/kgsl-3d0/cur_freq",
        "/sys/class/kgsl/kgsl-3d0/gpu_freq",
        "/sys/class/devfreq/b00000.qcom,kgsl-3d0/cur_freq",
    ]
    MAX_NODE = "/sys/class/kgsl/kgsl-3d0/max_gpuclk"

    # —— 通用 root 频率/最高频发现（不写死机型名，devfreq 通配自适应）——
    # 所有 Adreno(kgsl) 设备的规范类路径，root 可读 symlink。
    KGSL_CLASS = "/sys/class/kgsl/kgsl-3d0"
    FREQ_FILENAMES = ("gpuclk", "gpu_freq")        # kgsl 直接频率文件（Hz）
    MAX_FILENAMES = ("max_gpuclk",)                # kgsl 最高频文件（Hz）
    # devfreq 频率节点通配：机型名不固定（如 b00000.qcom,kgsl-3d0 / 5000000.qcom,kgsl-3d0）。
    # 2026-10-25 K40 Pro(骁龙888) 实测：真实设备路径为
    #   /sys/devices/platform/soc/3d00000.qcom,kgsl-3d0/devfreq/<同名>/cur_freq
    # 与 /sys/class/devfreq/ 下的 symlink 是两套入口，通配都要覆盖。
    DEVFREQ_CUR_GLOBS = (
        "/sys/class/kgsl/kgsl-3d0/devfreq/*/cur_freq",
        "/sys/class/devfreq/*kgsl*/cur_freq",
        "/sys/class/devfreq/*gpu*/cur_freq",
        "/sys/devices/platform/soc/*/kgsl/kgsl-3d0/devfreq/*/cur_freq",
        "/sys/devices/platform/soc/*/devfreq/*kgsl*/cur_freq",
        "/sys/devices/platform/soc/*/devfreq/*gpu*/cur_freq",
        "/sys/devices/platform/soc@0/*/devfreq/*kgsl*/cur_freq",
        "/sys/devices/soc/*/kgsl/kgsl-3d0/devfreq/*/cur_freq",
    )
    DEVFREQ_MAX_GLOBS = tuple(g.replace("cur_freq", "max_freq")
                             for g in DEVFREQ_CUR_GLOBS)

    # Adreno 芯片 → GPU 参考峰值频率（MHz）。来源：公开拆解实测（极客湾等口径），
    # 为**芯片理论峰值**；OEM 在手机上常因功耗/发热预算限频低于此值，故与设备实测
    # 的 max_gpuclk（设备允许的最高频）往往不同。仅作参考展示，不参与任何计算。
    # 不确定/未列出的型号返回 None（界面不显示参考峰值）。
    ADRENO_PEAK_MHZ = {
        "Adreno660": 840,   # 骁龙 888（lahaina / SM8350）
        "Adreno650": 587,   # 骁龙 865 / 870（SM8250）
        "Adreno640": 585,   # 骁龙 855 / 856 / 858（SM8150）
        "Adreno630": 710,   # 骁龙 845（SDM845）
        "Adreno730": 900,   # 骁龙 8 Gen1（SM8450）
        "Adreno740": 900,   # 骁龙 8 Gen2（SM8550）
    }

    DISCOVER_CMD = (
        "for p in $(pm list packages -3 2>/dev/null | cut -d: -f2); "
        "do run-as $p true >/dev/null 2>&1 && echo $p; done"
    )

    def __init__(
        self,
        transport: AdbTransport,
        run_as_pkg: str | None = None,
        logcat_collector: LogcatGPUCollector | None = None,
    ) -> None:
        self.transport = transport
        self.run_as_pkg = run_as_pkg
        self.logcat_collector = logcat_collector
        self.busy_node: str | None = None
        self.pct_node: str | None = None
        self.freq_node: str | None = None
        self.max_node: str | None = None
        self.mode: str | None = None
        self.freq_mode: str | None = None
        self.max_mode: str | None = None
        self.gpu_model: str | None = None
        self.gpu_peak_freq_mhz: float | None = None
        self.gpu_max_peak_mhz: float | None = None   # max_gpuclk 实测粘滞峰值（动态上限）
        self._prev: tuple[int, int] | None = None
        self._ratio_mode: bool = False     # gpubusy 窗口刷新型设备：单次比值即瞬时占用率
        self._busy_resets: int = 0         # 观测到的计数器回退次数（区分节点语义）
        self._rooted: bool | None = None     # 最近一次 root 探测结果（变化时重探）
        self._rediscover_ts: float = 0.0     # 上次重新探测的时间戳
        self._discover_nodes()
        self._rooted = self.transport.has_root()   # 与探测结论一致（缓存命中）
        # 读 GPU 型号（多数机型 shell 域可读），并据此给出芯片理论峰值参考
        self.gpu_model = self._read(self.GPU_MODEL_NODE)
        if self.gpu_model:
            for key, peak in self.ADRENO_PEAK_MHZ.items():
                if key in self.gpu_model:
                    self.gpu_peak_freq_mhz = peak
                    break

    def _read(self, path: str) -> str | None:
        out = self.transport.shell(f"cat {path}")
        if out is None:
            return None
        out = out.strip()
        return out if out else None

    def _read_runas(self, pkg: str, path: str) -> str | None:
        out = self.transport.shell(f"run-as {pkg} cat {path}")
        if out is None:
            return None
        out = out.strip()
        if not out or out.startswith("run-as:"):
            return None
        return out

    def _read_root(self, path: str) -> str | None:
        """经 su 读节点（root 模式；Android 9+ 读 kgsl 的唯一通路）。"""
        out = self.transport.shell_root(f"cat {path}")
        if out is None:
            return None
        out = out.strip()
        return out if out else None

    def _read_root_digit(self, path: str) -> int | None:
        v = self._read_root(path)
        if v and v.isdigit():
            return int(v)
        return None

    def _discover_root_freq(self) -> tuple:
        """通用 root 频率节点发现：先 kgsl 直接文件(gpuclk/gpu_freq)，
        再扫描 devfreq（机型名不固定，用通配），返回 (node, mode, None)。"""
        for fn in self.FREQ_FILENAMES:
            p = "%s/%s" % (self.KGSL_CLASS, fn)
            if self._read_root_digit(p) is not None:
                return p, "root", None
        out = self.transport.shell_root(
            "for g in %s; do [ -e \"$g\" ] && echo \"$g\"; done 2>/dev/null"
            % " ".join(self.DEVFREQ_CUR_GLOBS))
        for line in (out or "").splitlines():
            line = line.strip()
            if line and self._read_root_digit(line) is not None:
                return line, "root", None
        return None, None, None

    def _discover_root_max(self) -> str | None:
        """通用 root 最高频节点发现：max_gpuclk → devfreq max_freq。"""
        for fn in self.MAX_FILENAMES:
            p = "%s/%s" % (self.KGSL_CLASS, fn)
            if self._read_root_digit(p) is not None:
                return p
        out = self.transport.shell_root(
            "for g in %s; do [ -e \"$g\" ] && echo \"$g\"; done 2>/dev/null"
            % " ".join(self.DEVFREQ_MAX_GLOBS))
        for line in (out or "").splitlines():
            line = line.strip()
            if line and self._read_root_digit(line) is not None:
                return line
        return None

    @staticmethod
    def _parse_busy(out: str | None) -> tuple[int, int] | None:
        if not out:
            return None
        parts = out.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
        return None

    @staticmethod
    def _parse_pct(out: str | None) -> float | None:
        if not out:
            return None
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", out)
        if m:
            return float(m.group(1))
        s = out.strip()
        if s.replace(".", "", 1).isdigit():
            return float(s)
        return None

    @staticmethod
    def _shell_ok(out: str | None, kind: str) -> bool:
        out = (out or "").strip()
        if not out:
            return False
        if kind == "busy":
            parts = out.split()
            return len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit()
        if kind == "pct":
            return GPUCollector._parse_pct(out) is not None
        return out.isdigit()

    @classmethod
    def _discover_debuggable(cls, transport: AdbTransport) -> str | None:
        out = transport.shell(cls.DISCOVER_CMD)
        if not out:
            return None
        for line in out.splitlines():
            line = line.strip()
            if line:
                return line
        return None

    def _discover_nodes(self) -> None:
        # 优先级：root > shell 域 > run-as app 域。
        # 用户要求：检测到 root 就走 root（最稳、不依赖 debuggable 载体）；
        # 无 root 时回退免 root 方案（安卓 8 及以下可免 root 读 kgsl）。
        rooted = self.transport.has_root()
        if rooted:
            out = self._read_root(self.BUSY_PCT_NODE)
            if self._parse_pct(out) is not None:
                self.pct_node = self.BUSY_PCT_NODE
                self.mode = "root"
            else:
                out = self._read_root(self.BUSY_NODE)
                if self._parse_busy(out) is not None:
                    self.busy_node = self.BUSY_NODE
                    self.mode = "root"
            # 频率 / 最高频：通用发现（devfreq 机型名通配，不再单独适配）
            if self.freq_node is None:
                self.freq_node, self.freq_mode, _ = self._discover_root_freq()
                if self.freq_node and self.mode is None:
                    self.mode = "root"
            if self.max_node is None:
                self.max_node = self._discover_root_max()
                if self.max_node:
                    self.max_mode = "root"

        if self.mode is None:
            out = self._read(self.BUSY_PCT_NODE)
            if self._parse_pct(out) is not None:
                self.pct_node = self.BUSY_PCT_NODE
                self.mode = "shell"
            else:
                pkg0 = self.run_as_pkg or self._discover_debuggable(self.transport)
                if pkg0:
                    self.run_as_pkg = pkg0
                    r = self._read_runas(pkg0, self.BUSY_PCT_NODE)
                    if self._parse_pct(r) is not None:
                        self.pct_node = self.BUSY_PCT_NODE
                        self.mode = "runas"

        if self.mode is None:
            out = self._read(self.BUSY_NODE)
            if self._parse_busy(out) is not None:
                self.busy_node = self.BUSY_NODE
                self.mode = "shell"

        if self.busy_node is None and self.mode is None:
            pkg = self.run_as_pkg or self._discover_debuggable(self.transport)
            if pkg:
                self.run_as_pkg = pkg
                out = self._read_runas(pkg, self.BUSY_NODE)
                if self._parse_busy(out) is not None:
                    self.busy_node = self.BUSY_NODE
                    self.mode = "runas"

        if self.freq_node is None:
            pkg = self.run_as_pkg
            if pkg:
                for p in self.FREQ_NODES:
                    out = self._read_runas(pkg, p)
                    if out and out.isdigit():
                        self.freq_node = p
                        self.freq_mode = "runas"
                        break
        if self.freq_node is None:
            for p in self.FREQ_NODES:
                out = self._read(p)
                if out and out.isdigit():
                    self.freq_node = p
                    self.freq_mode = "shell"
                    break

        if self.max_node is None:
            pkg = self.run_as_pkg
            if pkg:
                out = self._read_runas(pkg, self.MAX_NODE)
                if out and out.isdigit():
                    self.max_node = self.MAX_NODE
                    self.max_mode = "runas"
        if self.max_node is None:
            out = self._read(self.MAX_NODE)
            if out and out.isdigit():
                self.max_node = self.MAX_NODE
                self.max_mode = "shell"

    def _read_multi(self, paths: list, mode: str) -> list:
        """一次 adb 调用读多个节点（'---' 分隔输出）→ 与 paths 等长的字符串列表。

        采样提速（2026-10-26）：pct/busy + freq + max 原本每个节点一次调用
        （免 root 常见 3 次/样本），合并为 1 次。三种通路各自的复合命令：
          · shell  直接 ``cat A; echo ---; cat B``；
          · runas  ``run-as <pkg> sh -c "…"``（Mi 5s 真机验证：一次拿回
            gpu_busy_percentage + gpuclk + max_gpuclk 三个值）；
          · root   ``su -c "…"``（transport 负责引号转义）。
        单个节点走原逐节点读取（避免无谓包装）；整体失败（run-as 拒绝等）
        返回全空列表，与逐节点读行为一致。
        """
        paths = [p for p in paths if p]
        if not paths:
            return []
        if len(paths) == 1:
            if mode == "runas" and self.run_as_pkg:
                return [self._read_runas(self.run_as_pkg, paths[0]) or ""]
            if mode == "root":
                return [self._read_root(paths[0]) or ""]
            return [self._read(paths[0]) or ""]
        inner = "; echo ---; ".join("cat %s 2>/dev/null" % p for p in paths)
        if mode == "runas" and self.run_as_pkg:
            out = self.transport.shell(
                'run-as %s sh -c "%s"' % (self.run_as_pkg, inner))
            if (out or "").strip().startswith("run-as:"):
                return [""] * len(paths)
        elif mode == "root":
            out = self.transport.shell_root(inner)
        else:
            out = self.transport.shell(inner)
        parts = (out or "").split("---")
        return [parts[i] if i < len(parts) else "" for i in range(len(paths))]

    def _compute_usage(self, out: str | None) -> float | None:
        """gpubusy (busy, idle) → 占用率，自适应两种内核节点语义。

        · 累计型（旧内核，计数单调递增）：差分算区间均值；
        · 窗口刷新型（K40 Pro 安卓14，阶跃替换/回退/空闲清零）：单次比值即瞬时占用率。
        返回 None 表示本样无有效数据（读失败/语义未定的无变化样本），由上层留空曲线。
        """
        parsed = self._parse_busy(out)
        if parsed is None:
            return None
        b, i = parsed
        total = b + i
        if total <= 0:
            # GPU 全空闲被电源折叠：ALWAYS-ON 计数器清零 → 0%
            self._prev = (b, i)
            return 0.0
        ratio = min(100.0, max(0.0, b / total * 100.0))
        if self._prev is None:
            # 首个样本：比值（窗口型设备即瞬时真值；累计型设备为历史均值，仅影响首点）
            self._prev = (b, i)
            return ratio
        db = b - self._prev[0]
        di = i - self._prev[1]
        self._prev = (b, i)
        if db < 0 or di < 0:
            # 计数器回退：窗口刷新型设备的正常现象（阶跃替换/清零）。
            # 累计型旧内核的偶发重置（DVFS）也会触发——用回退次数区分：
            # 累计回退 ≥2 次锁定比值模式；偶发一次仅当次用比值兜底，之后仍走差分。
            self._busy_resets += 1
            if self._busy_resets >= 2:
                self._ratio_mode = True
            return ratio
        if self._ratio_mode:
            return ratio
        if db == 0 and di == 0:
            # 数值无变化：窗口型设备是「本窗口未刷新」（比值仍有效），
            # 累计型设备是「GPU 空闲」（应为 0）——语义未定，留空不伪造。
            return None
        dtot = db + di
        if dtot > 0:
            return min(100.0, max(0.0, db / dtot * 100.0))
        return 0.0

    def sample(self) -> dict:
        # root 状态变化时自动切换通路：
        # · 新获得 root（用户刚 root/刚授权）→ 重新探测，切到 root 读取
        # · root 丢失（授权被回收/Magisk 卸载）→ 重新探测，回退免 root 方案
        # · 始终无通路 → 每 8s 重试探测（等 root 授权或载体出现）
        rooted = self.transport.has_root()
        prev = getattr(self, "_rooted", None)
        if rooted != prev or self.mode is None:
            if time.monotonic() - getattr(self, "_rediscover_ts", 0.0) >= (
                    2.0 if rooted != prev else 8.0):
                self._rediscover_ts = time.monotonic()
                self._rooted = rooted
                self._discover_nodes()
        readable = self.mode is not None
        # 采样提速（2026-10-26）：本次要读的节点按通路模式分组，每组**一次**
        # adb 调用读完（'---' 分隔）——免 root 常见通路从 3 次调用
        # （pct/busy + freq + max）降到 1 次；混合模式（如 pct 走 shell、
        # max 走 run-as）自动拆成多组，各一次调用。
        want: list = []                      # [(kind, node, mode)]
        if self.pct_node:
            want.append(("pct", self.pct_node, self.mode))
        elif self.busy_node:
            want.append(("busy", self.busy_node, self.mode))
        if self.freq_node:
            want.append(("freq", self.freq_node, self.freq_mode))
        if self.max_node:
            want.append(("max", self.max_node, self.max_mode))
        groups: dict = {}
        for kind, node, m in want:
            if m:
                groups.setdefault(m, []).append((kind, node))
        got: dict = {}
        for m, items in groups.items():
            outs = self._read_multi([n for _, n in items], m)
            for (kind, _), out in zip(items, outs):
                got[kind] = out

        usage: float | None = None
        if "pct" in got:
            usage = self._parse_pct(got["pct"])
        elif "busy" in got:
            # _compute_usage 内部维护差分/比值状态，每样本必须且只能调一次
            usage = self._compute_usage(got["busy"])
        if usage is not None:
            usage = min(100.0, max(0.0, usage))

        freq: float | None = None
        f = (got.get("freq") or "").strip()
        if f.isdigit():
            freq = int(f) / 1e6

        max_freq: float | None = None
        mf = (got.get("max") or "").strip()
        if mf.isdigit():
            max_freq = int(mf) / 1e6

        # max_gpuclk 是动态上限（待机~608 / 游戏拉满840）；记录实测粘滞峰值供面板展示
        if max_freq is not None and (self.gpu_max_peak_mhz is None
                                     or max_freq > self.gpu_max_peak_mhz):
            self.gpu_max_peak_mhz = max_freq

        source: str | None = None
        if self.mode == "shell":
            source = "kgsl:shell"
        elif self.mode == "runas" and self.run_as_pkg:
            source = "kgsl:run-as(%s)" % self.run_as_pkg
        elif self.mode == "root":
            source = "kgsl:root(su)"

        if (freq is None or max_freq is None) and self.logcat_collector is not None:
            try:
                ldata = self.logcat_collector.sample()
            except Exception:
                ldata = {}
            if isinstance(ldata, dict):
                if freq is None and ldata.get("gpu_freq_mhz") is not None:
                    freq = float(ldata["gpu_freq_mhz"])
                    if source is None:
                        source = "logcat:ThermalEngine"
                if max_freq is None and ldata.get("gpu_max_freq_mhz") is not None:
                    max_freq = float(ldata["gpu_max_freq_mhz"])

        available = (self.mode is not None) or (freq is not None)

        # GPU Total：最高频等效占用率（与 MIUI gameturbo 同口径）。
        # gpu_busy_percentage 是「当前频率下的忙时占比」——GPU 跑在低频时同样的活
        # 耗时更长（214MHz 下 52% 忙 = 624MHz 下只需 17.8%），原始值会放大负载。
        # × cur/max 换算成「若全程跑在最高频需要多少占比」，gameturbo/用户口径即此。
        gpu_total: float | None = None
        if usage is not None and freq and max_freq and max_freq > 0:
            gpu_total = round(min(100.0, usage * freq / max_freq), 1)

        return {
            "gpu_usage": round(usage, 1) if usage is not None else None,
            "gpu_total": gpu_total,
            "gpu_freq_mhz": round(freq, 1) if freq is not None else None,
            "gpu_max_freq_mhz": round(max_freq, 1) if max_freq is not None else None,
            "gpu_max_peak_mhz": round(self.gpu_max_peak_mhz, 1)
                                if self.gpu_max_peak_mhz is not None else None,
            "gpu_model": self.gpu_model,
            "gpu_peak_freq_mhz": self.gpu_peak_freq_mhz,
            "gpu_available": bool(available),
            "readable": bool(readable),
            "source": source,
            "node": self.busy_node or self.pct_node or self.freq_node or "",
        }

    @classmethod
    def probe(cls, transport: AdbTransport) -> None:
        print("=" * 64)
        print("GPU 节点探测（免 root 方案：shell 域 或 run-as app 域）")
        print("=" * 64)
        pkg = cls._discover_debuggable(transport)
        print("debuggable 第三方应用(run-as 可读): %s"
              % (pkg or "（无，可用 --run-as-pkg 指定或安装助手）"))
        print("-" * 64)
        print("%-50s %-6s %-6s 样例" % ("节点路径", "shell", "run-as"))
        probe_rows = [(cls.BUSY_PCT_NODE, "pct"), (cls.BUSY_NODE, "busy")]
        probe_rows.extend((p, "freq") for p in cls.FREQ_NODES)
        probe_rows.append((cls.MAX_NODE, "max"))
        core_readable = False
        for path, kind in probe_rows:
            out_shell = (transport.shell(f"cat {path}") or "").strip()
            ok_shell = cls._shell_ok(out_shell, kind)
            ok_runas = False
            out_runas = ""
            if pkg:
                r = (transport.shell(f"run-as {pkg} cat {path}") or "").strip()
                if not r.startswith("run-as:"):
                    out_runas = r
                    ok_runas = cls._shell_ok(r, kind)
            if (ok_shell or ok_runas) and kind in ("pct", "busy", "freq"):
                core_readable = True
            s_shell = "是" if ok_shell else "否"
            s_runas = "是" if ok_runas else "否"
            sample = (out_runas or out_shell)[:24]
            print("%-50s %-6s %-6s %s" % (path, s_shell, s_runas, sample))
        print("=" * 64)
        if core_readable:
            if pkg:
                print("结论：本机 GPU 节点可经 run-as(%s) 在 app 域免 root 读取"
                      "（占用率/频率）。" % pkg)
            else:
                print("结论：本机 GPU 节点 shell 域可读，可免 root 获取 GPU 占用率/频率。")
        else:
            print("结论：本机 GPU 占用率节点不可直接从 shell 读，"
                  "若有 debuggable 应用仍可 run-as 读取；频率仍可经 logcat(ThermalEngine)。")
        print("=" * 64)
