.PHONY: run discover install clean help

# 默认目标
help:
	@echo "mmag — Mattermost AI Agent"
	@echo ""
	@echo "用法:"
	@echo "  make run        启动 Agent"
	@echo "  make discover   探测 Mattermost 环境 (Team/Channel/User ID)"
	@echo "  make install    安装包 (editable 模式)"
	@echo "  make clean      清理缓存和编译文件"
	@echo ""
	@echo "环境:"
	@echo "  .env            主配置 (默认加载)"
	@echo "  .env1~.env3     备选环境配置"

# 启动 Agent
run:
	uv run python -m mmag.cli

# 探测环境
discover:
	uv run python -m mmag.discover

# 安装 (editable)
install:
	uv pip install -e .

# 清理
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache dist build *.egg-info 2>/dev/null || true
	@echo "✅ 已清理"
