"""Pinned CPU ONNX models; download only the explicitly listed artifacts."""

ONNX_MODELS = {
    "zipformer-zh-en-small-int8": {
        "repo_id": "pkufool/zipformer-small-streaming",
        "revision": "bd3eaf0a8d400d869fab7b600bc4c626a6bebf56",
        "description": "实验性中英小型 CTC 真流式模型，约 28 MiB 权重；内存较省，当前中文短句样本有漏字。自动模式不判断源语言，翻译时请指定语言。",
        "languages": ("zh", "en"), "streaming": True,
        "license": "Apache-2.0",
        "license_url": "https://huggingface.co/pkufool/zipformer-small-streaming",
        "files": {
            "ctc-chunk-16-left-64.int8.onnx": {"size": 29146304, "sha256": "210c84e337b2288aa09b5318f855ca9bb0e83c9ceb49d7c37bcecbaf252b42ed"},
            "data/tokens.txt": {"size": 114134, "sha256": "5beaddf82e078ef5644dcad130f3c4817a04016bfb0f31c0df28c6b4fa8df3af"},
        },
    },
    "zipformer-zh-en-medium-int8": {
        "repo_id": "pkufool/zipformer-medium-streaming",
        "revision": "3825bc7ff812787d352d35af0e6c6c151e4ce842",
        "description": "实验性中英中型 CTC 真流式模型，约 71 MiB 权重；比 small 使用更多资源，当前中文短句样本仍有错字。自动模式不判断源语言，翻译时请指定语言。",
        "languages": ("zh", "en"), "streaming": True,
        "license": "Apache-2.0",
        "license_url": "https://huggingface.co/pkufool/zipformer-medium-streaming",
        "files": {
            "ctc-chunk-16-left-64.int8.onnx": {"size": 74410051, "sha256": "7cb84949b8d4f40d72df137e2e3529a1aadfc897e5257460f720bb73c99f10f1"},
            "data/tokens.txt": {"size": 86313, "sha256": "e2f16836a4c712a9a01a1656f4610f66093fe283159883ee66b73ebb6e80364b"},
        },
    },
    "sensevoice-int8": {
        "repo_id": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
        "description": "CPU 多语默认方案，约 228 MiB 权重；支持中、粤、英、日、韩及自动检测，按语音片段预览和定稿。",
        "languages": ("zh", "en", "ja", "ko", "yue"),
        "streaming": False,
        "license": "SenseVoiceSmall model license",
        "license_url": "https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE",
        "license_source": "https://huggingface.co/FunAudioLLM/SenseVoiceSmall/blob/main/README.md",
        "files": {
            "model.int8.onnx": {
                "size": 239233841,
                "sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
            },
            "tokens.txt": {
                "size": 315894,
                "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
            },
        },
    },
    "zipformer-zh-en-int8": {
        "repo_id": "csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
        "revision": "98590b7ed6443e77b714204da2757d75e1a642f4",
        "description": "实验性中英 Transducer 真流式识别。当前短句样本有错字，默认优先 SenseVoice；自动模式不判断源语言，翻译时请指定语言。",
        "languages": ("zh", "en"),
        "streaming": True,
        "license": "Apache-2.0",
        "license_url": "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/blob/98590b7ed6443e77b714204da2757d75e1a642f4/README.md",
        "license_source": "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/blob/98590b7ed6443e77b714204da2757d75e1a642f4/README.md",
        "files": {
            "encoder-epoch-99-avg-1.int8.onnx": {
                "size": 181895032,
                "sha256": "8fa764187a261844f859d7143ebaa563af5d10adfece4c18a8f414c88cba2a9b",
            },
            "decoder-epoch-99-avg-1.int8.onnx": {
                "size": 13091040,
                "sha256": "1a70c593d71e53f023f5f55b0b4cfff5055abb786ee3992e5f63dc2e273cc4fa",
            },
            "joiner-epoch-99-avg-1.int8.onnx": {
                "size": 3228404,
                "sha256": "1ed689c5ed19dbaa725d9d191bb4822b5f4855a39e1ffd28cbc1f340d25b2ee0",
            },
            "tokens.txt": {
                "size": 56317,
                "sha256": "a8e0e4ec53810e433789b54a5c0134a7eaa2ffca595a6334d54c00da858841d3",
            },
        },
    },
}
