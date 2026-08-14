# Evaluation Framework

MMAG 的评估分为 Package 发布评估、离线系统测试和显式真实 Mattermost E2E。三者共享版本、Hash、
结构化结果和默认拒绝外部访问的原则，但不混用资产与凭据。

## 目录与职责

```text
agents/<name>/evals/       # Agent 版本的 contract/quality 发布资产
skills/<name>/evals/       # Skill 版本的 contract/quality 发布资产
evals/                     # 跨 Mattermost、Agent、审批、Artifact 的系统场景
src/mmag/evaluation/       # Loader、Runner、Driver、断言和报告
tests/evaluation/          # 默认离线的框架测试与显式 external smoke
.eval-runs/                # 本地评估报告，不进入 Git
```

顶层资产采用 `mmag.ai/eval/v1`，并由 Draft 2020-12 Schema 拒绝未知字段。Suite 只引用 `evals/`
根目录内的 Scenario；相对路径不能越界。每个 Profile、Suite 和 Scenario 都计算 SHA-256 并进入结果。

## 运行边界

静态验证只解析和校验资产，不导入应用全局 Config，也不访问 Mattermost、LLM 或公网：

```bash
uv run mmag-eval --root evals validate
```

真实 E2E 需要：

1. 专用 Mattermost 测试频道和低权限测试账号；
2. `.env` 或 CI Secret 中提供 Profile 引用的变量；
3. `MMAG_E2E_ENABLED=1`；
4. 命令行显式传入 `--allow-external`；
5. 远程 Mattermost 使用 HTTPS，或仅在受信 localhost 使用 HTTP；
6. Bot 已独立启动并监听目标频道。

```bash
uv run mmag-eval --root evals run suites/smoke.yml \
  --profile profiles/staging-mattermost.yml \
  --allow-external
```

`staging-mattermost` 使用 `MM_USERNAME` / `MM_PASSWORD` 作为 requester；审批和越权套件按需读取独立
approver/unauthorized 账号。缺失的非当前 Case 账号不会被读取。登录 Session 在 Case 退出时注销。

## 观察与断言

Mattermost Driver 使用用户账号发出显式 `@bot` 消息，以原 post ID 关联 MMAG Run，轮询同一 Thread
直到出现成功、失败、耗尽或等待审批状态。需要审批的 Scenario 可以指定 decision 和 actor；Driver
只发送现有文本批准/拒绝命令，不绕过审批服务。

确定性断言覆盖：

- Response kind、终态和 Thread 一致性；
- 默认用户界面不得直接暴露原始 JSON；
- 必含/禁含文本和 Secret 标记；
- 是否进入审批以及未授权审批是否被拒绝；
- Mattermost 文件数量；
- 可选 AgentRun、Task 和 Delivery 只读状态；
- Case deadline。

安全断言失败单独计入 `security_violation_count`，不能被平均质量分抵消。完整响应只保存脱敏后的有限
excerpt 和 SHA-256；报告不保存密码、Token 或 Session。

## Suite 与报告

- `suites/smoke.yml`：单账号用户—Bot Thread 主链；
- `suites/mattermost-e2e.yml`：聊天、统一入口创建真实项目 Task，以及 PPT 审批与 Artifact 交付；Task 场景同时断言 Project Agent 路由、`create_task` 授权调用、新增业务记录、可信 actor/channel 和 execution key；
- `suites/security.yml`：独立未授权账号尝试批准他人的请求。

每次运行在 `.eval-runs/<evaluation-run-id>.json` 原子生成报告，记录资产 Hash、Profile ID、Case 结果、
MMAG Run ID、响应 Hash、断言、耗时和错误码。报告文件是本地运行证据，不是正式发布记录。

## 真实体验录制

仓库提供可重复的七章节演示录制脚本。它使用独立 Playwright session 登录 Mattermost，依次展示：

1. 使用 `/mmag help` 与 `/mmag skills` 发现系统能力；
2. 查看个人 Skill、历史案例和长期记忆，并点击按钮查看版本；
3. 运行 Personal Skill，并通过结果按钮将优秀结果保存为案例；
4. 第二位用户询问所有者已发布的个人数字人；
5. 在新建的私有多人频道中总结真实讨论，并保留来源 Post ID；
6. PPT Agent 完成前置整理，真人点击“批准”按钮允许文件外发；
7. 点击真实 PNG 附件进入 Mattermost 原生预览，并保留可编辑 PPTX 交付证据。

