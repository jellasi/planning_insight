#!/usr/bin/env python
"""Weekly PM/PO product-insight report generator.

- Collects product/planning articles from RSS/Atom feeds.
- Scrapes article excerpts, and publish dates for feeds that omit them.
- Produces a strict JSON report plus markdown artifacts.
- Sends Slack bot/webhook and SMTP email notifications in GitHub Actions.

Classification rules, scoring keywords and report limits live in sources.json so the
report can be retuned without touching this file.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "sources.json"
DEFAULT_REPORT_JSON = ROOT / "last_report.json"
DEFAULT_REPORT_MD = ROOT / "last_report.md"
DEFAULT_SLACK = ROOT / "last_slack_message.md"

USER_AGENT = "Mozilla/5.0 PlanningInsightBot/1.0 (+https://github.com/jellasi/planning_insight)"

FALLBACK_CATEGORY = "기타 제품·기획 인사이트"
SLACK_CHAR_LIMIT = 1200


@dataclass
class ContentItem:
    source: str
    source_url: str
    title: str
    url: str
    published_at: str
    collected_at: str
    summary: str
    excerpt: str
    language: str
    score: float
    priority: str
    topic: str


@dataclass
class Ruleset:
    """Compiled classification and scoring rules loaded from sources.json."""

    categories: list[tuple[str, list[re.Pattern[str]]]] = field(default_factory=list)
    default_category: str = FALLBACK_CATEGORY
    high: list[re.Pattern[str]] = field(default_factory=list)
    medium: list[re.Pattern[str]] = field(default_factory=list)
    penalty: list[re.Pattern[str]] = field(default_factory=list)
    high_threshold: float = 3.0
    medium_threshold: float = 1.5


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_date(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).date().isoformat() if dt else "확인 필요"


def parse_date_start(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def parse_date_end_exclusive(value: str) -> datetime:
    return parse_date_start(value) + timedelta(days=1)


def parse_any_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = html.unescape(re.sub(r"\s+", " ", value.strip()))
    try:
        dt = parsedate_to_datetime(value)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def http_get(url: str, limit: int = 1_500_000) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urlopen(req, timeout=25) as resp:
        data = resp.read(limit)
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


TRACKING_PARAMS = re.compile(r"(?i)^(utm_[a-z_]+|source|ref|ref_src|fbclid|gclid)$")


def clean_url(url: str) -> str:
    """Strip feed tracking parameters so shared links stay short and deduplicate."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text or "")
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile one keyword into a matcher.

    ASCII keywords are bounded by ASCII word characters only, so short tokens such
    as "ai" or "ux" cannot match inside unrelated words ("email", "auxiliary") while
    still matching when a Korean particle follows ("LLM은", "UX를"). Korean keywords
    are agglutinative and have no equivalent boundary, so they match as substrings
    ("지표" must still match "핵심지표를").
    """
    escaped = re.escape(keyword.lower())
    if keyword.isascii():
        return re.compile(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])")
    return re.compile(escaped)


def compile_keywords(keywords: list[str]) -> list[re.Pattern[str]]:
    return [keyword_pattern(k) for k in keywords if k]


def count_matches(patterns: list[re.Pattern[str]], text: str) -> int:
    return sum(1 for p in patterns if p.search(text))


def weighted_matches(patterns: list[re.Pattern[str]], head: str, body: str, body_weight: float = 0.5) -> float:
    """Count keyword hits, discounting ones found only in the scraped page body.

    A 1,300-character excerpt carries shared navigation and footer text, so a hit
    there is much weaker evidence than one in the title or feed summary.
    """
    total = 0.0
    for pattern in patterns:
        if pattern.search(head):
            total += 1.0
        elif pattern.search(body):
            total += body_weight
    return total


def build_ruleset(config: dict[str, Any]) -> Ruleset:
    report = config.get("report", {})
    scoring = config.get("scoring", {})
    categories = [
        (c["name"], compile_keywords(c.get("keywords", [])))
        for c in config.get("categories", [])
        if c.get("name")
    ]
    thresholds = scoring.get("priority_thresholds", {})
    return Ruleset(
        categories=categories,
        default_category=report.get("default_category", FALLBACK_CATEGORY),
        high=compile_keywords(scoring.get("high", [])),
        medium=compile_keywords(scoring.get("medium", [])),
        penalty=compile_keywords(scoring.get("penalty", [])),
        high_threshold=float(thresholds.get("high", 3.0)),
        medium_threshold=float(thresholds.get("medium", 1.5)),
    )


def category_names(config: dict[str, Any]) -> list[str]:
    return [c["name"] for c in config.get("categories", []) if c.get("name")]


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def tag_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    # namespace fallback
    for child in list(node):
        local = child.tag.split("}")[-1].lower()
        if local in names and child.text:
            return child.text.strip()
    return ""


def link_text(node: ET.Element) -> str:
    link = tag_text(node, ["link"])
    if link:
        return link
    for child in list(node):
        if child.tag.split("}")[-1].lower() == "link":
            href = child.attrib.get("href")
            if href:
                return href
    return ""


def extract_xml_items(raw: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(raw.encode("utf-8"))
        nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        items = []
        for node in nodes:
            title = tag_text(node, ["title"])
            url = link_text(node)
            published = tag_text(node, ["pubDate", "published", "updated", "dc:date"])
            summary = tag_text(node, ["description", "summary", "content", "encoded"])
            if title and url:
                items.append({"title": title, "url": url, "published": published, "summary": summary})
        return items
    except Exception:
        return []


def extract_regex_items(raw: str) -> list[dict[str, str]]:
    blocks = re.findall(r"(?is)<item\b.*?</item>|<entry\b.*?</entry>", raw)
    items = []
    for block in blocks:
        def pick(*tags: str) -> str:
            for tag in tags:
                m = re.search(rf"(?is)<(?:[\w]+:)?{re.escape(tag)}\b[^>]*>(.*?)</(?:[\w]+:)?{re.escape(tag)}>", block)
                if m:
                    return strip_html(m.group(1))
            return ""
        title = pick("title")
        published = pick("pubDate", "published", "updated", "date")
        summary = pick("description", "summary", "encoded", "content")
        link_match = re.search(r"(?is)<link\b[^>]*href=['\"]([^'\"]+)['\"]", block)
        url = link_match.group(1) if link_match else pick("link")
        if title and url:
            items.append({"title": title, "url": url, "published": published, "summary": summary})
    return items


# ---------------------------------------------------------------------------
# Article page scraping
# ---------------------------------------------------------------------------

PAGE_DATE_PATTERNS = [
    re.compile(r'(?is)"datePublished"\s*:\s*"([^"]+)"'),
    re.compile(r'(?is)<meta\b[^>]*\bproperty=["\']article:published_time["\'][^>]*\bcontent=["\']([^"\']+)["\']'),
    re.compile(r'(?is)<meta\b[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bproperty=["\']article:published_time["\']'),
    re.compile(r'(?is)<meta\b[^>]*\bname=["\']date["\'][^>]*\bcontent=["\']([^"\']+)["\']'),
    re.compile(r'(?is)<time\b[^>]*\bdatetime=["\']([^"\']+)["\']'),
]


def fetch_page(url: str) -> str:
    try:
        return http_get(url, limit=900_000)
    except Exception:
        return ""


def page_excerpt(raw: str) -> str:
    if not raw:
        return ""
    paragraphs = re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", raw)
    text = strip_html("\n".join(paragraphs[:18]) if paragraphs else raw)
    return textwrap.shorten(text, width=1300, placeholder="...")


def page_published_date(raw: str) -> datetime | None:
    """Read a publish date off the article page, for feeds that ship no pubDate."""
    for pattern in PAGE_DATE_PATTERNS:
        m = pattern.search(raw or "")
        if m:
            dt = parse_any_date(m.group(1))
            if dt:
                return dt
    return None


# ---------------------------------------------------------------------------
# Classification and collection
# ---------------------------------------------------------------------------

def classify(rules: Ruleset, title: str, summary: str, excerpt: str, weight: float) -> tuple[str, str, float]:
    # Topic classification intentionally uses title/summary only. Full-page excerpts often
    # contain shared navigation/sidebar text that over-biases categories such as AI.
    head_text = f"{title}\n{summary}".lower()
    body_text = (excerpt or "").lower()

    # Pick the category with the most keyword hits rather than the first that matches,
    # so a piece about AI design tooling lands in UX rather than always in AI.
    topic = rules.default_category
    best_hits = 0
    for name, patterns in rules.categories:
        hits = count_matches(patterns, head_text)
        if hits > best_hits:
            best_hits = hits
            topic = name

    score = 2.0 * weighted_matches(rules.high, head_text, body_text)
    score += 1.0 * weighted_matches(rules.medium, head_text, body_text)
    score -= 0.8 * weighted_matches(rules.penalty, head_text, body_text)
    score *= weight
    if topic == rules.default_category:
        # Matching no category means the piece is off-topic for this report. Keep it
        # eligible, but below anything that classified cleanly.
        score *= 0.75

    if score >= rules.high_threshold:
        priority = "HIGH"
    elif score >= rules.medium_threshold:
        priority = "MEDIUM"
    else:
        priority = "LOW"
    return topic, priority, round(score, 2)


def collect_items(config: dict[str, Any], rules: Ruleset, date_from: str, date_to: str, max_per_source: int = 12) -> tuple[list[ContentItem], list[str]]:
    start = parse_date_start(date_from)
    end = parse_date_end_exclusive(date_to)
    collected_at = now_utc().date().isoformat()
    items: list[ContentItem] = []
    errors: list[str] = []
    seen: set[str] = set()

    for source in config.get("sources", []):
        try:
            raw = http_get(source["url"])
            entries = extract_xml_items(raw) or extract_regex_items(raw)
            date_from_page = bool(source.get("date_from_page"))
            stale_streak = 0
            for entry in entries[:max_per_source]:
                url = clean_url(entry["url"].strip())
                if not url or url in seen:
                    continue
                pub_dt = parse_any_date(entry.get("published"))
                page_raw = ""
                if pub_dt is None and date_from_page:
                    page_raw = fetch_page(url)
                    pub_dt = page_published_date(page_raw)
                    time.sleep(0.3)
                if not pub_dt:
                    continue
                if pub_dt < start:
                    # Only short-circuit feeds whose dates cost an extra page fetch.
                    # Elsewhere the date is free, so scanning the whole window is safer
                    # than assuming the feed is strictly reverse-chronological.
                    if date_from_page:
                        stale_streak += 1
                        if stale_streak >= 3:
                            break
                    continue
                stale_streak = 0
                if pub_dt >= end:
                    continue
                seen.add(url)
                summary = textwrap.shorten(strip_html(entry.get("summary", "")), width=650, placeholder="...")
                if not page_raw:
                    page_raw = fetch_page(url)
                excerpt = page_excerpt(page_raw)
                topic, priority, score = classify(rules, entry["title"], summary, excerpt, float(source.get("weight", 1.0)))
                items.append(ContentItem(
                    source=source["name"],
                    source_url=source["url"],
                    title=strip_html(entry["title"]),
                    url=url,
                    published_at=iso_date(pub_dt),
                    collected_at=collected_at,
                    summary=summary,
                    excerpt=excerpt,
                    language=source.get("language", "en"),
                    score=score,
                    priority=priority,
                    topic=topic,
                ))
                time.sleep(0.3)
        except Exception as e:
            errors.append(f"{source.get('name', source.get('id'))}: {type(e).__name__}: {e}")
    priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda x: (priority_order.get(x.priority, 9), -x.score, x.published_at, x.source))
    return items, errors


def report_url() -> str:
    explicit = os.getenv("REPORT_URL", "").strip()
    if explicit:
        return explicit
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "jellasi/planning_insight").strip()
    run_id = os.getenv("GITHUB_RUN_ID", "").strip()
    return f"{server}/{repo}/actions/runs/{run_id}" if run_id else f"{server}/{repo}/actions"


def load_previous_topics(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(t.get("topic", "")) for t in data.get("top_topics", []) if t.get("topic")}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Topic playbook: role guidance, proposals and checklist items per category.
# Sections of the report are assembled from the topics that actually appeared,
# so the output changes week to week instead of repeating a fixed block.
# ---------------------------------------------------------------------------

TOPIC_PLAYBOOK: dict[str, dict[str, Any]] = {
    "AI 기반 제품·자동화": {
        "implication": "AI 기능을 단순 추가 기능이 아니라 업무 흐름·운영 효율·고객 접점 개선 관점에서 설계할 필요가 있습니다.",
        "question": "우리 서비스에서 AI가 실제로 줄여야 하는 사용자/운영자의 반복 업무는 무엇인가?",
        "roles": {
            "서비스 기획자": "AI가 개입하는 화면은 오탐·실패 케이스와 사람이 개입하는 경로까지 요구사항에 함께 정의합니다.",
            "Product Manager": "AI/자동화는 도입 여부보다 어떤 병목을 줄이는지, 무엇으로 측정할지를 먼저 정의합니다.",
            "Product Owner": "자동 처리 결과의 검수 주체와 예외 처리 기준을 인수 조건에 명시합니다.",
        },
        "proposal": (
            "AI/자동화 후보 업무 목록화",
            "AI 도입 논의를 기능 아이디어가 아닌 업무 병목 기준으로 전환",
            "반복 업무를 처리량·오류율·소요시간 기준으로 정렬해 후보를 추립니다",
            "우선순위가 높은 자동화 과제 도출",
            "Product, Ops, Data, Engineering",
            "처리시간, 수동 처리 건수, 오류율",
        ),
        "checklist": "AI/자동화 기능이라면 줄어드는 수작업과 오류 발생 시 책임 주체가 명확한가?",
    },
    "제품 발견·사용자 리서치": {
        "implication": "요구사항 작성 전 고객 문제와 검증 가설을 명확히 분리해 백로그 품질을 높이는 데 활용할 수 있습니다.",
        "question": "현재 백로그 중 고객 문제 검증 없이 해결책부터 정해진 항목은 무엇인가?",
        "roles": {
            "서비스 기획자": "기획 착수 전 고객 문제의 빈도·강도·대상을 확인한 근거를 문서에 남깁니다.",
            "Product Manager": "발견 활동을 별도 프로젝트가 아니라 스프린트에 상시 포함되는 활동으로 배치합니다.",
            "Product Owner": "스토리마다 검증하려는 가설과 기각 조건을 함께 적습니다.",
        },
        "proposal": (
            "기획안 1페이지 문제 정의 추가",
            "해결책 중심 기획으로 인한 우선순위 혼선을 줄임",
            "모든 신규 기획안 상단에 고객 문제·가설·성공 지표를 1페이지로 정리합니다",
            "기획 리뷰 속도와 의사결정 품질 향상",
            "Product, Design, Data",
            "기획안 반려율, 리뷰 리드타임",
        ),
        "checklist": "문제의 빈도, 강도, 대상 고객은 실제 데이터나 인터뷰로 확인되었는가?",
    },
    "제품 지표·실험·성장": {
        "implication": "기능 출시 후 성공 여부를 판단할 지표와 실험 설계를 사전에 정의하는 실무 기준으로 활용할 수 있습니다.",
        "question": "이번 분기 기능 중 성공/실패를 판정할 지표가 정의되지 않은 것은 무엇인가?",
        "roles": {
            "서비스 기획자": "화면·플로우 설계 시 어떤 이벤트를 심어야 측정이 가능한지 함께 정의합니다.",
            "Product Manager": "기능 단위가 아니라 지표 단위로 로드맵을 검토해 중복 투자를 걸러냅니다.",
            "Product Owner": "인수 조건에 측정 이벤트와 기대 수치를 포함해 출시 후 판정이 가능하게 합니다.",
        },
        "proposal": (
            "출시 전 성공 지표 사전 정의",
            "출시 후 성패를 판단할 근거가 없어 학습이 누적되지 않는 문제",
            "기능 착수 시점에 목표 지표·측정 이벤트·판정 기준을 함께 확정합니다",
            "출시 회고에서 재현 가능한 학습 확보",
            "Product, Data, Engineering",
            "지표 정의율, 출시 후 판정 완료율",
        ),
        "checklist": "실험 기간과 최소 표본 수, 판정 시점이 사전에 합의되었는가?",
    },
    "제품 전략·로드맵": {
        "implication": "로드맵 항목을 산출물이 아니라 고객 문제·사업 우선순위·검증 지표 중심으로 재정렬하는 데 참고할 수 있습니다.",
        "question": "이번 분기 로드맵 항목은 각각 어떤 지표 변화를 만들기 위한 것인가?",
        "roles": {
            "서비스 기획자": "개별 기획을 상위 전략·목표와 연결해 우선순위 근거를 설명할 수 있게 합니다.",
            "Product Manager": "로드맵 항목을 고객 문제, 사업 임팩트, 검증 지표 기준으로 재정렬합니다.",
            "Product Owner": "분기 목표에 기여하지 않는 백로그 항목을 정리하거나 보류로 옮깁니다.",
        },
        "proposal": (
            "로드맵 항목별 목표 지표 연결",
            "산출물 나열식 로드맵으로 인한 우선순위 논쟁",
            "로드맵 각 항목에 목표 지표와 근거를 1줄씩 붙여 재정렬합니다",
            "우선순위 논의 시간 단축, 투자 근거 명확화",
            "Product, Business, Data",
            "분기 목표 달성률, 로드맵 변경 횟수",
        ),
        "checklist": "이 항목이 상위 전략·분기 목표 중 무엇에 기여하는가?",
    },
    "기능·정책 설계": {
        "implication": "정상 흐름뿐 아니라 예외·운영·어드민 케이스까지 포함해야 출시 후 운영 부담을 줄일 수 있습니다.",
        "question": "이 기획에서 아직 정의되지 않은 예외·운영 케이스는 무엇인가?",
        "roles": {
            "서비스 기획자": "정책, 화면, 프로세스, 운영 예외 케이스를 요구사항에 명시하고 누락 지점을 점검합니다.",
            "Product Manager": "정책 변경이 기존 고객·데이터에 미치는 영향 범위를 사전에 확인합니다.",
            "Product Owner": "인수 조건에 정상/예외/운영자 케이스를 구분해 기재합니다.",
        },
        "proposal": (
            "백로그 인수 조건 템플릿 정비",
            "개발 착수 후 해석 차이로 생기는 재작업",
            "주요 스토리에 정상/예외/운영자 케이스와 측정 이벤트를 포함시킵니다",
            "QA 누락과 운영 이슈 감소",
            "Product, Engineering, QA, Ops",
            "재오픈 이슈 수, QA 결함 수",
        ),
        "checklist": "정상 케이스 외 예외/운영/어드민 케이스가 정의되었는가?",
    },
    "UX·고객 경험": {
        "implication": "화면 단위 개선보다 전체 고객 여정과 예외 케이스까지 포함한 경험 설계 관점이 필요합니다.",
        "question": "고객 여정에서 이탈이 가장 많이 발생하는 지점은 어디이고 원인은 무엇인가?",
        "roles": {
            "서비스 기획자": "단일 화면이 아니라 진입-완료까지의 여정 기준으로 누락 지점을 점검합니다.",
            "Product Manager": "UX 개선 과제도 지표 가설을 세워 우선순위를 매깁니다.",
            "Product Owner": "접근성·오류 메시지·빈 상태 같은 세부 상태를 인수 조건에 포함합니다.",
        },
        "proposal": (
            "핵심 여정 이탈 지점 점검",
            "화면 단위 개선이 전체 전환율로 이어지지 않는 문제",
            "핵심 여정 1개를 골라 단계별 이탈률과 원인 가설을 정리합니다",
            "개선 우선순위의 근거 확보",
            "Product, Design, Data",
            "단계별 이탈률, 완료율",
        ),
        "checklist": "핵심 여정의 빈 상태·오류 상태·재시도 경로가 설계되었는가?",
    },
    "조직 운영·협업": {
        "implication": "팀 협업 방식, 의사결정 기준, 운영 프로세스 개선 논의의 참고 자료로 활용할 수 있습니다.",
        "question": "지금 우리 팀에서 의사결정이 가장 자주 지연되는 지점은 어디인가?",
        "roles": {
            "서비스 기획자": "리뷰 단계에서 반복되는 질문을 템플릿에 반영해 재작업을 줄입니다.",
            "Product Manager": "의사결정 주체와 기준을 문서화해 논의가 매번 원점으로 돌아가지 않게 합니다.",
            "Product Owner": "개발 협업 전 요구사항의 범위와 비범위를 명확히 해 재작업을 줄입니다.",
        },
        "proposal": (
            "의사결정 기준 문서화",
            "같은 논의가 반복되며 결정이 지연되는 문제",
            "자주 부딪히는 결정 유형별로 주체와 판단 기준을 한 페이지로 정리합니다",
            "리뷰 리드타임 단축",
            "Product, Design, Engineering",
            "결정 리드타임, 재논의 횟수",
        ),
        "checklist": "이 건의 최종 의사결정 주체와 판단 기준이 합의되었는가?",
    },
    "국내외 제품 사례": {
        "implication": "외부 사례는 화면 패턴보다 문제 정의·운영 조건·제약사항을 먼저 비교해야 우리 맥락에 적용할 수 있습니다.",
        "question": "이 사례의 전제 조건 중 우리 상황과 다른 것은 무엇인가?",
        "roles": {
            "서비스 기획자": "사례의 화면을 그대로 옮기기 전에 그 팀이 풀던 문제와 제약을 먼저 비교합니다.",
            "Product Manager": "사례의 성과 수치는 조직 규모·시장 조건과 함께 해석합니다.",
            "Product Owner": "차용할 부분을 작은 단위로 쪼개 우선 검증합니다.",
        },
        "proposal": (
            "사례 스터디 결과 백로그 반영",
            "좋은 사례를 읽고도 실무에 반영되지 않는 문제",
            "사례별로 우리 서비스에 적용 가능한 항목 1개를 백로그 후보로 등록합니다",
            "외부 학습의 실행 전환",
            "Product, Design",
            "사례 기반 백로그 등록 수",
        ),
        "checklist": "참고한 사례의 산업·조직 규모·제약이 우리와 어떻게 다른가?",
    },
    FALLBACK_CATEGORY: {
        "implication": "분류 기준에 정확히 들어맞지 않는 자료로, 팀 논의 시 참고 자료 수준으로 활용하는 것이 적절합니다.",
        "question": "이 인사이트를 다음 스프린트 또는 기획 리뷰에서 어떻게 작게 검증할 수 있는가?",
        "roles": {},
        "proposal": None,
        "checklist": "",
    },
}

BASE_ROLE_GUIDANCE = {
    "서비스 기획자": "정책, 화면, 프로세스, 운영 예외 케이스를 요구사항에 명시하고 고객 여정 기준으로 누락 지점을 점검합니다.",
    "Product Manager": "로드맵 항목을 고객 문제, 사업 임팩트, 검증 지표 기준으로 재정렬합니다.",
    "Product Owner": "백로그에는 사용자 가치, 인수 조건, 예외 케이스, 측정 지표를 함께 포함합니다.",
}

BASE_CHECKLIST = [
    "이 기획은 어떤 고객 문제를 해결하는가?",
    "출시 후 성공 여부를 어떤 지표로 판단할 것인가?",
    "이번 스프린트에서 가장 작게 검증할 수 있는 가설은 무엇인가?",
]

ROLES = ["서비스 기획자", "Product Manager", "Product Owner"]


def playbook(topic: str) -> dict[str, Any]:
    return TOPIC_PLAYBOOK.get(topic, TOPIC_PLAYBOOK[FALLBACK_CATEGORY])


def implication_for(item: ContentItem) -> str:
    return playbook(item.topic)["implication"]


def discussion_question(item: ContentItem) -> str:
    return playbook(item.topic)["question"]


def caution_for(item: ContentItem) -> str:
    if item.priority == "LOW":
        return "광고성·일반론 가능성이 있으므로 바로 적용하기보다 내부 맥락과 맞는지 확인이 필요합니다."
    return "원문 사례의 산업·조직 규모가 우리 상황과 다를 수 있으므로 그대로 복제하지 말고 문제 정의와 지표를 먼저 맞춰야 합니다."


def ordered_topics(items: list[ContentItem]) -> list[str]:
    topics: list[str] = []
    for item in items:
        if item.topic not in topics:
            topics.append(item.topic)
    return topics


def role_guidance(topics: list[str]) -> dict[str, list[str]]:
    """Base guidance per role, plus a line for each topic that appeared this week."""
    guidance: dict[str, list[str]] = {role: [BASE_ROLE_GUIDANCE[role]] for role in ROLES}
    for topic in topics:
        for role, line in playbook(topic).get("roles", {}).items():
            if role in guidance and line not in guidance[role]:
                guidance[role].append(line)
    return guidance


def proposals_for(topics: list[str], limit: int = 3) -> list[tuple[str, ...]]:
    proposals: list[tuple[str, ...]] = []
    for topic in topics:
        proposal = playbook(topic).get("proposal")
        if proposal and proposal not in proposals:
            proposals.append(proposal)
        if len(proposals) >= limit:
            break
    if not proposals:
        proposals.append(playbook("제품 발견·사용자 리서치")["proposal"])
    return proposals


def checklist_for(topics: list[str]) -> list[str]:
    checklist = list(BASE_CHECKLIST)
    for topic in topics:
        line = playbook(topic).get("checklist")
        if line and line not in checklist:
            checklist.append(line)
    return checklist


def observations(topics: list[str], previous_topics: set[str], all_categories: list[str]) -> list[str]:
    lines: list[str] = []
    recurring = [t for t in topics if t in previous_topics]
    fresh = [t for t in topics if t not in previous_topics]
    missing = [c for c in all_categories if c not in topics]
    if recurring:
        lines.append(f"{', '.join(recurring)}: 이전 리포트에 이어 다시 등장했습니다. 단발성 트렌드가 아닌지, 실제 지표 개선 사례로 이어지는지 계속 확인합니다.")
    if fresh:
        lines.append(f"{', '.join(fresh)}: 이번 기간에 새로 관찰된 주제입니다. 다음 리포트에서도 이어지는지 확인이 필요합니다.")
    if missing:
        lines.append(f"{', '.join(missing[:4])}: 이번 기간 수집 자료에서 확인되지 않았습니다. 소스 보강이 필요한지 점검합니다.")
    lines.append("단순 홍보성 콘텐츠와 실무 적용 가능한 사례를 계속 분리해 평가 필요")
    return lines


# ---------------------------------------------------------------------------
# Selection and rendering
# ---------------------------------------------------------------------------

def select_items(items: list[ContentItem], limit: int, per_topic: int = 1) -> list[ContentItem]:
    """Pick top-scoring items, spreading across topics before doubling up on any one.

    Items arrive sorted by priority then score, so each pass takes the strongest
    remaining item per topic. Widening the allowance one pass at a time keeps a
    single prolific topic from filling the whole report.
    """
    selected: list[ContentItem] = []
    chosen: set[str] = set()
    counts: dict[str, int] = {}
    for allowance in range(1, max(per_topic, 1) + 1):
        for item in items:
            if item.url in chosen:
                continue
            if counts.get(item.topic, 0) >= allowance:
                continue
            selected.append(item)
            chosen.add(item.url)
            counts[item.topic] = counts.get(item.topic, 0) + 1
            if len(selected) >= limit:
                return selected
    return selected


def related_titles(items: list[ContentItem], topic: str, limit: int = 4) -> list[str]:
    titles = []
    for item in items:
        if item.topic == topic and item.title not in titles:
            titles.append(item.title)
        if len(titles) >= limit:
            break
    return titles


def md_escape(text: str) -> str:
    """Neutralise characters that would break a markdown link label."""
    return text.replace("[", "\\[").replace("]", "\\]")


def md_link(url: str, label: str) -> str:
    return f"[{md_escape(label)}]({url})"


def markdown_report(items: list[ContentItem], top: list[ContentItem], errors: list[str], config: dict[str, Any], date_from: str, date_to: str, previous_topics: set[str]) -> str:
    period = f"{date_from} ~ {date_to}"
    report_date = now_utc().date().isoformat()
    target = config.get("report", {}).get("target_audience", "서비스 기획자, PM, PO")
    all_categories = category_names(config)
    topics = ordered_topics(top)
    lines: list[str] = [
        f"# {period} 서비스 기획·PM·PO 인사이트 리포트",
        "",
        "## 리포트 정보",
        f"- 리포트 기간: {period}",
        f"- 작성 기준일: {report_date}",
        f"- 주요 독자: {target}",
        f"- 분류 기준 주제: {', '.join(all_categories)}",
        f"- 수집 건수: 전체 {len(items)}건 중 {len(top)}건 선정",
        f"- 이전 리포트: {'있음' if previous_topics else '확인 필요'}",
        f"- 상세 리포트 URL: {report_url()}",
        "",
        "## 1. Executive Summary",
    ]
    if top:
        for best in top[:3]:
            status = "지속 이슈" if best.topic in previous_topics else "신규 관찰"
            titles = "; ".join(related_titles(items, best.topic, limit=3))
            lines.append(f"- {best.topic}: {titles} 등을 통해 {status}로 확인되었습니다. {implication_for(best)}")
    else:
        lines.append("- 이번 기간 입력 데이터 기준 유의미한 PM·PO 인사이트가 확인되지 않았습니다.")

    lines += ["", "## 2. 주요 인사이트"]
    if not top:
        lines += ["- 유의미한 자료 없음. 억지 인사이트를 생성하지 않습니다.", ""]
    for item in top:
        status = "지속" if item.topic in previous_topics else "신규"
        lines += [
            f"### [{item.topic}] {md_link(item.url, item.title)}",
            f"- 바로가기: {item.url}",
            f"- 중요도: {item.priority}",
            f"- 핵심 내용: {item.summary or item.excerpt or '요약 확인 필요'}",
            f"- 등장 배경: {item.source}에 {item.published_at} 발행된 콘텐츠로 수집되었습니다. 관련 수집 자료: {'; '.join(related_titles(items, item.topic, limit=4))}. 이전 리포트 대비 구분: {status}.",
            f"- 실무적으로 중요한 이유: {implication_for(item)}",
            "- 적용 가능한 업무: 기획 리뷰, 백로그 정리, 로드맵 논의, 요구사항 작성, 실험/지표 설계",
            f"- 적용 시 주의사항: {caution_for(item)}",
            f"- 팀에서 논의할 질문: {discussion_question(item)}",
            f"- 출처: {item.source}, 발행일 {item.published_at}",
            "",
        ]

    lines += ["## 3. 역할별 시사점", ""]
    for role, bullets in role_guidance(topics).items():
        lines.append(f"### {role}")
        lines += [f"- {b}" for b in bullets]
        lines.append("")

    lines.append("## 4. 실무 적용 제안")
    for p in proposals_for(topics):
        lines += [
            f"### {p[0]}",
            f"- 해결하려는 문제: {p[1]}",
            f"- 적용 방법: {p[2]}",
            f"- 기대 효과: {p[3]}",
            f"- 필요한 협업 조직: {p[4]}",
            f"- 확인할 지표: {p[5]}",
            "",
        ]

    lines += ["## 5. 체크리스트 또는 프레임워크"]
    lines += [f"- {c}" for c in checklist_for(topics)]
    lines += ["", "## 6. 추가 관찰 주제"]
    lines += [f"- {o}" for o in observations(topics, previous_topics, all_categories)]
    lines += ["", "## 7. 출처"]
    for item in top:
        lines.append(f"- {md_link(item.url, item.title)} — {item.source}, {item.published_at}")
    if errors:
        lines += ["", "## 수집 오류", *[f"- {e}" for e in errors]]
    return "\n".join(lines).strip() + "\n"


def slack_escape(text: str) -> str:
    """Slack mrkdwn requires &, < and > to be entity-escaped inside message text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_link(url: str, label: str) -> str:
    return f"<{url}|{slack_escape(label)}>"


