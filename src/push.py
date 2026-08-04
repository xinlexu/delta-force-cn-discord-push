#!/usr/bin/env python3
"""Push new Delta Force China official-site updates/events to one Discord channel.

The Discord webhook URL must be provided through the DISCORD_WEBHOOK_URL
GitHub Actions secret. It is deliberately never read from config.json.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

OFFICIAL_HOST = "df.qq.com"
DETAIL_PATH_FRAGMENT = "/cp/a20240906main/newsdetail.html"
DETAIL_RE = re.compile(
    r"(?:(?:https?:)?//df\.qq\.com)?"
    r"(?:(?:/[A-Za-z0-9._-]+)*/)?newsdetail\.html\?[^\"'<>\\\s]*?id=(\d+)",
    re.IGNORECASE,
)
MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]\n]{2,240})\]\("
    r"((?:https?:)?//df\.qq\.com/(?:[A-Za-z0-9._-]+/)*newsdetail\.html\?[^\s)]+)"
    r"(?:\s+[^)]*)?\)",
    re.IGNORECASE,
)
CMC_ENDPOINT_RE = re.compile(
    r"(?:(?:https?:)?//apps\.game\.qq\.com/cmc/cross\?[^\"'<>\\\s]+)",
    re.IGNORECASE,
)

DATE_FULL_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?(?!\d)"
)
DATE_SHORT_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")

LOGGER = logging.getLogger("delta-force-push")
BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Article:
    article_id: str
    title: str
    url: str
    published: str = ""
    image_url: str = ""
    category: str = ""


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError(f"{path.name} 顶层必须是 JSON 对象")
    return data


def save_state(state: dict[str, Any]) -> None:
    temp_path = STATE_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp_path.replace(STATE_PATH)


def clean_title(value: Any) -> str:
    if value is None:
        return ""
    title = html_lib.unescape(str(value))
    title = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", title)
    title = re.sub(r"<[^>]+>", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" \t\r\n-|—_")
    suffixes = (
        "- 三角洲行动",
        "_三角洲行动",
        "---新一代战术射击品质标杆-腾讯游戏",
        "—新一代战术射击品质标杆-腾讯游戏",
    )
    for suffix in suffixes:
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip(" -—_|")
    if title in {"新闻资讯", "最新", "公告", "新闻", "赛事", "首页"}:
        return ""
    return title[:240]


def normalize_detail_url(raw_url: str, base_url: str) -> tuple[str, str] | None:
    raw_url = html_lib.unescape(raw_url).replace("\\/", "/").strip()
    raw_candidate = "https:" + raw_url if raw_url.startswith("//") else raw_url
    raw_parsed = urlparse(raw_candidate)
    if raw_parsed.hostname and raw_parsed.hostname != OFFICIAL_HOST:
        return None
    match = DETAIL_RE.search(raw_url)
    if not match:
        return None
    article_id = match.group(1)
    matched_url = match.group(0)
    if matched_url.startswith("//"):
        matched_url = "https:" + matched_url
    candidate = urljoin(base_url, matched_url)
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != OFFICIAL_HOST:
        return None
    if not parsed.path.lower().endswith("/newsdetail.html"):
        return None
    query = parse_qs(parsed.query)
    if article_id not in query.get("id", []):
        return None
    normalized_url = urlunparse(
        ("https", OFFICIAL_HOST, parsed.path, "", urlencode({"id": article_id}), "")
    )
    return article_id, normalized_url


def normalize_image_url(raw_url: Any, base_url: str) -> str:
    if not raw_url:
        return ""
    value = html_lib.unescape(str(raw_url)).replace("\\/", "/").strip()
    if value.startswith("//"):
        value = "https:" + value
    else:
        value = urljoin(base_url, value)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urlunparse(parsed)


def normalize_date(
    value: Any, *, context: str = "", reference: datetime | None = None
) -> str:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=BEIJING).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass

    text = str(value or "")
    candidates = [text, context]
    for candidate in candidates:
        full = DATE_FULL_RE.search(candidate)
        if full:
            year, month, day = map(int, full.groups())
            try:
                return datetime(year, month, day, tzinfo=BEIJING).strftime("%Y-%m-%d")
            except ValueError:
                continue

    for candidate in candidates:
        short = DATE_SHORT_RE.search(candidate)
        if short:
            month, day = map(int, short.groups())
            now = reference or datetime.now(BEIJING)
            year = now.year
            try:
                parsed = datetime(year, month, day, tzinfo=BEIJING)
                # List pages often omit the year. Treat dates more than two days
                # in the future as last year's archive entry (not future news).
                if parsed.date() > (now + timedelta(days=2)).date():
                    try:
                        parsed = datetime(year - 1, month, day, tzinfo=BEIJING)
                    except ValueError:
                        pass
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def article_from_parts(
    raw_url: str,
    *,
    title: Any = "",
    published: Any = "",
    image_url: Any = "",
    context: str = "",
    base_url: str,
) -> Article | None:
    normalized = normalize_detail_url(raw_url, base_url)
    if not normalized:
        return None
    article_id, url = normalized
    return Article(
        article_id=article_id,
        title=clean_title(title),
        url=url,
        published=normalize_date(published, context=context),
        image_url=normalize_image_url(image_url, base_url),
    )


def merge_articles(*article_lists: Iterable[Article]) -> list[Article]:
    """Merge while preserving the first-seen ordering and best metadata."""
    order: list[str] = []
    merged: dict[str, Article] = {}
    for articles in article_lists:
        for article in articles:
            if article.article_id not in merged:
                order.append(article.article_id)
                merged[article.article_id] = article
                continue
            current = merged[article.article_id]
            merged[article.article_id] = Article(
                article_id=current.article_id,
                title=current.title or article.title,
                url=current.url,
                published=current.published or article.published,
                image_url=current.image_url or article.image_url,
                category=current.category or article.category,
            )
    return [merged[article_id] for article_id in order]


def response_text(response: requests.Response) -> str:
    content_type = response.headers.get("content-type", "").lower()
    if "charset=" in content_type:
        return response.text
    for encoding in ("utf-8", "gb18030"):
        try:
            return response.content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return response.text


def request_with_retries(
    session: requests.Session,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 45,
    attempts: int = 3,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(
                method.upper(), url, headers=headers, data=data, timeout=timeout
            )
            if response.status_code == 429:
                retry_after = 2.0
                try:
                    payload = response.json()
                    retry_after = float(payload.get("retry_after", retry_after))
                except (AttributeError, ValueError, TypeError, json.JSONDecodeError):
                    try:
                        retry_after = float(
                            response.headers.get("retry-after", retry_after)
                        )
                    except (TypeError, ValueError):
                        retry_after = 2.0
                LOGGER.warning("抓取被限流，%.1f 秒后重试", retry_after)
                last_error = RuntimeError("HTTP 429 rate limit")
                if attempt < attempts:
                    time.sleep(min(max(retry_after, 1.0), 15.0))
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < attempts:
                delay = 2 ** (attempt - 1)
                LOGGER.warning(
                    "抓取失败（第 %s/%s 次）：%s；%s 秒后重试",
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
    raise RuntimeError(f"无法抓取 {url}: {last_error}") from last_error


def parse_html_articles(raw_html: str, base_url: str) -> list[Article]:
    soup = BeautifulSoup(raw_html, "html.parser")
    articles: list[Article] = []

    for tag in soup.find_all(True):
        candidate_url = tag.get("href") or tag.get("data-href") or tag.get("data-url")
        if not candidate_url:
            onclick = tag.get("onclick") or ""
            match = DETAIL_RE.search(onclick)
            candidate_url = match.group(0) if match else ""
        if not candidate_url:
            continue
        context = (
            tag.parent.get_text(" ", strip=True)
            if tag.parent
            else tag.get_text(" ", strip=True)
        )
        article = article_from_parts(
            str(candidate_url),
            title=tag.get("title") or tag.get_text(" ", strip=True),
            context=context,
            image_url=(tag.find("img") or {}).get("src", "") if tag.find("img") else "",
            base_url=base_url,
        )
        if article:
            articles.append(article)

    # Some Tencent pages embed URLs in JavaScript rather than anchor tags.
    for match in DETAIL_RE.finditer(raw_html):
        start = max(0, match.start() - 450)
        end = min(len(raw_html), match.end() + 450)
        snippet = BeautifulSoup(raw_html[start:end], "html.parser").get_text(
            " ", strip=True
        )
        article = article_from_parts(match.group(0), context=snippet, base_url=base_url)
        if article:
            articles.append(article)

    return merge_articles(articles)


def parse_jsonp(text: str) -> Any:
    stripped = text.strip().lstrip("\ufeff")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def value_ci(data: dict[str, Any], *names: str) -> Any:
    normalized = {str(key).lower(): value for key, value in data.items()}
    for name in names:
        if name.lower() in normalized:
            return normalized[name.lower()]
    return None


def source_category(data: dict[str, Any]) -> str:
    """Extract a coarse official category from Tencent channel/tag metadata."""
    values = [
        value_ci(data, "sChannelInfo", "channelInfo"),
        value_ci(data, "sTagInfo", "tagInfo"),
        value_ci(data, "sChannelInfoJson", "channelInfoJson"),
        value_ci(data, "sTagInfoList", "tagInfoList"),
    ]
    text = json.dumps(values, ensure_ascii=False)
    if "赛事" in text:
        return "赛事"
    if "公告" in text:
        return "公告"
    if "新闻" in text:
        return "新闻"
    return ""


def parse_cmc_payload(payload: Any, base_url: str) -> list[Article]:
    articles: list[Article] = []
    for data in iter_dicts(payload):
        raw_url = value_ci(
            data,
            "sUrl",
            "sRedirectURL",
            "url",
            "link",
            "sLink",
            "detailUrl",
        )
        # Do not accept a generic `id`: nested cover/tag/channel objects also
        # contain small numeric IDs and are not news articles.
        doc_id = value_ci(data, "iDocID", "docid", "doc_id")
        title = value_ci(data, "sTitle", "title", "name", "sName")
        published = value_ci(
            data,
            "sCreated",
            "sCreateTime",
            "sPublishTime",
            "publishTime",
            "published_at",
            "date",
        )
        # Recursive payloads contain nested cover/tag dictionaries. Requiring
        # article metadata prevents a stray nested iDocID from becoming a fake,
        # titleless announcement.
        if not raw_url and not title and published is None:
            continue
        if doc_id and (not raw_url or not DETAIL_RE.search(str(raw_url))):
            raw_url = f"{DETAIL_PATH_FRAGMENT}?id={doc_id}"
        if not raw_url:
            continue

        image_url = value_ci(
            data,
            "sCoverUrl",
            "sIMG",
            "image",
            "imageUrl",
            "thumb",
            "thumbnail",
        )
        article = article_from_parts(
            str(raw_url),
            title=title,
            published=published,
            image_url=image_url,
            context=json.dumps(data, ensure_ascii=False)[:1000],
            base_url=base_url,
        )
        if article:
            articles.append(replace(article, category=source_category(data)))
    return merge_articles(articles)


def discover_milo_flow(
    session: requests.Session, config: dict[str, Any]
) -> tuple[str, str, str]:
    """Resolve the public Milo chart/token used by the current official site."""
    desc_url = str(config["milo_act_desc_url"])
    parsed_desc = urlparse(desc_url)
    if parsed_desc.scheme != "https" or parsed_desc.hostname != OFFICIAL_HOST:
        raise RuntimeError("milo_act_desc_url 必须位于 df.qq.com")
    response = request_with_retries(session, desc_url, timeout=25, attempts=2)
    try:
        description = json.loads(response_text(response))
        alias = str(config.get("milo_flow_alias") or "d46289")
        flow_id = str(description["alias"][alias])
        flow = description["flows"][f"f_{flow_id}"]
        chart_id = str(flow["mapid"])
        ide = description["ide"]
        ide_flow = ide["flows"][chart_id]
        ide_token = str(ide_flow["sIdeToken"])
        endpoint = str(ide_flow.get("sIdeUrl") or ide["sIdeUrl"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法从官网 Milo 描述解析动态新闻流程") from exc
    if endpoint.startswith("//"):
        endpoint = "https:" + endpoint
    elif "://" not in endpoint:
        endpoint = "https://" + endpoint.lstrip("/")
    return endpoint, chart_id, ide_token


def fetch_via_milo(session: requests.Session, config: dict[str, Any]) -> list[Article]:
    """Fetch the live paginated list used by the official site's news UI."""
    try:
        endpoint, chart_id, ide_token = discover_milo_flow(session, config)
    except RuntimeError as exc:
        LOGGER.warning("官网 Milo 流程发现失败，使用已验证的固定参数：%s", exc)
        endpoint = str(
            config.get("milo_endpoint") or "https://comm.ams.game.qq.com/ide/"
        )
        chart_id = str(config.get("milo_chart_id") or "329849")
        ide_token = str(config.get("milo_ide_token") or "o3Iw53")

    parsed_endpoint = urlparse(endpoint)
    if (
        parsed_endpoint.scheme != "https"
        or parsed_endpoint.hostname != "comm.ams.game.qq.com"
    ):
        raise RuntimeError("milo_endpoint 必须是腾讯官方 HTTPS 接口")

    channel_id = str(config.get("milo_channel_id") or "6895")
    max_items = min(max(1, int(config.get("max_scan_items", 60))), 200)
    articles: list[Article] = []
    start = 0
    total = max_items

    while len(articles) < max_items and start < total:
        page_limit = min(50, max_items - len(articles))
        form = {
            "iChartId": chart_id,
            "iSubChartId": chart_id,
            "sIdeToken": ide_token,
            "chanid": channel_id,
            "typeids": "1",
            "limit": str(page_limit),
            "start": str(start),
            "isPreengage": "1",
            "needGopenid": "1",
        }
        response = request_with_retries(
            session,
            endpoint,
            method="POST",
            headers={
                "Origin": "https://df.qq.com",
                "Referer": str(config["source_url"]),
            },
            data=form,
            timeout=35,
            attempts=3,
        )
        try:
            payload = json.loads(response_text(response))
            if str(payload.get("iRet", payload.get("ret", "0"))) != "0":
                message = clean_title(payload.get("sMsg")) or "未知错误"
                raise RuntimeError(f"腾讯动态新闻接口失败：{message}")
            data = payload["jData"]["data"]
            raw_items = data["items"]
            total = int(data.get("total", len(raw_items)))
            if not isinstance(raw_items, list):
                raise TypeError("items is not a list")
        except RuntimeError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("腾讯动态新闻接口返回结构无效") from exc

        page_articles = parse_cmc_payload(
            {"items": raw_items}, str(config["source_url"])
        )
        if raw_items and not page_articles:
            raise RuntimeError("腾讯动态新闻接口条目无法解析")
        articles = merge_articles(articles, page_articles)
        start += len(raw_items)
        if not raw_items or len(raw_items) < page_limit:
            break

    if not articles:
        raise RuntimeError("腾讯动态新闻接口未返回可识别的官网文章")
    return articles[:max_items]


