# 声幕 2.0 维护说明

本轮目标是把声音输入、持续字幕、翻译、阅读和故障恢复连成完整可用的桌面产品。维护源码沿用当前工作树，保留1.4修订，重构分支为 `codex/shengmu-2-refactor`；本地构建按真实modified状态记录，不作为远端正式发布。

## 产品与依赖

Windows桌面应用，继续使用Tk/ttk、原有Windows分层浮窗、soundcard/WASAPI、sherpa-onnx和faster-whisper。主窗采用公司黑金/白金语义令牌，支持系统/深色/浅色主题；字幕浮窗个人样式和现有配置保留。现有三类识别、音源、翻译、64段近期历史、TXT导出和托盘继续支持。模型不是UI启动的前置条件。

新界面按任务划分为实时字幕、字幕外观、模型与资源、高级设置、帮助与诊断。主页面承担音源/语言/翻译与最近结果；模型筛选、运行设备和自定义模型设置集中到模型页。开始和停止常驻，关闭设置后字幕继续，托盘失效保留任务栏入口。

## 模块边界

- `application_views.py`：五页控件和布局，向应用发送操作，不加载模型。
- `application_controller.py`：拥有会话和状态，统一开始/停止/配置命令；投递到UI后再次检查会话和停止状态，拒绝迟到回写。
- `pipeline.py`：音频调度、三类识别路径、配置版本、字幕和翻译调度；保持Whisper词时间合并与两类文本会话差异。
- `runtime_workers.py`：识别和翻译各一个spawn进程，串行有界RPC、状态回传、共享音频和可取消等待。5秒协作退出后最多2秒强制回收。原生解码或网络等待不在UI线程执行。Windows通知使用内核manual-reset Event，spawn传递随机Local名称，每进程独立句柄；仅进程内锁保护等待与关闭，有限分片等待使关闭始终可进入。不能用跨进程Condition作为强制终止后仍需访问的通知。
- `display_module.py`、`subtitle_events.py`：版本化字幕与不可变显示快照，段落更新与译文配对。
- `theme.py`、`design_tokens.json`：原生主题适配与公司令牌的随包快照；读取Windows减弱动态偏好。
- `settings.py`：1.x迁移、类型校验与原子保存。损坏加载实例拒绝覆盖原文件，提示通过 `Settings.last_load_warning` 提供。
- `diagnostics.py`：只包含明确字段的本地JSON诊断导出，不含录音、字幕正文、音源名称、模型路径和完整日志。

`main.py`继续承载Tk应用装配及薄的界面命令，不持有模型实例。新入口必须在创建Tk前执行 `multiprocessing.freeze_support()`；PyInstaller显式收录惰性服务和公司令牌。进程取消或退出必须经管理器确认，不把“已请求停止”当作资源已释放。

## 状态与有界资源

音频最多15秒，缺口重置上下文；翻译最多64个待处理段，同段合并到最新版本，超量发出拥塞状态。翻译后端切换和退出时关闭旧进程；翻译错误保留原文并在翻译区显示。停止后旧回调不再改变字幕。

原文草稿和译文在同一段原位修订。译文携带其实际对应的原文快照、识别会话、段落修订和翻译配置代次，显示端在实际消费回调时再次检查归属。调整翻译不重置识别音源；当前段优先按新配置刷新，历史仍有界。ASR保留粤语yue身份，翻译适配层把其书写文本交给已有中文模型；这是可用性适配，不表示该模型经过粤语专门训练，界面提示口语译文的限制。窗口回看暂停追赶，保留“回到最新字幕”操作。

历史回看的空间动画使用阻尼弹簧，默认质量1、刚度400、阻尼比1，继承当前位置和速度；旧duration参数只保留接口兼容。减弱动态或关闭动画立即到达目标，静止后不继续调度动画。

## 开发与验收

使用Apps索引记录的现有Windows Python环境，不复制依赖。测试入口：

```text
python -m unittest discover -s tests
python tools/check_desktop_ui.py --output E:\AI\<本聊天>\evidence\desktop
python tools/verify_refactor_runtime.py --output E:\AI\<本聊天>\evidence\live --seconds 70 --cycles 20
```

桌面检查使用独立配置，不开启音频采集；现场运行检查使用独立APPDATA、现有模型缓存与当前默认播放设备，不操控浏览器和系统设置。运行检查的音频/全文和截图仅属本地私有验收证据，不得发布到仓库。`--trace-audio`只用于显式有限录音的同素材对比。

主包沿用项目构建和安装工具，构建临时目录指定到E盘工作区。源码及令牌内容记录到BUILD_INFO，打包前核对；安装到原应用身份、更新原快捷方式及Apps入口，重新验证安装版字幕和翻译。最终版本、位置、验证证据和后续迭代计划以本轮交付记录及Apps索引为准。


安装版长期观察及恢复检查：

