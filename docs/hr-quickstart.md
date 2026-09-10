# HR 首次使用（Codex 辅助）

适用：本人电脑、本人固定一个 BOSS 账号、一份独立安装；多个开放岗位逐个处理。不复制他人的 data/local、认证、候选数据或 .env。共享 Git common 的 worktree 不等于独立账号安装。

把仓库交给 Codex，先让它阅读 AGENTS.md 和本页。助手可以安装依赖、自检、解释命令和分析本地报告；真实登录、来源、详情、模型调用确认与收藏均由本人执行，不能让助手代为确认。

## 安装与本地自检

当前 70 分试点只支持 macOS 和 Google Chrome。Windows、Linux、其他浏览器及 WSL 暂不宣称支持。

先检查 Python 版本。必须是 3.11 或更高版本；如果版本较低，不要继续创建虚拟环境，让 coding agent 帮助安装新版 Python，并确认后续命令使用的是新版解释器。

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-boss.txt
.venv/bin/python -m pip check
.venv/bin/python scripts/setup_hr.py init
```

初始化只补缺失文件，保留已有内容。第一次自检报缺少模型配置是预期结果。本人编辑 .env 的 OPENAI_BASE_URL 和 OPENAI_API_KEY；地址填服务根地址，例如 `https://provider.example`，不要填写末尾 `/v1`。默认模型 gpt-5.4-mini 可按本人服务修改。这是外部模型服务，对持久化简历脱敏后发送输入，并非模型在本机运行。不要将密钥发送给助手。

```bash
.venv/bin/python scripts/setup_hr.py check
```

自检不访问网络、不读取浏览器凭据；它会检查系统、Python、项目虚拟环境、Git、Chrome、声明依赖和模型配置，并给出下一条动作。`local_ready` 只表示基础配置就绪，不证明登录、模型或 BOSS 实际可用。

## 当前操作边界

本人先在 Chrome 官方页面登录固定账号，确认正常且没有安全验证。不要换账号复用本安装，不复制 Cookie，不使用旁路提取或重放请求。

## 第一次选岗位并准备材料

以下命令由本人执行，助手只解释步骤。每次命令会退出，不会自动串联下一阶段。

```bash
.venv/bin/python scripts/run_single_job_live.py jobs --refresh
.venv/bin/python scripts/run_single_job_live.py jobs --select 2
.venv/bin/python scripts/run_single_job_live.py prepare-job
```

将示例编号 2 换成列表中想处理的岗位编号；同名岗位会显示岗位标识供本人核对。第一条输入 `确认读取岗位列表1次`，只读一次列表；第二条输入 `确认读取所选JD1次`，只读该 JD；第三条输入 `确认生成岗位材料`，只调用模型生成评分卡和搜索计划。岗位列表跨日要重新读取；找不到岗位就停止，不自动补抓。

材料生成成功后会显示本地目录，交给 Codex 帮你阅读评分标准和搜索路线。相同 JD 且搜索生成契约未变化时会复用材料；契约升级会保留旧目录并生成新的不可变材料。JD 在 BOSS 发生变化时，重新读取列表、选择岗位并准备材料。生成失败可查看本地错误和安全诊断后再由本人重新执行 prepare-job，零路线或其他失败半成品不进入就绪库存。

## 找候选、评分和收藏

首次真实闭环建议先跑最小冒烟范围。`smoke` 关闭推荐，只读取冻结搜索计划中的第 1 条路线、第 1 页；随后只选择 1 人读取详情，因此也只会评分这 1 人：

```bash
.venv/bin/python scripts/run_single_job_live.py start --config smoke
.venv/bin/python scripts/run_single_job_live.py continue --select 1
.venv/bin/python scripts/run_single_job_live.py continue
```

三条命令必须由本人逐条执行。正常情况下依次看到并输入 `确认来源3次`、`确认详情1人`、`确认评分1人`。来源的 3 次由岗位列表复核 1 次、所选 JD 复核 1 次、单个搜索词第 1 页 1 次组成。若搜索页实际返回 0 人，或命中的人都已有缓存详情和有效评分，冒烟不算完成新的详情与评分调用；程序不会删除缓存，也不会追加搜索词、推荐、第二页、重试或补量，先保留终端输出和状态交给 Codex 诊断。