def discover_and_fetch_cmc(
    session: requests.Session, raw_html: str, base_url: str
) -> list[Article]:
    endpoints: list[str] = []
    for raw_endpoint in CMC_ENDPOINT_RE.findall(raw_html):
        endpoint = html_lib.unescape(raw_endpoint).replace("\\/", "/")
        if endpoint.startswith("//"):
            endpoint = "https:" + endpoint
        endpoint = urljoin(base_url, endpoint)
        if endpoint not in endpoints:
            endpoints.append(endpoint)

    articles: list[Article] = []
    for endpoint in endpoints[:10]:
        try:
            response = request_with_retries(
                session,
                endpoint,
                headers={"Referer": base_url},
                timeout=30,
                attempts=2,
            )
            payload = parse_jsonp(response_text(response))
            articles.extend(parse_cmc_payload(payload, base_url))
        except (RuntimeError, json.JSONDecodeError) as exc:
            LOGGER.warning("腾讯 CMC 数据源解析失败：%s", exc)
    return merge_articles(articles)


def parse_markdown_articles(markdown: str, base_url: str) -> list[Article]:
    articles: list[Article] = []
    for match in MARKDOWN_LINK_RE.finditer(markdown):
        context = markdown[
            max(0, match.start() - 140) : min(len(markdown), match.end() + 140)
        ]
        article = article_from_parts(
            match.group(2),
            title=match.group(1),
            context=context,
            base_url=base_url,
        )
        if article:
            articles.append(article)

    for match in DETAIL_RE.finditer(markdown):
        context = markdown[
            max(0, match.start() - 180) : min(len(markdown), match.end() + 180)
        ]
        # A nearby line is often the title when the URL is listed separately.
        nearby_lines = [clean_title(line) for line in context.splitlines()]
        nearby_lines = [line for line in nearby_lines if 4 <= len(line) <= 180]
        title = max(nearby_lines, key=len, default="")
        article = article_from_parts(
            match.group(0),
            title=title,
            context=context,
            base_url=base_url,
        )
        if article:
            articles.append(article)

    return merge_articles(articles)