def slack_message(top: list[ContentItem], date_from: str, date_to: str) -> str:
    """Compose the channel message.

    Slack mrkdwn is not markdown: bold is *single* asterisks and links are
    <url|label>. Markdown syntax renders literally, so it is not used here.
    """
    period = f"{date_from}~{date_to}"
    header = [f"*📌 PM·PO 인사이트 | {period}*", "", "*이번 주 핵심*"]
    blocks: list[list[str]] = []
    for item in top:
        icon = "💡 " if item.priority == "HIGH" else ""
        title = textwrap.shorten(item.title, width=80, placeholder="...")
        implication = textwrap.shorten(implication_for(item), width=105, placeholder="...")
        blocks.append([
            f"• {icon}*[{slack_escape(item.topic)}]* {slack_link(item.url, title)}",
            f"    ↳ {slack_escape(implication)}",
        ])
    if not blocks:
        blocks.append(["• 이번 기간 입력 데이터 기준 유의미한 자료 없음"])

    footer = ["", "*실무 적용 제안*"]
    for p in proposals_for(ordered_topics(top), limit=2):
        footer.append(f"• {slack_escape(p[0])}: {slack_escape(p[2])}")
    footer += ["", f"🔗 {slack_link(report_url(), '전체 리포트 보기')}"]

    # Trim whole items rather than slicing mid-string, which would break a link.
    while True:
        msg = "\n".join(header + [line for block in blocks for line in block] + footer)
        if len(msg) <= SLACK_CHAR_LIMIT or len(blocks) <= 1:
            return msg
        blocks.pop()


