"""
URL 分析器 — 分析消息中的链接内容

能力:
  - GitHub 仓库: 走 api.github.com (匿名, 60次/小时)
  - GitHub PR/Issue: 走 api.github.com
  - 通用网页: 抓取 HTML，优先用 Trafilatura 提取正文，失败/过短时回退到 OG 标签
  - 全部结果通过 memory.url_cache 缓存 (默认 1h, 错误 5min)
  - SSRF 防护: 拒绝内网 IP (防御恶意用户让 bot 探测内网)

入口:
  - extract_urls(text) -> list[str]  从文本中提取 URL
  - analyze_url(url, memory=None) -> dict  主分析入口（async）

返回结构 (LinkInfo dict):
  {
    "url": str,
    "kind": "github_repo" | "github_pr" | "github_issue" | "webpage" | "unknown",
    "status": "ok" | "error" | "rate_limited" | "not_found",
    "title": str,
    "summary": str,             # 截断到 ~3000 字符，正文 or OG 描述
    "metadata": dict,           # 含 extraction_method / full_text_length_chars
    "content": str,             # 完整正文 (最多 50k 字符)，供缓存复用
    "cached": bool,
    "error": str | None,
  }
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .logger import get_logger

# 正文摘要长度上限：超过此长度按 head/tail 策略截断
SUMMARY_MAX_CHARS = 3000
# 判定 Trafilatura 提取是否成功的最小字符数
_TRAFILATURA_MIN_CHARS = 100

log = get_logger(__name__)

# ============================================================
# 常量
# ============================================================

USER_AGENT = "mmag-bot/0.1 (+https://github.com)"
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
GITHUB_API_BASE = "https://api.github.com"

# 文本中识别 URL 的正则（保守：截断到空白/常见终止符）
URL_PATTERN = re.compile(
    r'https?://[^\s<>"\'`。，、；：\)\]\}\\]+',
    re.IGNORECASE,
)

# GitHub URL 模式（顺序敏感：先 PR/Issue，再 Repo）
GITHUB_REPO_PATTERN = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$",
    re.IGNORECASE,
)
GITHUB_PR_PATTERN = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?$",
    re.IGNORECASE,
)
GITHUB_ISSUE_PATTERN = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?$",
    re.IGNORECASE,
)
GITHUB_BLOB_PATTERN = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+)/blob/[^/]+/(.+)$",
    re.IGNORECASE,
)

# 缓存 TTL（秒）
CACHE_TTL_OK = 3600  # 1 小时
CACHE_TTL_ERROR = 300  # 5 分钟

# 内网 IP 段（SSRF 防护）
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


# ============================================================
# httpx 异步客户端单例
# ============================================================


def _get_client() -> httpx.AsyncClient:
    """获取共享的 httpx 异步客户端（懒加载单例）"""
    global _client
    try:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                },
            )
    except NameError:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
        )
    return _client


_client: httpx.AsyncClient | None = None


async def close_client() -> None:
    """关闭共享客户端（Agent 退出时调用）"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


# ============================================================
# URL 提取与分类
# ============================================================


def extract_urls(text: str) -> list[str]:
    """从文本中提取所有 URL (去重，保持顺序)"""
    if not text:
        return []
    seen = set()
    out = []
    for m in URL_PATTERN.finditer(text):
        url = m.group(0).rstrip(".,;:!?")  # 去掉句末标点
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _classify_url(url: str) -> tuple[str, re.Match | None]:
    """把 URL 分类为 github_repo / github_pr / github_issue / webpage

    Returns: (kind, match_object)
    """
    for kind, pat in (
        ("github_pr", GITHUB_PR_PATTERN),
        ("github_issue", GITHUB_ISSUE_PATTERN),
        ("github_repo", GITHUB_REPO_PATTERN),
    ):
        m = pat.match(url)
        if m:
            return kind, m
    return "webpage", None


# ============================================================
# SSRF 防护
# ============================================================