def fetch_via_jina(
    session: requests.Session, source_url: str, jina_api_key: str = ""
) -> list[Article]:
    reader_url = "https://r.jina.ai/" + source_url
    headers = {
        "X-Engine": "browser",
        "X-No-Cache": "true",
        "X-Timeout": "30",
        "X-Respond-With": "markdown",
    }
    if jina_api_key:
        headers["Authorization"] = f"Bearer {jina_api_key}"
    response = request_with_retries(
        session, reader_url, headers=headers, timeout=75, attempts=3
    )
    return parse_markdown_articles(response_text(response), source_url)


def hydrate_article(
    session: requests.Session, article: Article, jina_api_key: str = ""
) -> Article:
    if article.title and article.published:
        return article

    try:
        response = request_with_retries(session, article.url, timeout=30, attempts=2)
        raw_html = response_text(response)
        soup = BeautifulSoup(raw_html, "html.parser")
        title_candidates = [
            soup.find("h1"),
            soup.select_one(".art-title"),
            soup.select_one(".article-title"),
            soup.select_one(".news-title"),
            soup.find("title"),
        ]
        title = article.title
        for candidate in title_candidates:
            if candidate:
                candidate_title = clean_title(candidate.get_text(" ", strip=True))
                if candidate_title:
                    title = candidate_title
                    break
        page_text = soup.get_text(" ", strip=True)
        published = article.published or normalize_date("", context=page_text[:3000])
        image_url = article.image_url
        if not image_url:
            meta_image = soup.find("meta", property="og:image") or soup.find(
                "meta", attrs={"name": "og:image"}
            )
            if meta_image:
                image_url = normalize_image_url(meta_image.get("content"), article.url)
        return replace(article, title=title, published=published, image_url=image_url)
    except RuntimeError as exc:
        LOGGER.warning("详情页补全失败（%s）：%s", article.article_id, exc)

    # Last-resort title extraction through Reader for an otherwise unknown item.
    if not article.title:
        try:
            headers = {
                "X-Engine": "browser",
                "X-No-Cache": "true",
                "X-Timeout": "25",
                "X-Respond-With": "markdown",
            }
            if jina_api_key:
                headers["Authorization"] = f"Bearer {jina_api_key}"
            response = request_with_retries(
                session,
                "https://r.jina.ai/" + article.url,
                headers=headers,
                timeout=60,
                attempts=2,
            )
            text = response_text(response)
            title_match = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
            title = clean_title(title_match.group(1)) if title_match else ""
            published = article.published or normalize_date("", context=text[:2500])
            return replace(article, title=title, published=published)
        except RuntimeError:
            pass
    return article


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        }
    )
    return session


