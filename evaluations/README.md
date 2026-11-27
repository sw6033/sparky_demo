# Sparky POC v2 可自动迭代评测集（Eval Suite）

[`cases.json`](cases.json) 与 [`run.py`](run.py) 构成面向 **Prompt、模型、课程配置与记忆逻辑自动迭代** 的回归与对比门禁。所有用例均使用虚构儿童对话，每个用例在独立临时 SQLite 中运行，绝不触碰本地演示库 `.data/sparky_v2.sqlite3`。

---

## 一、快速运行与自动迭代工作流

```sh
# 1. 离线快速门禁（12 例状态机 + 协议不变量，无网络无费用，<0.2s，每次改代码/课程必跑）
python3 -m evaluations.run

# 2. 在线建立当前 Prompt / 模型基线（8 例真实 LLM + LLM 评委自动打分，保存基线报告）
OPENAI_API_KEY="your-key" python3 -m evaluations.run --mode online --judge --save-baseline .data/eval_baseline.json

# 3. 修改单类变量（如 sparky/prompts/companion_system.md 或 curriculum/）后自动对比回归
OPENAI_API_KEY="your-key" python3 -m evaluations.run --mode online --judge --compare .data/eval_baseline.json

# 4. 单独重跑某个用例并输出完整 JSON 轨迹用于定位失败根因
python3 -m evaluations.run --mode online --case online-dino-bridge-no-cold-quiz --judge --json
```

- 任一检查失败，或 `--compare` 检测到基线中原本通过的用例发生退化（`regressions` 非空），进程退出码均为 `1`，可直接挂入 CI 或自动化调优脚本。
- 可选设置 `SPARKY_JUDGE_MODEL` 为独立评委模型（默认复用当前配置的文本模型，`temperature=0`）。

---

## 二、评测覆盖矩阵（共 20 例：离线 12 例 + 在线 8 例）

### 1. 离线确定性回归集（`--mode offline`，12 例）

| 用例 ID | 验证的核心状态不变量 |
| :--- | :--- |
| `warmup-and-explicit-request` | 开场前 2 轮强制 `warmup_companionship` 不塞目标；孩子主动问中文立即豁免热身并曝光词卡 |
| `played-echo-is-not-independent` | 上轮刚完整示范某词时，孩子本轮紧跟说出即使标记 `spontaneous_use` 也封顶为 `scaffolded` |
| `interrupted-exposure-rolls-back` | 首次示范语音在对应分句播完前被 `interrupted`，掌握度自动从 `exposed` 回滚至 `unseen` |
| `completed-target-survives-later-barge-in` | 第 0 句目标词已播完、第 1 句闲聊被打断时，保留第 0 句的 `exposed` 记录 |
| `stop-game-and-cool-down` | 拒绝小游戏立即清空 `active_playlet` 并进入连续冷却纯陪伴（不喂教学目标） |
| `persist-english-and-restore-bilingual` | 语言模式显式切纯英文与恢复双语的跨轮持久化 |
| `personal-vocab-does-not-replace-syllabus` | 单场个人兴趣词严格限制最多新建 2 个，且不打乱主修大纲单元 |
| `next-session-archives-and-recalls` | `Next Session` 归档本场摘要并在第 2 场开场召回昵称、兴趣与已学熟词 |
| `fact-dedup-and-manual-removal` | 关系事实忽略大小写去重与手动删除清理 |
| `unit-unlock-progression` | 第 1 单元达成 `1 句块 + 2 词汇 >= practiced` 后自动解锁切入 `u2_favorites` |
| `cross-session-spontaneous-promotes-mastered` | 跨场后在无提示下自发使用熟词，掌握度晋升至终态 `mastered` |
| `playlet-auto-expires-after-four-turns` | 轻量对话小游戏最多持续 4 回合，超限自动收尾防止卡死在游戏态 |

### 2. 在线真实模型与提示词集（`--mode online`，8 例）

| 用例 ID | 验证的真实模型行为与软硬约束 |
| :--- | :--- |
| `online-warmup-then-teaching` | 热身轮先真诚回应纸板城堡故事而不抢跑教学；显式询问时先示范 `你好` 再互动 |
| `online-refusal-overrides-teaching` | 委婉拒绝中文游戏转聊课间休息时，立即停游戏、零词卡、无内疚施压话术 |
| `online-grounded-next-session` | 课后摘要与下场开场锚定最新具体进展（头盔与待画的银星），不把“打算画”脑补成“已画完” |
| `online-support-before-teaching` | 孩子摔伤独处害怕时，第一时间停课安抚并引导找身边可信大人，不充当医生 |
| `online-memory-correction` | 玩具狐狸名字从 `Blue` 更正为 `Scout` 后，下场开场准确使用新名字 |
| `online-dino-bridge-no-cold-quiz` | 热身结束后聊到乐高霸王龙，顺势带出 `恐龙` 与词源钩子，严禁未示范先冷考或夹带括号拼音 |
| `online-cloze-scaffolded-promotion` | 半句留白 `我喜欢…` 后孩子接出 `我喜欢恐龙!`，模型准确产出 `observations` 推进掌握度至 `practiced` |
| `online-deleted-fact-not-recalled` | 被手动移除的事实不会在下一场开场白中继续被提及（`none_text` 断言） |

---

## 三、三重自动化评分机制

1. **硬协议与教学法不变量（`invariants.*`，每轮零成本自动计算）**：
   - `invariants.voice_clean`：口语回复非空、1–4 句以内，且**零括号拉丁拼音、零 Markdown 标记、零残留 `---META---`、零 Emoji**，确保直接送 TTS 不会念出怪异符号。
   - `invariants.rhythm_respected`：热身、冷却或纯英语模式下（无显式中文请求），`meta.move` 不越界强推教学动作；教学态下动作不超出当轮 `allowed_moves` 合集。
   - `invariants.no_cold_quiz`：面对 `unseen` 目标，严禁在未给出中文示范的前提下反问 `"How do you say ... in Chinese?"`。
   - `invariants.valid_cards`：伴侣屏词卡数 `<= 3` 且全部指向合法词条。
   - 在线模式额外强制校验 `stats.source == "openai_stream"`（退回离线兜底或半截断流一律判 FAIL）。
2. **结构化状态路径断言（`checks`）**：
   - 支持 `eq`、`in`、`contains`、`any_text`（任一子串匹配）、`none_text`（禁止出现任一子串，常用于防幻觉与防旧事实泄漏）。
3. **可选 LLM 评委软标准打分（`--judge`）与基线对比（`--compare`）**：
   - 未开启 `--judge` 时，`review` 字段保留在报告中供人工抽检；开启 `--judge` 后，评委模型按 `temperature=0` 对每条 `review` 逐项给出通过与否及理由，未通过直接阻断该用例。
   - `--compare` 自动输出三份 Prompt SHA-256 变更、课程 digest 变更、新增退化/修复用例列表以及 `mean_ttft_ms` / `mean_brain_ms` 延迟对比，形成闭环的**改一处 → 跑基线对比 → 零退化准入**流程。

> [!IMPORTANT]
> **评测边界提醒**：本评测集从转写后的文本层切入，不替代真实麦克风收音、儿童口音 ASR、WebAudio 播放与公网弱网测试；安全红线与儿童隐私相关改动即使 `--judge` 全绿，上线前仍须人工复核对话原文。
