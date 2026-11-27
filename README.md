# Sparky · 儿童双语陪伴+教学伙伴 (POC v2)

面向 **5–8 岁儿童（英语流利、中文零基础～初级）** 的内部验证原型（Demo POC）：
- **核心产品定位**：以温暖、自然的日常双语陪伴为主干，借聊天话头顺势切入中文浸润、二选一/半句留白引导（Cloze）与轻量假装小游戏（Playlets），遇阻即退。
- **终端交互形态**：**纯语音为主 + 形态 A 可控伴侣屏**（Sparky 动态角色状态 + 后端受控弹出的双语灵感词卡 + 默契词收集条 + 连续免按键收音与开口自动打断）。
- **语音与模型链路**：重构版 **OpenAI 全流式低延迟 Cascade**（`gpt-transcribe` 双语热词识别 → `gpt-4.1` 两段式流式生成 → `gpt-realtime-2.1-mini` / PCM 边收边播 WebAudio 管道），无外部第三方 Python 依赖即可运行。
- **验证闭环**：内置侧边 **POC 观测与大纲控制台**，支持实时透视 `SessionFocus` 教学切片、可配置大纲热重载，以及一键 **「模拟开启下一场对话（Next Session）」** 验证跨会话长期记忆与熟词滚雪球复现。

完整产品取舍、可配置大纲数据结构与运行时架构说明请参见 **[DESIGN_V2.md](./DESIGN_V2.md)**。

工程交接文档：[架构与系统提示词说明](docs/architecture.md)（[Word 版](docs/Sparky%20POC%20v2%20架构与系统提示词说明文档.docx)） · [自动化评测集与迭代文档](docs/eval_suite.md)（[Word 版](docs/Sparky%20POC%20v2%20自动化评测集与自动迭代文档.docx) / [可执行用例](evaluations/README.md)） · [记忆策略与 Dreaming 评估](docs/memory.md)。

---

## 快速启动

需要 Python 3.10+（零第三方强制依赖，开箱即用）：

```sh
# 1. 静态校验可配置教学大纲与掌握度规则
python3 -m sparky check

# 2. 启动 Web 伴侣屏与流式语音服务（默认监听 http://127.0.0.1:8765）
OPENAI_API_KEY="your-key" python3 server.py --port 8765
```

- 若未设置 `OPENAI_API_KEY`，启动时可按隐藏提示输入，或直接回车进入 **本地离线规则演示模式**（无需联网亦可完整体验单元解锁、词卡弹出与跨场记忆归档）。
- 打开浏览器访问 `http://127.0.0.1:8765/`：
  - **主舞台（左侧）**：点击「开启连续语音对话」即可免按键自然交谈（支持 Sparky 说话时直接开口打断；嘈杂环境可取消勾选「免按键连续听」切换为手动点击说话），也可直接在底部输入框打字测试。
  - **POC 观测与大纲控制台（右侧抽屉）**：
    - 点击 **「⏭ 结束本场 → 归档记忆并开启下一场 (Next Session)」**，立即触发本场记忆归档并观察 Sparky 在下一场如何结合上场经历与已学中文熟词主动打招呼；
    - 点击 **「+ 恐龙迷老朋友 Maya (S2)」** 可一键加载预设的跨场回归画像（已完成第 1 单元、带恐龙兴趣记忆，直接验证第 2 单元 `我喜欢…`）。

命令行多轮 + 跨场快速演示：

```sh
python3 -m sparky demo
```

---

## 可配置教学大纲设计（`curriculum/`）

教学法与教学内容完全外置为两份声明式配置文件，修改后可在右侧控制台点击 **「↻ 热重载配置」** 立即生效（无需重启服务）：

| 配置文件 | 职责 | 核心配置项 |
| :--- | :--- | :--- |
| `curriculum/pedagogy.json` | **怎么教 & 陪伴节奏** | `session_rhythm`（开场纯陪伴热身轮数、单场新词上限、拒绝后冷却轮数）、`mastery_stages` 与 `mastery_ladder`（`unseen → exposed → scaffolded → practiced → mastered` 各阶段允许的教学动作 `allowed_moves`）、`scaffolding_principles` |
| `curriculum/syllabus.json` | **教什么 & 递进顺序** | 有序 `units` 单元树：前置依赖 `prerequisites`、通关条件 `unlock_next`、日常聊天切口 `topic_bridges`、带开放槽位的功能句块 `patterns`（如 `我喜欢{item}`）、预设锚点词 `vocabulary`（含趣味记忆钩子 `hook`）及可选轻量对话游戏 `playlets` |

### 运行时如何应用大纲

1. **确定性切片调度（`sparky/mastery.py`）**：每轮对话前，代码根据当前轮次、孩子掌握度账本与冷却状态计算出精简的 `SessionFocus`（含当前节奏档位、最多 1 个句块 + 2 个锚点词及其允许动作、跨场滚雪球熟词池 `snowball_familiar_bank`），避免把整份大纲塞进 Prompt。
2. **教学防跳级与送达联动不变量**：
   - **输入先于输出**：`unseen` 目标仅允许 `expose_in_context`（借聊天话头自然带出中文与词卡），不冷启动考翻译；
   - **防复读跳级**：若上一轮 Sparky 刚完整示范过该词句，孩子紧接着复读在代码层最高记为 `imitated`（进入 `scaffolded`），不会误判为独立掌握；
   - **打断回滚**：若首次示范语音在播完前被打断（`interrupted`），掌握度自动回滚至 `unseen`；
   - **动态个人兴趣词库（`personal_lexicon`）**：孩子聊到大纲外的具体兴趣词（如鲨鱼、披萨）且契合当前句块槽位时，可现场生成专属词卡并纳入跨场熟词复现。

---

## 环境变量（可选）

- `OPENAI_API_KEY`：OpenAI API 密钥（服务端持有，永不下发浏览器）
- `SPARKY_MODEL` / `SPARKY_MODEL_URL` / `SPARKY_MODEL_KEY`：文本大脑配置（默认 `gpt-4.1` 流式 SSE）
- `SPARKY_ASR_MODEL`：语音转写模型（默认 `gpt-transcribe`，自动附带当前单元中英热词提示）
- `SPARKY_TTS_MODEL` / `SPARKY_TTS_VOICE`：流式语音合成配置（默认 `gpt-realtime-2.1-mini` + `marin` 音色，流式输出 24kHz PCM）

---

## 运行测试

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.test.cjs
```
