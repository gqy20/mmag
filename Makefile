.PHONY: help run discover install setup-system clean lint format test coverage typecheck build wheel-smoke verify sync debug-update debug-collect debug-reply debug-test debug-status

# 默认目标
help:
	@echo "mmag — Mattermost AI Agent (uv managed)"
	@echo ""
	@echo "用法:"
	@echo "  make run        启动 Agent"
	@echo "  make discover   探测 Mattermost 环境 (Team/Channel/User ID)"
	@echo "  make setup-system 安装系统依赖 (rsvg-convert, 字体)"
	@echo "  make install    安装包 (editable 模式)"
	@echo "  make test       运行测试"
	@echo "  make coverage   运行测试并检查覆盖率基线"
	@echo "  make typecheck  运行 mypy 宽松类型基线"
	@echo "  make build      构建 wheel"
	@echo "  make wheel-smoke 验证 wheel 可独立加载运行资源"
	@echo "  make verify     执行与 CI 相同的完整工程门禁"
	@echo "  make lint       Ruff 静态检查"
	@echo "  make format     Ruff 格式化代码"
	@echo "  make sync       同步依赖 (uv sync)"
	@echo "  make clean      清理缓存和编译文件"
	@echo "  make debug-update  发布开发更新摘要到 Bugs 频道"
	@echo "  make debug-collect 收集 Bugs 频道最近的问题"
	@echo "  make debug-reply   回复 Bugs 频道指定帖子 (用法: make debug-reply POST_ID=xxx MSG=xxx)"
	@echo "  make debug-test    发测试消息到 mmag-test 并追踪日志/审计 (用法: make debug-test MSG=xxx [WAIT=120])"
	@echo "  make debug-status  查看 bot 进程、已加载能力和最近运行"
	@echo ""
	@echo "环境:"
	@echo "  .env            主配置 (默认加载)"
	@echo "  .env.debug      调试工具配置 (频道 ID, 测试账号)"
	@echo "  .env1~.env3     备选环境配置"

# ---- 运行 ----

run:
	uv run python -m mmag.cli

discover:
	uv run python -m mmag.discover

# ---- 系统依赖 ----

setup-system:
	@echo "安装系统依赖..."
	@if command -v apt-get >/dev/null 2>&1; then \
		sudo apt-get update -qq && \
		sudo apt-get install -y --no-install-recommends librsvg2-bin fonts-noto-cjk; \
	elif command -v dnf >/dev/null 2>&1; then \
		sudo dnf install -y librsvg2-tools google-noto-sans-cjk-fonts; \
	elif command -v yum >/dev/null 2>&1; then \
		sudo yum install -y librsvg2-tools google-noto-sans-cjk-fonts; \
	else \
		echo "不支持的包管理器，请手动安装 librsvg2-bin 和中文字体"; exit 1; \
	fi
	@echo "✅ 系统依赖已安装: rsvg-convert, 中文字体"

# ---- 开发工具 ----

install:
	uv pip install -e .

sync:
	uv sync
	@echo "✅ 依赖已同步"

lint:
	uv run ruff check src tests scripts
	@echo "✅ Lint 通过"

format:
	uv run ruff format src tests scripts
	@echo "✅ 格式化完成"

test:
	uv run pytest tests/ -v --tb=short
	@echo "✅ 测试完成"

coverage:
	uv run pytest --cov=mmag --cov-branch --cov-report=term
	@echo "✅ Coverage 基线通过"

typecheck:
	uv run mypy src/mmag
	@echo "✅ mypy 基线通过"

build:
	uv build --wheel

wheel-smoke: build
	uv run python scripts/verify_wheel.py dist
	@echo "✅ Wheel smoke 通过"

verify: lint coverage typecheck wheel-smoke
	@echo "✅ 完整工程门禁通过"

# ---- 清理 ----

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache dist build *.egg-info .ruff_cache 2>/dev/null || true
	rm -rf logs/*.log 2>/dev/null || true
	@echo "✅ 已清理"

# ---- 调试工具 ----

debug-update:
	uv run python -m scripts.debug update

debug-collect:
	uv run python -m scripts.debug collect

debug-reply:
	@if [ -z "$(POST_ID)" ] || [ -z "$(MSG)" ]; then \
		echo "用法: make debug-reply POST_ID=<帖子ID> MSG=<回复内容>"; exit 1; \
	fi
	uv run python -m scripts.debug reply $(POST_ID) "$(MSG)"

debug-test:
	@if [ -z "$(MSG)" ]; then \
		echo "用法: make debug-test MSG=<测试消息> [WAIT=<秒数>]"; exit 1; \
	fi
	uv run python -m scripts.debug test "$(MSG)" $(or $(WAIT),120)

debug-status:
	uv run python -m scripts.debug status
