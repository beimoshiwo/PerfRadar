# -*- coding: utf-8 -*-
"""自包含 HTML 报告生成（离线打开，无外部依赖）。

本模块是代码库的「导出/报告」能力，GUI 实时监控是主交付物，这里仅提供
采集结束后一键导出历史数据的 HTML 报告。
"""

from __future__ import annotations

import os
from datetime import datetime


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%.1f" % v)
    return str(v)


def _series(samples: list, key: str) -> list:
    out = []
    for i, s in enumerate(samples):
        val = s.get(key)
        if val is not None:
            out.append((i, val))
    return out


def _line_chart(key, title, unit, color, samples, ymax) -> str:
    pts = _series(samples, key)
    if not pts:
        return "<p style='color:#888'>无数据</p>"
    n = len(samples)
    w, h = 760, 150
    pad = 30
    xs = [pad + (i / max(1, n - 1)) * (w - 2 * pad) for i, _ in pts]
    vals = [v for _, v in pts]
    if ymax is None:
        ymax = max(vals) * 1.15 if max(vals) > 0 else 1.0
    ymax = max(ymax, 1.0)
    ys = [h - pad - (v / ymax) * (h - 2 * pad) for v in vals]
    path = " ".join("L%.1f,%.1f" % (x, y) if j else "M%.1f,%.1f" % (x, y)
                    for j, (x, y) in enumerate(zip(xs, ys)))
    grid = ""
    for g in range(0, 5):
        gy = pad + g * (h - 2 * pad) / 4
        gv = ymax * (1 - g / 4)
        grid += ("<line x1='%d' y1='%.1f' x2='%d' y2='%.1f' stroke='#2a2f3a'/>"
                 "<text x='%d' y='%.1f' fill='#888' font-size='10'>%.0f</text>"
                 % (pad, gy, w - pad, gy, 2, gy + 3, gv))
    circles = "".join("<circle cx='%.1f' cy='%.1f' r='2' fill='%s'/>" % (x, y, color)
                      for x, y in zip(xs, ys))
    return ("<div class='card'><h3>%s <span class='u'>(%s)</span></h3>"
           "<svg viewBox='0 0 %d %d' width='100%%' height='%d'>"
           "%s<path d='%s' fill='none' stroke='%s' stroke-width='2'/>%s"
           "</svg></div>" % (title, unit, w, h, h, grid, path, color, circles))


def build_html_report(samples: list, meta: dict | None = None) -> str:
    meta = meta or {}
    device = meta.get("device", "未知设备")
    interval = meta.get("interval", 1.0)
    gpu_available = meta.get("gpu_available", False)
    gpu_source = meta.get("gpu_source")

    latest = samples[-1] if samples else {}
    rows = []
    labels = [
        ("FPS", "fps"), ("CPU 占用率(%)", "cpu_total"), ("CPU 频率(MHz)", "cpu_freq_mhz"),
        ("GPU 占用率(%)", "gpu_usage"), ("GPU 频率(MHz)", "gpu_freq_mhz"),
        ("内存(MB)", "mem_used_mb"), ("功耗(mW)", "power_mw"),
        ("电量(%)", "level"), ("温度(℃)", "temp_c"),
    ]
    for label, key in labels:
        rows.append("<tr><td>%s</td><td>%s</td></tr>" % (label, _fmt(latest.get(key))))

    gpu_banner = ""
    if gpu_available:
        chan = (" 免 root 读取（通道：%s）" % gpu_source) if gpu_source else " 免 root 读取"
        gpu_banner = "<div class='banner ok'>GPU 数据：%s</div>" % chan
    else:
        gpu_banner = "<div class='banner warn'>GPU 节点本设备暂不可读，仅 CPU/FPS/内存等可用</div>"

    charts = []
    charts.append(_line_chart("fps", "FPS", "帧/秒", "#3ec46d", samples, None))
    charts.append(_line_chart("cpu_total", "CPU 占用率", "%", "#4ea3ff", samples, 100))
    charts.append(_line_chart("cpu_freq_mhz", "CPU 频率", "MHz", "#4ea3ff", samples, None))
    charts.append(_line_chart("gpu_usage", "GPU 占用率", "%", "#ff5d5d", samples, 100))
    charts.append(_line_chart("gpu_freq_mhz", "GPU 频率", "MHz", "#b07dff", samples, None))
    charts.append(_line_chart("mem_used_mb", "内存占用", "MB", "#ffa64d", samples, None))
    charts.append(_line_chart("power_mw", "功耗", "mW", "#2dd4d4", samples, None))
    charts.append(_line_chart("temp_c", "温度", "℃", "#2dd4d4", samples, None))

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PerfDog 克隆 · 性能报告</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#1b1e24;color:#e6e9ef;font-family:-apple-system,"PingFang SC",Segoe UI,sans-serif}}
header{{padding:18px 24px;background:linear-gradient(90deg,#2563eb,#7c3aed);}}
header h1{{margin:0;font-size:20px}}
header p{{margin:4px 0 0;opacity:.85;font-size:13px}}
.banner{{margin:14px 24px;padding:10px 14px;border-radius:8px;font-size:14px}}
.ok{{background:#14331f;color:#7ee2a8;border:1px solid #2f6b46}}
.warn{{background:#3a2f14;color:#ffd27a;border:1px solid #6b5a2f}}
.summary{{margin:0 24px;background:#232732;border-radius:10px;overflow:hidden}}
.summary td{{padding:8px 14px;border-top:1px solid #2c313d;font-size:14px}}
.summary td:first-child{{color:#9aa3b2;width:160px}}
.cards{{display:flex;flex-wrap:wrap;gap:14px;margin:16px 24px 40px}}
.card{{flex:1 1 360px;background:#232732;border-radius:10px;padding:12px 14px}}
.card h3{{margin:0 0 8px;font-size:14px;color:#cdd3df}}
.card .u{{color:#7b8494;font-weight:400;font-size:12px}}
svg{{display:block;background:#1b1e24;border-radius:6px}}
</style></head>
<body>
<header><h1>PerfDog 克隆 · 性能监控报告</h1>
<p>设备：%s ｜ 采样间隔：%.2fs ｜ 样本数：%d ｜ 生成时间：%s</p></header>
%s
<table class="summary"><tbody>%s</tbody></table>
<div class="cards">%s</div>
</body></html>""" % (
        device, interval, len(samples), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        gpu_banner, "".join(rows), "".join(charts),
    )


def write_html(path: str, samples: list, meta: dict | None = None) -> None:
    html = build_html_report(samples, meta)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
