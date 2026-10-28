# 项目交付规则

## 发布与本机安装

- 每次发布新包，都必须同时更新本机安装版；不能只在 `dist_*` 或 `releases` 中生成新文件。
- 沿用已有安装根目录和应用身份，保留用户配置、模型、缓存及可恢复的旧版本。开始菜单只保留一个当前版本入口。
- 发布前先完成测试与 Windows 构建；使用同一份包安装到本机，回读版本、主程序哈希、依赖文件与快捷方式目标。安装失败时不发布正式 Release。
- 发布产物必须与对应源码提交、VERSION、安装版本和 SHA-256 一致。更新 README 和发布说明时只写实际存在且完成验证的功能。
- 实施、合入、安装和发布的授权以当前用户任务为准；用户已在同一任务明确授权时不要重复询问。

## 诊断与运行

- 不提交本机配置、识别文本、音频、诊断日志、私人路径或凭据。
- 实时性能分别报告输入终点、窗口末词、推理结束和实际绘制时间；编译成功不代表延迟或桌面交互已经验证。

## 运行日志保留

- `logs/app.log` 及其轮转文件由 `log_retention.py` 管理，遵循全局 30 天/30 MiB 上限和最近 15 天/至少 10 MiB 保留规则；单文件轮转上限 10 MiB，启动时和运行期间每日检查。
- GitHub正式v1.4.1提交为696dc4a9d50b57eee047e403fe7c57a18be73c84，保存在codex/release-1.4.1；2.1.0草稿翻译源码检查点已上传，当前源码维护2.2.0，实际安装版本以Apps索引为准，维护分支codex/shengmu-2-refactor；1.4修复已保留。安装位置以Apps索引为准；原开发工作树已有修改继续保留。

## 本机运行布局与后续维护

- 本机统一入口：`../Apps/AudioSubtitleOverlay`；实际安装版本与位置以 `../Apps/catalog.json` 和当前交付回执为准。
- 开发源码：`../audio_subtitle_overlay_followup`；本目录与其他工作树均保留已有分支/修改。构建与维护：build_windows.bat；本地改动版使用现有 tools/package_and_install.py 的开发构建参数，正式包按 clean 源码规则处理。
- 开始菜单沿用声幕.lnk和既有安装身份；Apps联接与实际生效入口按Apps索引和安装回执核对；不要把 main 的旧版本覆盖到当前开发版。模型归档统一保留当前正式发布的tiny模型包（见Apps索引）；旧校验/交付记录是历史记录。安装器当前仍生成版本目录，下次升级后必须按本机规则清理旧安装。
- 每次本地构建交付后更新唯一运行入口；历史最多留最近3实际版本压缩包及必要元数据，删除重复解压副本与构建中间产物，保留配置、源码、依赖及诊断/恢复资料。联接不要递归跟随清理，完整说明见 `../Apps/README.md`。

## 1.4 构建与识别维护

- 模型与识别能力由 `engine_models.py`、`asr_backends.py`、`recognition_sessions.py` 及 `model_manager.py` 维护；SenseVoice分段、Zipformer原生流式、Whisper词时间三条路径分别验收。译文配置变化不能丢掉文本后端已经消费的音频；停止通知在模型释放后发送。
- 实际开发Windows Python见 Apps/catalog.json 的 build_python；沿用现有共享环境，不为工作树复制另一套大依赖。`requirements-windows.txt` 已加入 sherpa-onnx 2.0.0。
- AI构建先设置 `ASR_TASK_ROOT` 为E盘当前对话目录；build_windows.bat支持将dist/work/metadata/pip缓存及TMP/TEMP写到其scratch。Windows Git不能读取WSL工作树时，传入由实际WSL仓库核对的SOURCE_COMMIT/SOURCE_STATE，不能用unknown或假clean绕过交付检查。
- 本机打包使用 `tools/package_and_install.py --local-build --skip-model-package --source <对话目录>/scratch/dist/AudioSubtitleOverlay --output-dir <对话目录>/deliverables/release`，复用已有模型。正式发布保留完整资产要求；不以此本地参数发布CI包。
- 1.4研究、需求/设计/验证及当前归档落点见Apps索引；历史归档与清理结果以Apps索引为准，最多保留当前及前两实际版本压缩包；旧安装内容核对归档及引用后再清理，配置与模型保留原位置。

## 设置页与模型选择维护

- Tk的FocusIn也发送给父容器；滚动可见性只处理实际焦点控件，延后执行时重新检查焦点和标签页是否可见。验证覆盖每个标签页、缩小窗口、鼠标选择与键盘移到屏幕外控件。
- 模型设备标识与筛选使用ModelDefinition.supported_devices；运行设备显示与保存的Whisper设备偏好分开，CPU模型显示CPU，切回Whisper恢复原偏好。相关回归见tests/test_ui_guidance.py。

