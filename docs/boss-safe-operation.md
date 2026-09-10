# BOSS 安全操作手册

`scripts/run_single_job_live.py` 是本仓库唯一的真实 BOSS 入口。coding agent 只能安装依赖、运行 `scripts/setup_hr.py init/check`、离线测试，以及本地只读的 `configs` 和 `status`；它不能替 HR 访问 BOSS、读取浏览器凭据或执行任何真实阶段。

## 基本规则

- 一份安装固定使用一个 BOSS 账号。换账号时停止，不复制认证或清空账本继续。
- HR 只在 Chrome 官方页面登录，不复制 Cookie、Token 或浏览器数据库。
- BOSS 请求全程严格串行，不并发、不重试、不追随 `hasMore`、不请求计划外页面，也不为了凑人数补抓。
- 每次命令至多执行一个外部阶段。岗位、JD、来源、详情、评分、收藏同步和收藏写入分别确认。
- 候选详情只能来自已持久化候选池；评分只读取本地保存的简历，不产生 BOSS 请求。
- 所有生成数据位于 gitignored 的本地目录，不提交 `.env`、认证、候选资料、评分或账本。

## coding agent 禁止执行

coding agent 不得执行 `jobs`、`prepare-job`、`start`、`continue`、`favorite`、`close`、`favorite-close`、`clear-circuit`、兼容入口 `authorize/run`，不得使用浏览器、CDP、Playwright、raw `boss-agent-cli` 或直接 HTTP 访问 BOSS。

需要验证真实行为时，只能使用冻结 fixture、注入客户端、临时时钟和临时 guard 数据库。`scripts/run_offline_tests.py` 会阻断 `zhipin.com` 及其子域的 DNS 和连接。

## HR 日常入口

首次选择岗位：

```bash
.venv/bin/python scripts/run_single_job_live.py jobs --refresh
.venv/bin/python scripts/run_single_job_live.py jobs --select 2
.venv/bin/python scripts/run_single_job_live.py prepare-job
```

最小冒烟：

```bash
.venv/bin/python scripts/run_single_job_live.py start --config smoke
.venv/bin/python scripts/run_single_job_live.py continue --select 1
.venv/bin/python scripts/run_single_job_live.py continue
.venv/bin/python scripts/run_single_job_live.py report
```

扩大范围前先查看配置：

```bash
.venv/bin/python scripts/run_single_job_live.py configs
.venv/bin/python scripts/search_filters.py list
.venv/bin/python scripts/run_single_job_live.py start --config default
.venv/bin/python scripts/run_single_job_live.py status
.venv/bin/python scripts/run_single_job_live.py continue
```

当前运行不再继续时，由 HR 本人执行：

```bash
.venv/bin/python scripts/run_single_job_live.py close --note "本轮不再继续"
```

## 收藏

评分完成后先查看 `report` 和 `pool`。第一次执行 `favorite` 只同步账号收藏状态并冻结真正未收藏的 Top 5；第二次执行才选择并写入收藏。

```bash
.venv/bin/python scripts/run_single_job_live.py pool
.venv/bin/python scripts/run_single_job_live.py favorite
```

每个仍未收藏的候选最多一次收藏 POST 和一次精确回读 GET。只有回读明确确认收藏状态才登记成功。最终名单冻结后，新发现已收藏者只跳过、不补人。

## 必须停止的情况

出现以下任一情况立即停止：

- BOSS 安全验证、滑块、异常登录或账号身份不确定；
- HTTP 403/429，业务 code 9/32/36/37，或程序报告 risk/circuit；
- JD、岗位、登录会话或冻结计划在确认后发生变化；
- 收藏返回失败、`favorite_unknown`、`write_reserved` 或回读无法确认；
- 收藏列表同步未到列表末尾或既有锚点，达到 40 页仍未完成，或响应结构异常。

停止后由 HR 在 BOSS 官方页面人工核对，把不含 Cookie、Token、密钥和候选隐私的错误类别交给 agent 分析。不要自动重试、切换目录、删除账本、绕过熔断或改用其他采集通道。

程序不能保证账号“不封禁”。安全边界的作用是限制请求范围、保持严格串行、保留本地证据，并在结果不确定时停止。
