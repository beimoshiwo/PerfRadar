# PerfRadar

腾讯 PerfDog 的开源克隆 · Android 性能实时监控工具（macOS GUI）。

通过 ADB **免 root** 采集连接中的 Android 设备性能数据：

- **FPS**：SurfaceFlinger 前台应用渲染面帧时间戳（自动定位 BLAST/SurfaceView 层，跳过壁纸/主窗口等无效层，含停滞检测）
- **CPU / GPU / 内存 / 温度 / 电量**：多路并行采样
- **实时 GUI**：PerfRadar.app 双击即开，实时曲线与指标面板
- **报告导出**：HTML 性能报告

## 目录

```
代码/
├── perfdog/            # Python 采集库
│   ├── transport.py    # ADB 通信层
│   ├── fps.py          # FPS（SurfaceFlinger）
│   ├── cpu.py / gpu.py / logcat_gpu.py
│   ├── memory.py / thermal.py / battery.py
│   ├── appinfo.py      # 前台应用识别
│   ├── session.py      # 采样会话（并行采样 + 节奏控制）
│   └── report.py       # HTML 报告导出
└── tools/
    └── gpu_webgl_test.html   # WebGL GPU 测试页
```

## 要求

- macOS（运行 PerfRadar.app）
- 一台已开启 USB 调试的 Android 设备，通过 ADB 连接

## 下载

macOS 安装包见 [Releases](https://github.com/beimoshiwo/PerfRadar/releases)。
