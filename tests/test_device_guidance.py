import unittest

from ui_guidance import (
    DEVICE_MODE_LABELS,
    device_mode_guidance,
    device_mode_label,
    device_mode_value,
)


class DeviceGuidanceTests(unittest.TestCase):
    def test_storage_keys_keep_friendly_labels(self):
        self.assertEqual(
            DEVICE_MODE_LABELS,
            {
                "自动（优先GPU）": "自动选择（推荐）",
                "CPU（int8）": "CPU（通用兼容）",
                "NVIDIA GPU（float16）": "NVIDIA GPU（标准精度）",
                "NVIDIA GPU（int8_float16）": "NVIDIA GPU（节省显存）",
            },
        )

    def test_labels_and_legacy_values_round_trip(self):
        for value, label in DEVICE_MODE_LABELS.items():
            with self.subTest(value=value):
                self.assertEqual(device_mode_label(value), label)
                self.assertEqual(device_mode_value(label), value)
                self.assertEqual(device_mode_value(value), value)

    def test_unknown_values_are_preserved(self):
        unknown = "未来设备模式"
        self.assertEqual(device_mode_label(unknown), unknown)
        self.assertEqual(device_mode_value(unknown), unknown)
        self.assertIn(unknown, device_mode_guidance(unknown))

    def test_auto_explains_gpu_priority_and_cpu_fallback(self):
        text = device_mode_guidance("自动（优先GPU）")
        self.assertIn("优先使用 GPU", text)
        self.assertIn("否则使用 CPU", text)
        self.assertIn("NVIDIA CUDA", text)

    def test_gpu_modes_explain_tradeoffs_without_claiming_guarantees(self):
        standard = device_mode_guidance("NVIDIA GPU（float16）")
        compact = device_mode_guidance("NVIDIA GPU（int8_float16）")
        self.assertIn("显存充足", standard)
        self.assertIn("compute type: float16", standard)
        self.assertIn("大模型或显存不足", compact)
        self.assertIn("需用本机设备与声音素材验证", compact)
        self.assertIn("不保证更快或无损", compact)
        self.assertIn("compute type: int8_float16", compact)

    def test_cpu_only_model_guidance_overrides_device_mode(self):
        text = device_mode_guidance("NVIDIA GPU（float16）", cpu_only=True)
        self.assertIn("当前模型只使用 CPU", text)
        self.assertIn("仅适用于支持 GPU 的模型", text)
        self.assertNotIn("标准精度", text)


if __name__ == "__main__":
    unittest.main()
