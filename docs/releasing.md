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
.venv\Scripts\python.exe tools\sync_release_install.py --version v1.4.1
```

此入口验证发布的 `SHA256SUMS.txt`，下载匹配的主程序包，校验全体安装文件并更新同一快捷方式。`releases/vX.Y.Z/installed.local.json` 保存最终安装读回记录。确认 GitHub 包哈希与这条记录相同后，发布才算交付完成。

## 验证范围

- 单元测试用于代码行为；桌面检查使用隔离配置，不开始音频采集。
- 安装脚本默认不启动程序，也不结束旧进程。程序启动与实际识别需要单独核对。
- 延迟对照使用同一段音频、同一个模型；将解码、合成和实际窗口提交分开报告。

## 只更新本机的未提交构建

需要安装已验证但尚未提交的修订时，使用独立版本号，正常构建后执行：

```powershell
.venv\Scripts\python.exe -X utf8 tools\package_and_install.py --local-build --output-dir releases\local-版本号
```

此模式保留包内 `source_state=modified`，记录基础提交及源码文件哈希，并核对当前源码与构建相同。它仍会运行测试、校验并安装；CI 不允许使用此模式。正式发布仍要求已提交且干净的源码，不能将本地构建冒充正式包。


## 发布前文档与公开内容

先维护README.md与README.en.md的功能、当前主程序文件名、首次使用和FAQ；中英文技术细节分别链接docs/technical-guide.md和docs/technical-guide.en.md。图像使用可公开的实际界面，注明示例状态；不提交私人录音、真实识别全文、用户路径或本机安装回执。包内使用说明与首页同步更新。

正式发布从已提交且干净的源码构建；保留源码提交、版本与源文件指纹。可以在独立worktree完成发布而保留开发工作区的未提交改动。Windows无法读取WSL工作树的Git元数据时，由实际WSL仓库核对干净提交后传SOURCE_COMMIT和SOURCE_STATE，不能伪造clean。

发布工作流先创建草稿、上传主程序/tiny模型/校验表，读回准确提交与三项资产后再公开。公开后下载发布的同一份主程序并按SHA256校验、安装、更新唯一入口，完成实际启动和字幕验证。