完成冒烟并核对报告后，再使用常规配置扩大来源范围：

```bash
.venv/bin/python scripts/run_single_job_live.py configs
.venv/bin/python scripts/run_single_job_live.py start --config default
.venv/bin/python scripts/run_single_job_live.py status
.venv/bin/python scripts/run_single_job_live.py continue
```

start 展示精确来源次数并要求 `确认来源N次`；第一次 continue 展示详情人数并要求 `确认详情N人`；下一次 continue 展示模型和评分人数并要求 `确认评分N人`。人数按终端实际摘要填写；每次只推进一个阶段。可先用 `scripts/search_filters.py list` 查看中文筛选，在 start 添加 `--filter "学历=本科及以上"` 等已列出的条件。

评分完成后：

```bash
.venv/bin/python scripts/run_single_job_live.py report
.venv/bin/python scripts/run_single_job_live.py pool
.venv/bin/python scripts/run_single_job_live.py favorite
```

核对评分依据后，第一次 favorite 不会先展示一个可能过期的 Top 5；它先冻结完整本地评分排名池。输入 `确认生成未收藏Top5` 后只核对账号收藏状态，核对完整才在冻结池内顺延并展示真正未收藏的最高分 5 人。若全部已收藏会直接说明无需写入；不足 5 人如实显示。再次执行 favorite，直接回车默认全选或选择显示编号，再输入终端要求的 `确认收藏N人`，才执行收藏。最终名单冻结后新发现已收藏者只跳过、不补人；失败、结果未知或风险会停止，不能自动重试。

当前运行未完成时，先 status/continue；确需放弃则本人执行 `close --note "本轮不继续"`。完成或关闭后再选另一岗位，一个岗位一个岗位处理。收藏恢复若提示岗位不一致，本人可执行 `favorite-close --note "不继续旧岗位名单"` 关闭尚未尝试交付的旧会话；已有交付材料时会拒绝关闭，需要人工核对，不能重试或换目录绕过。

已有岗位材料时，日常命令为 configs → start --config default → status / continue；来源、详情、评分各需一次独立确认。评分完成用 report / pool 看分数与证据，再执行 favorite 核对，第二次 favorite 选择收藏。不要复制高级授权 ID、同步回执和批次路径作为日常流程。

遇到错误先保留终端输出并让助手读取本地状态。风险、收藏未知或失败不得自动重试或清除熔断；到官方页面人工核对。详细停止规则见 [安全手册](boss-safe-operation.md)。


## 登录或运行失败时

- **未找到 wt2**：本人确认实际登录的是 Chrome 中的 BOSS 招聘方页面，核对所用 Profile；本工具没有验证其他浏览器的登录提取。
- **Chrome 登录态提取失败**：保留原认证，核对系统钥匙串授权与 Chrome/Profile；只向助手提供错误类别和系统信息，不提供 Cookie 或密钥。没有确认兼容前，不用其他采集通道绕过。
- **登录会话变化**：仅会话指纹变化不代表换账号。本人在官方页面确认仍是原账号后输入 `确认仍为本人原账号`，再签发本阶段的新授权；输入不匹配就停止且不覆盖旧认证。换账号不能继续复用本安装，不能清账本绕过。
- **模型配置或服务失败**：自检只检查是否配置，不验证可达性。先检查本人的服务根地址、可用模型及额度；已保存的有效评分保留。不要将“本地准备完成”当成模型请求成功。
- **JD 变化**：先查看 status，必要时本人 close；重新 jobs --refresh、jobs --select 和 prepare-job。旧运行、旧评分不会被当作新 JD 的结果。
- **风险、收藏失败或结果未知**：本次立即停止，本人到官方页面核对，助手只读本地证据。不要自动再次收藏、换目录、换账号或清除 circuit。

目前已通过 fixture 与临时认证目录的离线验证；新 HR 的实际操作系统、Chrome Profile 与账号可用性仍须本人试点验收。
