# 识别提示词表来源

这些文件只作为语音识别提示词使用，分别保留各自来源的许可证。

## Cassotis Lexicon

- 来源：<https://github.com/shenmin/cassotis-lexicon>
- 固定提交：`029841eb409521e0a8c8c0321a9e763bcbff7b24`
- 用途：日常短语、游戏术语、计算机术语
- 许可证：CC BY-SA 4.0，完整文本见 `LICENSE-CC-BY-SA-4.0.txt`

## THUOCL

- 来源：<https://github.com/thunlp/THUOCL>
- 固定提交：`a30ce79d895d01ab5132a5c74c29703ff7efb4cc`
- 用途：计算机相关词汇
- 许可证：上游 MIT 许可证及其来源说明，完整文本见 `LICENSE-THUOCL-MIT.txt`

词表由 `scripts/build_lexicon.py` 下载、去重和整理。程序运行时只选取有限的高优先级词条传给识别模型。
