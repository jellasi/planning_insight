#!/usr/bin/env python
"""Weekly PM/PO product-insight report generator.

- Collects product/planning articles from RSS/Atom feeds.
- Scrapes article excerpts when possible.
- Produces a strict JSON report plus markdown artifacts.
- Sends Slack bot/webhook and SMTP email notifications in GitHub Actions.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "sources.json"
DEFAULT_REPORT_JSON = ROOT / "last_report.json"
DEFAULT_REPORT_MD = ROOT / "last_report.md"
DEFAULT_SLACK = ROOT / "last_slack_message.md"
DEFAULT_SLACK_THREAD = ROOT / "last_slack_thread.json"

USER_AGENT = "Mozilla/5.0 PlanningInsightBot/1.0 (+https://github.com/jellasi/planning_insight)"

PM_KEYWORDS = {
    "HIGH": [
        "product strategy", "roadmap", "prioritization", "pricing", "metrics", "experimentation",
        "a/b test", "growth", "retention", "activation", "ai product", "agent", "automation",
        "customer research", "discovery", "north star", "gtm", "go-to-market",
        "제품 전략", "로드맵", "우선순위", "실험", "지표", "리텐션", "고객 문제", "AI",
    ],
    "MEDIUM": [
        "product management", "product manager", "product owner", "ux", "user experience",
        "customer experience", "collaboration", "stakeholder", "requirements", "backlog",
        "사용자 경험", "협업", "요구사항", "백로그", "정책", "운영", "프로세스",
    ],
}

CATEGORY_RULES = [
    ("AI 기반 제품·자동화", ["ai", "agent", "llm", "automation", "automate", "copilot", "인공지능", "자동화"]),
    ("제품 발견·사용자 리서치", ["discovery", "research", "interview", "customer problem", "user research", "고객 문제", "리서치", "인터뷰"]),
    ("제품 전략·로드맵", ["strategy", "roadmap", "prioritization", "vision", "portfolio", "전략", "로드맵", "우선순위"]),
    ("제품 지표·실험·성장", ["metrics", "experiment", "a/b", "growth", "retention", "activation", "funnel", "지표", "실험", "성장"]),
    ("UX·고객 경험", ["ux", "user experience", "customer experience", "journey", "design", "사용자 경험", "고객 경험"]),
    ("조직 운영·협업", ["team", "collaboration", "stakeholder", "leadership", "meeting", "culture", "협업", "조직", "회의"]),
]


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


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text or "")
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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


def article_excerpt(url: str) -> str:
    try:
        raw = http_get(url, limit=900_000)
    except Exception:
        return ""
    paragraphs = re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", raw)
    text = strip_html("\n".join(paragraphs[:18]) if paragraphs else raw)
    return textwrap.shorten(text, width=1300, placeholder="...")


def kw_in(kw: str, text: str) -> bool:
    kw = kw.lower()
    if re.fullmatch(r"[a-z0-9/ \-]+", kw):
        # 단어 단위(복수형 s 허용): 'ai' 는 email·detail 에 걸리지 않고, 'team' 은 teams 에도 걸린다.
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"s?(?![a-z0-9])", text) is not None
    return kw in text


def classify(title: str, summary: str, excerpt: str, weight: float) -> tuple[str, str, float]:
    # Topic classification intentionally uses title/summary only. Full-page excerpts often
    # contain shared navigation/sidebar text that over-biases categories such as AI.
    topic_text = f"{title}\n{summary}".lower()
    score_text = f"{title}\n{summary}\n{excerpt}".lower()
    score = 0.0
    for kw in PM_KEYWORDS["HIGH"]:
        if kw_in(kw, score_text):
            score += 2.0
    for kw in PM_KEYWORDS["MEDIUM"]:
        if kw_in(kw, score_text):
            score += 1.0
    if "sponsored" in score_text or "webinar" in score_text or "event" in score_text:
        score -= 0.8
    score *= weight
    if score >= 4.0:
        priority = "HIGH"
    elif score >= 1.6:
        priority = "MEDIUM"
    else:
        priority = "LOW"
    topic = "서비스 및 제품 전략"
    for name, kws in CATEGORY_RULES:
        if any(kw_in(kw, topic_text) for kw in kws):
            topic = name
            break
    return topic, priority, round(score, 2)


def collect_items(config: dict[str, Any], date_from: str, date_to: str, max_per_source: int = 12) -> tuple[list[ContentItem], list[str]]:
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
            for entry in entries[:max_per_source]:
                pub_dt = parse_any_date(entry.get("published"))
                if not pub_dt or not (start <= pub_dt < end):
                    continue
                url = entry["url"].strip()
                if url in seen:
                    continue
                seen.add(url)
                summary = textwrap.shorten(strip_html(entry.get("summary", "")), width=650, placeholder="...")
                excerpt = article_excerpt(url)
                topic, priority, score = classify(entry["title"], summary, excerpt, float(source.get("weight", 1.0)))
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


def implication_for(item: ContentItem) -> str:
    if item.topic == "AI 기반 제품·자동화":
        return "AI 기능을 단순 추가 기능이 아니라 업무 흐름·운영 효율·고객 접점 개선 관점에서 설계할 필요가 있습니다."
    if item.topic == "제품 발견·사용자 리서치":
        return "요구사항 작성 전 고객 문제와 검증 가설을 명확히 분리해 백로그 품질을 높이는 데 활용할 수 있습니다."
    if item.topic == "제품 전략·로드맵":
        return "로드맵 항목을 산출물이 아니라 고객 문제·사업 우선순위·검증 지표 중심으로 재정렬하는 데 참고할 수 있습니다."
    if item.topic == "제품 지표·실험·성장":
        return "기능 출시 후 성공 여부를 판단할 지표와 실험 설계를 사전에 정의하는 실무 기준으로 활용할 수 있습니다."
    if item.topic == "UX·고객 경험":
        return "화면 단위 개선보다 전체 고객 여정과 예외 케이스까지 포함한 경험 설계 관점이 필요합니다."
    if item.topic == "조직 운영·협업":
        return "팀 협업 방식, 의사결정 기준, 운영 프로세스 개선 논의의 참고 자료로 활용할 수 있습니다."
    return "다른 팀이 무엇을 만들지·만들지 않을지 어떤 기준으로 정했는지 보는 사례로, 우리 기획 우선순위 판단에 비교 기준으로 쓸 수 있습니다."


def caution_for(item: ContentItem) -> str:
    if item.priority == "LOW":
        return "광고성·일반론 가능성이 있으므로 바로 적용하기보다 내부 맥락과 맞는지 확인이 필요합니다."
    return "원문 사례의 산업·조직 규모가 우리 상황과 다를 수 있으므로 그대로 복제하지 말고 문제 정의와 지표를 먼저 맞춰야 합니다."


def discussion_question(item: ContentItem) -> str:
    if item.topic == "AI 기반 제품·자동화":
        return "우리 서비스에서 AI가 실제로 줄여야 하는 사용자/운영자의 반복 업무는 무엇인가?"
    if item.topic == "제품 발견·사용자 리서치":
        return "현재 백로그 중 고객 문제 검증 없이 해결책부터 정해진 항목은 무엇인가?"
    if item.topic == "제품 전략·로드맵":
        return "이번 분기 로드맵 항목은 어떤 지표 변화를 만들기 위한 것인가?"
    if item.topic == "제품 지표·실험·성장":
        return "지금 진행 중인 기능 중 성공 기준(지표·기간)을 정하지 않고 시작한 것은 무엇인가?"
    if item.topic == "UX·고객 경험":
        return "우리 고객 여정에서 이탈이 가장 큰 단계는 어디이고, 그 이유를 확인한 적이 있는가?"
    if item.topic == "조직 운영·협업":
        return "지금 일이 막히는 지점은 사람 수 문제인가, 결정·인수인계 방식 문제인가?"
    return "이 사례의 판단 기준을 우리 다음 기획 리뷰에 그대로 대 보면 무엇이 달라지는가?"


TOPIC_PROPOSALS = {
    "AI 기반 제품·자동화": ("AI/자동화 후보 업무 목록화", "반복 업무를 처리량·오류율·소요시간 기준으로 정렬해 자동화 후보 1~2개를 고릅니다.", "처리시간, 수동 처리 건수, 오류율"),
    "제품 발견·사용자 리서치": ("백로그 가설 표시", "백로그 항목마다 '검증된 문제 / 아직 가설' 표시를 붙이고 가설 항목은 고객 인터뷰 5건으로 먼저 확인합니다.", "가설 검증 비율, 재작업 건수"),
    "제품 전략·로드맵": ("로드맵 항목에 목표 지표 달기", "이번 분기 로드맵 항목마다 바꾸려는 지표 한 개와 목표치를 적습니다.", "지표가 정해진 항목 비율"),
    "제품 지표·실험·성장": ("출시 전 성공 기준 정하기", "새 기능 기획서에 성공 지표·측정 이벤트·판단 시점을 출시 전에 적습니다.", "사후 판단 가능 기능 비율"),
    "UX·고객 경험": ("이탈 단계 점검", "핵심 플로우 단계별 이탈률을 뽑고 가장 큰 단계 하나를 다음 개선 대상으로 정합니다.", "단계별 이탈률"),
    "조직 운영·협업": ("인수 조건 템플릿 정비", "주요 스토리에 정상/예외/운영자 케이스와 측정 이벤트를 포함합니다.", "재오픈 이슈 수, QA 결함 수"),
    "서비스 및 제품 전략": ("기획안 1페이지 문제 정의", "신규 기획안 상단에 고객 문제·가설·성공 지표를 1페이지로 정리합니다.", "기획안 반려율, 리뷰 리드타임"),
}


def proposals_for(top: list[ContentItem], limit: int = 3) -> list[tuple[str, str, str, str]]:
    """이번 주 고른 글의 주제에 맞춰 실무 적용 제안을 만든다(매주 같은 2줄이 나오지 않게)."""
    out: list[tuple[str, str, str, str]] = []
    for item in top:
        p = TOPIC_PROPOSALS.get(item.topic)
        if p and p[0] not in [x[0] for x in out]:
            out.append((p[0], p[1], p[2], item.title))
        if len(out) >= limit:
            break
    return out or [(*TOPIC_PROPOSALS["서비스 및 제품 전략"], "")]


def key_points(item: ContentItem, limit: int = 2) -> list[str]:
    """RSS 요약·본문 앞부분에서 핵심 문장 두 개."""
    text = item.summary or item.excerpt or ""
    text = re.sub(r"^(Listen now|Watch now)\s*(\([^)]*\))?\s*\|?\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"^[^\w]+", "", text)
    parts = [x.strip() for x in re.split(r"(?<=[.!?。])\s+", text) if len(x.strip()) > 20]
    return [textwrap.shorten(x, width=220, placeholder="...") for x in parts[:limit]] or ([textwrap.shorten(text, 220, placeholder="...")] if text else [])


def full_report_url() -> str:
    explicit = os.getenv("FULL_REPORT_URL", "").strip()
    if explicit:
        return explicit
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "jellasi/planning_insight").strip()
    return f"{server}/{repo}/blob/main/last_report.md"


def slack_escape(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")




def unique_topic_items(items: list[ContentItem], limit: int = 5) -> list[ContentItem]:
    """Pick one representative per topic, preserving score order."""
    selected: list[ContentItem] = []
    seen: set[str] = set()
    for item in items:
        if item.topic in seen:
            continue
        selected.append(item)
        seen.add(item.topic)
        if len(selected) >= limit:
            break
    return selected


def related_titles(items: list[ContentItem], topic: str, limit: int = 4) -> list[str]:
    titles = []
    for item in items:
        if item.topic == topic and item.title not in titles:
            titles.append(item.title)
        if len(titles) >= limit:
            break
    return titles


def markdown_report(items: list[ContentItem], errors: list[str], config: dict[str, Any], date_from: str, date_to: str, previous_topics: set[str]) -> str:
    period = f"{date_from} ~ {date_to}"
    report_date = now_utc().date().isoformat()
    target = config.get("report", {}).get("target_audience", "서비스 기획자, PM, PO")
    focus = ", ".join(config.get("report", {}).get("focus_areas", []))
    top = unique_topic_items(items, limit=5)
    lines: list[str] = [
        f"# {period} 서비스 기획·PM·PO 인사이트 리포트",
        "",
        "## 리포트 정보",
        f"- 리포트 기간: {period}",
        f"- 작성 기준일: {report_date}",
        f"- 주요 독자: {target}",
        f"- 관심 주제: {focus}",
        f"- 이전 리포트: {'있음' if previous_topics else '확인 필요'}",
        f"- 실행 기록: {report_url()}",
        "",
        "## 1. Executive Summary",
    ]
    if top:
        for best in top[:3]:
            topic = best.topic
            status = "지속 이슈" if topic in previous_topics else "신규 관찰"
            titles = "; ".join(related_titles(items, topic, limit=3))
            lines.append(f"- {topic}: {titles} 등을 통해 {status}로 확인되었습니다. {implication_for(best)}")
    else:
        lines.append("- 이번 기간 입력 데이터 기준 유의미한 PM·PO 인사이트가 확인되지 않았습니다.")

    lines += ["", "## 2. 주요 인사이트"]
    if not top:
        lines += ["- 유의미한 자료 없음. 억지 인사이트를 생성하지 않습니다.", ""]
    for item in top:
        status = "지속" if item.topic in previous_topics else "신규"
        lines += [
            f"### [{item.topic}]",
            f"- 중요도: {item.priority}",
            f"- 핵심 내용: {item.title}",
            *[f"  - {kp}" for kp in key_points(item, limit=3)],
            f"- 등장 배경: {item.source}에 {item.published_at} 발행된 콘텐츠로 수집되었습니다. 관련 수집 자료: {'; '.join(related_titles(items, item.topic, limit=4))}. 이전 리포트 대비 구분: {status}.",
            f"- 실무적으로 중요한 이유: {implication_for(item)}",
            f"- 적용 가능한 업무: 기획 리뷰, 백로그 정리, 로드맵 논의, 요구사항 작성, 실험/지표 설계",
            f"- 적용 시 주의사항: {caution_for(item)}",
            f"- 팀에서 논의할 질문: {discussion_question(item)}",
            f"- 출처: {item.title}, {item.source}, {item.url}",
            f"- 발행일: {item.published_at}",
            "",
        ]

    lines += [
        "## 3. 역할별 시사점",
        "",
        "### 서비스 기획자",
        "- 정책, 화면, 프로세스, 운영 예외 케이스를 요구사항에 명시하고 고객 여정 기준으로 누락 지점을 점검합니다.",
        "- 외부 사례는 화면 패턴보다 문제 정의·운영 조건·제약사항을 먼저 비교합니다.",
        "",
        "### Product Manager",
        "- 로드맵 항목을 고객 문제, 사업 임팩트, 검증 지표 기준으로 재정렬합니다.",
        "- AI/자동화 관련 콘텐츠는 도입 여부보다 어떤 병목을 줄이는지부터 정의합니다.",
        "",
        "### Product Owner",
        "- 백로그에는 사용자 가치, 인수 조건, 예외 케이스, 측정 지표를 함께 포함합니다.",
        "- 개발 협업 전 요구사항의 범위와 비범위를 명확히 해 재작업을 줄입니다.",
        "",
        "## 4. 실무 적용 제안",
    ]
    for title, how, metric, from_title in proposals_for(top):
        lines += [
            f"### {title}",
            f"- 적용 방법: {how}",
            f"- 확인할 지표: {metric}",
            f"- 계기가 된 글: {from_title or '해당 없음'}",
            "",
        ]

    lines += [
        "## 5. 체크리스트 또는 프레임워크",
        "- 이 기획은 어떤 고객 문제를 해결하는가?",
        "- 문제의 빈도, 강도, 대상 고객은 확인되었는가?",
        "- 출시 후 성공 여부를 어떤 지표로 판단할 것인가?",
        "- 정상 케이스 외 예외/운영/어드민 케이스가 정의되었는가?",
        "- 이번 스프린트에서 가장 작게 검증할 수 있는 가설은 무엇인가?",
        "- AI/자동화 기능이라면 줄어드는 수작업과 책임 주체가 명확한가?",
        "- 백로그 항목의 인수 조건이 개발·QA·운영 모두에게 해석 가능한가?",
        "",
        "## 6. 추가 관찰 주제",
        "- AI 기반 제품 운영 사례가 실제 지표 개선으로 이어지는지 계속 확인 필요",
        "- 제품 발견/고객 리서치 방법론이 국내 서비스 기획 프로세스에 어떻게 적용 가능한지 추가 관찰 필요",
        "- 단순 홍보성 콘텐츠와 실무 적용 가능한 사례를 계속 분리해 평가 필요",
        "",
        "## 7. 출처",
    ]
    for item in top:
        lines.append(f"- {item.title}, {item.source}, {item.published_at}, {item.url}")
    if errors:
        lines += ["", "## 수집 오류", *[f"- {e}" for e in errors]]
    return "\n".join(lines).strip() + "\n"


PRIORITY_ICON = {"HIGH": "🔥", "MEDIUM": "💡", "LOW": "📎"}
PRIORITY_KO = {"HIGH": "높음", "MEDIUM": "보통", "LOW": "낮음"}


def slack_message(items: list[ContentItem], date_from: str, date_to: str) -> str:
    period = f"{date_from}~{date_to}"
    top = unique_topic_items(items, limit=5)
    sources = sorted({i.source for i in items})
    lines = [
        f"📌 *PM·PO 인사이트*  |  `{period}`",
        f"이번 주 수집 {len(items)}건 · 출처 {len(sources)}곳 · 주제별 대표 {len(top)}건" + ("  _(나머지 글은 스레드 👇)_" if len(items) > len(top) else ""),
    ]
    if not top:
        lines += ["", "이번 기간 입력 데이터 기준 유의미한 자료 없음"]
    for idx, item in enumerate(top, start=1):
        icon = PRIORITY_ICON.get(item.priority, "📎")
        title = slack_escape(textwrap.shorten(item.title.replace("|", "–"), width=110, placeholder="..."))
        lines += [
            "",
            f"{icon} *{idx}. [{item.topic}]* <{item.url}|{title}>",
            f"   {slack_escape(item.source)} · {item.published_at} · 중요도 {PRIORITY_KO.get(item.priority, item.priority)}",
        ]
        for kp in key_points(item, limit=2):
            lines.append(f"   ▸ {slack_escape(kp)}")
        lines.append(f"   → {slack_escape(implication_for(item))}")
        lines.append(f"   ❓ {slack_escape(discussion_question(item))}")
    proposals = proposals_for(top)
    lines += ["", "*실무 적용 제안*"]
    for title, how, metric, _ in proposals:
        lines.append(f"• *{slack_escape(title)}* — {slack_escape(how)} _(지표: {slack_escape(metric)})_")
    lines += ["", f"🔗 <{full_report_url()}|전체 리포트 보기>"]
    return "\n".join(lines)


def slack_thread_messages(items: list[ContentItem], errors: list[str]) -> list[str]:
    top_urls = {i.url for i in unique_topic_items(items, limit=5)}
    rest = [i for i in items if i.url not in top_urls]
    out: list[str] = []
    if rest:
        lines = [f"📚 *그 밖에 이번 주 수집된 글 {len(rest)}건*"]
        for item in rest[:20]:
            title = slack_escape(textwrap.shorten(item.title.replace("|", "–"), width=100, placeholder="..."))
            lines.append(f"• `{item.topic}` <{item.url}|{title}> — {slack_escape(item.source)} · {item.published_at}")
        out.append("\n".join(lines))
    if errors:
        out.append("🛠️ *수집 오류*\n" + "\n".join(f"• {slack_escape(e)}" for e in errors))
    return out


def build_json_report(items: list[ContentItem], md: str, slack: str, date_from: str, date_to: str) -> dict[str, Any]:
    period = f"{date_from} ~ {date_to}"
    top_topics = []
    for item in unique_topic_items(items, limit=5):
        top_topics.append({
            "topic": item.topic,
            "priority": item.priority,
            "summary": f"{item.title}: {item.summary or item.excerpt or '요약 확인 필요'}",
            "practical_implication": implication_for(item),
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


def send_slack(text: str, thread: list[str] | None = None) -> None:
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
        for reply in (thread or []):
            body_obj = {"channel": channel_id, "text": reply[:3800], "thread_ts": data.get("ts"), "unfurl_links": False, "unfurl_media": False}
            r = Request("https://slack.com/api/chat.postMessage", data=json.dumps(body_obj).encode("utf-8"), headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT}, method="POST")
            with urlopen(r, timeout=20) as resp2:
                d2 = json.loads(resp2.read().decode("utf-8", errors="replace"))
                if not d2.get("ok"):
                    raise RuntimeError(f"Slack thread reply failed: {d2.get('error')}")
            time.sleep(1.1)
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
    parser.add_argument("--slack-thread-out", type=Path, default=DEFAULT_SLACK_THREAD)
    args = parser.parse_args(argv)

    config = json.loads(args.config.read_text(encoding="utf-8"))
    previous_topics = load_previous_topics(args.json_out)
    items, errors = collect_items(config, args.period_from, args.period_to)
    md = markdown_report(items, errors, config, args.period_from, args.period_to, previous_topics)
    slack = slack_message(items, args.period_from, args.period_to)
    thread = slack_thread_messages(items, errors)
    result = build_json_report(items, md, slack, args.period_from, args.period_to)

    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_out.write_text(md, encoding="utf-8")
    args.slack_out.write_text(slack + "\n", encoding="utf-8")
    args.slack_thread_out.write_text(json.dumps(thread, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Collected content: {len(items)}")
    if errors:
        print("Collection errors:")
        for err in errors:
            print(f"- {err}")
    if args.notify:
        send_slack(slack, thread)
        send_email(result["report_title"], md)
    else:
        print("No notification sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