脚本通过当前 Thread 的真实 `file_ids` 核验 `preview.png` 与 `deck.pptx`，并打开 Mattermost 原生图片预览。
普通执行片段的前 80% 使用 8 倍速，最后 20% 降至 3 倍速以展示结果；Slash Command 与个人资产等
阅读片段统一使用 2 倍速，Skill 版本、保存案例、批准和图片预览等真实按钮操作保持正常速度。开场
静态片头保持约 2.5 秒；剪辑器按真实片段边界生成场景时间轴，并在七个章节前插入约 1.8 秒的居中介绍卡；
使用 MMX `speech-2.8-hd` 和女性音色 `Chinese (Mandarin)_Warm_Bestie` 为每个已审阅短句独立生成音频。
解说采用“工作痛点—产品价值—可信治理—成果交付”的宣传叙事，并通过短句切换保留自然的情绪起伏，
再按真实音频时长生成确定性逐句 SRT。旁白
覆盖全部业务章节与约 90% 的关键信息，但为按钮点击、状态变化和预览结果保留必要静默；剪辑器同时输出
实际有声时长覆盖率，避免用连续填满音轨代替叙事节奏。
整条视频固定使用同一 TTS 语速，禁止按场景单独变速；旁白超过对应画面窗口时构建直接失败并要求精简文案。
每个短句先经轻压缩并规范到约 `-20 LUFS / -3 dBFS`，混音后再以双遍 loudnorm 将母带规范到
`-16 LUFS / -2.2 dBTP`，然后将字幕烧录进最终 2K MP4。审批阶段点击与当前
审批 ID 对应的 Mattermost 原生“批准”按钮，
不再发送文本批准命令。录制 Thread 自动切换到 Mattermost 宽屏侧栏；PPT 交付点击真实
`preview.png` 附件并校验原生图片 Dialog 后再关闭。标题和结束页保持正常速度，便于阅读。凭据只从
`.env` 中的评估账号变量读取，
不写入 Bot 日志、录制文件或最终产物。
录制脚本使用原生 2560×1440（2K/QHD），并在片段之间复用同一浏览器页面，避免把 Mattermost 整页
重载动画录入等待阶段。

先做无外部访问的配置与分镜检查：

```bash
MMAG_E2E_ENABLED=1 scripts/record_mattermost_demo.sh --dry-run
```

确认专用测试频道和账号后执行真实录制：

```bash
MMAG_E2E_ENABLED=1 scripts/record_mattermost_demo.sh
```

如果 Bot 已经独立运行，增加 `--bot-already-running`。原始片段、Bot 日志、中间转码和最终 MP4 都写入
`.eval-runs/recordings/<UTC timestamp>/`，不会进入 Git。旁白目录同时保留分段 MP3/SRT、合并后的
`mmag-demo.srt`、解说词和时间轴，重复剪辑时会按解说配置复用 MMX 缓存。脚本需要已认证的 `mmx`、
`playwright-cli`、FFmpeg、ffprobe、字体发现工具和项目现有的 `uv` 环境。

模型或外部服务失败时，可以保留已经成功的素材并从指定章节续录；失败进程会自动回滚本次追加的
`clips.tsv` 记录，不会把失败尝试混入最终成片：

```bash
MMAG_E2E_ENABLED=1 scripts/record_mattermost_demo.sh \
  --from-scene 3 \
  --resume-dir .eval-runs/recordings/<UTC timestamp>
```

Demo 子进程显式启用本地 Host Workspace，并把全局运行期限放宽到 300 秒；Agent Manifest、Execution
Profile、Policy 与审批仍是实际约束。应用默认安全配置不因此改变。

### 录制与成片流水线

一次完整执行按以下顺序进行：

1. `record_mattermost_demo.sh` 创建一次性私有演示频道、投放三位成员的业务讨论并启动 MMAG；
2. 使用两个真实账号依次展示 Slash Command、个人资产和按钮，再运行 Personal Skill、个人数字人、
   会议总结与 PPT 交付；
3. 每个录制片段写入 `raw/*.webm`，同时在 `clips.tsv` 记录稳定片段名和用户可见标题；
4. `render_mattermost_demo.py` 对普通流程应用“前 80% 8 倍、后 20% 3 倍”，Slash Command 和个人资产
   使用 2 倍，对按钮交互保持正常速度，并生成 2.5 秒片头、七个 1.8 秒章节卡和片尾；
