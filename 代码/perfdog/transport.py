# -*- coding: utf-8 -*-
"""ADB 通信层：封装设备发现、``adb shell`` 与 ``adb <cmd>``，并提供离线 mock 模式。

真实模式：
  · ``adb_bin``    adb 可执行文件路径（None 时自动从 PATH/候选路径发现）。
  · ``serial``     设备序列号（``adb -s <serial>``）；None 用默认设备。
  · ``auto_discover``  True 时若未给 adb_bin 则自动寻找。

mock 模式：完全不依赖真机，所有 shell 输出由内置生成器构造，便于自测与演示。

设备列表：
  · ``AdbTransport._adb_devices(adb_bin)``  返回在线设备序列号列表（类方法）。
  · ``AdbTransport._discover_adb()``        返回 adb 可执行路径（类方法）。
"""

from __future__ import annotations

import errno
import os
import re
import shlex
import shutil
import socket
import subprocess
import time


class AdbTransport:
    """ADB 通信层。

    公开方法：
        shell(cmd)          -> str : 在设备执行一条 shell 命令，返回 stdout。
        run(cmd)            -> str : 执行非 shell 的 adb 命令（如 ``devices``）。
        is_device_online()  -> bool : 当前设备是否在线（mock 恒为 True）。

    静态方法：
        tcp_probe(ip, port) -> tuple: 裸 TCP 探测，返回 (errno, 中文说明)。

    类方法：
        _discover_adb()     -> str : 发现 adb 可执行文件。
        _adb_devices(adb)   -> list: 在线设备序列号列表。
        _adb(adb, args)     -> str : 底层执行一条 adb 命令，返回 stdout。
    """

    _last_err: str = ""   # 最近一次 adb 子进程的 stderr（排查用）

    def __init__(
        self,
        adb_bin: str | None = None,
        serial: str | None = None,
        mock: bool = False,
        auto_discover: bool = True,
    ) -> None:
        self.serial = serial
        self.mock = mock
        self._last_stderr = ""      # 最近一次 shell/run 的 stderr（排查用）
        if mock:
            self.adb_bin = "adb"
        elif adb_bin:
            self.adb_bin = adb_bin
        elif auto_discover:
            self.adb_bin = self._discover_adb()
        else:
            self.adb_bin = "adb"

        self._mock: dict = {
            "gpu_busy": 0,
            "gpu_idle": 0,
            "gpu_freq_idx": 0,
            "stat_seq": 0,
        }

        self._MOCK_GPU_FREQS = [
            624_000_000, 510_000_000, 401_800_000, 315_000_000, 214_000_000,
        ]

        self._MOCK_THERMAL = [
            ("cpu-thermal", 35000), ("gpu-thermal", 42000), ("battery", 30000),
            ("tsens_tz_sensor0", 33000), ("tsens_tz_sensor1", 31000),
            ("msm_therm", 41000),
        ]

    @staticmethod
    def _discover_adb() -> str:
        found = shutil.which("adb")
        if found:
            return found
        candidates = [
            os.path.expanduser("~/.local/bin/adb"),
            os.path.expanduser("/usr/local/bin/adb"),
            os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
            os.path.expanduser("/opt/android-sdk/platform-tools/adb"),
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return "adb"

    @staticmethod
    def _adb(adb_bin: str, args: list, timeout: int = 15) -> str:
        try:
            proc = subprocess.run(
                [adb_bin] + args, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ""
        AdbTransport._last_err = proc.stderr or ""
        return proc.stdout or ""

    @classmethod
    def _adb_devices(cls, adb_bin: str | None = None) -> list:
        if adb_bin is None:
            adb_bin = cls._discover_adb()
        out = cls._adb(adb_bin, ["devices"], timeout=8)
        devs = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            serial, state = line.split("\t", 1)
            if state.strip() == "device":
                devs.append(serial.strip())
        return devs

    def shell(self, cmd: str) -> str:
        if self.mock:
            return self._mock_shell(cmd)
        prefix = [self.adb_bin] + (["-s", self.serial] if self.serial else [])
        try:
            proc = subprocess.run(
                prefix + ["shell", cmd],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=25,
            )
        except subprocess.TimeoutExpired:
            return ""
        except Exception:
            return ""
        self._last_stderr = proc.stderr or ""
        return proc.stdout or ""

    def run(self, cmd: str) -> str:
        if self.mock:
            return ""
        prefix = [self.adb_bin] + (["-s", self.serial] if self.serial else [])
        try:
            proc = subprocess.run(
                prefix + shlex.split(cmd),
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=25,
            )
        except subprocess.TimeoutExpired:
            return ""
        except Exception:
            return ""
        self._last_stderr = proc.stderr or ""
        return proc.stdout or ""

    # ---- root 支持（Android 9+ 读 GPU/电流需要；root 后自动生效）----
    _ROOT_PROBE_TTL = 8.0        # root 探测间隔（秒）：防 root 授权弹窗被反复触发
    _ROOT_TIMEOUT = 6            # su 调用超时：弹窗未响应时不阻塞采样循环

    def shell_root(self, cmd: str, timeout: int | None = None) -> str:
        """经 ``su -c`` 以 root 权限在设备执行命令，返回 stdout。

        Android 9+ 的 SELinux 对 shell 域封锁 kgsl/power_supply 节点，免 root
        读不到 GPU 与电池电流（实测 MI 6/Android 9 全路径被封）。用户 root 后
        本方法自动可用，无需改代码。无 root / 超时（授权弹窗未响应）返回 ""。
        """
        if self.mock:
            return ""
        safe = cmd.replace("'", "'\\''")          # 单引号转义，安全透传设备 shell
        prefix = [self.adb_bin] + (["-s", self.serial] if self.serial else [])
        try:
            proc = subprocess.run(
                prefix + ["shell", "su -c '%s'" % safe],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout or self._ROOT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ""
        except Exception:
            return ""
        err = proc.stderr or ""
        if "not found" in err or "Permission denied" in err:
            return ""
        return proc.stdout or ""

    def has_root(self) -> bool:
        """探测设备是否已 root（带间隔的缓存探测）。

        Magisk 等管理器首次 su 会弹窗请求授权——用短超时避免阻塞采样；
        用户点「允许」后，最多 8 秒内的下一次探测即成功，root 读取路径随即生效。
        """
        if self.mock:
            return False
        now = time.monotonic()
        if (getattr(self, "_root_state", None) is not None
                and now - getattr(self, "_root_ts", 0.0) < self._ROOT_PROBE_TTL):
            return self._root_state
        out = self.shell_root("id", timeout=8)
        ok = "uid=0" in out
        self._root_state = ok
        self._root_ts = now
        return ok

    def is_device_online(self) -> bool:
        if self.mock:            return True
        return self.serial in self._adb_devices(self.adb_bin)

    # ------------------------------------------------------------------
    # WiFi（无线）连接：USB 检测后切到 TCP/IP，拔线继续监控
    # ------------------------------------------------------------------
    def get_wlan_ip(self) -> str | None:
        """读取手机 wlan0 的 IPv4 地址（无线连接需要同网段）。"""
        out = self.shell(
            "ip addr show wlan0 2>/dev/null || ifconfig wlan0 2>/dev/null")
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out or "")
        return m.group(1) if m else None

    def tcpip(self, port: int = 5555) -> str:
        """开启设备端 adb over TCP/IP（默认 5555）。需 serial 指向 USB 设备。"""
        return self.run("tcpip %d" % port)

    @staticmethod
    def tcp_probe(ip: str, port: int = 5555, timeout: float = 2.0) -> tuple:
        """裸 TCP 探测 ``ip:port``（不经 adb），用于把「连不上」分成可操作的类型。

        返回 ``(errno, 中文说明)``；errno 为 0 表示端口开放、可直接建连。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            rc = s.connect_ex((ip, port))
        except socket.timeout:
            return errno.ETIMEDOUT, "超时（流量被过滤，检查路由器防火墙/安全软件）"
        except Exception as e:
            return -1, "探测异常：%r" % (e,)
        finally:
            try:
                s.close()
            except Exception:
                pass
        if rc == 0:
            return 0, "端口开放（TCP 可建连）"
        if rc == errno.ECONNREFUSED:
            return rc, "连接被拒绝（手机 5555 未监听，先执行 adb tcpip 5555）"
        if rc in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            return rc, "无路由/不可达（IP 失效、手机不在线，或路由器 AP 隔离）"
        if rc in (errno.EPERM, errno.EACCES):
            return rc, "被系统拦截（macOS「本地网络」权限未授权本应用）"
        if rc == errno.ETIMEDOUT:
            return rc, "超时（流量被过滤，检查路由器防火墙/安全软件）"
        return rc, "错误码 %d" % rc

    @classmethod
    def connect_ip(cls, adb_bin: str | None, ip: str, port: int = 5555) -> str:
        """``adb connect <ip>:<port>``，返回输出文本。"""
        if adb_bin is None:
            adb_bin = cls._discover_adb()
        return cls._adb(adb_bin, ["connect", "%s:%d" % (ip, port)], timeout=8)

    @classmethod
    def connect_wifi(cls, adb_bin: str | None, serial: str, port: int = 5555,
                     tries: int = 8, delay: float = 1.2) -> tuple:
        """USB→WiFi 一条龙：读 wlan IP → tcpip → 重试 connect。返回 (ip, wifi_serial, ok)。

        adbd 在 tcpip 后需数秒才开始监听，故对 connect 多次重试。
        """
        if adb_bin is None:
            adb_bin = cls._discover_adb()
        t = cls(adb_bin=adb_bin, serial=serial)
        ip = t.get_wlan_ip()
        if not ip:
            return None, None, False
        t.tcpip(port)
        wifi_serial = "%s:%d" % (ip, port)
        import time as _time
        for _ in range(max(1, tries)):
            _time.sleep(delay)
            out = cls._adb(adb_bin, ["connect", wifi_serial], timeout=8)
            if wifi_serial in cls._adb_devices(adb_bin):
                return ip, wifi_serial, True
        return ip, wifi_serial, False

    def _mock_shell(self, cmd: str) -> str:
        c = cmd.strip()
        # —— 批量命令（2026-10-26 采样提速：一次 adb 调用读多个数据源）——
        # GPU 三段：pct/busy + gpuclk + max_gpuclk（'---' 分隔）
        if (("gpu_busy_percentage" in c or "gpubusy" in c)
                and "gpuclk" in c and "max_gpuclk" in c):
            first = (self._mock_gpu_pct() if "gpu_busy_percentage" in c
                     else self._mock_gpubusy())
            return (first + "---\n" + self._mock_gpu_freq()
                    + "---\n" + self._mock_gpu_max_freq())
        # GPU：gpubusy + max_gpuclk 合并（'---' 分隔两段）
        if "gpubusy" in c and "max_gpuclk" in c:
            return self._mock_gpubusy() + "---\n" + self._mock_gpu_max_freq()
        # CPU：/proc/stat + 逐核频率（F<i> <kHz> 标记行）合并
        if "/proc/stat" in c and "scaling_cur_freq" in c:
            return (self._mock_proc_stat()
                    + "F0 1804800\nF1 1804800\nF2 1516800\nF3 1000000\n")
        # 内存：pidof + /proc/meminfo + /proc/<pid>/status 合并（'PID <n>' 标记行）
        if "pidof" in c and "/proc/meminfo" in c:
            return (self._mock_meminfo()
                    + "Name:\tcom.example.game\n"
                      "VmSize:\t  1234567 kB\n"
                      "VmPeak:\t  1300000 kB\n"
                      "VmRSS:\t    456789 kB\n"
                      "VmHWM:\t    500000 kB\n"
                      "PID 12345\n")
        if "gpubusy" in c:
            return self._mock_gpubusy()
        if "gpu_busy_percentage" in c:
            return self._mock_gpu_pct()
        if "max_gpuclk" in c:
            return self._mock_gpu_max_freq()
        if "gpuclk" in c:
            return self._mock_gpu_freq()
        if "gpu_freq" in c or ("cur_freq" in c and "scaling" not in c):
            # 备选频率节点（devfreq）在 mock 中“不存在”；排除 CPU 的 scaling_cur_freq
            return ""
        if "/proc/stat" in c:
            return self._mock_proc_stat()
        if "/proc/meminfo" in c:
            return self._mock_meminfo()
        if "/sys/class/thermal/" in c:
            return self._mock_thermal(c)
        if "dumpsys battery" in c:
            return self._mock_battery()
        if "current_now" in c:
            return self._mock_current()
        if "cpuinfo_max_freq" in c:
            return self._mock_cpuinfo_max(c)
        if "scaling_cur_freq" in c or "cpufreq" in c:
            return self._mock_cpufreq(c)
        if "pidof" in c:
            return self._mock_pidof()
        if "/proc/" in c and "/status" in c:
            # /proc/<pid>/status（应用内存四指标）。必须早于下面的 /stat 判断——
            # "/status" 含有子串 "/stat"。
            return ("Name:\tcom.example.game\n"
                    "VmSize:\t  1234567 kB\n"
                    "VmPeak:\t  1300000 kB\n"
                    "VmRSS:\t    456789 kB\n"
                    "VmHWM:\t    500000 kB\n")
        if "/proc/" in c and "/stat" in c:
            return self._mock_pid_stat()
        if "dumpsys SurfaceFlinger" in c and "--list" in c:
            # SF 层列表（FPS 采集器按前台包名定位游戏层用）
            return ("com.android.systemui.ImageWallpaper#0\n"
                    "SurfaceView - com.example.game/com.example.game.MainActivity#0\n"
                    "com.example.game/com.example.game.MainActivity#0\n")
        if "dumpsys SurfaceFlinger" in c:
            return self._mock_surfaceflinger()
        if "dumpsys gfxinfo" in c:
            return self._mock_gfxinfo()
        if "dumpsys meminfo" in c:
            return self._mock_app_meminfo()
        if "dumpsys window" in c:
            # 焦点窗口（focus_window 实际发的是 'dumpsys window'；旧写作
            # 'dumpsys window windows' 匹配不上，mock 下前台应用/层定位全空）
            return ("  mCurrentFocus=Window{1 u0 com.example.game/"
                    "com.example.game.MainActivity}\n")
        if "dumpsys package" in c:
            return ("  Package [com.example.game] (384dfab):\n"
                    "    userId=10293\n"
                    "    primaryCpuAbi=arm64-v8a\n"
                    "    versionCode=12345 minSdk=21 targetSdk=30\n"
                    "    versionName=1.2.3\n"
                    "    legacyNativeLibraryDir=/data/app/com.example.game-1==/lib\n")
        if c.startswith("ls "):
            # APK 原生库清单（Unity 引擎 mock，不带 libvulkan → 走引擎运行时分支）
            return "libunity.so\nlibil2cpp.so\nlibmain.so\n"
        return ""

    def _mock_gpubusy(self) -> str:
        self._mock["gpu_busy"] += 650
        self._mock["gpu_idle"] += 350
        return f"{self._mock['gpu_busy']} {self._mock['gpu_idle']}\n"

    def _mock_gpu_pct(self) -> str:
        seq = self._mock["stat_seq"]
        pct = 30.0 + (seq * 7) % 50
        return f"{pct:.0f} %\n"

    def _mock_gpu_freq(self) -> str:
        idx = self._mock["gpu_freq_idx"] % len(self._MOCK_GPU_FREQS)
        self._mock["gpu_freq_idx"] += 1
        return f"{self._MOCK_GPU_FREQS[idx]}\n"

    def _mock_gpu_max_freq(self) -> str:
        return f"{self._MOCK_GPU_FREQS[0]}\n"

    def _mock_proc_stat(self) -> str:
        cores = 4
        self._mock["stat_seq"] += 1
        seq = self._mock["stat_seq"]
        core_lines: list[str] = []
        total = [0] * 10
        for k in range(cores):
            base = seq * 1000 + k * 50
            user = base + (seq % 7) * 30
            nice = 0
            system = base // 2
            idle = base + (seq % 5) * 40
            iowait = 5
            irq = 1
            softirq = 1
            steal = 0
            guest = 0
            guest_nice = 0
            vals = [user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice]
            core_lines.append("cpu%d %s" % (k, " ".join(str(v) for v in vals)))
            for i in range(10):
                total[i] += vals[i]
        overall = "cpu " + " ".join(str(v) for v in total)
        return overall + "\n" + "\n".join(core_lines) + "\n"

    def _mock_meminfo(self) -> str:
        return ("MemTotal:        2048000 kB\nMemFree:          200000 kB\n"
                "MemAvailable:     800000 kB\nBuffers:           50000 kB\n"
                "Cached:           400000 kB\n")

    def _mock_thermal(self, cmd: str) -> str:
        # 新的逐区配对命令（type=temp 单行输出）
        if "$t=$v" in cmd or 't=$(cat $z/type' in cmd:
            return "\n".join(f"{name}={temp_mc}"
                             for name, temp_mc in self._MOCK_THERMAL) + "\n"
        if "thermal_zone*/type" in cmd:
            type_lines = [name for name, _ in self._MOCK_THERMAL]
            temp_lines = [str(temp_mc) for _, temp_mc in self._MOCK_THERMAL]
            return "\n".join(type_lines) + "\n__SEP__\n" + "\n".join(temp_lines) + "\n"
        m = re.search(r"thermal_zone(\d+)/(type|temp)", cmd)
        if m:
            idx = int(m.group(1)) % len(self._MOCK_THERMAL)
            field = m.group(2)
            name, temp_mc = self._MOCK_THERMAL[idx]
            if field == "type":
                return name + "\n"
            return f"{temp_mc}\n"
        return ("thermal_zone0\nthermal_zone1\nthermal_zone2\n"
                "thermal_zone3\nthermal_zone4\ncooling_device0\ncooling_device1\n")

    def _mock_battery(self) -> str:
        return ("Current Battery Service state:\n  AC powered: false\n  USB powered: true\n"
                "  Wireless powered: false\n  status: 2\n  level: 87\n  scale: 100\n"
                "  temperature: 350\n  voltage: 4200\n")

    def _mock_current(self) -> str:
        # 模拟放电电流（µA），随负载波动：约 1.8A~2.6A
        seq = self._mock["stat_seq"]
        ua = -(1_800_000 + (seq * 37_000) % 800_000)
        return f"{ua}\n"

    def _mock_cpufreq(self, cmd: str) -> str:
        # 每个被 cat 的 scaling_cur_freq 输出一行当前频率(kHz)
        n = max(1, cmd.count("scaling_cur_freq"))
        seq = self._mock["stat_seq"]
        lines = []
        for k in range(n):
            khz = 300_000 + (seq * 53_000 + k * 120_000) % 1_850_000
            lines.append(str(khz))
        return "\n".join(lines) + "\n"

    def _mock_cpuinfo_max(self, cmd: str) -> str:
        # 每个核一行最高频率(kHz)：模拟骁龙821 大小核（2 大 + 2 小）
        n = max(1, cmd.count("cpuinfo_max_freq"))
        lines = []
        for k in range(n):
            lines.append("2150400" if k < 2 else "1593600")
        return "\n".join(lines) + "\n"

    def _mock_pidof(self) -> str:
        return "12345\n"

    def _mock_pid_stat(self) -> str:
        # 每采样累计 utime+stime，模拟应用 CPU 占用
        self._mock["app_ticks"] = self._mock.get("app_ticks", 0) + 260
        utime = 100_000 + self._mock["app_ticks"]
        stime = 50_000 + self._mock["app_ticks"] // 2
        fields = ["S", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0",
                  str(utime), str(stime), "0", "0", "0", "0", "0", "0"]
        return "12345 (com.example.game) %s\n" % " ".join(fields)

    def _mock_surfaceflinger(self) -> str:
        period = 16_666_666
        base = 1_000_000_000_000
        lines = [str(period)]
        for i in range(120):
            desired = base + i * period
            actual = desired + 1_000_000
            lines.append(f"{desired} {actual} {desired}")
        return "\n".join(lines) + "\n"

    def _mock_gfxinfo(self) -> str:
        return ("Applications Graphics Acceleration Info:\nTotal frames rendered: 600\n"
                "Janky frames: 12\n50th percentile: 8ms\n90th percentile: 18ms\n"
                "95th percentile: 25ms\n99th percentile: 40ms\n")

    def _mock_app_meminfo(self) -> str:
        return ("Applications Memory Usage (in Kilobytes):\n"
                "TOTAL   123456  78901  234567  123456  45000  30000\n")