def assert_source_freshness(
    articles: list[Article], *, max_age_days: int, reference: datetime | None = None
) -> None:
    now = reference or datetime.now(BEIJING)
    dates = []
    for article in articles:
        if not article.published:
            continue
        try:
            dates.append(date.fromisoformat(article.published))
        except ValueError:
            continue
    if not dates:
        raise RuntimeError("官网数据没有可验证的发布日期；为避免静默漏报，本次停止")
    newest = max(dates)
    age_days = (now.date() - newest).days
    if age_days > max_age_days:
        raise RuntimeError(
            f"官网数据最新日期为 {newest.isoformat()}，已陈旧 {age_days} 天；"
            "疑似抓到了静态缓存，本次停止"
        )
    if age_days < -2:
        raise RuntimeError(
            f"官网数据最新日期为 {newest.isoformat()}，明显晚于当前日期；"
            "疑似年份解析错误，本次停止"
        )


def fetch_articles(config: dict[str, Any], jina_api_key: str = "") -> list[Article]:
    max_items = int(config.get("max_scan_items", 60))
    session = make_session()
    # Milo is the live source used by the official page. Raw HTML currently
    # contains an old static snapshot, so it must never count as a successful
    # monitoring result.
    combined = fetch_via_milo(session, config)
    LOGGER.info("腾讯动态新闻接口返回 %s 个条目", len(combined))

    if not combined:
        raise RuntimeError("没有从国服官网解析到任何公告；为避免误报，本次不发送消息")

    # Hydrate only the newest few unknown records. Hydrating every historical
    # detail page would make a scheduled run unnecessarily slow and fragile.
    max_hydrate = max(0, int(config.get("max_hydrate_items", 12)))
    hydrated: list[Article] = []
    for index, article in enumerate(combined[:max_items]):
        if index < max_hydrate and (not article.title or not article.published):
            article = hydrate_article(session, article, jina_api_key)
        hydrated.append(article)

    # Tencent responses are normally newest-first. A stable date sort corrects
    # occasional source-order differences while retaining order for ties.
    final_items = merge_articles(hydrated)[:max_items]
    final_items = sorted(
        final_items, key=lambda item: item.published or "0000-00-00", reverse=True
    )
    assert_source_freshness(
        final_items, max_age_days=max(1, int(config.get("max_source_age_days", 45)))
    )
    return final_items


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(str(keyword).casefold() in lowered for keyword in keywords)


