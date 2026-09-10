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

## 数据和安全

`.env`、认证、候选详情、评分结果和运行账本只保存在本机的 gitignored 目录。不要提交或复制 `data/local`、`.env`、Cookie、Token、简历或终端中的敏感内容。

任何风控提示、HTTP 403/429、收藏失败或结果不明都应立即停止，并在 BOSS 官方页面人工核对。不要自动重试、换目录清账本或绕过确认。

## 发布与许可

本仓库有意不包含 `LICENSE`。公开可见不等于授予复制、修改或再分发许可，详见 [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md)。