## 2.0 维护入口

- 架构和验证工具见 `docs/refactor-2.0.md`。界面构建在application_views.py，会话在application_controller.py，runtime_workers.py负责识别与翻译spawn进程；禁止把关闭的worker继续当成有效服务。
- 翻译线程独占翻译服务的创建/替换；停止先取消并等待生产者退出，再读取并关闭最终服务，避免清理时失去新worker归属。待翻译段最多64，同段仅保留最新修订；终止故障清空服务并在下一段恢复。
- ASR终止故障每会话自动重建一次；再次故障明确停止，不能无限重启或持续消费音频却没有字幕。源码测试和轻工厂spawn不能代替冻结包真实模型验收。
- ASR语言代码和翻译模型代码分别维护。粤语yue身份保留，中文文本模型适配与繁简规范化在translation.py；这不是粤语专门训练模型，口语质量另行评估。
- 物理滚动保留位置/速度；关闭动态时即便目标未变也必须立即snap。系统减弱动态偏好优先。配置损坏实例不覆盖原文件，正常保存采用同目录原子替换。
- 本机当前安装位置、包与运行验收状态以Apps索引和本聊天交付记录为准；旧1.4及2.0工程文档保留其版本事实。2.0历史验收包括302项测试、20轮启停、冻结包恢复及3605秒安装观察，当时沿Beta/SenseVoice配置；本轮运行验收另记，不挪用旧包结论。后续计划见docs/next-iterations.md。

- 语言模型清单与模型文件下载统一复用download_manager.urlopen的代理/NO_PROXY处理；不能只验证模型文件下载而漏掉首次准备清单。

- Windows跨进程通知使用原生manual-reset Event；不能把multiprocessing.Event的wait超时当成强杀后仍有界。每进程本地锁避免wait期间CloseHandle；无限等待也按有限片段释放本地锁。tests/test_runtime_workers.py保留旧Condition持锁死亡的反证与新实现句柄/并发测试。冻结包另跑tools/verify_installed_recovery.py验证翻译恢复、一次识别恢复、重复故障停止及正常退出。

## 2.0.1 组件设计与真实走查

- 组件契约、依据、适用范围和深浅参考图见 `docs/design-system.md`；项目令牌为 `design_tokens.json`，`theme.py` 实现控件样式。Tk字体pt、Tk逻辑像素与Windows显示缩放分别记录，不二次乘系统比例。
- 所有受影响控件、子窗口和下拉层检查默认、悬停、按下、焦点、禁用与选中；“推荐”表达建议，不当成当前选中或实际运行。参数预设选中显示勾，当前页有底部指示和独立焦点环。
- Windows走查使用 `python tools/verify_design.py --output <E盘目录> --text-scale 1`，文字放大另跑1.5和2；脚本用隔离配置、真实鼠标键盘且不采音。主窗五页和模型管理在最终安装包复核，截图不代替真实操作。
- 模型管理表格长字段仅在表内横滚；整个管理页支持窄/短窗口滚动与键盘显露末尾控件。查询元数据只保留一个在途请求，完成后显示结果，迟到信息不覆盖下载提示；关闭窗口不再回写。
- UI维护后的运行切换按改动范围选安装观察时长。`tools/activate_installed_runtime.py --minimum-seconds 70` 可核对本次短回归，默认仍为3600秒；必须匹配同一安装EXE，不能把旧包观察结果当新包通过。识别/翻译服务、并发或生命周期改变时按影响重做相应恢复/长测。

- Beta自动音源验收须与正式声幕采集串行，单独APPDATA不能隔离Windows音频会话。先安全结束自有正式实例，再跑隔离回归，结束后按最新保存配置恢复正式版；不让两个声幕采集互选。verify_installed_runtime的--keep-source-selection会检查已有实例。

## 2.0.2 逐条组件走查

- 几何、状态和主题维护先按公司 `../SoftwareCompany/templates/component-review.md` 写来源、项目值、部件/热区、状态和判退条件，再按同一ID核对原型与最终安装包。声幕本轮取值与复验见 `docs/control-review-2.0.2.md`；数值读回与独立看图分开记录。
- `python tools/verify_control_geometry.py --output <E盘目录> --text-scale 1` 检查四Scale、18个共用输入实例、鼠标/键盘焦点、极值和底部操作；文字200%另跑2。最终包再用真实指针复核，缺状态不可按文件名或单测判通过。
- 自动音源只使用身份可核对的应用会话，查询失败跳过并记调试原因；保留前台优先、相近音量稳定性，明显更强且达到检测阈值的候选可接管底噪。`tests/test_process_audio_priority.py`覆盖身份与选择行为；真实Beta音源回归仍须与正式实例串行。

