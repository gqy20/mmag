"""
url_analyzer 单元测试

覆盖:
  - URL 提取正则 (extract_urls)
  - URL 路由分类 (_classify_url)
  - SSRF 防护 (_is_safe_url)
  - HTML OG 标签提取 (_extract_og_tags)
  - GitHub repo / PR / issue (mock api.github.com)
  - 通用网页抓取 (mock GET)
  - 错误路径 (404 / 403 rate limit / 5xx / timeout)
  - 缓存命中 (memory mock)
  - 工具注册 (analyze_link tool 在 ToolRegistry 中)
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mmag.memory import Memory
from mmag.tools import ToolRegistry, build_builtin_tools
from mmag.url_analyzer import (
    _classify_url,
    _extract_body_text,
    _extract_og_tags,
    _is_safe_url,
    _truncate_summary,
    analyze_url,
    close_client,
    extract_urls,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def temp_db():
    """临时 SQLite 数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def memory(temp_db):
    return Memory(temp_db)


@pytest.fixture
def mock_response():
    """构造 httpx.Response mock 的工厂"""

    def _make(
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
        headers: dict | None = None,
        url: str = "https://api.github.com/test",
    ):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.headers = headers or {}
        resp.url = httpx.URL(url)
        if json_data is not None:
            resp.json.return_value = json_data
        else:
            resp.json.side_effect = Exception("no json")
        resp.text = text
        return resp

    return _make


# ============================================================
# URL 提取
# ============================================================


class TestExtractUrls:
    def test_basic(self):
        urls = extract_urls("看 https://github.com/x/y 不错")
        assert urls == ["https://github.com/x/y"]

    def test_multiple(self):
        urls = extract_urls("a: https://a.com/ b: https://b.com/")
        assert urls == ["https://a.com/", "https://b.com/"]

    def test_dedup(self):
        urls = extract_urls("https://x.com/ 和 https://x.com/ 一样")
        assert urls == ["https://x.com/"]

    def test_strip_trailing_punct(self):
        urls = extract_urls("看这里 https://example.com. 注意句号")
        # 句末的句号应被剥掉
        assert urls == ["https://example.com"]

    def test_chinese_punct_not_break(self):
        urls = extract_urls("看 https://example.com，怎么了")
        assert urls == ["https://example.com"]

    def test_empty(self):
        assert extract_urls("") == []
        assert extract_urls(None) == []  # type: ignore[arg-type]

    def test_mattermost_url(self):
        urls = extract_urls("看 https://mattermost.example.com/team/pl/123")
        assert "https://mattermost.example.com/team/pl/123" in urls


# ============================================================
# URL 分类
# ============================================================


class TestClassifyUrl:
    def test_github_repo(self):
        kind, m = _classify_url("https://github.com/owner/repo")
        assert kind == "github_repo"
        assert m.group(1) == "owner"
        assert m.group(2) == "repo"

    def test_github_repo_with_git_suffix(self):
        kind, m = _classify_url("https://github.com/owner/repo.git")
        assert kind == "github_repo"

    def test_github_repo_with_anchor(self):
        kind, m = _classify_url("https://github.com/owner/repo#readme")
        assert kind == "github_repo"

    def test_github_pr(self):
        kind, m = _classify_url("https://github.com/owner/repo/pull/123")
        assert kind == "github_pr"
        assert m.group(3) == "123"

    def test_github_pr_trailing_slash(self):
        kind, m = _classify_url("https://github.com/owner/repo/pull/123/")
        assert kind == "github_pr"

    def test_github_issue(self):
        kind, m = _classify_url("https://github.com/owner/repo/issues/456")
        assert kind == "github_issue"

    def test_webpage(self):
        kind, _ = _classify_url("https://example.com/foo")
        assert kind == "webpage"

    def test_webpage_with_query(self):
        kind, _ = _classify_url("https://example.com/search?q=test")
        assert kind == "webpage"

    def test_pr_takes_priority_over_repo(self):
        # /pull/123 应被识别为 PR 而不是 repo
        kind, _ = _classify_url("https://github.com/owner/repo/pull/123")
        assert kind == "github_pr"


