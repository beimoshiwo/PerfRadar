# -*- coding: utf-8 -*-
"""前台应用信息采集（免 root）：名称 / 版本 / ABI / 引擎 / 渲染 API 线索。

渲染 API 可行性说明（本机 MI 5s 实测）：
  · ``/proc/<pid>/maps`` 在 shell 域被 SELinux 拒绝（Permission denied）；
  · ``run-as`` 仅对 debuggable 应用可用（QQ飞车：package not debuggable）；
  · ``dumpsys meminfo`` 只有 ``.so mmap`` 聚合值，没有库名。
因此运行时「实际」API 无法直接观测。本采集器给出可验证的组合信号：
  ① APK 自带 ``libvulkan*.so``                → Vulkan（确定）
  ② 当前 Activity 类名含 vulkan/gles 等关键字   → 对应 API
  ③ 引擎识别（Unity / Unreal / Cocos / 原生）   → 引擎名
  ④ 以上均无                                  → 原生应用按系统渲染（OpenGL ES）
"""

from __future__ import annotations

import re
import time

from .transport import AdbTransport

# 常见应用包名 → 中文名（查不到时回退显示包名）
KNOWN_APPS = {
    "com.smile.gifmaker": "快手",
    "com.kuaishou.nebula": "快手极速版",
    "com.kuaishou.offlinestore": "快手",
    "com.tencent.tmgp.speedmobile": "QQ飞车手游",
    "com.tencent.tmgp.speedmobileEx": "QQ飞车手游(体验服)",
    "com.tencent.tmgp.sgame": "王者荣耀",
    "com.tencent.tmgp.pubgmhd": "和平精英",
    "com.tencent.tmgp.cf": "穿越火线手游",
    "com.tencent.mobileqq": "QQ",
    "com.tencent.mm": "微信",
    "com.tencent.tmgp.WeMeet": "腾讯会议",
    "com.miHoYo.Yuanshen": "原神",
    "com.miHoYo.hkrpg": "崩坏：星穹铁道",
    "com.netease.dwrg": "第五人格",
    "com.netease.mrzh": "明日之后",
    "com.netease.mc": "我的世界",
    "com.futuremark.dmandroid.application": "3DMark",
    "com.fukebench7": "FukeBench",
    "com.miui.home": "小米桌面",
    "com.android.settings": "设置",
}

# 引擎 → 原生库标记（命中任意子串即认定；按顺序优先匹配）
ENGINE_LIBS = [
    ("Unity", ("libunity.so", "libil2cpp.so", "libPixUI_Unity.so",
               "libMSDKUnityAdapter.so", "libunityplayer.so")),
    ("Unreal", ("libUE4.so", "libUnreal.so", "libUE4-")),
    ("Cocos2d", ("libcocos2d", "libcocos")),
    ("Godot", ("libgodot",)),
    ("Egret", ("libegret",)),
]

# 跨安卓版本 / 厂商（AOSP + HyperOS / EMUI 等）通用的前台应用识别。
# 各安卓版本 dumpsys 字段演进（均已在真机实测）：
#   Android ≤9     : dumpsys window  ->  mCurrentFocus=Window{... u0 PKG/ACT}
#   Android 10-13   : dumpsys activity activities -> mResumedActivity=ActivityRecord{... u0 PKG/ACT}
#   Android 14 AOSP : 同级用  topResumedActivity=ActivityRecord{... u0 PKG/ACT}
#   HyperOS(安卓14) : 用  ResumedActivity: ActivityRecord{...}  以及  mFocusedApp=ActivityRecord{...}
# 最稳的单一信号是 mFocusedApp（WindowManager / ATMS 都会输出，指向"真正获得焦点的应用"，
# 即便有状态栏 / 游戏助手覆盖也准确）。依次回退到各版本 resumed 字段，再到 mCurrentFocus 兜底
# （并过滤系统窗口），最后用任意 ActivityRecord 兜底。
RE_FOCUSED_APP = re.compile(
    r"mFocusedApp=ActivityRecord\{[^}]*\s+u0\s+([^}\s]+)")
RE_RESUMED = re.compile(
    r"(?:topResumedActivity|ResumedActivity|mResumedActivity|mFocusedActivity)"
    r"[=:\s]*ActivityRecord\{[^}]*\s+u0\s+([^}\s]+)")
