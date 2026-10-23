import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import systemCollector as module


class FakeNVMLError(Exception):
    pass


def fake_nvml():
    return SimpleNamespace(
        NVMLError=FakeNVMLError,
        NVML_TEMPERATURE_GPU=0,
        nvmlInit=Mock(),
        nvmlShutdown=Mock(),
        nvmlDeviceGetCount=Mock(return_value=2),
        nvmlDeviceGetHandleByIndex=Mock(side_effect=lambda index: index),
        nvmlDeviceGetName=Mock(side_effect=lambda handle: b"Test GPU"),
        nvmlDeviceGetUUID=Mock(side_effect=lambda handle: f"GPU-{handle}"),
        nvmlDeviceGetMemoryInfo=Mock(return_value=SimpleNamespace(total=1000, used=250, free=750)),
        # Controller busy percentage must not be confused with capacity usage.
        nvmlDeviceGetUtilizationRates=Mock(return_value=SimpleNamespace(gpu=70, memory=99)),
        nvmlDeviceGetTemperature=Mock(return_value=55),
        nvmlDeviceGetPowerUsage=Mock(return_value=123456),
    )


class CollectorTests(unittest.TestCase):
    def setUp(self):
        memory = SimpleNamespace(total=16000, used=6000, available=10000, percent=37.5)
        self.cpu_patch = patch.object(module.psutil, "cpu_percent", side_effect=lambda interval=None, percpu=False: [20.0, 40.0] if percpu else 30.0)
        self.memory_patch = patch.object(module.psutil, "virtual_memory", return_value=memory)
        self.count_patch = patch.object(module.psutil, "cpu_count", side_effect=lambda logical=True: 2 if logical else 1)
        self.cpu_patch.start()
        self.memory_patch.start()
        self.count_patch.start()
        self.addCleanup(self.cpu_patch.stop)
        self.addCleanup(self.memory_patch.stop)
        self.addCleanup(self.count_patch.stop)

    def test_cpu_needs_a_sampling_window_and_snapshot_is_json(self):
        with patch.object(module.time, "monotonic", side_effect=[10.0, 11.0]):
            with module.SystemCollector(enable_gpu=False) as collector:
                first = collector.collect()
                second = collector.collect()
        self.assertIsNone(first["cpu"]["percent"])
        self.assertIsNone(first["cpu"]["per_core_percent"])
        self.assertEqual(second["sample_interval_seconds"], 1.0)
        self.assertEqual(second["cpu"]["percent"], 30.0)
        self.assertEqual(second["cpu"]["per_core_percent"], [20.0, 40.0])
        self.assertEqual(second["memory"]["available_bytes"], 10000)
        self.assertTrue(second["timestamp"].endswith("+00:00"))
        self.assertEqual(json.loads(json.dumps(second)), second)

    def test_missing_gpu_package_does_not_stop_cpu_or_memory(self):
        with patch.object(module, "pynvml", None):
            with module.SystemCollector() as collector:
                sample = collector.collect()
        self.assertEqual(sample["gpu"]["status"], "unavailable")
        self.assertIn("nvidia-ml-py", sample["gpu"]["error"])
        self.assertEqual(sample["memory"]["total_bytes"], 16000)

    def test_driver_failure_does_not_stop_memory_collection(self):
        nvml = fake_nvml()
        nvml.nvmlInit.side_effect = FakeNVMLError("driver unavailable")
        with patch.object(module, "pynvml", nvml):
            with module.SystemCollector() as collector:
                sample = collector.collect()
        self.assertEqual(sample["gpu"]["error"], "driver unavailable")
        self.assertEqual(sample["memory"]["used_bytes"], 6000)
        nvml.nvmlShutdown.assert_not_called()

    def test_multiple_gpus_units_and_cleanup(self):
        nvml = fake_nvml()
        with patch.object(module, "pynvml", nvml):
            with module.SystemCollector() as collector:
                sample = collector.collect()
            collector.close()
        self.assertEqual(len(sample["gpu"]["devices"]), 2)
        gpu = sample["gpu"]["devices"][1]
        self.assertEqual(gpu["uuid"], "GPU-1")
        self.assertEqual(gpu["name"], "Test GPU")
        self.assertEqual(gpu["memory_percent"], 25.0)
        self.assertEqual(gpu["utilization_percent"], 70)
        self.assertEqual(gpu["power_watts"], 123.456)
        nvml.nvmlInit.assert_called_once()
        nvml.nvmlShutdown.assert_called_once()
        with self.assertRaises(RuntimeError):
            collector.collect()

    def test_unsupported_metric_is_null_but_other_metrics_survive(self):
        nvml = fake_nvml()
        nvml.nvmlDeviceGetPowerUsage.side_effect = FakeNVMLError("not supported")
        with patch.object(module, "pynvml", nvml):
            with module.SystemCollector() as collector:
                sample = collector.collect()
        self.assertEqual(sample["gpu"]["status"], "partial")
        gpu = sample["gpu"]["devices"][0]
        self.assertIsNone(gpu["power_watts"])
        self.assertEqual(gpu["temperature_c"], 55)
        self.assertEqual(gpu["errors"]["power"], "not supported")

    def test_inaccessible_device_does_not_hide_other_gpus(self):
        nvml = fake_nvml()
        nvml.nvmlDeviceGetHandleByIndex.side_effect = [FakeNVMLError("GPU lost"), 1]
        with patch.object(module, "pynvml", nvml):
            with module.SystemCollector() as collector:
                sample = collector.collect()
        devices = sample["gpu"]["devices"]
        self.assertEqual(sample["gpu"]["status"], "partial")
        self.assertIsNone(devices[0]["memory_used_bytes"])
        self.assertEqual(devices[0]["errors"]["device"], "GPU lost")
        self.assertEqual(devices[1]["memory_used_bytes"], 250)


if __name__ == "__main__":
    unittest.main()