# ============================================================
# SSRF 防护
# ============================================================


class TestSsrfProtection:
    def test_public_host_allowed(self):
        safe, reason = _is_safe_url("https://github.com/x/y")
        assert safe is True
        assert reason is None

    def test_localhost_blocked(self):
        safe, _ = _is_safe_url("http://127.0.0.1/")
        assert safe is False

    def test_private_10_blocked(self):
        safe, _ = _is_safe_url("http://10.0.0.1/")
        assert safe is False

    def test_private_192_blocked(self):
        safe, _ = _is_safe_url("http://192.168.1.1/")
        assert safe is False

    def test_private_172_blocked(self):
        safe, _ = _is_safe_url("http://172.16.0.1/")
        assert safe is False

    def test_link_local_blocked(self):
        safe, _ = _is_safe_url("http://169.254.169.254/")  # AWS metadata
        assert safe is False

    def test_unsupported_scheme_blocked(self):
        safe, _ = _is_safe_url("ftp://example.com/")
        assert safe is False
        safe, _ = _is_safe_url("file:///etc/passwd")
        assert safe is False

    def test_invalid_url(self):
        safe, _ = _is_safe_url("not-a-url")
        assert safe is False


# ============================================================
# HTML OG 提取
# ============================================================


class TestExtractOgTags:
    def test_basic(self):
        html = """<html><head>
        <title>Test Page</title>
        <meta property="og:title" content="OG Title">
        <meta property="og:description" content="OG Description">
        <meta property="og:image" content="https://img.example.com/x.png">
        </head><body></body></html>"""
        meta = _extract_og_tags(html)
        assert meta["title"] == "Test Page"
        assert meta["og"]["title"] == "OG Title"
        assert meta["og"]["description"] == "OG Description"
        assert meta["og"]["image"] == "https://img.example.com/x.png"
        assert meta["description"] == "OG Description"

    def test_meta_description_fallback(self):
        html = """<html><head>
        <title>Title</title>
        <meta name="description" content="Fallback Desc">
        </head></html>"""
        meta = _extract_og_tags(html)
        assert meta["title"] == "Title"
        assert meta["description"] == "Fallback Desc"

    def test_no_meta(self):
        html = "<html><head><title>Only</title></head><body></body></html>"
        meta = _extract_og_tags(html)
        assert meta["title"] == "Only"
        assert meta["og"] == {}
        assert meta["description"] == ""

    def test_empty(self):
        meta = _extract_og_tags("")
        assert meta["title"] == ""

    def test_truncate_long_html(self):
        # 2MB 的 HTML 不会让解析器爆掉
        huge = "<html><head>" + "<title>X</title>" + "A" * 2_000_000 + "</head></html>"
        meta = _extract_og_tags(huge)
        assert meta["title"] == "X"


# ============================================================
# GitHub 仓库分析
# ============================================================