```text
python tools/verify_installed_runtime.py --exe <当前安装EXE> --output E:\AI\<本聊天>\evidence\installed-soak --seconds 3600
python tools/verify_installed_recovery.py --exe <当前安装EXE> --output E:\AI\<本聊天>\evidence\installed-recovery
python tools/summarize_installed_acceptance.py E:\AI\<本聊天>\evidence\installed-soak --output E:\AI\<本聊天>\evidence\installed-summary.json
```

恢复检查只终止自己启动的独立实例的工作进程，并根据父子归属与加载的识别/翻译DLL同时确认身份；无法唯一确认时停止注入。返回译文与其后实际绘制片段需对应，不能只用译文区域的重绘计数证明译文完成。所有验收输出仍只留本地。

## 2.0.1设计修订

组件契约、官方依据与实际参考图见[桌面设计系统](design-system.md)。主窗和模型管理已统一圆角、焦点、禁用、选择及深浅主题；模型管理支持短窗滚动，元数据查询反馈有结束状态。运行验证以本次安装包回执及设计走查交付记录为准，2.0的一小时记录保留其原始版本与范围。


## 2.1 草稿翻译与修订配对

SenseVoice分段、Zipformer流式识别与Whisper滚动窗口都可在原文定稿前提交翻译。当前Argos和MyMemory仍按完整片段快照处理；原文持续增长时按节奏重译，同一段只排最新待译快照。这是持续修订片段译文，不是翻译模型原生逐token输出。

最少4个非空白字符的草稿可翻译，短内容在定稿时立即翻译。本地草稿按至少500ms的调度间隔合并；可能联网的模式至少间隔1500ms，在线适配层原有限流继续生效。这里是提交节奏，实际返回时间取决于识别、模型和网络。定稿优先且不受草稿节流；连续定稿请求仍给到期草稿机会。每段仅保留最新等待项，最多64段；缓存最多64项且源文与译文合计最多65536字符。

慢翻译完成时，可显示仍属于当前源文祖先的草稿快照，并标记继续更新；原文改写、源语变化或会话变化会使不适用的结果失效。共享判定在subtitle_events.source_text_extends：只为非定稿快照处理临时句末标点及其闭引号；内部文字和标点仍精确保留，ASCII词内延长按改写处理，避免把I can的旧译文挂到I cannot。最终译文必须与当前定稿快照精确对应；相同输入可复用已完成译文并发出新的定稿身份。Whisper只有实际落段才发final，文字不变的落段同样触发最终刷新。

SourceUpdate.session_id表示识别上下文；translation_epoch表示翻译配置代次。TranslationContext、SourceUpdate与TranslationUpdate按同一发布锁排序，锁不跨模型RPC。UI临时关闭/再开启后选项被合并为原配置时，通过refresh_translation_context()刷新代次并重投当前源文；有界缓存可复用内容，旧代次回调继续失效。切换翻译配置或重置识别会话时，正在等待的旧RPC取消，由翻译执行器关闭旧服务并为后续请求重建。普通草稿更新不反复取消慢模型。

显式诊断增加translation_call、translation_call_finished和translation_cached，用于统计调用次数、输入字符数、排队与实际调用耗时；这些事件不包含正文。开启--trace-text后，translation/translation_not_applied记录完整结果身份，translation_frame记录实际提交到Windows合成器的非占位译文片段。默认诊断过滤text、source_text和word_spans；音频和正文保留各自的显式开关。

有界固定WAV验收复用现有benchmark_replay.py，以隔离配置运行真实ASR、MT与字幕浮窗，不播放到扬声器。新工具核对同一段至少两次不同草稿译文实际提交早于ASR定稿、最终译文与当前源文精确配对，并统计调用成本；占位文字不计输出。示例：

```text
python tools/verify_streaming_translation.py --source . --wav E:\AI\<本聊天>\input.wav --audio-seconds 18 --output E:\AI\<本聊天>\evidence\streaming-replay
python tools/verify_streaming_translation.py --exe <当前安装EXE> --trace <trace下的run目录> --run-record <安装验收的process.json> --output E:\AI\<本聊天>\evidence\streaming-installed-analysis
```

回放只运行一轮，音频时长限制2至60秒，子进程与输入/源码身份写入本地证据。分析要求显式正文诊断且记录完整；缺少最终对应关系、只有翻译中占位或旧代次片段均判未通过。提交到Windows合成器不等于显示扫描或声学端到端延迟。具体安装路径、同包真实模型及可见验收结果以Apps索引和本次安装回执为准。

显式trace.json同时记录真实process_id、sys.executable路径及其SHA-256。固定回放provenance记录实际producer和父PID、录制时源码指纹与trace根；安装观察process.json记录启动EXE指纹、已核对的子PID和结束时指纹保持。分析已有trace时必须提供该轮run-record，不能用分析时的文件身份代替录制来源；共享Windows venv的launcher PID与实际Python producer PID可能不同。
