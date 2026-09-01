# 量心 Perform

LLM-based Performance Assessment for Psychological Support Practice

[![在 GitHub Codespaces 中体验](https://github.com/codespaces/badge.svg)](https://codespaces.new/LeiLeiZi2002/liangxin-perform?quickstart=1)

欢迎体验面向心理支持工作情境的交互式测评 DEMO。你可以与虚拟来访者自由对话，通过语音或文字完成一次会谈，再提交专业工作记录，回看分析所依据的原始材料。

项目关注受测者在真实交流过程中的表现，例如如何建立关系、理解问题、识别风险、寻找现实支持、商量下一步行动并妥善结束会谈。它不使用固定选项代替交流，也不播放预制的来访者台词。

## 可以体验什么

- 与虚拟来访者自由对话，受测者可以按自己的思路提问和回应。
- 在心理热线场景中使用麦克风说话，查看实时转写并听取自然语音回应。
- 在文字场景中完成连续的在线交流。
- 会谈结束后填写与场域对应的专业工作记录，并引用会谈原话作为判断依据。
- 查看由大模型辅助生成、可以回到原始证据核对的能力分析报告。

## 轻量角色链

正式测评采用轻量角色链。每个正常话轮都由来访者模型根据人物档案、当前处境、行为规则、完整会谈原文和受测者本轮发言实时生成。模型需要站在人物当时的处境里思考，再组织符合身份、媒介和谈话进展的口语回应。

案例中的现实事件由程序维护，模型不能自行宣布尚未发生的结果。语音识别和语音合成分别处理，实时对话中不再串接额外的语义分析模型。这样既能保留必要的情境约束，也能减少等待，让表达有更多变化。

在语音场景中，浏览器会持续接收麦克风音频并形成转写。停顿和思考不会自动提交话轮；受测者说完后点击 `我说完了`，系统才会生成下一次回应。已经提交的会谈原文会完整保留，不会在中途被摘要替换。

## 在 GitHub Codespaces 中体验

点击页面顶部的 Codespaces 按钮，登录 GitHub 后创建一个临时运行环境。依赖会自动安装，前后端启动后浏览器会打开体验页面。进入语音场景时，请允许浏览器使用麦克风。

模型服务需要体验者自己的阿里云百炼 API Key。Key 只保存在当前后端进程的内存中，不会写入浏览器存储、数据库或仓库。Codespaces 的个人配额和模型调用费用由体验者承担；使用结束后可以停止或删除自己的 Codespace。

GitHub Pages 只能托管静态网页，无法运行本项目所需的后端、WebSocket 和本地数据库，因此本仓库使用 Codespaces 提供在线体验环境。

## 配置阿里云百炼 API Key

先在[阿里云百炼密钥管理页](https://bailian.console.aliyun.com/?apiKey=1)创建或复制 API Key。具体步骤可查看[阿里云官方说明](https://help.aliyun.com/zh/model-studio/get-api-key)。默认业务空间通常可以直接使用，只有使用独立业务空间时才需要填写业务空间标识。

启动项目后，可以在 `任务配置` 的 `模型与语音服务` 中填写 Key。这种方式只对当前后端进程有效，重启后需要重新填写。

如果在自己的 Windows 电脑上反复使用，也可以在 PowerShell 7 中保存为当前用户环境变量：

```powershell
[Environment]::SetEnvironmentVariable(
  'DASHSCOPE_API_KEY',
  '<YOUR_DASHSCOPE_API_KEY>',
  [EnvironmentVariableTarget]::User
)
```

保存后请重新打开 PowerShell，再启动项目。一键启动脚本会把 Key 同步到后端内存。不要把真实 Key 写进 `.env`、截图、Issue 或 commit。

## 本地安装与启动

当前一键启动入口面向 Windows 10 或 Windows 11，需要 PowerShell 7、WSL2、Ubuntu 26.04、Python 3.14、Node 22 和 npm。首次安装时，在 PowerShell 7 中执行：

```powershell
git clone https://github.com/LeiLeiZi2002/liangxin-perform.git
cd liangxin-perform
wsl.exe -d Ubuntu-26.04 -u root -- bash scripts/bootstrap-wsl.sh
wsl.exe -d Ubuntu-26.04
```

进入 WSL 后，在项目目录安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
.venv/bin/python -m pip install --no-deps -e ./backend
cd frontend && npm ci && cd ..
```

以后可以直接双击项目根目录的 `启动DEMO.cmd`，也可以在 PowerShell 7 中运行：

```powershell
pwsh -NoProfile -File .\scripts\start-demo.ps1
```

如使用其他 WSL 发行版，可通过 `-Distribution` 指定名称。浏览器默认打开 <http://127.0.0.1:5173>，请确保本机的 5173 和 8000 端口未被其他程序占用。

## 当前进度

项目目前是可运行的竞赛 DEMO，核心测评、语音互动、工作记录和报告链路已经接通。虚拟来访者的表达自然度、案例内容和报告质量仍在持续打磨，欢迎实际体验并反馈具体问题。

## 验证

完整的零成本验证不会调用百炼服务：

```bash
bash scripts/verify-demo.sh
```

也可以分别执行后端测试、静态检查、前端测试和构建。真实模型、语音识别、语音合成和报告生成需要先配置自己的 API Key，会产生相应的模型调用费用。

本地数据库、录音和运行日志位于忽略目录中，可能包含受测者声音和会谈内容。请勿将这些材料提交到 GitHub 或随意分享。

## 使用边界

本项目只用于竞赛 DEMO、训练和测评研究，不提供真实心理咨询、心理诊断或紧急干预。案例、评分和报告不能替代专业人员判断。现实中出现即时自伤、伤人或其他安全风险时，请联系当地紧急服务和能够到场的可信人员。

## 开发与许可

项目开发：LeiLeiZi2002

协作开发工具：OpenAI Codex

程序代码按照 [MIT License](LICENSE) 发布。案例、人物档案、量规、模拟材料和其他测评内容适用单独的[内容许可说明](CONTENT_LICENSE.md)，不随 MIT License 开放。内容来源和虚构性说明见 [CONTENT_PROVENANCE.md](CONTENT_PROVENANCE.md)，第三方依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