class TestAnalyzeGithubRepo:
    @pytest.mark.asyncio
    async def test_success(self, mock_response):
        resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "owner/repo",
                "description": "A test repo",
                "stargazers_count": 100,
                "forks_count": 10,
                "language": "Python",
                "topics": ["ai", "agent"],
                "license": {"spdx_id": "MIT"},
                "archived": False,
                "private": False,
                "homepage": "https://example.com",
                "default_branch": "main",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-06-01T00:00:00Z",
                "pushed_at": "2024-06-01T00:00:00Z",
            },
            headers={"X-RateLimit-Remaining": "59"},
        )
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/owner/repo")
        assert info["status"] == "ok"
        assert info["kind"] == "github_repo"
        assert info["title"] == "owner/repo"
        assert "A test repo" in info["summary"]
        assert "100" in info["summary"]  # stars
        assert info["metadata"]["stargazers_count"] == 100

    @pytest.mark.asyncio
    async def test_404(self, mock_response):
        resp = mock_response(status_code=404, headers={"X-RateLimit-Remaining": "59"})
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/missing/repo")
        assert info["status"] == "not_found"
        assert "不存在" in info["error"]

    @pytest.mark.asyncio
    async def test_rate_limit(self, mock_response):
        resp = mock_response(status_code=403, headers={"X-RateLimit-Remaining": "0"})
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/owner/repo")
        assert info["status"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_network_error(self):
        with patch(
            "mmag.url_analyzer._github_get",
            AsyncMock(side_effect=httpx.ConnectError("boom")),
        ):
            info = await analyze_url("https://github.com/owner/repo")
        assert info["status"] == "error"
        assert "网络错误" in info["error"]

    @pytest.mark.asyncio
    async def test_archived_flag(self, mock_response):
        resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "old/x",
                "description": "old",
                "stargazers_count": 0,
                "forks_count": 0,
                "language": "Go",
                "topics": [],
                "license": None,
                "archived": True,
                "private": False,
            },
        )
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/old/x")
        assert "已归档" in info["summary"]

    @pytest.mark.asyncio
    async def test_fetches_readme_and_embeds_in_content(self, mock_response):
        """成功拉取 README 时, content 应为完整 README, summary 应含 README 摘抄"""
        import base64

        readme_text = "# Test Repo\n\nThis is a test repository for unit testing.\n" * 50
        readme_b64 = base64.b64encode(readme_text.encode("utf-8")).decode("ascii")
        # 模拟 _github_get 两次调用: 第一次 /repos/x/y, 第二次 /repos/x/y/readme
        repo_resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "x/y",
                "description": "A test repo",
                "stargazers_count": 100,
                "forks_count": 10,
                "language": "Python",
                "topics": ["test", "demo"],
                "license": None,
                "archived": False,
                "private": False,
            },
        )
        readme_resp = mock_response(
            status_code=200,
            json_data={
                "name": "README.md",
                "encoding": "base64",
                "content": readme_b64,
                "size": len(readme_text),
            },
        )
        with patch(
            "mmag.url_analyzer._github_get",
            AsyncMock(side_effect=[repo_resp, readme_resp]),
        ):
            info = await analyze_url("https://github.com/x/y")
        assert info["status"] == "ok"
        # content 字段是完整 README (数据库层不截断)
        assert info["content"] == readme_text
        assert len(info["content"]) == len(readme_text)
        # summary 含结构化部分 + README 摘抄标记
        assert "A test repo" in info["summary"]
        assert "Python" in info["summary"]
        assert "100" in info["summary"]
        assert "[README 摘抄]" in info["summary"]
        # metadata 标记 README 拉取成功
        assert info["metadata"]["readme_fetched"] is True
        assert info["metadata"]["readme_length_chars"] == len(readme_text)

    @pytest.mark.asyncio
    async def test_readme_404_falls_back_to_description(self, mock_response):
        """README 拉取 404 (无 README) 时, content 降级到 description"""
        repo_resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "x/y",
                "description": "no readme",
                "stargazers_count": 0,
                "forks_count": 0,
                "language": None,
                "topics": [],
                "license": None,
                "archived": False,
                "private": False,
            },
        )
        # README 接口返回 404
        readme_resp = mock_response(status_code=404, json_data={"message": "Not Found"})
        with patch(
            "mmag.url_analyzer._github_get",
            AsyncMock(side_effect=[repo_resp, readme_resp]),
        ):
            info = await analyze_url("https://github.com/x/y")
        assert info["status"] == "ok"
        assert info["content"] == "no readme"  # 降级到 description
        assert info["metadata"]["readme_fetched"] is False
        # summary 不应含 README 摘抄标记
        assert "[README 摘抄]" not in info["summary"]

    @pytest.mark.asyncio
    async def test_readme_long_truncated_in_summary(self, mock_response):
        """长 README 在 summary 中被截断到 2000 字符, 但 content 保留完整"""
        import base64

        readme_text = "X" * 5000  # 5000 字符
        readme_b64 = base64.b64encode(readme_text.encode("utf-8")).decode("ascii")
        repo_resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "x/y",
                "description": "x",
                "stargazers_count": 0,
                "forks_count": 0,
                "language": None,
                "topics": [],
                "license": None,
                "archived": False,
                "private": False,
            },
        )
        readme_resp = mock_response(
            status_code=200,
            json_data={"encoding": "base64", "content": readme_b64},
        )
        with patch(
            "mmag.url_analyzer._github_get",
            AsyncMock(side_effect=[repo_resp, readme_resp]),
        ):
            info = await analyze_url("https://github.com/x/y")
        # content 完整 (5000 字符)
        assert len(info["content"]) == 5000
        # summary 中 README 部分被截断 (含省略标记)
        assert "已截断" in info["summary"]
        assert info["summary"].count("X") == 2000  # 摘抄 = 前 2000 字符

    @pytest.mark.asyncio
    async def test_readme_short_included_fully_in_summary(self, mock_response):
        """短 README (< 2000 字符) 在 summary 中完整保留"""
        import base64

        readme_text = "# Short README\n\nThis is short." * 20  # ~520 chars
        readme_b64 = base64.b64encode(readme_text.encode("utf-8")).decode("ascii")
        repo_resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "x/y",
                "description": "x",
                "stargazers_count": 0,
                "forks_count": 0,
                "language": None,
                "topics": [],
                "license": None,
                "archived": False,
                "private": False,
            },
        )
        readme_resp = mock_response(
            status_code=200,
            json_data={"encoding": "base64", "content": readme_b64},
        )
        with patch(
            "mmag.url_analyzer._github_get",
            AsyncMock(side_effect=[repo_resp, readme_resp]),
        ):
            info = await analyze_url("https://github.com/x/y")
        # 短 README 完整进 summary
        assert "Short README" in info["summary"]
        assert "已截断" not in info["summary"]

    @pytest.mark.asyncio
    async def test_readme_network_error_does_not_break_repo(self, mock_response):
        """README 网络错误时, 仓库元数据应仍正常返回"""
        repo_resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "x/y",
                "description": "ok",
                "stargazers_count": 1,
                "forks_count": 0,
                "language": "Rust",
                "topics": [],
                "license": None,
                "archived": False,
                "private": False,
            },
        )
        # README 端点抛网络错误
        with patch(
            "mmag.url_analyzer._github_get",
            AsyncMock(side_effect=[repo_resp, httpx.ConnectError("boom")]),
        ):
            info = await analyze_url("https://github.com/x/y")
        # 仓库信息应正常
        assert info["status"] == "ok"
        assert "Rust" in info["summary"]
        # README 标记为未拉取
        assert info["metadata"]["readme_fetched"] is False
        assert info["content"] == "ok"  # 降级到 description


