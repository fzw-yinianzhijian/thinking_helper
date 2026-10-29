# 零上下文交接

## 从这里开始

先读根目录 `AGENTS.md` 和 `README.md`，再读 [PRODUCT.md](PRODUCT.md) 确认目标、[ARCHITECTURE.md](ARCHITECTURE.md) 确认边界。当前结果读 `docs/STATUS.md`。做变更前检查 Git 状态、测试及 [DECISIONS.md](DECISIONS.md)，避免按旧会话猜测。

## 当前依据

用户已建立远端 `~/codex_try/thinking_helper`，本仓库为本地镜像。选定 Python ≥3.10、FastAPI、SQLite 和云端语音模型。远端 Node 12 不适合依赖新前端构建链；没有 GPU 命令不等于已证明没有 GPU。

## 交接状态

2026-10-30已做冷读审查并据真实失败迭代。测试数量、运行版本与实网证据以 `docs/STATUS.md` 为准；不要把旧会话或设计文档当成当前实现状态。

## 本地启动合同

在 Python ≥3.10 的虚拟环境内执行：

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python -m uvicorn app.main:app --host 127.0.0.1 --port 8890
```

浏览器访问 `http://127.0.0.1:8890`。远端驻留与转发以根 README 和 `scripts/` 为准；保持回环监听。

## 状态与凭证

默认数据库：`data/workspace.sqlite3`，可用 `TH_DATABASE` 改路径。凭证：`~/.config/thinking-helper/credentials.json`，支持 `TH_CREDENTIALS` 覆盖；字段为 `api_key`、`voice_id`。服务端也读取 `DASHSCOPE_API_KEY` 和 `TH_VOICE_ID`。不得把凭证提交到仓库。

## 首轮预算合同

每日轻量交互120次、后台LLM 24次请求；TTS 12,000 计费字符；ASR 1,800 秒。汉字 2、其他字符 1 计 TTS，ASR 每 30 秒保守预留且不退还。主题默认间隔 3,600 秒。当前无 token 明细；额度不是人民币硬上限，扩大前需用户确认。

## 生命周期

创建主题默认未暂停，服务会安排首轮研究，之后按间隔继续。普通聊天延迟20秒入队整理；这不是完成时限，还受队列、暂停和预算约束。原话保留，未确认分支须审阅。“记入结构”纠正立即增加版本；“停止回应”停聊天和朗读；“暂停后台”持久停止该主题任务。

## 界面验收路径

创建问题 → 推进一小步 → 查看任务及候选 → 展开来源核查 → 整合或不采纳 → 向我汇报 → 记入结构纠正 → 确认版本变化。已过期候选仅供回看，需重新研究。复盘建议的“接受”只记状态，不改代码。

## REST 定位

| 目的 | 入口 |
| --- | --- |
| 健康、额度 | `GET /api/status` |
| 主题及历史 | `GET /api/topics/{id}` |
| 工作区纠正 | `POST /api/topics/{id}/correction`，含 text/version |
| 内容/方法任务 | `POST /api/topics/{id}/research` 或 `/reflect` |
| 候选决定 | `POST /api/candidates/{id}/apply` 或 `/reject` |
| 导出 | `GET /api/topics/{id}/export` |

## 控制与诊断

`PATCH /api/topics/{id}` 接收 paused、auto_apply、interval_seconds；`POST /api/jobs/{id}/cancel` 取消任务。浏览器 `/docs` 可查完整 API。任务与候选看主题详情，错误看 jobs.last_error 和 events；生成完成与实际播放完成分别记录。

## 备份与恢复

优先用SQLite backup API创建一致副本；也可停服务后备份完整数据库及相关文件。恢复前保留旧副本，启动后核对版本与任务。JSON导出交接上下文，当前无通用JSON导入。真实验收已有两份数据库快照，位于远端data/。

## 请求额度的含义

研究通常先搜索再整合，消耗2次LLM请求；原话整理和方法提案也共享24次后台额度。开放对话另有120次/日，默认结构汇报不调用模型。120次聊天不保证120次整理，小时检查也不保证每小时均能研究。额度不足时任务延至次日，需检查实际状态。

## 已读语音接口

