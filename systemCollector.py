"""Sample CPU, RAM and NVIDIA GPUs; emit UTF-8 JSON Lines.

Run from the project root: python systemCollector.py --once --pretty
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any

import psutil

try:
    import pynvml
except ImportError:
    pynvml = None


class SystemCollector:
    """One sampler per thread; call collect() at the desired interval.

    CPU percentages cover the interval since the previous call. The first call
    reports null CPU percentages because there is no preceding sample yet.
    Use the context manager so NVML is released when the collector stops.
    """

    def __init__(self, *, enable_gpu: bool = True) -> None:
        self._enable_gpu = enable_gpu
        self._nvml_ready = False
        self._closed = False
        self._gpu_error: str | None = None
        self._previous_sample: float | None = None
        self._hostname = socket.gethostname()
        self._logical_cores = psutil.cpu_count(logical=True)
        self._physical_cores = psutil.cpu_count(logical=False)

        # Prime both independent psutil counters; discard their initial values.
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        if enable_gpu:
            if pynvml is None:
                self._gpu_error = "GPU package missing: install nvidia-ml-py."
            else:
                try:
                    pynvml.nvmlInit()
                    self._nvml_ready = True
                except pynvml.NVMLError as exc:
                    self._gpu_error = str(exc)

    def __enter__(self) -> SystemCollector:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release NVML once, including when Ctrl+C stops the CLI."""
        if not self._closed:
            if self._nvml_ready:
                try:
                    pynvml.nvmlShutdown()
                except pynvml.NVMLError:
                    pass
                self._nvml_ready = False
            self._closed = True

    def _collect_gpus(self) -> dict[str, Any]:
        if not self._enable_gpu:
            return {"status": "disabled", "devices": [], "error": None}
        if not self._nvml_ready:
            return {"status": "unavailable", "devices": [], "error": self._gpu_error}

        try:
            count = pynvml.nvmlDeviceGetCount()
        except pynvml.NVMLError as exc:
            return {"status": "unavailable", "devices": [], "error": str(exc)}

        devices = []
        for index in range(count):
            errors: dict[str, str] = {}
            device: dict[str, Any] = {
                "index": index, "uuid": None, "name": None,
                "utilization_percent": None,
                "memory_total_bytes": None, "memory_used_bytes": None,
                "memory_free_bytes": None, "memory_percent": None,
                "temperature_c": None, "power_watts": None,
                "errors": errors,
            }
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            except pynvml.NVMLError as exc:
                errors["device"] = str(exc)
                devices.append(device)
                continue

            def read(field: str, function: Any, *args: Any) -> Any:
                # An unsupported optional metric must not discard CPU/RAM or
                # the other GPU metrics. Unknown values remain null, not zero.
                try:
                    value = function(handle, *args)
                    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
                except pynvml.NVMLError as exc:
                    errors[field] = str(exc)
                    return None

            device["name"] = read("name", pynvml.nvmlDeviceGetName)
            device["uuid"] = read("uuid", pynvml.nvmlDeviceGetUUID)
            memory = read("memory", pynvml.nvmlDeviceGetMemoryInfo)
            if memory is not None:
                device.update(
                    memory_total_bytes=memory.total,
                    memory_used_bytes=memory.used,
                    memory_free_bytes=memory.free,
                    memory_percent=round(memory.used / memory.total * 100, 2) if memory.total else None,
                )
            utilization = read("utilization", pynvml.nvmlDeviceGetUtilizationRates)
            if utilization is not None:
                device["utilization_percent"] = utilization.gpu
            device["temperature_c"] = read(
                "temperature", pynvml.nvmlDeviceGetTemperature, pynvml.NVML_TEMPERATURE_GPU,
            )
            power = read("power", pynvml.nvmlDeviceGetPowerUsage)
            if power is not None:
                device["power_watts"] = round(power / 1000, 3)
            devices.append(device)

        return {
            "status": "partial" if any(device["errors"] for device in devices) else "ok",
            "devices": devices,
            "error": None,
        }

    def collect(self) -> dict[str, Any]:
        """Return a JSON-compatible snapshot without sleeping or writing files."""
        if self._closed:
            raise RuntimeError("Collector has been closed.")
        now = time.monotonic()
        elapsed = None if self._previous_sample is None else round(now - self._previous_sample, 6)
        cpu_percent = psutil.cpu_percent(interval=None)
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        self._previous_sample = now
        memory = psutil.virtual_memory()
        return {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hostname": self._hostname,
            "sample_interval_seconds": elapsed,
            "cpu": {
                "percent": cpu_percent if elapsed is not None else None,
                "per_core_percent": per_core if elapsed is not None else None,
                "logical_cores": self._logical_cores,
                "physical_cores": self._physical_cores,
            },
            "memory": {
                "total_bytes": memory.total,
                "used_bytes": memory.used,
                "available_bytes": memory.available,
                "percent": memory.percent,
            },
            "gpu": self._collect_gpus(),
        }


def positive_interval(value: str) -> float:
    interval = float(value)
    if not math.isfinite(interval) or interval < 0.1:
        raise argparse.ArgumentTypeError("interval must be finite and at least 0.1 seconds")
    return interval


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Sample CPU, RAM and NVIDIA GPUs as JSON.")
    parser.add_argument("--interval", type=positive_interval, default=1.0, help="sampling interval in seconds (default: 1)")
    limits = parser.add_mutually_exclusive_group()
    limits.add_argument("--once", action="store_true", help="print one sample and exit")
    limits.add_argument("--count", type=int, default=0, help="number of samples; 0 means continuous")
    parser.add_argument("--no-gpu", action="store_true", help="disable NVIDIA collection")
    parser.add_argument("--pretty", action="store_true", help="print indented JSON instead of JSON Lines")
    parser.add_argument("--output", type=Path, help="also append samples to a UTF-8 JSON Lines file")
    args = parser.parse_args(argv)
    if args.count < 0:
        parser.error("count cannot be negative")

    output = None
    try:
        if args.output is not None:
            output = args.output.open("a", encoding="utf-8")
        with SystemCollector(enable_gpu=not args.no_gpu) as collector:
            collector.collect()  # Warm up; CLI emits only subsequent CPU samples.
            deadline = time.monotonic() + args.interval
            count = 1 if args.once else args.count
            emitted = 0
            while count == 0 or emitted < count:
                time.sleep(max(0, deadline - time.monotonic()))
                sample = collector.collect()
                line = json.dumps(sample, ensure_ascii=False, allow_nan=False)
                print(json.dumps(sample, ensure_ascii=False, indent=2, allow_nan=False) if args.pretty else line, flush=True)
                if output is not None:
                    output.write(line + "\n")
                    output.flush()
                emitted += 1
                # Skip missed slots instead of producing a burst of samples.
                deadline += args.interval
                now = time.monotonic()
                if deadline <= now:
                    deadline += (math.floor((now - deadline) / args.interval) + 1) * args.interval
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Collector error: {exc}", file=sys.stderr)
        return 1
    finally:
        if output is not None:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