## 2.0.3完整页面与组合状态

- 受影响页面/展开及组合状态先生成可编辑SVG与PNG并审查图稿，再对照实际窗口/最终安装包；契约及本轮流程缺口见docs/ui-design-2.0.3.md。选项卡选中3px底线，只有键盘模式且页签有焦点时原位加厚5px，不叠全轮廓。
- 只读选择器使用choice_popup.py；动态postcommand、当前值和单次选中事件保持，Toplevel焦点不显露背景表单。首次跨窗可只有Enter，悬停同时处理Enter/Motion。关闭只释放自己拥有的grab和绑定，原生focus/grab查询异常也清理。
- `python tools/verify_choice_ui.py --output <E盘目录> --text-scale 1` 检查全页选择器与长值，200%另跑2；`python tools/verify_tab_states.py --output <E盘目录>` 检查鼠标、方向键、Tab进正文和窗口重新激活。两者隔离配置、不采音；图像仍须专业复核，最终包真实字幕/翻译验收另记。临时工具控制会话用完及时释放，正式应用保留。


## 2.1 草稿翻译维护

- SourceUpdate.session_id独立于翻译配置translation_epoch；TranslationContext、SourceUpdate、TranslationUpdate贯穿后台和UI，UI消费时仍检查身份。on_reset先于新context，不能借旧回调恢复已失效的译文。
- 字幕追加关系统一调用subtitle_events.source_text_extends。SenseVoice草稿通常自带句末标点；只允许非final临时句末标点随正文增长，内部改写及ASCII词内延长仍失效。测试须包含真实基线的带句号增长、I can到I cannot、问号/闭引号和慢MT持续进展。
- 每段只排最新待译快照；定稿优先但不得持续推迟到期草稿。翻译设置刷新先处理当前最新段；同配置noop显式刷新可复用内容，但结果使用新epoch身份。配置/会话变化取消旧RPC并清空关闭服务归属，普通partial不取消慢模型。
- tools/verify_streaming_translation.py复用有界固定WAV回放或读取安装版trace，连接MT结果与实际非占位translation_frame片段；正常、失败、改写、final、配置切换、停止和有界性共同验收。受控单测不替代同包真实模型效率及可见译文。
- 新增诊断字段时检查RuntimeTrace默认脱敏；source_text和text仅在显式--trace-text时保留。普通运行不开启正文或录音证据。

2.1安装记录：2026-10-28已由既有安装器安装2.1.0，沿用AudioSubtitleOverlay身份与声幕.lnk；真实运行验收与最终入口回读按本轮安装回执更新。正式配置恢复使用当时最新保存值，不能套用旧记录中的Beta、模型或语言组合。


## 2.2 控件与鼠标穿透维护

- 功能与输入范围见docs/controls-input-2.2.0.md；本轮源码版本2.2.0。已上传2.1检查点与已发布1.4.1仍为各自历史事实，2.2安装/包与实际用途结果只按该版本交付回执回填。该文档同时维护呈现版本、原生/Tk消息和默认DPI/真实焦点链的QA约定；相关工具或诊断字段变化时同步核对。
- recognition_parameters.py是数值类型、编辑允许域和已提交编解码的共同来源；推荐列表不作白名单。UI原始draft与committed分开，Enter/真正失焦/推荐选择才提交，自己的弹层不先提交草稿；Esc取消与销毁丢弃遵同一合同。未改动的合法已保存值不重套编辑字面量限长。验证必须沿Settings save/load到实际decoder入参，不只测validator。
- 只读选择器闭合框仍为ttk.Combobox，choice_popup只替换弹层；whole-widget hover与元素active不同。实际指针逐一经过文字、空白、边和箭头，核对state/identify/像素、真正离开及focus/open/disabled组合。错误内边框与焦点外环并存。
- overlay_mouse_passthrough保存偏好与原生effective分开；callback在Tk主线程统一消费，unknown用alternate并保旧偏好。保存True应用前先连接当前owner回调和主窗/托盘外部恢复，无托盘保留任务栏。原生失败按实际bool回读、None明确未知且False恢复始终可达；退出/销毁/旧owner/迟到托盘动作不得重建窗口或保存。
- GUI和真实输入保持唯一操作者；仅向核准PID/HWND/创建时间且实际命中的自有窗口发有界输入。移动指针前也核五鼠标键和foreground/current GUI capture/menu/movesize；外部capture或身份变化即停止，保留准备失败，不改第三方窗口/游戏或系统前台锁。源码无窗口、原生输入与最终冻结包证据分别记录。
- 复用物理滚动时核对隐藏/销毁停调度、关动态即刻snap且清速度、减少动态优先，以及当前速度的中途反向。设计职责、实际轨迹与原生时序按受影响范围交接，静态控件不加无收益动画。