def classify_article(article: Article, config: dict[str, Any]) -> tuple[bool, str]:
    title = article.title or "国服官网新内容"
    esports = article.category == "赛事" or contains_any(
        title, config.get("esports_keywords", [])
    )
    esports_override = contains_any(title, config.get("esports_override_keywords", []))
    if esports and not esports_override:
        return False, "赛事"
    if contains_any(title, config.get("event_keywords", [])):
        return True, "活动资讯"
    if contains_any(title, config.get("update_keywords", [])):
        return True, "更新公告"
    if contains_any(title, config.get("announcement_keywords", [])):
        return True, "官方公告"
    if article.category == "公告":
        return True, "官方公告"
    if not article.title and bool(config.get("push_unknown_titles", True)):
        return True, "官方公告"
    return False, "其他新闻"


def webhook_wait_url(webhook_url: str) -> str:
    separator = "&" if "?" in webhook_url else "?"
    if re.search(r"(?:\?|&)wait=", webhook_url):
        return webhook_url
    return webhook_url + separator + "wait=true"


WEBHOOK_SECRET_RE = re.compile(
    r"https://(?:www\.|canary\.|ptb\.)?(?:discord(?:app)?\.com)"
    r"/api/webhooks/\d+/[^\s?&#]+(?:\?[^\s]*)?",
    re.IGNORECASE,
)
WEBHOOK_PATH_SECRET_RE = re.compile(
    r"/api/webhooks/\d+/[^\s?&#]+(?:\?[^\s]*)?", re.IGNORECASE
)