# ============================================================
# GitHub PR / Issue
# ============================================================


class TestAnalyzeGithubPr:
    @pytest.mark.asyncio
    async def test_pr_success(self, mock_response):
        resp = mock_response(
            status_code=200,
            json_data={
                "number": 42,
                "title": "Add feature X",
                "state": "open",
                "user": {"login": "alice"},
                "labels": [{"name": "enhancement"}, {"name": "good first issue"}],
                "comments": 5,
                "body": "This PR adds...",
                "html_url": "https://github.com/o/r/pull/42",
                "pull_request": {},  # 标记是 PR
            },
        )
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/o/r/pull/42")
        assert info["status"] == "ok"
        assert info["kind"] == "github_pr"
        assert "[PR]" in info["title"]
        assert "open" in info["summary"]
        assert "@alice" in info["summary"]
        assert "enhancement" in info["summary"]

    @pytest.mark.asyncio
    async def test_issue_success(self, mock_response):
        resp = mock_response(
            status_code=200,
            json_data={
                "number": 7,
                "title": "Bug report",
                "state": "closed",
                "user": {"login": "bob"},
                "labels": [{"name": "bug"}],
                "comments": 2,
                "body": "Steps to reproduce...",
                "html_url": "https://github.com/o/r/issues/7",
            },
        )
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/o/r/issues/7")
        assert info["kind"] == "github_issue"
        assert "[Issue]" in info["title"]
        assert "closed" in info["summary"]

    @pytest.mark.asyncio
    async def test_pr_url_but_actual_issue(self, mock_response):
        # /pull/123 但实际是 Issue (PR merge 后不会，但理论上可能)
        resp = mock_response(
            status_code=200,
            json_data={"number": 123, "title": "Issue"},  # 没有 pull_request 字段
        )
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/o/r/pull/123")
        assert info["status"] == "not_found"
        assert "Issue" in info["error"]


