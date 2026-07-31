.PHONY: help run discover install clean lint format test coverage typecheck build wheel-smoke verify sync

# 默认目标
help:
	@echo "mmag — Mattermost AI Agent (uv managed)"
	@echo ""
	@echo "用法:"
	@echo "  make run        启动 Agent"
	@echo "  make discover   探测 Mattermost 环境 (Team/Channel/User ID)"
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
	@echo ""
	@echo "环境:"
	@echo "  .env            主配置 (默认加载)"
	@echo "  .env1~.env3     备选环境配置"

# ---- 运行 ----

run:
	uv run python -m mmag.cli

discover:
	uv run python -m mmag.discover

# ---- 开发工具 ----

install:
	uv pip install -e .

sync:
	uv sync
	@echo "✅ 依赖已同步"

lint:
	uv run ruff check src tests --exclude tests/poc
	@echo "✅ Lint 通过"

format:
	uv run ruff format src tests --exclude tests/poc
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