def redact_webhook_text(value: Any, webhook_url: str = "") -> str:
    text = str(value or "")
    candidates = [webhook_url, webhook_wait_url(webhook_url) if webhook_url else ""]
    for candidate in candidates:
        if candidate:
            text = text.replace(candidate, "[Discord Webhook 已隐藏]")
    text = WEBHOOK_SECRET_RE.sub("[Discord Webhook 已隐藏]", text)
    return WEBHOOK_PATH_SECRET_RE.sub("[Discord Webhook 已隐藏]", text)


def send_webhook(webhook_url: str, payload: dict[str, Any]) -> None:
    url = webhook_wait_url(webhook_url)
    last_error = ""
    for attempt in range(1, 4):
        try:
            response = requests.post(url, json=payload, timeout=35)
        except requests.RequestException as exc:
            # requests exceptions often include the full request URL. Never put
            # that value in Actions logs because the URL itself is the secret.
            last_error = f"{type(exc).__name__}: Discord Webhook 网络请求失败"
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
                continue
            break

        if response.status_code in {200, 204}:
            return
        if response.status_code == 429:
            retry_after = 1.5
            try:
                retry_after = float(response.json().get("retry_after", retry_after))
            except (ValueError, TypeError, json.JSONDecodeError):
                for header_name in ("Retry-After", "X-RateLimit-Reset-After"):
                    raw_retry_after = response.headers.get(header_name)
                    if raw_retry_after is None:
                        continue
                    try:
                        retry_after = float(raw_retry_after)
                        break
                    except (TypeError, ValueError):
                        continue
            retry_after = max(retry_after, 1.0)
            if retry_after > 120:
                raise RuntimeError(
                    f"Discord 限流要求等待 {retry_after:.1f} 秒；本轮停止，避免提前重试"
                )
            safe_body = redact_webhook_text(response.text[:300], webhook_url)
            last_error = f"Discord 429: {safe_body}"
            if attempt < 3:
                time.sleep(retry_after + 0.25)
            continue
        safe_body = redact_webhook_text(response.text[:500], webhook_url)
        last_error = f"Discord HTTP {response.status_code}: {safe_body}"
        if 500 <= response.status_code < 600 and attempt < 3:
            time.sleep(2 ** (attempt - 1))
            continue
        break
    raise RuntimeError(f"Webhook 发送失败：{last_error}")