# ============================================================
# 通用网页
# ============================================================


class TestAnalyzeWebpage:
    @pytest.mark.asyncio
    async def test_success(self, mock_response):
        html = """<html><head>
        <title>My Page</title>
        <meta property="og:description" content="Page summary">
        </head></html>"""
        resp = mock_response(status_code=200, text=html, url="https://example.com/")
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=resp)
            info = await analyze_url("https://example.com/")
        assert info["status"] == "ok"
        assert info["kind"] == "webpage"
        assert info["title"] == "My Page"
        assert "Page summary" in info["summary"]

    @pytest.mark.asyncio
    async def test_404(self, mock_response):
        resp = mock_response(status_code=404, text="Not Found")
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=resp)
            info = await analyze_url("https://example.com/missing")
        assert info["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_no_og_meta(self, mock_response):
        html = "<html><head><title>X</title></head><body>plain</body></html>"
        resp = mock_response(status_code=200, text=html)
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=resp)
            info = await analyze_url("https://example.com/bare")
        assert info["status"] == "error"
        assert "og:description" in info["error"]

    @pytest.mark.asyncio
    async def test_ssrf_blocked(self, mock_response):
        # 内部 IP 不应触发 HTTP 请求
        info = await analyze_url("http://127.0.0.1/admin")
        assert info["status"] == "error"
        assert "内网" in info["error"] or "回环" in info["error"] or "IP" in info["error"]

    @pytest.mark.asyncio
    async def test_timeout(self):
        # 用 example.com (可解析) 让 SSRF 通过，再 mock 客户端抛超时
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
            info = await analyze_url("https://example.com/slow")
        assert info["status"] == "error"
        assert "网络" in info["error"]


# ============================================================
# Trafilatura 正文提取
# ============================================================


# 真实的、可被 Trafilatura 解析的 HTML（article 结构）
_REAL_ARTICLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>测试文章标题</title>
    <meta name="description" content="简短描述">
    <meta property="og:title" content="og 标题">
    <meta property="og:description" content="og 描述">
</head>
<body>
    <header><nav>导航菜单</nav></header>
    <main>
        <article>
            <h1>深度学习入门指南</h1>
            <p>深度学习是机器学习的一个分支，它基于人工神经网络。深度学习模型可以学习数据的多层次抽象表示，
            从而在图像识别、自然语言处理、语音识别等领域取得突破性进展。</p>
            <p>本文将从感知机开始，逐步介绍反向传播算法、卷积神经网络、循环神经网络等核心概念，
            并通过 PyTorch 示例代码展示如何构建一个简单的图像分类器。我们将涵盖以下几个关键主题：
            梯度下降优化、损失函数设计、模型正则化以及超参数调优技巧。</p>
            <p>在实际项目中，深度学习的成功很大程度上依赖于数据质量和计算资源。ImageNet 数据集的出现
            标志着深度学习的崛起，而 GPU 的并行计算能力则让大规模神经网络的训练成为可能。近年来，
            Transformer 架构的兴起更是将深度学习推向了新的高度，GPT、BERT 等大语言模型在各个领域
            都展现出了惊人的能力。</p>
            <p>对于初学者来说，建议从简单的全连接网络开始，逐步过渡到更复杂的架构。PyTorch 和
            TensorFlow 是目前最流行的两个深度学习框架，它们都提供了丰富的工具和社区支持。
            通过动手实践和阅读经典论文，可以更快地掌握深度学习的核心思想。</p>
            <p>总结：深度学习不是万能的，它需要大量数据和计算资源。但在合适的场景下，它能够
            解决传统方法难以处理的复杂问题。掌握深度学习的关键在于理解其数学原理和工程实践。</p>
        </article>
    </main>
    <footer>页脚版权信息</footer>