def _is_safe_url(url: str) -> tuple[bool, str | None]:
    """检查 URL 是否安全（不指向内网）

    Returns: (is_safe, reason_if_blocked)
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL 解析失败: {e}"
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议: {parsed.scheme}"
    host = parsed.hostname
    if not host:
        return False, "无主机名"
    # 解析为 IP
    try:
        # 如果 host 是 IP 字面量则直接用；否则 DNS 解析
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            infos = socket.getaddrinfo(host, None)
            ips = {info[4][0] for info in infos}
            for ip_str in ips:
                try:
                    ip = ipaddress.ip_address(ip_str)
                except ValueError:
                    continue
                for net in _BLOCKED_NETWORKS:
                    if ip in net:
                        return False, f"目标解析到内网/回环 IP: {ip_str} ({net})"
            return True, None
        # host 本身就是 IP
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                return False, f"内网/回环 IP 被禁止: {ip} ({net})"
        return True, None
    except socket.gaierror as e:
        return False, f"DNS 解析失败: {e}"


# ============================================================
# HTML OG 标签提取（纯 stdlib html.parser）
# ============================================================


class _OGParser(HTMLParser):
    """提取 <title> 和 <meta property="og:*"> 的轻量级解析器"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og: dict[str, str] = {}
        self.description = ""
        self._in_title = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            prop = (attr_dict.get("property") or "").lower()
            content = attr_dict.get("content")
            if not content:
                return
            if prop.startswith("og:"):
                self.og[prop[3:]] = content
            elif (
                prop == "description" or attr_dict.get("name", "").lower() == "description"
            ) and not self.description:
                self.description = content

    def handle_endtag(self, tag: str):
        if tag == "title":
            self._in_title = False
            self.title = "".join(self._title_buf).strip()

    def handle_data(self, data: str):
        if self._in_title:
            self._title_buf.append(data)


def _extract_og_tags(html: str, max_bytes: int = 200_000) -> dict:
    """从 HTML 文本中提取 OG 标签和 title

    Args:
        html: 原始 HTML
        max_bytes: 只解析前 N 字节（OG 标签都在 <head>，无需全量解析）
    """
    truncated = html[:max_bytes] if len(html) > max_bytes else html
    parser = _OGParser()
    try:
        parser.feed(truncated)
    except Exception as e:
        log.debug("HTML 解析异常: %s", e)
    return {
        "title": parser.title or parser.og.get("title", ""),
        "description": parser.description or parser.og.get("description", ""),
        "og": parser.og,
    }


# ============================================================
# 正文提取（Trafilatura）与摘要截断
# ============================================================


def _extract_body_text(html: str) -> str | None:
    """用 Trafilatura 从 HTML 中提取正文

    Returns:
        提取到的纯文本（已 strip），失败或 None 时返回 None
    """
    try:
        import trafilatura  # 延迟 import：不在所有调用路径上都强依赖

        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
    except Exception as e:
        log.debug("trafilatura 提取异常: %s", e)
        return None
    if not text:
        return None
    cleaned = text.strip()
    return cleaned if len(cleaned) >= _TRAFILATURA_MIN_CHARS else None