RE_FOCUS_WIN = re.compile(r"mCurrentFocus=Window\{[^}]*\s+u0\s+([^}\s]+)")
# 兜底：任意 ActivityRecord 里取首个非系统包（仅在以上全失败时用）
RE_ANY_ACT = re.compile(r"ActivityRecord\{[^}]*\s+u0\s+([^}\s]+)")

# 系统窗口 / 系统包（mCurrentFocus 偶尔指向它们，不应当作被测应用）
_SYSTEM_WINDOWS = ("NotificationShade", "StatusBar", "NavigationBar", "Keyguard",
                   "Wallpaper", "Dream")


def _as_component(token: str | None):
    """从 PKG/ACT 形式中拆分；返回 (pkg, 完整组件串) 或 (None, None)。"""
    if not token or "/" not in token:
        return None, None
    return token.split("/", 1)[0], token


def focus_window(transport: AdbTransport) -> tuple:
    """当前焦点应用 → (包名, 完整组件串 PKG/ACT)。解析失败返回 (None, None)。

    兼容 Android ≤9(Window.mCurrentFocus) → 10-13(mResumedActivity) → 14 AOSP
    (topResumedActivity) → HyperOS(ResumedActivity: / mFocusedApp)。优先采用 mFocusedApp，
    它对覆盖层 / 沉浸式游戏更稳定；再做各版本 resumed 回退、mCurrentFocus 兜底。
    """
    win_out = transport.shell("dumpsys window") or ""
    act_out = transport.shell("dumpsys activity activities") or ""
    blob = win_out + "\n" + act_out

    m = RE_FOCUSED_APP.search(blob)
    if m:
        pkg, comp = _as_component(m.group(1).strip())
        if pkg:
            return pkg, m.group(1).strip()
    m = RE_RESUMED.search(blob)
    if m:
        pkg, comp = _as_component(m.group(1).strip())
        if pkg:
            return pkg, m.group(1).strip()
    m = RE_FOCUS_WIN.search(win_out)
    if m:
        token = m.group(1).strip()
        pkg, comp = _as_component(token)
        if pkg and pkg not in _SYSTEM_WINDOWS:
            return pkg, token
    m = RE_ANY_ACT.search(act_out)
    if m:
        pkg, comp = _as_component(m.group(1).strip())
        if pkg and not pkg.startswith(("com.android.", "com.miui.",
                                       "android.", "com.tencent.mm")):
            return pkg, m.group(1).strip()
    return None, None


def focus_window_cached(transport: AdbTransport, ttl: float = 1.5) -> tuple:
    """带 TTL 缓存的前台探测——多个采集器共享同一次 dumpsys（2026-10-26 采样提速）。

    FPS 层定位与内存采集各自缓存前台包名（TTL 2s/3s），并行采样时同一轮会重复
    跑 2 次 focus_window（各 2 次 dumpsys，慢设备上 >400ms）。共享缓存后每 1.5s
    全进程只探测一次。缓存挂在 transport 对象上（每个连接独立、随连接销毁）。
    线程安全：元组赋值原子，最坏情况是一次重复探测，不会读到半状态。
    """
    now = time.monotonic()
    cache = getattr(transport, "_focus_cache", None)
    if cache is not None:
        val, ts = cache
        if val[0] and now - ts < ttl:
            return val
    val = focus_window(transport)
    try:
        transport._focus_cache = (val, now)
    except Exception:
        pass
    return val


