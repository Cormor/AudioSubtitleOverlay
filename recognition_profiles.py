"""Explicit starting points, not a promise of accuracy or latency on every device."""

PROFILES = {
    "极速 · tiny": dict(model="tiny", beam=1, temperature="0", window=8, interval=0.1,
                       description="优先尽快出字；适合清晰语音、低算力设备。复杂口音和噪声下更容易认错。"),
    "流畅 · base": dict(model="base", beam=1, temperature="0", window=8, interval=0.1,
                       description="比 tiny 使用更大的模型，仍采用最小搜索范围；适合日常对话。"),
    "均衡 · small": dict(model="small", beam=3, temperature="0", window=8, interval=0.25,
                        description="兼顾识别质量与耗时；可作为多数场景的起点。"),
    "精度优先 · medium": dict(model="medium", beam=5, temperature="0,0.2,0.4", window=12, interval=0.5,
                             description="更大模型、更多搜索和备用解码；更吃显存，出字可能明显变慢。"),
    "大型精度 · large-v3": dict(model="large-v3", beam=5, temperature="0,0.2,0.4", window=16, interval=0.75,
                              description="高算力设备可尝试；显存和耗时需求最高，不适合追求最低延迟。"),
}


def profile_values(name):
    profile = PROFILES[name]
    return dict(model=profile["model"], beam=profile["beam"], temperature=profile["temperature"],
                context=False, window_seconds=profile["window"], update_interval=profile["interval"],
                repetition_penalty=1.0, no_repeat_ngram_size=0,
                no_speech_threshold=0.6, vad_silence_ms=400)