</body>
</html>"""


class TestTrafilaturaExtraction:
    """Trafilatura 集成测试"""

    @pytest.mark.asyncio
    async def test_extracts_body_text_from_realistic_html(self, mock_response):
        """真实结构 HTML 应能提取出正文（>= 100 字符）"""
        resp = mock_response(
            status_code=200, text=_REAL_ARTICLE_HTML, url="https://example.com/article"
        )
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=resp)
            info = await analyze_url("https://example.com/article")
        assert info["status"] == "ok"
        assert info["kind"] == "webpage"
        assert info["title"] == "测试文章标题"  # <title> 优先于 og:title
        assert info["metadata"]["extraction_method"] == "trafilatura"
        # 摘要应包含正文关键内容
        assert "深度学习" in info["summary"]
        # 摘要中不应包含导航/页脚
        assert "导航菜单" not in info["summary"]
        assert "页脚" not in info["summary"]
        # OG 描述在正文中已被覆盖，summary 不应只是 og 描述
        assert "og 描述" not in info["summary"]

    @pytest.mark.asyncio
    async def test_short_html_falls_back_to_og(self, mock_response):
        """HTML 太短 (Trafilatura 提取不到 100 字) → 回退到 OG"""
        html = """<html><head>
            <title>短页</title>
            <meta property="og:description" content="OG 兜底描述">
        </head><body><p>短</p></body></html>"""
        resp = mock_response(status_code=200, text=html, url="https://example.com/short")
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=resp)
            info = await analyze_url("https://example.com/short")
        assert info["status"] == "ok"
        assert info["metadata"]["extraction_method"] == "og_fallback"
        assert "OG 兜底描述" in info["summary"]

    @pytest.mark.asyncio
    async def test_trafilatura_exception_falls_back_to_og(self, mock_response):
        """Trafilatura 抛异常 → 回退到 OG（不污染上层）"""
        html = """<html><head>
            <title>页面</title>
            <meta property="og:description" content="OG 描述">
        </head><body></body></html>"""
        resp = mock_response(status_code=200, text=html)
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=resp)
            with patch("trafilatura.extract", side_effect=RuntimeError("boom")):
                info = await analyze_url("https://example.com/err")
        assert info["status"] == "ok"
        assert info["metadata"]["extraction_method"] == "og_fallback"

    @pytest.mark.asyncio
    async def test_long_body_is_truncated(self, mock_response):
        """超长正文应: analyze_url 返回完整正文, _format_link_info 做截断"""
        para = (
            "深度学习是机器学习的一个分支。"
            "它通过构建多层神经网络来学习数据的多层次抽象表示。"
            "这种方法在图像识别、自然语言处理等领域取得了突破性进展。" * 20
        )
        html = f"""<html><head><title>长文</title></head><body>
            <article><p>{para}</p><p>{para}</p><p>{para}</p></article>
        </body></html>"""
        resp = mock_response(status_code=200, text=html, url="https://example.com/long")
        with patch("mmag.url_analyzer._get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=resp)
            info = await analyze_url("https://example.com/long")
        # analyze_url 应返回**完整**正文 (不再在此层截断)
        assert info["status"] == "ok"
        assert info["metadata"]["extraction_method"] == "trafilatura"
        assert info["metadata"]["full_text_length_chars"] > 3000
        assert len(info["summary"]) > 3000  # 完整正文, 没截断

        # presentation 层 (_format_link_info) 才截断
        from mmag.tools.builtin import _format_link_info

        formatted = _format_link_info(info)
        assert formatted["truncated"] is True
        assert formatted["full_text_length_chars"] > 3000
        # 截断后 summary 长度 <= ~3000 + 省略标记
        assert len(formatted["summary"]) <= 3000 + 50
        assert "截断" in formatted["summary"]

    @pytest.mark.asyncio
    async def test_format_link_info_exposes_trafilatura_fields(self, mock_response):
        """_format_link_info 应暴露 extraction_method / text_length (webpage 级别)"""
        from mmag.tools.builtin import _format_link_info

        info = {
            "url": "https://example.com/x",
            "kind": "webpage",
            "status": "ok",
            "title": "X",
            "summary": "短正文无需截断",  # < 3000 字符, 不触发截断
            "metadata": {
                "og": {},
                "status": 200,
                "final_url": "https://example.com/x",
                "extraction_method": "trafilatura",
                "full_text_length_chars": 8,
            },
            "content": "短正文无需截断",
            "cached": False,
            "error": None,
        }
        formatted = _format_link_info(info)
        assert formatted["webpage"]["extraction_method"] == "trafilatura"
        assert formatted["webpage"]["text_length"] == 8
        # 短文本不应触发截断
        assert "truncated" not in formatted
        assert formatted["summary"] == "短正文无需截断"

    @pytest.mark.asyncio
    async def test_format_link_info_truncates_github_repo_long_description(self):
        """GitHub 长 description 也应被 presentation 层截断 (虽然现在 description 通常不长)"""
        from mmag.tools.builtin import _format_link_info

        long_desc = "X" * 5000
        info = {
            "url": "https://github.com/o/r",
            "kind": "github_repo",
            "status": "ok",
            "title": "o/r",
            "summary": long_desc,  # 模拟很长的 description
            "metadata": {"stargazers_count": 1, "language": "Python"},
            "content": long_desc,
            "cached": False,
            "error": None,
        }
        formatted = _format_link_info(info)
        assert formatted["truncated"] is True
        assert formatted["full_text_length_chars"] == 5000
        assert len(formatted["summary"]) <= 3000 + 50


# ============================================================
# _truncate_summary 单元测试
# ============================================================


class TestTruncateSummary:
    def test_short_text_unchanged(self):
        text = "x" * 100
        out, truncated = _truncate_summary(text, max_chars=200)
        assert out == text
        assert truncated is False

    def test_exact_boundary_unchanged(self):
        text = "x" * 200
        out, truncated = _truncate_summary(text, max_chars=200)
        assert out == text
        assert truncated is False

    def test_moderately_long_takes_middle(self):
        # (max_chars, 2*max_chars] 区间：取中段 + 截断标记
        # 构造边界对齐的字符串：让 quarter 落在 EFGH 段开头
        text = "ABCD" * 25 + "EFGH" * 50 + "IJKL" * 25  # 100+200+100 = 400 chars
        out, truncated = _truncate_summary(text, max_chars=200)
        # 400 ∈ (200, 400] → middle 分支
        # quarter = 400//4 = 100 → text[100:300] = "EFGH"*50
        assert out.startswith("EFGH")
        assert "截断" in out
        assert truncated is True

    def test_very_long_takes_head_and_tail(self):
        # > 2*max_chars 区间：head (max_chars*2/3) + tail (max_chars - head - marker)
        text = "A" * 200 + "B" * 200 + "C" * 200  # 600 chars
        out, truncated = _truncate_summary(text, max_chars=100)
        assert truncated is True
        # 头 66 字符全是 A
        assert out.startswith("A" * 66)
        # 计算 tail 长度：marker 是 "\n\n... [内容已截断] ...\n\n" (19 字符)
        marker_len = len("\n\n... [内容已截断] ...\n\n")
        expected_tail = 100 - 66 - marker_len  # 15
        assert out.endswith("C" * expected_tail)
        assert "截断" in out

    def test_truncation_marker_present(self):
        text = "X" * 1000
        out, _ = _truncate_summary(text, max_chars=50)
        assert "截断" in out


# ============================================================
# _extract_body_text 单元测试
# ============================================================


class TestExtractBodyText:
    def test_returns_none_for_too_short(self):
        # 极短 HTML → 提取不到 100 字
        result = _extract_body_text("<html><body>hi</body></html>")
        assert result is None

    def test_returns_text_for_realistic_html(self):
        result = _extract_body_text(_REAL_ARTICLE_HTML)
        assert result is not None
        assert len(result) >= 100
        assert "深度学习" in result

    def test_handles_trafilatura_exception(self):
        with patch("trafilatura.extract", side_effect=RuntimeError("boom")):
            result = _extract_body_text(_REAL_ARTICLE_HTML)
        assert result is None

    def test_handles_trafilatura_returns_none(self):
        with patch("trafilatura.extract", return_value=None):
            result = _extract_body_text(_REAL_ARTICLE_HTML)
        assert result is None

    def test_handles_empty_string(self):
        assert _extract_body_text("") is None



# ============================================================
# 缓存集成
# ============================================================


class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_http(self, memory, mock_response):
        # 预填充缓存
        memory.cache_url(
            "https://github.com/cached/repo",
            {
                "kind": "github_repo",
                "status": "ok",
                "title": "cached/repo",
                "summary": "from cache",
                "metadata": {"stargazers_count": 999},
                "content": "cached",
            },
            ttl_seconds=3600,
        )
        # 不应触发 HTTP
        info = await analyze_url("https://github.com/cached/repo", memory=memory)
        assert info["cached"] is True
        assert info["title"] == "cached/repo"
        assert info["metadata"]["stargazers_count"] == 999

    @pytest.mark.asyncio
    async def test_cache_miss_then_cache(self, memory, mock_response):
        resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "x/y",
                "description": "first fetch",
                "stargazers_count": 50,
                "forks_count": 0,
                "language": "Rust",
                "topics": [],
                "license": None,
                "archived": False,
                "private": False,
            },
        )
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info1 = await analyze_url("https://github.com/x/y", memory=memory)
        assert info1["cached"] is False
        assert info1["status"] == "ok"

        # 第二次调用: 应从缓存读取 (mock 应不再被调用)
        with patch("mmag.url_analyzer._github_get", AsyncMock()) as mock_gh:
            info2 = await analyze_url("https://github.com/x/y", memory=memory)
        mock_gh.assert_not_called()
        assert info2["cached"] is True
        assert info2["metadata"]["stargazers_count"] == 50

    @pytest.mark.asyncio
    async def test_error_cached_with_short_ttl(self, memory, mock_response):
        resp = mock_response(status_code=404, headers={"X-RateLimit-Remaining": "59"})
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            info = await analyze_url("https://github.com/missing/x", memory=memory)
        assert info["status"] == "not_found"
        # 验证缓存
        cached = memory.get_cached_url("https://github.com/missing/x")
        assert cached is not None
        assert cached["status"] == "not_found"


# ============================================================
# 工具注册 (analyze_link 工具)
# ============================================================


class TestToolRegistration:
    def test_analyze_link_in_builtin_tools(self, memory):
        # 构造一个简单的 mm_client mock (其他工具会用到)
        mm_client = MagicMock()
        tools = build_builtin_tools(mm_client, memory)
        names = [t.name for t in tools]
        assert "analyze_link" in names
        assert "get_posts" in names  # 旧工具仍存在
        assert "save_knowledge" in names

    @pytest.mark.asyncio
    async def test_tool_registry_execute_analyze_link(self, memory, mock_response):
        mm_client = MagicMock()
        tools = build_builtin_tools(mm_client, memory)
        registry = ToolRegistry()
        for t in tools:
            registry.register(t)

        resp = mock_response(
            status_code=200,
            json_data={
                "full_name": "o/r",
                "description": "test",
                "stargazers_count": 1,
                "forks_count": 0,
                "language": "Python",
                "topics": [],
                "license": None,
                "archived": False,
                "private": False,
            },
        )
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            result_str = await registry.execute("analyze_link", {"url": "https://github.com/o/r"})
        import json

        result = json.loads(result_str)
        assert result["status"] == "ok"
        assert result["kind"] == "github_repo"
        assert "stats" in result  # _format_link_info 提取的字段
        assert "repo_info" in result

    @pytest.mark.asyncio
    async def test_format_link_info_for_error(self, memory, mock_response):
        mm_client = MagicMock()
        tools = build_builtin_tools(mm_client, memory)
        registry = ToolRegistry()
        for t in tools:
            registry.register(t)

        resp = mock_response(status_code=404, headers={"X-RateLimit-Remaining": "59"})
        with patch("mmag.url_analyzer._github_get", AsyncMock(return_value=resp)):
            result_str = await registry.execute(
                "analyze_link", {"url": "https://github.com/missing/r"}
            )
        import json

        result = json.loads(result_str)
        assert result["status"] == "not_found"
        assert "error" in result
        # 错误情况下不应包含 stats/repo_info 字段
        assert "stats" not in result
        assert "repo_info" not in result


# ============================================================
# 清理
# ============================================================


@pytest.fixture(autouse=True)
async def cleanup_client():
    """每个测试后关闭 httpx 客户端，避免事件循环警告"""
    yield
    await close_client()