def _truncate_summary(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> tuple[str, bool]:
    """智能截断正文到 max_chars 以内

    策略:
      - 长度 <= max_chars → 原文返回，不标记截断
      - 长度在 (max_chars, 2*max_chars] → 取中段 (去掉头尾各 1/4)，加省略标记
      - 长度 > 2*max_chars → 头部 2/3 + 省略标记 + 尾部 1/3
        这样同时保留文章开头 (通常含导言/摘要) 和结尾 (通常含结论)

    Returns:
        (truncated_text, was_truncated)
    """
    if len(text) <= max_chars:
        return text, False
    if len(text) <= 2 * max_chars:
        quarter = len(text) // 4
        return f"{text[quarter : quarter + max_chars]}\n\n... [内容已截断] ...", True
    head = (max_chars * 2) // 3
    tail = max_chars - head - len("\n\n... [内容已截断] ...\n\n")
    return f"{text[:head]}\n\n... [内容已截断] ...\n\n{text[-tail:]}", True


# ============================================================
# GitHub API 调用
# ============================================================


def _is_rate_limit_response(resp: httpx.Response) -> bool:
    """GitHub API 返回 403 + X-RateLimit-Remaining: 0 时为限流"""
    if resp.status_code != 403:
        return False
    remaining = resp.headers.get("X-RateLimit-Remaining", "")
    return remaining == "0"


async def _github_get(path: str) -> httpx.Response:
    client = _get_client()
    return await client.get(f"{GITHUB_API_BASE}{path}")


async def _fetch_readme(owner: str, repo: str) -> str | None:
    """获取仓库默认分支的 README (Markdown 原文)

    Returns:
        解码后的 README 文本, 失败/无 README 时返回 None
        失败场景: 网络错误、404 (无 README)、限流、解码异常
    """
    try:
        resp = await _github_get(f"/repos/{owner}/{repo}/readme")
    except httpx.HTTPError as e:
        log.debug("README 拉取失败 (%s/%s): %s", owner, repo, e)
        return None
    # 限流/404/5xx: 不影响主流程, 静默失败
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception as e:
        log.debug("README JSON 解析失败: %s", e)
        return None
    encoding = data.get("encoding", "")
    content_b64 = data.get("content", "")
    if encoding != "base64" or not content_b64:
        return None
    import base64

    try:
        # GitHub 用 \n 分隔 base64 块 (RFC 2045), 去掉空白后解码
        decoded = base64.b64decode(content_b64.replace("\n", "")).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("README base64 解码失败: %s", e)
        return None
    return decoded


async def _analyze_github_repo(owner: str, repo: str) -> dict:
    """GitHub 仓库信息 (含 README 拉取)

    README 与仓库元数据并行拉取, README 失败不影响主流程。
    """
    try:
        repo_resp, readme_text = await asyncio.gather(
            _github_get(f"/repos/{owner}/{repo}"),
            _fetch_readme(owner, repo),
        )
    except httpx.HTTPError as e:
        return _err_dict("github_repo", f"网络错误: {e}")
    return _process_github_repo_response(repo_resp, owner, repo, readme=readme_text)


def _process_github_repo_response(
    resp: httpx.Response, owner: str, repo: str, *, readme: str | None = None
) -> dict:
    if _is_rate_limit_response(resp):
        return _err_dict(
            "github_repo", "GitHub API 限流 (60次/小时)，稍后重试", status="rate_limited"
        )
    if resp.status_code == 404:
        return _err_dict(
            "github_repo", f"仓库不存在或无访问权限: {owner}/{repo}", status="not_found"
        )
    if resp.status_code != 200:
        return _err_dict("github_repo", f"GitHub API 返回 {resp.status_code}", status="error")
    try:
        data = resp.json()
    except Exception as e:
        return _err_dict("github_repo", f"JSON 解析失败: {e}")
    description = (data.get("description") or "").strip()
    title = f"{data.get('full_name', f'{owner}/{repo}')}"
    summary_parts = []
    if description:
        summary_parts.append(description)
    lang = data.get("language")
    if lang:
        summary_parts.append(f"语言: {lang}")
    stars = data.get("stargazers_count")
    if stars is not None:
        summary_parts.append(f"⭐ {stars:,}")
    forks = data.get("forks_count")
    if forks:
        summary_parts.append(f"🍴 {forks:,}")
    topics = data.get("topics") or []
    if topics:
        summary_parts.append(f"话题: {', '.join(topics[:5])}")
    if data.get("archived"):
        summary_parts.append("⚠️ 已归档")
    if data.get("private"):
        summary_parts.append("🔒 私有仓库")
    # summary: 结构化元数据 + README 摘抄 (前 2000 字) 拼成的快速摘要
    #  - 有 README: 结构化 parts + "\n\n" + README 前 2000 字符
    #  - 无 README: 仅结构化 parts (旧行为, description 已包含)
    # content: 完整 README 原文 (数据库层不截断, 截断在 presentation 层)
    #   - README 失败时降级到 description
    if readme:
        readme_excerpt = readme[:2000]
        # README 较长时加省略标记, 让 LLM 知道还有更多
        if len(readme) > 2000:
            readme_excerpt = readme_excerpt.rstrip() + "\n... (README 已截断, 完整内容已缓存)"
        summary = (
            " | ".join(summary_parts) + "\n\n[README 摘抄]\n" + readme_excerpt
            if summary_parts
            else "[README 摘抄]\n" + readme_excerpt
        )
    else:
        summary = " | ".join(summary_parts) if summary_parts else "(无描述)"
    content = readme if readme else description
    metadata = dict(data)
    metadata["readme_fetched"] = readme is not None
    if readme:
        metadata["readme_length_chars"] = len(readme)
    return {
        "url": f"https://github.com/{owner}/{repo}",
        "kind": "github_repo",
        "status": "ok",
        "title": title,
        "summary": summary,
        "metadata": metadata,
        "content": content,
        "cached": False,
        "error": None,
    }


async def _analyze_github_pr(owner: str, repo: str, number: str) -> dict:
    """GitHub PR 信息（与 Issue 共用 API，PR 也是 Issue）"""
    return await _analyze_github_issue_impl(owner, repo, number, kind="github_pr")


async def _analyze_github_issue(owner: str, repo: str, number: str) -> dict:
    """GitHub Issue 信息"""
    return await _analyze_github_issue_impl(owner, repo, number, kind="github_issue")


async def _analyze_github_issue_impl(owner: str, repo: str, number: str, *, kind: str) -> dict:
    try:
        resp = await _github_get(f"/repos/{owner}/{repo}/issues/{number}")
    except httpx.HTTPError as e:
        return _err_dict(kind, f"网络错误: {e}")
    if _is_rate_limit_response(resp):
        return _err_dict(kind, "GitHub API 限流 (60次/小时)，稍后重试", status="rate_limited")
    if resp.status_code == 404:
        return _err_dict(
            kind,
            f"{'PR' if kind == 'github_pr' else 'Issue'} 不存在: #{number}",
            status="not_found",
        )
    if resp.status_code != 200:
        return _err_dict(kind, f"GitHub API 返回 {resp.status_code}", status="error")
    try:
        data = resp.json()
    except Exception as e:
        return _err_dict(kind, f"JSON 解析失败: {e}")

    # 区分 PR vs Issue: PR 数据有 "pull_request" 字段
    is_pull_request = "pull_request" in data
    if kind == "github_pr" and not is_pull_request:
        return _err_dict(
            "github_pr",
            f"#{number} 不是 PR 而是 Issue",
            status="not_found",
        )
    if kind == "github_issue" and is_pull_request:
        return _err_dict(
            "github_issue",
            f"#{number} 不是 Issue 而是 PR",
            status="not_found",
        )

    title = data.get("title", "")
    state = data.get("state", "unknown")
    user = (data.get("user") or {}).get("login", "")
    labels = [label.get("name", "") for label in (data.get("labels") or []) if label.get("name")]
    comments = data.get("comments", 0)
    body = (data.get("body") or "").strip()
    summary_parts = [f"状态: {state}"]
    if user:
        summary_parts.append(f"作者: @{user}")
    if labels:
        summary_parts.append(f"标签: {', '.join(labels[:5])}")
    if comments:
        summary_parts.append(f"💬 {comments} 评论")
    # body 摘要（前 200 字符）
    body_preview = body[:200].replace("\n", " ").strip()
    if body_preview:
        summary_parts.append(f"\n\n{body_preview}{'...' if len(body) > 200 else ''}")
    type_label = "PR" if is_pull_request else "Issue"
    return {
        "url": data.get(
            "html_url",
            f"https://github.com/{owner}/{repo}/{'pull' if is_pull_request else 'issues'}/{number}",
        ),
        "kind": kind,
        "status": "ok",
        "title": f"[{type_label}] {title}",
        "summary": "\n".join(summary_parts),
        "metadata": data,
        "content": body,
        "cached": False,
        "error": None,
    }


# ============================================================
# 通用网页抓取
# ============================================================


async def _analyze_webpage(url: str) -> dict:
    """通用网页抓取: 优先用 Trafilatura 提取正文，失败/过短时回退到 OG 标签

    流程:
      1. SSRF + HTTP 状态检查（不变）
      2. 取 HTML（限 500KB）
      3. 优先用 Trafilatura 提取正文（>= 100 字才算成功）
      4. 失败时回退到 OG/description 提取（保留旧行为）
      5. summary 按 3000 字上限做 head/tail 截断
    """
    safe, reason = _is_safe_url(url)
    if not safe:
        return _err_dict("webpage", reason or "URL 被拒绝", status="error")
    try:
        client = _get_client()
        resp = await client.get(url)
    except httpx.HTTPError as e:
        return _err_dict("webpage", f"网络错误: {e}")
    if resp.status_code == 404:
        return _err_dict("webpage", "404 Not Found", status="not_found")
    if resp.status_code == 403:
        return _err_dict("webpage", "403 Forbidden (可能需要登录或被反爬)", status="error")
    if resp.status_code >= 400:
        return _err_dict("webpage", f"HTTP {resp.status_code}", status="error")
    # 限制 HTML 大小
    html = resp.text[:500_000] if hasattr(resp, "text") else ""
    if not html:
        return _err_dict("webpage", "响应体为空", status="error")

    # 先用 OG 拿 title（无论如何都要 title）
    meta = _extract_og_tags(html)
    title = meta["title"] or "(无标题)"
    og_description = meta["description"] or ""

    # 1) 优先 Trafilatura
    body_text = _extract_body_text(html)
    if body_text:
        # 注意: 不在这里截断 — 数据库要存完整正文, 截断是 presentation 层职责
        return {
            "url": str(resp.url),
            "kind": "webpage",
            "status": "ok",
            "title": title,
            "summary": body_text,  # 完整正文 (后续 _format_link_info 会截断到 3000)
            "metadata": {
                "og": meta["og"],
                "status": resp.status_code,
                "final_url": str(resp.url),
                "extraction_method": "trafilatura",
                "full_text_length_chars": len(body_text),
            },
            "content": body_text,
            "cached": False,
            "error": None,
        }

    # 2) 回退到 OG/description（旧行为）
    if not og_description:
        return _err_dict("webpage", "页面缺少正文和 og:description / description", status="error")
    return {
        "url": str(resp.url),
        "kind": "webpage",
        "status": "ok",
        "title": title,
        "summary": og_description,
        "metadata": {
            "og": meta["og"],
            "status": resp.status_code,
            "final_url": str(resp.url),
            "extraction_method": "og_fallback",
            "full_text_length_chars": len(og_description),
        },
        "content": og_description,
        "cached": False,
        "error": None,
    }


# ============================================================
# 主入口
# ============================================================


def _err_dict(kind: str, msg: str, *, status: str = "error") -> dict:
    return {
        "url": "",
        "kind": kind,
        "status": status,
        "title": "",
        "summary": "",
        "metadata": {},
        "content": "",
        "cached": False,
        "error": msg,
    }


async def analyze_url(url: str, *, memory=None) -> dict:
    """主入口: 分析一个 URL

    Args:
        url: 要分析的 URL (http/https)
        memory: Memory 实例，提供 url_cache 读写方法 (可选)

    Returns:
        LinkInfo dict (见模块文档)
    """
    url = (url or "").strip()
    if not url:
        return _err_dict("unknown", "URL 为空", status="error")

    # 1. 缓存查询
    if memory is not None:
        cached = memory.get_cached_url(url)
        if cached is not None:
            log.debug("url_analyzer: 缓存命中 %s", url[:60])
            # cached 包含 url/status/title/summary/metadata 等
            return {
                "url": cached.get("url", url),
                "kind": cached.get("kind", "unknown"),
                "status": cached.get("status", "ok"),
                "title": cached.get("title", ""),
                "summary": cached.get("summary", ""),
                "metadata": cached.get("metadata") or {},
                "content": cached.get("content", ""),
                "cached": True,
                "error": cached.get("error") or None,
            }

    # 2. URL 分类 + SSRF 检查
    kind, match = _classify_url(url)
    if kind == "unknown" or match is None:
        # 也可能是 Mattermost 链接或非 github 网页
        kind = "webpage"
    safe, reason = _is_safe_url(url)
    if not safe:
        info = _err_dict(kind, reason or "URL 被拒绝")
        info["url"] = url
        _maybe_cache(memory, url, info, ttl_seconds=CACHE_TTL_ERROR)
        return info

    # 3. 路由分析
    t0 = time.monotonic()
    try:
        if kind == "github_repo":
            info = await _analyze_github_repo(match.group(1), match.group(2))
        elif kind == "github_pr":
            info = await _analyze_github_pr(match.group(1), match.group(2), match.group(3))
        elif kind == "github_issue":
            info = await _analyze_github_issue(match.group(1), match.group(2), match.group(3))
        elif kind == "webpage":
            info = await _analyze_webpage(url)
        else:
            info = _err_dict("unknown", f"无法识别 URL 类型: {url}")
    except Exception as e:
        log.error("url_analyzer 异常: %s", e, exc_info=True)
        info = _err_dict(kind, f"内部错误: {type(e).__name__}: {e}")
    info["url"] = url
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "url_analyzer: %s [%s] status=%s (%.0fms)",
        url[:60],
        kind,
        info["status"],
        elapsed_ms,
    )

    # 4. 写缓存 (ok 1h, error/not_found/rate_limited 5min)
    ttl = CACHE_TTL_OK if info["status"] == "ok" else CACHE_TTL_ERROR
    _maybe_cache(memory, url, info, ttl_seconds=ttl)
    return info


def _maybe_cache(memory, url: str, info: dict, *, ttl_seconds: int) -> None:
    """有 memory 就写缓存；不抛异常"""
    if memory is None:
        return
    try:
        memory.cache_url(url, info, ttl_seconds=ttl_seconds)
    except Exception as e:
        log.debug("url_analyzer 写缓存失败: %s", e)
