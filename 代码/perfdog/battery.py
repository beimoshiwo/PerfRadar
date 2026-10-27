# -*- coding: utf-8 -*-
"""电池采集器：``dumpsys battery`` 解析 level / temperature / voltage，
电流经 power_supply 节点读取（shell 域被 SELinux 拒时走 run-as app 域）。

Kite/PerfDog 风格三线：功耗(mW) = |电流(µA)| × 电压(mV) ÷ 1e6。
"""

from __future__ import annotations

import re
import time

from .transport import AdbTransport


class BatteryCollector:
    """电池采集器（免 root）。temperature 字段单位为 0.1℃，转 ℃；
    功耗由 ``current_now``(µA) × 电压(mV) 估算实时功率(mW)。"""

    # BatteryManager 属性值 id
    PROPS_CURRENT_NOW = 2      # BATTERY_PROPERTY_CURRENT_NOW（µA，负=放电）
    PROPS_CURRENT_AVG = 3      # BATTERY_PROPERTY_CURRENT_AVERAGE
    PROPS_CAPACITY = 4         # BATTERY_PROPERTY_CAPACITY（0-100）

    # ``service call batteryproperties <code>`` 的 getProperty 事务码**随机型/系统
    # 版本而变**（实测：小米6 / Android 9 / MIUI → 3；华为 Mate10 / EMUI → 1）。
    # 写死事务码会在部分机型上永远返回空 Parcel，故全部枚举并用 CAPACITY 自检。
    _PROPS_CODES = (3, 1, 2, 4, 5)
    # getProperty 第二参是 out BatteryProperty（Parcelable），部分实现要求客户端
    # 先写一个 long 占位，部分不接受多余参数，两种都试。
    _PROPS_ARGS = (" i64 0", "")

    LONG_MIN = -(2 ** 63)      # BatteryProperty 用它表示“该属性不支持”

    # 电池电流节点候选（各家命名/大小写都试；目录禁列不代表文件禁读，逐个直读）
    CURRENT_NODES = [
        "/sys/class/power_supply/battery/current_now",      # 通用（小写）
        "/sys/class/power_supply/Battery/current_now",      # 华为/麒麟（大写 B）
        "/sys/class/power_supply/bms/current_now",          # 高通 BMS
        "/sys/class/power_supply/battery/current_avg",      # 平均电流（无瞬时值时回退）
        "/sys/class/power_supply/Battery/current_avg",
        "/sys/class/power_supply/mains/current_now",        # 充电输入电流（最后回退）
        "/sys/class/power_supply/Mains/current_now",
        "/sys/class/power_supply/usb/current_now",
        "/sys/class/power_supply/USB/current_now",
    ]

    def __init__(self, transport: AdbTransport, run_as_pkg: str | None = None) -> None:
        self.transport = transport
        self.run_as_pkg = run_as_pkg
        # 电流通路缓存：小米6 这类机型 sysfs 全目录被 SELinux 封死，若每次采样都
        # 重跑 8 次 cat（每次 ~200ms）会把采样间隔拖到 2s+，故探测一次后记住通路。
        self._cur_src: str | None = None
        self._cur_src_ts = 0.0
        self._root_seen = False
        # 单位口径是设备固定属性（AOSP 要求 µA，部分厂商 HAL 按 mA 上报），
        # 只在首次探测时判定一次并记住，避免小电流瞬时值被误判放大。
        self._cur_scale: int | None = None
        # batteryproperties 事务码缓存（code, arg）
        self._props_addr: tuple | None = None
        self._props_addr_bad = False
        # 电池电流是 BMS 瞬时采样，原始值抖动可达 ±200mA（小米6 实测同一秒内
        # -14mA~-449mA），直接画线会糊成一片，故做轻度指数平滑（α=0.35）。
        self._cur_ema: float | None = None
        self._charging_seen: bool | None = None   # 上一次的充电状态（切换时重置 EMA）

    @staticmethod
    def _field(out, key) -> str | None:
        """按行首匹配字段——必须锚定，否则会误捕获
        'Max charging voltage: 5000000' 这类同后缀字段。"""
        if not out:
            return None
        m = re.search(r"^\s*%s:\s*(\d+)" % re.escape(key), out, re.M)
        return m.group(1) if m else None

    @staticmethod
    def _bool(out, key) -> bool:
        if not out:
            return False
        m = re.search(r"^\s*%s:\s*(\w+)" % re.escape(key), out, re.M)
        return bool(m) and m.group(1).lower() == "true"

    @staticmethod
    def _as_int(v: str | None) -> int | None:
        v = (v or "").strip()
        return int(v) if v.lstrip("-").isdigit() else None

    @staticmethod
    def _parse_props(raw: str):
        """解析 ``service call`` 返回的 Parcel：None=无返回（事务码错误），
        LONG_MIN=该属性不支持，其余为属性值（小端 long）。
        Parcel 布局：第 1 字异常码、第 3 字 present 标志、第 4-5 字为 long 值。"""
        words: list = []
        for line in (raw or "").splitlines():
            m = re.match(r"\s*0x[0-9a-f]+:\s*(.*)", line, re.I)
            if not m:
                continue
            data = m.group(1).split("'")[0]        # 去掉行尾 ASCII 列
            words += re.findall(r"[0-9a-f]{8}", data, re.I)
        if len(words) < 5:
            return None
        try:
            val = (int(words[4], 16) << 32) | int(words[3], 16)   # 小端 long
            if val >= 2 ** 63:
                val -= 2 ** 64
        except ValueError:
            return None
        return val

    def _props_get(self, code: int, pid: int, arg: str):
        return self._parse_props(self.transport.shell(
            "service call batteryproperties %d i32 %d%s" % (code, pid, arg)))

    def _detect_props_addr(self, level):
        """确定本机的 getProperty 事务码（小米6/Android9→3，华为Mate10/EMUI→1）。

        优先用 CAPACITY 属性与 ``dumpsys battery`` 的电量百分比比对自检（小米6
        实测服务返回 93、dumpsys 显示 93），自检全失败时退化为“能返回有效电流的
        第一个组合”，保证 CAPACITY 口径特殊的机型（EMUI）仍能命中。
        """
        if self._props_addr is not None or self._props_addr_bad:
            return self._props_addr
        for code in self._PROPS_CODES:
            for arg in self._PROPS_ARGS:
                cap = self._props_get(code, self.PROPS_CAPACITY, arg)
                if cap is None or cap == self.LONG_MIN:
                    continue
                if level is None or (0 <= cap <= 100 and abs(cap - level) <= 2):
                    self._props_addr = (code, arg)
                    return self._props_addr
        for code in self._PROPS_CODES:
            for arg in self._PROPS_ARGS:
                v = self._props_get(code, self.PROPS_CURRENT_NOW, arg)
                if v not in (None, self.LONG_MIN, 0):
                    self._props_addr = (code, arg)
                    return self._props_addr
        self._props_addr_bad = True
        return None

    def _read_current_via_props(self, level=None) -> int | None:
        """经 batteryproperties 服务取实时电流（µA）——BatteryManager.getProperty
        通道，华为/EMUI、小米6 这类 sysfs 被 SELinux 封死的机型的唯一免 root 通路。"""
        addr = self._detect_props_addr(level)
        if not addr:
            return None
        val = self._props_get(addr[0], self.PROPS_CURRENT_NOW, addr[1])
        if val in (None, self.LONG_MIN, 0):
            return None
        return val

    def _normalize(self, v: int) -> int:
        """单位归一：AOSP 契约是 µA，但部分厂商 HAL 按 mA 上报（实测华为 EMUI
        Mate10 读数 -542 而电量趋势 -1.6A）。运行中的手机任何工况电流都远大于
        3mA，故 |值|<3000 判定为 mA 口径；口径是设备固定属性，只在首次取值时
        判定一次并记住，避免瞬时小电流被误放大。"""
        if self._cur_scale is None:
            self._cur_scale = 1000 if abs(v) < 3000 else 1
        return v * self._cur_scale

    def _smooth(self, v: int) -> int:
        """轻度指数平滑：保留趋势、压掉 BMS 采样噪声。"""
        self._cur_ema = float(v) if self._cur_ema is None else (
            0.35 * v + 0.65 * self._cur_ema)
        return int(round(self._cur_ema))

    @staticmethod
    def _read_via_nodes(nodes, read) -> int | None:
        for p in nodes:
            v = BatteryCollector._as_int(read(p))
            if v is not None:
                return v
        return None

    def _read_by_source(self, src: str, level):
        if src == "root":
            return self._read_via_nodes(
                self.CURRENT_NODES, lambda p: self.transport.shell_root("cat %s" % p))
        if src == "sysfs":
            return self._read_via_nodes(
                self.CURRENT_NODES, lambda p: self.transport.shell("cat %s" % p))
        if src == "props":
            return self._read_current_via_props(level)
        if src == "runas" and self.run_as_pkg:
            pkg = self.run_as_pkg

            def _ra(p):
                out = self.transport.shell("run-as %s cat %s" % (pkg, p)) or ""
                return "" if out.strip().startswith("run-as:") else out
            return self._read_via_nodes(self.CURRENT_NODES, _ra)
        return None

    def _read_current_ua(self, level=None) -> int | None:
        """按 root → sysfs → batteryproperties 服务 → run-as 的优先级取电流。

        小米6 这类机型 sysfs 全目录被封，若每次采样都重跑 8 次 cat（各 ~200ms）
        会把采样间隔拖到 2s 以上，故探测到可用通路后缓存（60s）复用；root 状态
        一变化立即重新探测，满足“root 后立刻启用”的要求。
        """
        rooted = self.transport.has_root()
        if rooted != self._root_seen:
            self._cur_src = None
            self._cur_scale = None
            self._root_seen = rooted
            self._props_addr = None
            self._props_addr_bad = False
        now = time.monotonic()
        if self._cur_src:
            if now - self._cur_src_ts < 60.0:
                v = self._read_by_source(self._cur_src, level)
                if v is not None:
                    return self._smooth(self._normalize(v))
            self._cur_src = None
        elif self._cur_src_ts and now - self._cur_src_ts < 30.0:
            return None      # 上次全通道失败，30s 内不再空跑（避免拖慢采样）
        order = (["root", "sysfs", "props", "runas"] if rooted
                 else ["sysfs", "props", "runas"])
        for src in order:
            v = self._read_by_source(src, level)
            if v is not None:
                self._cur_src = src
                self._cur_src_ts = now
                return self._smooth(self._normalize(v))
        self._cur_src_ts = now
        return None

    def sample(self) -> dict:
        out = self.transport.shell("dumpsys battery")
        level_raw = self._field(out, "level")
        temp_raw = self._field(out, "temperature")
        volt_raw = self._field(out, "voltage")
        ac = self._bool(out, "AC powered")
        usb = self._bool(out, "USB powered")
        wireless = self._bool(out, "Wireless powered")
        level = int(level_raw) if level_raw and level_raw.isdigit() else None
        temp_c = (int(temp_raw) / 10.0) if temp_raw and temp_raw.isdigit() else None
        voltage = int(volt_raw) if volt_raw and volt_raw.isdigit() else None

        # 实时功耗：|电流|(µA) × 电压(mV) ÷ 1e6 → mW。
        # 充电时这是**充电功率**（充电器还要额外供系统），放电时才是整机功耗，
        # 两者口径不同——数值一律照常输出（充电功率本身也是有效数据），由界面
        # 标注当前处于哪种口径，避免被误读成“游戏只耗这么点电”。
        charging = bool(ac or usb or wireless)
        if charging != self._charging_seen:
            # 充 ↔ 放电切换（插/拔充电器）：电流符号翻转且量级差数倍，沿用旧平滑
            # 值会爬升数秒，直接重置，保证切换后立刻给出真实值。
            self._cur_ema = None
            self._charging_seen = charging
        power_mw = None
        cur = self._read_current_ua(level)
        if cur is not None and voltage:
            power_mw = round(abs(cur) * voltage / 1e6, 1)

        return {
            "level": level,
            "temp_c": round(temp_c, 1) if temp_c is not None else None,
            "voltage_mv": voltage,
            "current_ua": cur,
            "power_mw": power_mw,
            "charging": charging,
            "current_source": self._cur_src,
        }
