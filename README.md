# Thinking Helper

持续推进长期问题与作品，语音是访问同一份状态的入口。每次后台工作应带来可追溯的新理解或成果，并说明它改变了原结构哪里。

先看[核心理解与当前判断](docs/BRIEF.md)，技术接手再读下文。

## 当前入口

远端：`ssh LLM_from_scratch` → `~/codex_try/thinking_helper`。服务只监听远端 `127.0.0.1:8890`。

在你的 Mac 建立隧道，然后打开 <http://127.0.0.1:8890/>：

```sh
ssh -N -L 8890:127.0.0.1:8890 LLM_from_scratch
```

页面关闭或隧道断开不停止远端后台。远端用户级systemd驻留并在异常后重启；既有Linger=yes支持登出后运行。主机停机期间不执行；SQLite保留任务，启动服务后恢复。

## 零上下文接手

先读 [AGENTS.md](AGENTS.md)、[产品原则](docs/PRODUCT.md)、[结构](docs/ARCHITECTURE.md)、[交接](docs/HANDOFF.md)、[实测状态](docs/STATUS.md)。当前数据以API导出为准，文档不代替运行状态。

## 最小循环

留下原话 → 待审问题分支 → 后台查证 → 带来源的候选更新 → 整合回原结构 → 口头汇报 → 用户纠正 → 下一轮推进。可在页面暂停后台、取消单项任务、导出完整上下文。

## 四个职责

| 职责 | 代码入口 |
| --- | --- |
| 交互与汇报 | `app/main.py`、`app/reporting.py`、语音与页面 |
| 持久工作区 | `app/core.py` 的版本、原话、来源与事件 |
| 后台探索 | `app/engine.py`，及模型、来源与问题选择适配 |
| 检查与整合 | `app/core.py` 的候选事务、`app/evaluation.py` |

运行时不自动改代码。复盘产生可审阅的方法改进建议；工程修改通过测试、记录依据、提交版本后采用。

## 安装与验证

```sh
bash scripts/setup.sh
.venv/bin/python -m scripts.seed
bash scripts/start.sh
```

无付费检查：`.venv/bin/python -m unittest discover -s tests -v`。真实合成与识别检查：`.venv/bin/python -m scripts.live_smoke --paid`，会计费且占用服务端额度。

## 凭据与默认边界

服务端读取 `~/.config/thinking-helper/credentials.json`，字段 `api_key`、`voice_id`。也支持 `DASHSCOPE_API_KEY`、`TH_VOICE_ID`、`TH_CREDENTIALS`。凭据不入Git、不返回网页。

默认每日轻量交互120次、后台模型24次请求、TTS12000计费字符、ASR1800秒保守预留；研究通常使用2次模型请求。它们不是人民币账单硬上限。默认每小时研究，候选先审阅；可按主题开启自动整合。

## 远端驻留与恢复

```sh
bash scripts/install-user-service.sh
systemctl --user status thinking-helper.service --no-pager
```

停止本项目：`bash scripts/stop-remote.sh`；重新启动：`bash scripts/serve-remote.sh`。检查 `journalctl --user -u thinking-helper.service` 与 `/api/status`。部署同步、版本核对和回退入口见 [交接](docs/HANDOFF.md)。

## 下一次迭代

一次只改一个明确问题：观察 → 定位模块 → 提案 → 验证 → 更新交接与状态 → Git提交。需确认的产品取舍、真实体感验收与当前局限见 [实测状态](docs/STATUS.md)。

## 调研与产品取舍

[市场与前沿依据](docs/RESEARCH.md)对照Dots、Claude、Gemini以及实时语音研究；[决策](docs/DECISIONS.md)说明便捷复用与自建的取舍。功能组合已有竞品；本项目要验证的是分支保全、明确增量与低负担接续，而非预设商业机会。

## 有证据地迭代

`.venv/bin/python -m scripts.evaluate --checks core planning provider api voice` 保存真实检查。人工变更需关联基线、修改及恢复引用。先把错误修成可复测的案例，再考虑更强模型或更多能力。