5. 剪辑器根据章节与真实片段时长计算旁白起点和相邻段可用时长；
6. 旁白先按完整语义写成不超过 18 个字符的短句；每句保留句末标点并分别通过 MMX 生成 MP3，禁止按
   字符机械截断；句间默认保留 0.16 秒停顿。整片固定同一语速，不使用逐段 `atempo`；任一组短句超过当前
   场景窗口时直接失败并回到文案精简。逐句生成按 Speech RPM 主动限速，并对 CLI 返回的限流错误做有界退避重试；
7. 每个原始语音短句保留为 `.mp3`，再经轻压缩、`-20 LUFS / -3 dBFS` loudnorm 和 limiter 生成
   `-norm.mp3`；混音后执行确定性双遍母带规范化，最终测量结果写入 `narration/loudness.json`；
8. 所有旁白按时间轴先合成为完整 WAV，再与视频混合，避免多段延迟音频触发 `-shortest` 提前截断；
9. 每句原文直接绑定对应规范化 MP3 的真实起止时间，移除展示字幕末尾的中英文句号后组成逐句 SRT；封面阶段
   不显示字幕，最终以无底色、无阴影、低位小字号样式烧录到 2K MP4；
10. FFmpeg 解码检查通过后输出最终路径、时长、大小和 SHA-256。

音频生成使用的等价 MMX 调用如下，实际执行时每个场景使用自己的解说文本和输出路径：

```bash
mmx speech synthesize \
  --model speech-2.8-hd \
  --voice 'Chinese (Mandarin)_Warm_Bestie' \
  --text '场景解说词' \
  --speed 1.08 \
  --pitch -1 \
  --language Chinese \
  --format mp3 \
  --sample-rate 32000 \
  --bitrate 128000 \
  --channels 1 \
  --out narration/sentences/scene-01.mp3 \
  --non-interactive --quiet --output json
```

开始前用 `mmx auth status` 确认 MMX 已认证。默认音频参数可以通过
`MMAG_RECORD_SPEECH_MODEL`、`MMAG_RECORD_VOICE`、`MMAG_RECORD_SPEECH_SPEED` 和
`MMAG_RECORD_SPEECH_PITCH` 覆盖。模型、音色、语速、音调或解说词发生变化时，相应片段的缓存签名失效并
重新生成；缓存以短句为粒度，修改一句只重生成对应音频。只有章节时长、字幕样式或视频倍速变化时，
未变化的 MMX 音频直接复用。

已有真实素材无需再次调用 Mattermost 或模型，可以直接重新渲染：

```bash
uv run python scripts/render_mattermost_demo.py \
  --run-dir .eval-runs/recordings/<UTC timestamp> \
  --output-name mmag-real-e2e-demo-2k.mp4 \
  --speed 8 \
  --result-speed 3 \
  --reading-speed 2 \
  --title-duration 2.5 \
  --chapter-duration 1.8 \
  --speech-model speech-2.8-hd \
  --voice 'Chinese (Mandarin)_Warm_Bestie' \
  --speech-speed 1.08 \
  --speech-pitch -1 \
  --subtitle-max-chars 18 \
  --sentence-gap 0.16
```

关键产物如下：

```text
.eval-runs/recordings/<UTC timestamp>/
  clips.tsv                    # 片段顺序、稳定名称和显示标题
  raw/*.webm                   # Playwright 原始录像
  edit/*.mp4                   # 片头、章节卡、加速后的业务片段和片尾
  narration/sentences/*.mp3   # MMX 逐句原始语音与响度规范化版本
  narration/sentences/*.json  # 逐句文本、模型与音色缓存签名
  narration/narration.txt     # 完整解说词
  narration/narration.json    # 起点、时长、固定语速和文本时间轴
  narration/loudness.json     # 分段与最终母带响度目标及实测值
  narration/narration-mix.wav # 完整旁白混音
  narration/mmag-demo.srt     # 偏移并合并后的最终字幕
  mmag-demo-silent.mp4        # 无音轨但含章节卡的中间视频
  mmag-real-e2e-demo-2k.mp4   # 带解说和烧录字幕的最终成片
```

## 后续发布门禁

当前实现完成了严格资产、真实 API Driver、确定性断言、Suite 阈值和原子报告。尚未完成：

- 激活 Agent/Skill 前实际执行其 contract/quality case；
- 数据集、评分器和人工评审版本化；
- EvaluationRun 与 AgentRun、Package snapshot 在控制面持久关联；
- 报告作为 `evaluation_report` Artifact 原子发布；
- 失败评估阻止制品发布以及按 Package Hash 回滚；
- 少量 Web/Desktop/Mobile 浏览器 smoke。
