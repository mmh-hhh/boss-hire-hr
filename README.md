# BOSS 招聘辅助（macOS beta）

这个仓库用于让 HR 在自己的 Mac、自己的 Chrome 和自己的 BOSS 招聘方账号上，按选定 JD 完成找候选、读取详情、本地模型评分和收藏。

当前版本面向“HR 会使用 Codex、Claude Code 等 coding agent，但没有公司内部代码权限”的试点场景。HR 不需要读代码；agent 可以完成 clone、安装、自检、离线测试和错误诊断。所有真实 BOSS 操作及终端精确确认必须由 HR 本人执行。

## 直接交给 coding agent 的提示词

复制下面这段：

> 请帮我安装并检查 https://github.com/mmh-hhh/boss-hire-hr 。先 clone 到一个新的本地目录，阅读 AGENTS.md 和 docs/hr-quickstart.md，然后使用 Python 3.11 或更高版本创建项目内的 .venv，安装 requirements.txt 和 requirements-boss.txt，运行 scripts/setup_hr.py init、scripts/setup_hr.py check 和 scripts/run_offline_tests.py。不要读取或展示 .env、浏览器凭据、Cookie、候选人资料；不要访问 BOSS 网站，也不要替我执行 jobs、prepare-job、start、continue、favorite、close、favorite-close 或 clear-circuit。完成后请用中文告诉我自检结果，并逐条指导我本人完成 smoke 流程。

## 支持范围

- macOS、Google Chrome、Python 3.11+。
- 一份安装固定使用一个 BOSS 账号；可以逐个处理多个在招岗位。
- 模型评分使用 HR 自己配置的 OpenAI-compatible 服务。
- Windows、Linux、其他浏览器、GUI 工作台和自动更新暂不支持。

## 安装

```bash
git clone https://github.com/mmh-hhh/boss-hire-hr.git
cd boss-hire-hr
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-boss.txt
.venv/bin/python scripts/setup_hr.py init
```

HR 本人编辑 `.env`，填写模型服务地址和密钥。不要把密钥发给 agent。随后 agent 可以执行：

```bash
.venv/bin/python scripts/setup_hr.py check
.venv/bin/python scripts/run_offline_tests.py
```

完整操作步骤见 [HR 首次使用](docs/hr-quickstart.md)，停止条件见 [安全手册](docs/boss-safe-operation.md)。

## 最小真实验收

HR 本人在 Chrome 官方页面登录 BOSS 招聘方账号，然后逐条执行岗位读取、岗位选择和材料生成。第一次找候选使用 `smoke`，只读取一条搜索路线的第一页，并最多选择一人读取详情和评分：

```bash
.venv/bin/python scripts/run_single_job_live.py jobs --refresh
.venv/bin/python scripts/run_single_job_live.py jobs --select 2
.venv/bin/python scripts/run_single_job_live.py prepare-job
.venv/bin/python scripts/run_single_job_live.py start --config smoke
.venv/bin/python scripts/run_single_job_live.py continue --select 1
.venv/bin/python scripts/run_single_job_live.py continue
.venv/bin/python scripts/run_single_job_live.py report
```

命令中的岗位编号和精确确认文本以终端显示为准。每条命令执行后都会退出，不会自动进入下一阶段。

## 常见使用场景

运行配置位于 `data/local/run_configs/<名称>.json`。初始化会生成 `smoke` 和 `default`；coding agent 可以按下面的场景创建其他配置，再运行 `configs` 检查，不需要改业务代码。

| 场景 | 推荐渠道 | 搜索词 | 第二页 | 近 14 天已查看 |
|---|---:|---:|---:|---|
| 第一次真实验证、排查环境 | 关 | 1 | 0 | 包含 |
| 日常精准搜索 | 关 | 3 | 0 | 包含 |
| 新岗位、希望扩大来源 | 开 | 3 | 0 | 包含 |
| 首页结果有效，希望沿同一路线多看一页 | 关 | 3 | 前 1–2 条 | 包含 |
| 已看过较多候选，希望减少重复 | 关 | 5 | 0 | 排除 |

四个配置字段分别是：

- `recommendation_source_enabled`：`1` 开启推荐首页，`0` 关闭。推荐只读第一页。
- `top_priority_search_query_count`：从岗位材料已经生成的搜索计划中选前 N 条路线；不会临时发明搜索词或为凑数量补路线。
- `second_page_search_query_count`：前 M 条已选搜索路线再读第 2 页，必须满足 `0 ≤ M ≤ N`。
- `recent_view_filter`：`include_all` 包含已查看候选；`exclude_14d` 排除近 14 天已查看候选。

搜索时可重复添加中文 `--filter`。例如：

```bash
.venv/bin/python scripts/run_single_job_live.py start --config default \
  --filter "城市=杭州" \
  --filter "工作经验=3-5年" \
  --filter "学历=本科及以上" \
  --filter "薪资=20K-30K" \
  --filter "活跃度=近一周活跃"
```

先运行 `.venv/bin/python scripts/search_filters.py list` 查看全部可用选项。筛选条件会应用到本轮所有搜索路线，不增加请求次数；推荐渠道不受这些筛选影响。必须严格满足筛选条件时关闭推荐。多选条件用英文逗号连接，例如 `院校要求=双一流,211院校,985院校`。

可以把下面这句话交给 agent：

> 请阅读 docs/hr-quickstart.md 的“按使用场景选择来源配置”和“添加搜索筛选”章节。根据我描述的岗位供给目标，在 data/local/run_configs 下创建一个只含四个允许字段的 JSON 配置，运行 scripts/run_single_job_live.py configs 验证并向我解释预计来源请求构成。不要替我执行 start 或 continue。

详细配置示例、请求次数计算和更多筛选组合见 [HR 首次使用](docs/hr-quickstart.md)。

## 数据和安全

`.env`、认证、候选详情、评分结果和运行账本只保存在本机的 gitignored 目录。不要提交或复制 `data/local`、`.env`、Cookie、Token、简历或终端中的敏感内容。

任何风控提示、HTTP 403/429、收藏失败或结果不明都应立即停止，并在 BOSS 官方页面人工核对。不要自动重试、换目录清账本或绕过确认。

## 发布与许可

本仓库有意不包含 `LICENSE`。公开可见不等于授予复制、修改或再分发许可，详见 [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md)。
