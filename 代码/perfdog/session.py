# -*- coding: utf-8 -*-
"""采样会话：按固定间隔循环采集，汇聚多个采集器结果到 samples。

采集器用线程池**并行**执行：单样本耗时从「串行累加 ~4s」降到「最慢单采集器 ~1.5s」。
"""

from __future__ import annotations

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .transport import AdbTransport
from .gpu import GPUCollector
from .cpu import CPUCollector
from .fps import FPSCollector
from .memory import MemoryCollector
from .thermal import ThermalCollector
from .battery import BatteryCollector
from .report import write_html


def _flatten(sample: dict) -> dict:
    out: dict = {}
    for key, value in sample.items():
        if isinstance(value, dict):
            for sub, sub_val in value.items():
                out[f"{key}_{sub}"] = sub_val
        else:
            out[key] = value
    return out


class PerfSession:
    """采样会话：按固定间隔循环采样，存到 ``self.samples``。"""

    def __init__(self, transport: AdbTransport, collectors: list, interval: float = 1.0) -> None:
        self.transport = transport
        self.collectors = collectors
        self.interval = interval
        self.samples: list[dict] = []

    def _sample_once(self) -> dict:
        sample: dict = {"ts": datetime.now().isoformat()}
        if not self.collectors:
            return sample
        results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=len(self.collectors)) as ex:
            futures = {ex.submit(c.sample): c for c in self.collectors}
            for fut in as_completed(futures):
                c = futures[fut]
                try:
                    data = fut.result()
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    results[id(c)] = data
        for c in self.collectors:
            data = results.get(id(c), {})
            if isinstance(data, dict):
                sample.update(data)
        return sample

    def sample_once(self) -> dict:
        """公开接口：并行采样所有采集器一次（供 GUI 等外部循环驱动）。"""
        return self._sample_once()

    def start(self, duration: float | None = None) -> list[dict]:
        self.samples = []
        if duration is not None:
            self._sample_once()  # 预热：建立 gpubusy / /proc/stat 基线
        start_ts = time.time()
        while True:
            iter_start = time.time()
            sample = self._sample_once()
            self.samples.append(sample)
            if duration is None:
                break
            if time.time() - start_ts >= duration:
                break
            remain = self.interval - (time.time() - iter_start)
            if remain > 0:
                time.sleep(remain)
        return self.samples

    def to_csv(self, path: str) -> None:
        if not self.samples:
            return
        flat = [_flatten(s) for s in self.samples]
        columns: list[str] = []
        for row in flat:
            for k in row:
                if k not in columns:
                    columns.append(k)
        if "ts" in columns:
            columns.remove("ts")
            columns.insert(0, "ts")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for row in flat:
                writer.writerow(row)

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.samples, f, ensure_ascii=False, indent=2)

    def to_html(self, path: str, meta: dict | None = None) -> None:
        write_html(path, self.samples, meta)


def mock_demo(samples: int = 10, interval: float = 0.2, out_dir: str = "/tmp") -> tuple:
    """无设备演示：用 mock transport 跑若干样本，输出 CSV + HTML 到 out_dir。"""
    t = AdbTransport(mock=True)
    collectors = [
        GPUCollector(t), CPUCollector(t), FPSCollector(t),
        MemoryCollector(t), ThermalCollector(t), BatteryCollector(t),
    ]
    session = PerfSession(t, collectors, interval=interval)
    session.start(duration=samples * interval)
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "perfdog_mock.csv")
    html_path = os.path.join(out_dir, "perfdog_mock.html")
    session.to_csv(csv_path)
    meta = {
        "device": "Mock（模拟 小米5s / Adreno 530 / Android 8）",
        "interval": interval, "samples": len(session.samples),
    }
    write_html(html_path, session.samples, meta)
    return session, csv_path, html_path