class AppInfoCollector:
    """前台应用信息（免 root）。按需查询，不参与每采样循环。"""

    def __init__(self, transport: AdbTransport) -> None:
        self.transport = transport
        self._cache: dict = {}        # pkg -> info（同一应用只查一次）

    # ---- 包详情 ----
    def _package_fields(self, pkg: str) -> dict:
        out = self.transport.shell("dumpsys package %s" % pkg) or ""
        def grab(key):
            m = re.search(r"^\s*%s=([^\s]+)" % key, out, re.M)
            return m.group(1) if m else None
        return {
            "version": grab("versionName"),
            "version_code": grab("versionCode"),
            "abi": grab("primaryCpuAbi"),
            "lib_dir": grab("legacyNativeLibraryDir"),
        }

    def _native_libs(self, lib_dir: str | None) -> list:
        if not lib_dir:
            return []
        for sub in ("arm64", "arm", "arm64-v8a", "armeabi-v7a"):
            out = self.transport.shell("ls %s/%s" % (lib_dir, sub))
            libs = [l.strip() for l in (out or "").splitlines()
                    if l.strip().endswith(".so")]
            if libs:
                return libs
        return []

    @staticmethod
    def _engine_of(libs: list):
        for name, markers in ENGINE_LIBS:
            if any(any(mk in lib for mk in markers) for lib in libs):
                return name
        return None

    # 外部存储中的应用偏好/设置文件（Unity 游戏常把 PlayerPrefs 备份到这里；
    # 腾讯系 Unity 游戏用 VulkanEnable 记录 Vulkan 开关，如 QQ飞车）
    PREF_FILE_TEMPLATES = (
        "/sdcard/Android/data/{pkg}/files/CustomPlayerPrefs_backup",
        "/sdcard/Android/data/{pkg}/files/PlayerPrefs_backup",
        "/sdcard/Android/data/{pkg}/files/playerprefs.xml",
        "/sdcard/Android/data/{pkg}/files/GameSettingData.json",
    )

    def _api_from_prefs(self, pkg: str):
        """从外部存储偏好备份里找图形 API 开关（如 "VulkanEnable":0）。

        返回 (API, 依据) 或 None。这是**游戏自己的设置**，比 Activity 猜准。
        Android 11+ 的 /sdcard/Android/data 受限时自动跳过（返回 None）。
        """
        for tpl in self.PREF_FILE_TEMPLATES:
            out = self.transport.shell("cat %s" % tpl.format(pkg=pkg))
            if not out or len(out.strip()) < 8:
                continue
            pairs = []
            # JSON 形式："Key":0 / "Key":"Vulkan"
            pairs += re.findall(r'"([A-Za-z_][\w.]*)"\s*:\s*("[^"]*"|[\w.]+)', out)
            # Unity XML：<string name="Key">1</string> / <int name="Key" value="1" />
            pairs += re.findall(r'name="([A-Za-z_][\w.]*)"[^>]*?>\s*([^<]+?)\s*<', out)
            pairs += re.findall(r'name="([A-Za-z_][\w.]*)"[^>]*?value="([^"]*)"', out)
            for key, val in pairs:
                kl = key.lower()
                vl = val.strip('"').strip().lower()
                if "vulkan" in kl:
                    on = (self._truthy(val) or vl == "vulkan")
                    if kl.startswith(("disable", "no", "not", "un", "close")):
                        on = not on
                    return (("Vulkan" if on else "OpenGL ES"),
                            "游戏设置 %s=%s" % (key, val.strip('"')))
                if "opengl" in kl or "gles" in kl:
                    return ("OpenGL ES", "游戏设置 %s=%s" % (key, val.strip('"')))
                if "vulkan" in vl:
                    return ("Vulkan", "游戏设置 %s=%s" % (key, val.strip('"')))
                if "opengl" in vl or "gles" in vl:
                    return ("OpenGL ES", "游戏设置 %s=%s" % (key, val.strip('"')))
        return None

    @staticmethod
    def _truthy(val: str) -> bool:
        v = val.strip().strip('"').lower()
        return v in ("1", "true", "yes", "on", "enable", "enabled")

    # 运行时驱动库匹配（/proc/<pid>/maps）：
    # 只看**驱动/ICD**，不看加载器 libvulkan.so（加载器可能被加载但并未真正用 Vulkan）。
    #   Adreno Vulkan 驱动 = vulkan.adreno.so / libvulkan_adreno.so
    #   Adreno GLES  驱动 = libGLESv2_adreno.so / libEGL_adreno.so
    #   Mali  Vulkan 驱动 = vulkan.mali.so / libvulkan_mali.so
    #   Mali  GLES  驱动 = libGLES_mali.so
    RE_VK_DRV = re.compile(r"vulkan\.[a-z0-9]+\.so|libvulkan_[a-z0-9]+\.so")
    RE_GL_DRV = re.compile(r"libGLESv2[_.]|libGLESv1[_.]|libGLES_[a-z0-9]+|libEGL_adreno|libglES")

    def _api_from_maps(self, pid):
        """读 /proc/<pid>/maps 判定**运行时**实际渲染 API（金标准）。

        root / 安卓 ≤10(hidepid 未收紧) / run-as debuggable 时可读；
        普通 shell 在安卓 11+ 被 hidepid 挡（Permission denied）→ 返回 None。
        依据实际映射进来的 GPU 驱动库，是“此刻真的在用”的 API。
        """
        if not pid:
            return None
        out = self.transport.shell("cat /proc/%s/maps" % pid)
        if not out or "Permission denied" in out or "No such" in out:
            return None
        if self.RE_VK_DRV.search(out):
            return "Vulkan"
        if self.RE_GL_DRV.search(out):
            return "OpenGL ES"
        return None

    # 引擎启动日志：Unity/Unreal 等会在启动时打印实际图形设备（含 Vulkan/OpenGLES + GPU 型号）
    RE_LOG_API = (
        (re.compile(r"gfxdevice.{0,40}vulkan|vulkan.{0,40}gfxdevice", re.I), "Vulkan"),
        (re.compile(r"gfxdevice.{0,40}(opengles|opengl|gles)", re.I), "OpenGL ES"),
        (re.compile(r"(initialize|create).{0,20}(vulkan)", re.I), "Vulkan"),
        (re.compile(r"\bvulkan\b.{0,30}(adreno|mali|powervr|gpu)", re.I), "Vulkan"),
        (re.compile(r"(opengles|gles\s?3?|opengl)\b.{0,30}(adreno|mali|powervr|gpu)", re.I),
         "OpenGL ES"),
    )

    def _api_from_logcat(self, pid):
        """从 logcat 抓引擎启动时打印的图形 API（游戏**刚启动**时有效）。

        只采信含该应用 PID 的行，避免被系统/其它应用的日志误导。缓冲区已滚掉的
        老实例抓不到（返回 None）。
        """
        if not pid:
            return None
        out = self.transport.shell("logcat -d 2>/dev/null")
        if not out:
            return None
        pat = re.compile(r"(^|\s)%s(\s|$)" % re.escape(str(pid)))
        for line in out.splitlines():
            if not pat.search(line):
                continue
            for rx, api in self.RE_LOG_API:
                if rx.search(line):
                    return api
        return None

    @staticmethod
    def _api_of(libs: list, activity: str | None, engine, pref, runtime=None) -> tuple:
        """返回 (API 结论, 依据)。runtime = 运行时实测 API（maps/日志），最高权威。"""
        if runtime:                      # 运行时驱动/日志实测，最权威
            return runtime, "运行时实测"
        if any(re.match(r"libvulkan", lib) for lib in libs):
            return "Vulkan", "APK 自带 libvulkan"
        if pref:                       # 游戏自己的设置，次权威
            return pref
        act = (activity or "").lower()
        if "vulkan" in act:
            return "Vulkan", "Activity 类名含 vulkan"
        if re.search(r"gles|glsurface|gl2[03]|opengl", act):
            return "OpenGL ES", "Activity 类名含 GLES"
        if engine:
            return "Vulkan / OpenGL ES", "%s 引擎运行时决定" % engine
        return "OpenGL ES", "原生应用·系统渲染"

    def fetch(self, pkg: str, activity: str | None = None,
              pid=None) -> dict:
        """查询并返回应用信息（带缓存，同一应用只查一次）。

        pid 传入时优先做**运行时实测**（/proc/maps 驱动库 → logcat 引擎日志），
        能在 root / 旧安卓 / 游戏刚启动时给出确定的 OpenGL/Vulkan 结论。
        """
        if pkg not in self._cache:
            fields = self._package_fields(pkg)
            libs = self._native_libs(fields.get("lib_dir"))
            engine = self._engine_of(libs)
            pref = self._api_from_prefs(pkg)
            self._cache[pkg] = {
                "pkg": pkg,
                "label": KNOWN_APPS.get(pkg, ""),
                "version": fields.get("version"),
                "abi": fields.get("abi"),
                "engine": engine,
                "libs": len(libs),
                "_libs": libs,
                "_pref": pref,
            }
        info = dict(self._cache[pkg])
        info["activity"] = activity or info.get("activity")
        # 运行时 API 现算（不缓存）：/proc maps 优先，其次 logcat 引擎日志。
        # 安卓 11+ 普通 shell 读 maps 被 hidepid 挡 → 两路都 None → 走启发式兜底。
        runtime = self._api_from_maps(pid)
        if runtime is None:
            runtime = self._api_from_logcat(pid)
        # Activity 可能在启动后变化（如 3DMark 的 .vulkan.*Activity）→ 重判 API
        info["api"], info["api_src"] = self._api_of(
            info.get("_libs", []), activity, info.get("engine"),
            info.get("_pref"), runtime)
        return info