def category_style(category: str) -> tuple[str, int]:
    if category == "活动资讯":
        return "🎁", 0x57F287
    if category == "更新公告":
        return "🛠️", 0xF0B232
    if category == "官方公告":
        return "📢", 0x5865F2
    return "📰", 0x99AAB5


def article_payload(article: Article, category: str) -> dict[str, Any]:
    emoji, color = category_style(category)
    published = article.published or "以官网页面为准"
    title = article.title or "三角洲行动国服官网发布了新内容"
    embed: dict[str, Any] = {
        "title": f"{emoji} {title}"[:256],
        "url": article.url,
        "description": (
            f"**类型：** {category}\n"
            f"**来源：** 三角洲行动国服官网\n"
            f"**发布时间：** {published}\n\n"
            "点击标题查看官方完整内容。"
        ),
        "color": color,
        "footer": {"text": "三角洲行动国服自动推送"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if article.image_url:
        embed["thumbnail"] = {"url": article.image_url}
    return {"allowed_mentions": {"parse": []}, "embeds": [embed]}


def status_payload(
    title: str, description: str, color: int = 0x57F287
) -> dict[str, Any]:
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": "三角洲行动国服自动推送"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def require_webhook() -> str:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError(
            "缺少 DISCORD_WEBHOOK_URL；请在 GitHub Actions Secrets 中配置"
        )
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "discord.com",
        "www.discord.com",
        "discordapp.com",
        "www.discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    }:
        raise RuntimeError("DISCORD_WEBHOOK_URL 看起来不是有效的 Discord Webhook 地址")
    if "/api/webhooks/" not in parsed.path:
        raise RuntimeError("DISCORD_WEBHOOK_URL 缺少 /api/webhooks/ 路径")
    return webhook_url


def run_test(webhook_url: str, articles: list[Article], config: dict[str, Any]) -> None:
    latest = next(
        (article for article in articles if classify_article(article, config)[0]),
        articles[0],
    )
    description = (
        "Webhook、GitHub Actions 与国服官网抓取均已连通。\n\n"
        f"**当前识别到的最新条目：** [{latest.title or '国服官网新内容'}]({latest.url})\n"
        f"**官网日期：** {latest.published or '未识别'}\n\n"
        "测试模式不会修改去重状态。"
    )
    send_webhook(webhook_url, status_payload("✅ 三角洲国服推送测试成功", description))


def run_normal(
    webhook_url: str,
    articles: list[Article],
    config: dict[str, Any],
    state: dict[str, Any],
    deadline: float | None = None,
) -> None:
    current_ids = [article.article_id for article in articles]
    seen_list = [str(value) for value in state.get("seen", [])]
    seen = set(seen_list)
    source_version = str(config.get("source_version") or "milo-v1")
    source_changed = state.get("source_version") != source_version

    if not bool(state.get("initialized")) or source_changed:
        was_initialized = bool(state.get("initialized"))
        state["version"] = 2
        state["initialized"] = True
        state["source_version"] = source_version
        state["seen"] = list(dict.fromkeys(current_ids + seen_list))[:1000]
        state["last_keepalive_month"] = datetime.now(BEIJING).strftime("%Y-%m")
        save_state(state)
        latest = articles[0]
        if was_initialized and source_changed:
            title = "🔄 三角洲行动国服监控数据源已升级"
            lead = "已切换到官网实时动态数据源并重新建立去重基线，不会补发旧文章。"
        else:
            title = "📡 三角洲行动国服监控已启用"
            lead = "当前官网条目已写入去重状态，因此不会把历史公告一次性刷到频道。"
        description = (
            f"{lead}\n\n"
            f"**当前最新条目：** [{latest.title or '国服官网新内容'}]({latest.url})\n"
            "以后仅推送新出现的更新、活动和重要公告。"
        )
        send_webhook(webhook_url, status_payload(title, description))
        return

    new_articles = [article for article in articles if article.article_id not in seen]
    if not new_articles:
        current_month = datetime.now(BEIJING).strftime("%Y-%m")
        if state.get("last_keepalive_month") != current_month:
            state["last_keepalive_month"] = current_month
            save_state(state)
        LOGGER.info("未发现新条目")
        return

    # Official list pages are expected newest-first. Select the oldest unseen
    # chunk and send it oldest-to-newest so a temporary outage does not reorder posts.
    max_push = max(1, int(config.get("max_push_per_run", 8)))
    selected = list(reversed(new_articles[-max_push:]))
    failures: list[str] = []
    deferred_for_budget = False

    for article in selected:
        # One Discord item can consume several minutes under repeated timeouts
        # and rate limits. Leave enough headroom for this item and the final Git
        # state commit; remaining unseen items are picked up next run.
        if deadline is not None and time.monotonic() + 360 > deadline:
            LOGGER.warning("运行时间预算不足，剩余新条目留到下一轮处理")
            deferred_for_budget = True
            break
        should_push, category = classify_article(article, config)
        if should_push:
            try:
                send_webhook(webhook_url, article_payload(article, category))
                LOGGER.info("已发送：%s", article.title or article.url)
            except RuntimeError as exc:
                failures.append(f"{article.article_id}: {exc}")
                LOGGER.error("发送失败：%s", exc)
                continue
        else:
            LOGGER.info("按过滤规则忽略：%s", article.title or article.url)

        seen.add(article.article_id)
        seen_list.append(article.article_id)
        state["seen"] = list(dict.fromkeys(seen_list))[-1000:]
        state["last_keepalive_month"] = datetime.now(BEIJING).strftime("%Y-%m")
        save_state(state)

    if deferred_for_budget:
        failures.append("运行时间预算不足，未处理完的新条目已留待下一轮")
    if failures:
        raise RuntimeError(
            "部分消息发送失败；成功项已保存去重状态：" + " | ".join(failures)
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("normal", "test", "resend_latest"),
        default=os.environ.get("MODE", "normal"),
        help="normal=正常监控；test=连通性测试；resend_latest=重发最新匹配条目",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    deadline = time.monotonic() + 9 * 60
    config = load_json(CONFIG_PATH, {})
    state = load_json(
        STATE_PATH,
        {
            "version": 2,
            "initialized": False,
            "source_version": "",
            "seen": [],
            "last_keepalive_month": "",
        },
    )
    webhook_url = require_webhook()
    jina_api_key = os.environ.get("JINA_API_KEY", "").strip()
    articles = fetch_articles(config, jina_api_key)
    LOGGER.info("解析到 %s 个官网条目", len(articles))

    if args.mode == "test":
        run_test(webhook_url, articles, config)
    elif args.mode == "resend_latest":
        latest = next(
            (
                (article, category)
                for article in articles
                for should_push, category in [classify_article(article, config)]
                if should_push
            ),
            (articles[0], "官方公告"),
        )
        send_webhook(webhook_url, article_payload(*latest))
    else:
        run_normal(webhook_url, articles, config, state, deadline=deadline)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - top-level job should fail loudly
        LOGGER.error("任务失败：%s", exc)
        raise SystemExit(1)
