# 发布与本地安装

## 固定要求

每次发布都更新本机安装版。只生成 ZIP、只更新 `dist_minimal`，或者只看到 CI 通过，都不能宣布本地已更新。

## 本地交付

1. 更新 `VERSION`、README 和发布说明，确认对应源码提交与检查结果。
2. 在 Windows 中运行 `build_windows.bat`。构建不会结束当前正在使用的安装版。
3. 运行 `package_windows.bat`：执行测试、创建主程序和可选 tiny 模型包，核对大小与哈希，再更新当前用户的本地安装版。
4. 安装脚本沿用已发现的安装根目录。新版本先放到独立版本目录并校验；准备成功后将唯一的开始菜单入口更新为“声幕”。旧版本、配置和模型不删除。
5. 检查 `releases/delivery.local.json` 与安装根目录的 `current-installation.json`，确认安装文件校验和快捷方式读回成功。不要提交这两个含有本机路径的记录。

本地目录已有同版本 ZIP 时不会覆盖。未发布的开发候选包可使用：

```powershell
.venv\Scripts\python.exe tools\package_and_install.py --source dist_minimal\AudioSubtitleOverlay --output-dir releases\candidate-日期
```

## GitHub 发布

仓库现有发布工作流在 `main` 收到标题以 `发布 vX.Y.Z` 开头的提交时构建正式包。只在本地检查和安装完成后合入这个发布提交，并核对目标版本。

CI 不在用户的电脑上，因此 CI 的“只打包”模式不等于本机安装。正式 Release 出现后，再同步安装 **GitHub 实际发布的同一份包**：

```powershell
.venv\Scripts\python.exe tools\sync_release_install.py --version v1.3.0
```

此入口验证发布的 `SHA256SUMS.txt`，下载匹配的主程序包，校验全体安装文件并更新同一快捷方式。`releases/vX.Y.Z/installed.local.json` 保存最终安装读回记录。确认 GitHub 包哈希与这条记录相同后，发布才算交付完成。

## 验证范围

- 单元测试用于代码行为；桌面检查使用隔离配置，不开始音频采集。
- 安装脚本默认不启动程序，也不结束旧进程。程序启动与实际识别需要单独核对。
- 延迟对照使用同一段音频、同一个模型；将解码、合成和实际窗口提交分开报告。
