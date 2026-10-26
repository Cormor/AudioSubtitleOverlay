# 第三方组件说明 | Third-party notices

本文件只说明仓库或发布包中使用的第三方组件。第三方组件不受
`LICENSE` 中 AudioSubtitleOverlay 自有代码许可证的重新授权，使用和分发
时必须同时遵守各自的许可证、版权声明和发布条款。

## 已固定或直接使用的组件

- `faster-whisper`：项目地址为
  <https://github.com/SYSTRAN/faster-whisper>；其源码仓库包含自己的 MIT
  许可证文本。
- `Systran/faster-whisper-tiny`：`assets/tiny` 中的固定提交文件来自
  <https://huggingface.co/Systran/faster-whisper-tiny>，当前固定提交为
  `d90ca5fe260221311c53c58e660288d3deb8d356`。模型仓库的许可证和模型卡
  以该来源仓库为准。
- `numpy`、`soundcard`、`requests`、`pyinstaller`、`Pillow`、`opencc` 和
  `sentencepiece`、`pystray` 和 `six`：版本范围位于 requirements 文件；每个包的许可证以对应
  发布版本中携带的 LICENSE、NOTICE 或元数据为准。
- 本地翻译模型：按语言对从
  <https://github.com/argosopentech/argospm-index> 按需下载；模型包中的
  说明和许可证适用于对应模型。
- NVIDIA CUDA 12 用户态运行库：由组件清单按固定版本和 SHA256 下载；其
  许可证、版权声明和再分发条件以对应 NVIDIA/PyPI 发布包为准。
## 发布时的保留要求

- `sherpa-onnx` 与 `sherpa-onnx-core`：使用 2.0.0 Windows 运行时，代码许可为 Apache-2.0，来源 <https://github.com/k2-fsa/sherpa-onnx>；模型许可单独判断。
- SenseVoiceSmall INT8：按需从 sherpa 维护者转换仓库取得指定文件；上游模型采用自定义模型许可，见 <https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE> 与 <https://huggingface.co/FunAudioLLM/SenseVoiceSmall>。不能将其代码的 MIT 许可当作模型许可。
- Zipformer CTC small / medium：来源 <https://huggingface.co/pkufool/zipformer-small-streaming> 与 <https://huggingface.co/pkufool/zipformer-medium-streaming>，模型卡声明 Apache-2.0。
- 中英 Zipformer Transducer：来源 <https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20>，模型卡声明 Apache-2.0。
- Whisper Turbo、英语专用 tiny.en / base.en 与 distil-large-v3：分别从模型管理列表中的公开转换仓库按需准备；Whisper 上游和 Distil-Whisper 使用 MIT 许可，各转换产物仍以其模型卡为准。ONNX 模型的确切 revision、文件清单与 SHA256 位于 `engine_models.py`，模型不内置进主程序。

重新打包或发布包含第三方组件的版本时，不得删除第三方许可证和版权声明。
如果某个发布包没有携带对应的上游许可证文本，应在发布包中补充，或在发布
说明中提供稳定的上游许可证链接。

AudioSubtitleOverlay 的自有代码、第三方库、语音模型和 GPU 运行库属于
不同的版权和许可证范围；本文件不把它们合并为同一个许可证。
