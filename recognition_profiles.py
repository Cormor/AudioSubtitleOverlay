"""Decoder presets independent of model selection and hardware."""

PROFILES = {
    "极速": dict(beam=1, temperature="0", context=False, window=8, interval=0.1,
                description="最小搜索范围，不做备用解码；优先尽快出字。"),
    "流畅": dict(beam=1, temperature="0", context=False, window=8, interval=0.25,
                description="保持较小搜索范围，稍降低更新频率，减少持续计算。"),
    "均衡": dict(beam=3, temperature="0", context=False, window=8, interval=0.25,
                description="增加搜索范围，兼顾识别结果与计算耗时。"),
    "精度优先": dict(beam=5, temperature="0,0.2,0.4", context=False, window=12, interval=0.5,
                    description="更多搜索、备用解码与更长音频窗口；出字可能变慢。"),
    "长上下文": dict(beam=5, temperature="0,0.2,0.4", context=True, window=16, interval=0.75,
                    description="结合窗口内前文，使用更长窗口；耗时更多，出现重复时可关闭参考前文。"),
}


def profile_values(name):
    profile = PROFILES[name]
    return dict(beam=profile["beam"], temperature=profile["temperature"],
                context=profile["context"], window_seconds=profile["window"],
                update_interval=profile["interval"], repetition_penalty=1.0,
                no_repeat_ngram_size=0, no_speech_threshold=0.6, vad_silence_ms=400)