`/ws/asr`：先发 `{"type":"start"}`，收到 ready 后传 16kHz 单声道 PCM16；partial 为临时转写，final 为定稿，stop 结束。单连接最多1800秒，同时受每日额度约束。`/ws/tts`：发 `{"type":"text","text":"内容"}`，收 24kHz PCM16；cancel 取消当前朗读。

## 语音取消边界

`VoiceIO.interrupt()` 清空本地排程音频，取消并关闭 TTS 连接；不自行停止后台研究。缺密钥或音色会返回明确错误，不切换浏览器合成声音。已做合成音频回送识别及浏览器排程验证；真实麦克风、声学与主观自然度仍待体验。

## 证据的含义

`verified_search` 表示来源通过规定的获取流程，不代表人工逐页验证；`supported` 也不等于事实确定。首轮实网产出已有“任务完成但内容无实质推进”的失败。先查看 STATUS 的后续复测，再决定能否扩大自动整合。

## 质量修正交接

已实现正文获取/checkpoint、问题轮转与新来源搜索、原话接管、拒绝理由、原文定位和三类进展校验。定位匹配与结构检查不能证明语义正确或有用；实网结果见STATUS，不扩大自动信任范围。

## 首次检查

1. 确认 Python、依赖安装与测试命令。
2. 确认数据库路径、目录权限及备份方法。
3. 运行不含付费调用的基础回归。
4. 配置服务端密钥，确认不进入 Git 和日志。
5. 验证重启、重复领取和版本冲突。
6. 再按 [EVALUATION.md](EVALUATION.md) 做真实语音验证。

## 每次结束时记录

写清当前提交、改了哪一职责、运行的命令与结果、未测部分、阻塞原因，以及下一步的最小动作。若远端与本地不同步，记录各自版本和未提交修改。不得用“基本完成”替代具体状态。

## 已知风险

真实账号、远端和浏览器验证看 STATUS；模拟通过不能替代音质、延迟和长期价值。SQLite 保持单机部署；持续研究仍可能没有有用增量。一次接受方法建议不等于策略已实施。遇到失败保留输入、状态和可重试信息。

## 冷启动验收

让未读聊天的人仅依据仓库完成：理解目标、启动服务、找到数据、完成一次后台候选 review、重启后继续、定位一次失败及停止后台。任何一步需要回看聊天，都应补到 README 或本文件，并写明具体入口。

## 本地修改如何部署

在本地仓库先跑单测与Node语法检查，再同步源码。排除.git、.venv、data、output、.playwright-cli、缓存和凭据。远端检查通过后重启本项目服务，比较本地source_revision与API runtime_revision。源码一致但运行版本不同即旧进程未重启。

```sh
rsync -az --exclude=.git --exclude=.venv --exclude=data --exclude=output --exclude=.playwright-cli --exclude=__pycache__ ./ LLM_from_scratch:codex_try/thinking_helper/
ssh LLM_from_scratch 'cd ~/codex_try/thinking_helper && .venv/bin/python -m unittest discover -s tests -q'
ssh LLM_from_scratch 'systemctl --user restart thinking-helper.service'
curl -fsS http://127.0.0.1:8890/api/status
```

## 持续进程的入口

远端支持用户级systemd，且既有Linger=yes。安装用 `bash scripts/install-user-service.sh`；已有unit后 `serve-remote.sh` 启动它。`stop-remote.sh`仅停止本仓库服务；unit已启用时，永久取消开机启动还需 `systemctl --user disable thinking-helper.service`。

## 方法变更的实际评估

先运行 `.venv/bin/python -m scripts.evaluate --checks core planning provider` 记录基线；人工变更后加 `--baseline-ref eval-ID --change-ref 提交 --rollback-ref 恢复引用`。只运行固定单测。baseline_only、checks_failed、checks_passed含义不同；均不代表长期价值已证实。

## 语音接续合同

逐句记录生成与播放，已播放进度是WebAudio时间线估计。最多提前合成一句，打断时已提交的预取可能计费。用户结束开麦会等待尾句final最多2500ms；切主题立即丢弃旧输入。新对话最多等播放收据1200ms，再保守处理未知播放状态。