def build_json_report(top: list[ContentItem], md: str, slack: str, date_from: str, date_to: str) -> dict[str, Any]:
    period = f"{date_from} ~ {date_to}"
    top_topics = []
    for item in top:
        top_topics.append({
            "topic": item.topic,
            "priority": item.priority,
            "title": item.title,
            "summary": f"{item.title}: {item.summary or item.excerpt or '요약 확인 필요'}",
            "practical_implication": implication_for(item),
            "source": item.source,
            "published_at": item.published_at,
            "source_url": item.url,
        })
    requires_discussion = any(t["priority"] == "HIGH" for t in top_topics) or len(top_topics) >= 3
    executive = "\n".join([f"- {t['topic']}: {t['practical_implication']}" for t in top_topics[:3]]) or "이번 기간 유의미한 자료가 확인되지 않았습니다."
    return {
        "report_title": f"{period} 서비스 기획·PM·PO 인사이트 리포트",
        "report_period": period,
        "executive_summary": executive,
        "top_topics": top_topics,
        "detailed_report_markdown": md,
        "slack_message_markdown": slack,
        "requires_team_discussion": requires_discussion,
    }


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_slack(text: str) -> None:
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    channel_id = os.getenv("SLACK_CHANNEL_ID", "").strip()
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if bot_token and channel_id:
        payload = json.dumps({"channel": channel_id, "text": text, "unfurl_links": False, "unfurl_media": False}).encode("utf-8")
        req = Request("https://slack.com/api/chat.postMessage", data=payload, headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT}, method="POST")
        with urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            if resp.status >= 300 or not data.get("ok"):
                raise RuntimeError(f"Slack bot API failed: {data.get('error', resp.status)}")
        print("Slack bot notification sent")
        return
    if webhook:
        payload = json.dumps({"text": text}).encode("utf-8")
        req = Request(webhook, data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
        with urlopen(req, timeout=20) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Slack webhook failed: HTTP {resp.status}")
        print("Slack webhook notification sent")
        return
    print("Slack secrets not set; skip Slack notification")


def send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    mail_from = os.getenv("EMAIL_FROM", username).strip()
    mail_to = os.getenv("EMAIL_TO", "").strip()
    if not host or not mail_to or not mail_from:
        print("SMTP_HOST/EMAIL_TO/EMAIL_FROM not fully set; skip email notification")
        return
    port = int(os.getenv("SMTP_PORT") or "587")
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body)
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
            if username or password:
                smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if username or password:
                smtp.login(username, password)
            smtp.send_message(msg)
    print("Email notification sent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect and report PM/PO product insights.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--period-from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--period-to", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--slack-out", type=Path, default=DEFAULT_SLACK)
    args = parser.parse_args(argv)

    config = json.loads(args.config.read_text(encoding="utf-8"))
    rules = build_ruleset(config)
    report_cfg = config.get("report", {})
    previous_topics = load_previous_topics(args.json_out)

    items, errors = collect_items(config, rules, args.period_from, args.period_to)

    # Items matching no category are off-topic for this report; keep them out of the
    # selection unless nothing else was collected.
    candidates = items
    if not report_cfg.get("include_uncategorized", False):
        classified = [i for i in items if i.topic != rules.default_category]
        if classified:
            candidates = classified

    top = select_items(
        candidates,
        limit=int(report_cfg.get("max_items_total", 10)),
        per_topic=int(report_cfg.get("max_items_per_topic", 2)),
    )
    slack_top = select_items(candidates, limit=int(report_cfg.get("slack_max_items", 4)), per_topic=1)

    md = markdown_report(items, top, errors, config, args.period_from, args.period_to, previous_topics)
    slack = slack_message(slack_top, args.period_from, args.period_to)
    result = build_json_report(top, md, slack, args.period_from, args.period_to)

    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_out.write_text(md, encoding="utf-8")
    args.slack_out.write_text(slack + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Collected content: {len(items)} / selected: {len(top)}")
    for topic in ordered_topics(top):
        print(f"- {topic}: {sum(1 for i in top if i.topic == topic)}건")
    if errors:
        print("Collection errors:")
        for err in errors:
            print(f"- {err}")
    if args.notify:
        send_slack(slack)
        send_email(result["report_title"], md)
    else:
        print("No notification sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
