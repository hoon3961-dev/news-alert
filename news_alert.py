#!/usr/bin/env python3
"""중수청·공소청 언론보도 텔레그램 알림

30분마다(GitHub Actions) 구글 뉴스(RSS)에서 새 기사를 찾아
요지를 요약하고 링크와 함께 텔레그램으로 보낸다.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

KST = dt.timezone(dt.timedelta(hours=9))

# ── 설정 (환경변수로 바꿀 수 있음) ─────────────────────────────
# 검색어(쉼표 구분). 2026.10.7.~11.3. 중수청 뉴스스크랩 32회분(기사 612건) 분석으로 정함
DEFAULT_QUERIES = ",".join([
    # 기관
    "중수청", "중대범죄수사청", "중수청장", "공소청",
    "서울중수청 OR 서울지방중수청 OR 부산중수청 OR 대구중수청 OR 광주중수청 OR 대전중수청 OR 수원중수청",
    "서울공소청 OR 서울중앙공소청 OR 수원공소청 OR 대구공소청 OR 부산공소청 OR 광주공소청 OR 대전공소청",
    # 인물
    "김지용", "임은정",
    # 제도·쟁점
    "수사 기소 분리", "형사사법체계 개편", "보완수사 요구", "검찰개혁",
    "검찰총장 대행", "수사기관장 국무회의",
    # 유관기관
    "공수처",
])
QUERIES = [q.strip() for q in (os.getenv("QUERIES") or DEFAULT_QUERIES).split(",") if q.strip()]
PRIORITY_TERMS = ("중수청", "중대범죄수사청", "김지용", "임은정")   # 이 단어가 제목에 있으면 맨 위에
SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")    # 이미 보낸 기사 기록
KEEP_DAYS = 7                                      # 기록 보관 기간
MAX_AGE_HOURS = 24                                 # 이보다 오래된 기사는 무시
FIRST_RUN_HOURS = 2                                # 첫 실행 때는 최근 2시간만
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "20"))      # 한 번에 보낼 최대 건수
QUIET_HOURS = os.getenv("QUIET_HOURS", "")         # 예: "0-6" → 0~6시엔 보내지 않고 모아뒀다가 7시에 발송
# 받지 않을 언론사(쉼표 구분). 언론사 이름이나 주소 일부가 들어가면 제외
EXCLUDE_PRESS = [p.strip() for p in (os.getenv("EXCLUDE_PRESS") or "중수청일보").split(",") if p.strip()]
# 제목·본문에 이 단어가 있으면 제외(쉼표 구분). 예: 동명이인 기사
EXCLUDE_WORDS = [w.strip() for w in (os.getenv("EXCLUDE_WORDS") or "촬영감독,촬영 감독").split(",") if w.strip()]
# 본문에 이 단어가 하나도 없으면 관련 없는 기사로 보고 제외 (동명이인 등 걸러내기)
# AI 판단을 못 쓸 때는 이 단어가 제목·본문에 있는 기사만 보냄(잡음 방지)
CORE_TERMS = ("중수청", "중대범죄수사청", "공소청", "수사·기소 분리", "수사-기소 분리", "수사 기소 분리",
              "수사·기소분리", "형사사법체계", "형사사법 체계", "형사사법 개편", "형사사법개편",
              "보완수사", "검찰청 폐지")
CONTEXT_TERMS = ("중수청", "중대범죄수사청", "공소청", "검찰", "검사", "수사", "청장", "공수처",
                 "경찰", "특검", "형사", "법무", "기소", "사법")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")   # 없으면 기사 첫머리로 대체

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

PRESS = {
    "yna.co.kr": "연합뉴스", "yonhapnewstv.co.kr": "연합뉴스TV", "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보", "donga.com": "동아일보", "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문", "hankookilbo.com": "한국일보", "seoul.co.kr": "서울신문",
    "kmib.co.kr": "국민일보", "segye.com": "세계일보", "munhwa.com": "문화일보",
    "news1.kr": "뉴스1", "newsis.com": "뉴시스", "kbs.co.kr": "KBS", "imbc.com": "MBC",
    "sbs.co.kr": "SBS", "jtbc.co.kr": "JTBC", "ytn.co.kr": "YTN", "mbn.co.kr": "MBN",
    "ichannela.com": "채널A", "tvchosun.com": "TV조선", "lawtimes.co.kr": "법률신문",
    "mk.co.kr": "매일경제", "hankyung.com": "한국경제", "edaily.co.kr": "이데일리",
    "mt.co.kr": "머니투데이", "sedaily.com": "서울경제", "heraldcorp.com": "헤럴드경제",
    "nocutnews.co.kr": "노컷뉴스", "ohmynews.com": "오마이뉴스", "pressian.com": "프레시안",
    "naver.com": "네이버뉴스", "lawissue.co.kr": "로이슈", "lawleader.co.kr": "법률저널",
}


# ── 유틸 ─────────────────────────────────────────────────────
def clean(text: str) -> str:
    # 네이버는 검색어 강조용 <b> 태그만 붙이고 나머지는 &lt; 등으로 이스케이프함
    return html.unescape(re.sub(r"</?b>", "", text or "")).strip()


def press_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    for domain, name in PRESS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def norm_title(t: str) -> str:
    return re.sub(r"[\W_]+", "", re.sub(r"\[[^\]]*\]", "", t)).lower()


def in_quiet_hours(now: dt.datetime) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", QUIET_HOURS)
    if not m:
        return False
    start, end = int(m.group(1)), int(m.group(2))
    h = now.hour
    return start <= h <= end if start <= end else (h >= start or h <= end)


# ── 기록 ─────────────────────────────────────────────────────
def load_seen() -> dict:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict, now: dt.datetime) -> None:
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    kept = {k: v for k, v in seen.items() if dt.datetime.fromisoformat(v) >= cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=0, sort_keys=True)


# ── 수집 ─────────────────────────────────────────────────────
def google_search(query: str) -> list:
    """구글 뉴스 RSS 최신 기사 (최근 1일)"""
    r = requests.get(
        "https://news.google.com/rss/search",
        params={"q": f"{query} when:1d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
        headers=UA, timeout=15,
    )
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        src = it.find("source")
        items.append({
            "title": it.findtext("title", ""),
            "link": it.findtext("link", ""),
            "guid": it.findtext("guid", "") or it.findtext("link", ""),
            "pubDate": it.findtext("pubDate", ""),
            "source": (src.text or "").strip() if src is not None else "",
            "source_url": src.get("url", "") if src is not None else "",
        })
    return items


def decode_links(arts: list) -> None:
    """구글 뉴스 중계 링크를 실제 언론사 주소로 변환 (실패하면 구글 링크 유지)"""
    if not arts:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
        results = gnewsdecoder([a["url"] for a in arts], timeout=15)
    except Exception as e:
        print(f"[경고] 링크 변환 실패, 구글 링크 사용: {e}", file=sys.stderr)
        return
    for a, res in zip(arts, results):
        if isinstance(res, dict) and res.get("success") and res.get("decoded_url"):
            a["url"] = res["decoded_url"]
            if not a["press"]:
                a["press"] = press_name(a["url"])


def collect(seen: dict, now: dt.datetime) -> list:
    first_run = not seen
    max_age = dt.timedelta(hours=FIRST_RUN_HOURS if first_run else MAX_AGE_HOURS)
    found, titles = {}, set()
    for q in QUERIES:
        try:
            items = google_search(q)
        except Exception as e:
            print(f"[경고] '{q}' 검색 실패: {e}", file=sys.stderr)
            continue
        for it in items:
            key = it["guid"]
            if not key or key in seen or key in found:
                continue
            try:
                pub = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
            except (TypeError, ValueError):
                pub = now
            if now - pub > max_age:
                if first_run:                      # 첫 실행 때 건너뛴 기사는 이후에도 보내지 않음
                    seen[key] = now.isoformat()
                continue
            press = it["source"]
            title = html.unescape(it["title"]).strip()
            if any(x in press or x in it["source_url"] or title.endswith(x) for x in EXCLUDE_PRESS):
                seen[key] = now.isoformat()        # 제외 언론사
                continue
            if press and title.endswith(" - " + press):   # 구글 뉴스 제목 끝의 ' - 언론사' 제거
                title = title[: -len(press) - 3].strip()
            if press and title.endswith(":" + press):     # 제목 끝 ':언론사' 제거
                title = title[: -len(press) - 1].strip()
            nt = norm_title(title)
            if nt in titles:                       # 같은 제목 중복 제거
                seen[key] = now.isoformat()
                continue
            titles.add(nt)
            found[key] = {
                "key": key,
                "url": it["link"],
                "title": title,
                "pub": pub,
                "press": press or press_name(it["source_url"]),
                "priority": any(t in title for t in PRIORITY_TERMS),
            }
    arts = sorted(found.values(), key=lambda a: (not a["priority"], -a["pub"].timestamp()))
    return arts


# ── 요약 ─────────────────────────────────────────────────────
def fetch_body(art: dict) -> str:
    """기사 본문 추출(실패하면 빈 문자열)."""
    try:
        import trafilatura
    except ImportError:
        return ""
    for url in (art["url"],):
        if not url or "news.google.com" in url:
            continue
        try:
            r = requests.get(url, headers=UA, timeout=10)
            r.encoding = r.apparent_encoding or r.encoding
            text = trafilatura.extract(r.text) or ""
            if re.search(r"Access Denied|permission to access|403 Forbidden|로봇이 아닙니다", text[:300]):
                continue                           # 차단 페이지는 본문으로 쓰지 않음
            if len(text) > 200:
                return text[:2500]
        except Exception:
            continue
    return ""


def summarize(arts: list) -> None:
    """각 기사에 summary(요지)와 relevant(관련 여부)를 채운다."""
    for a in arts:
        a["body"] = fetch_body(a)
        a["relevant"] = True
        text = a["title"] + " " + a["body"]
        if any(w in text for w in EXCLUDE_WORDS):
            a["relevant"] = False                  # 제외 단어
        elif a["body"] and not any(t in text for t in CONTEXT_TERMS):
            a["relevant"] = False                  # 수사·사법 관련 단어가 전혀 없는 기사(동명이인 등)
        # AI 키가 없을 때: 본문 첫머리 120자 정도를 요지로 사용
        first = re.sub(r"\s+", " ", a["body"])[:120]
        a["summary"] = (first.rsplit(" ", 1)[0] + "…") if len(first) == 120 else first
    if not arts:
        return
    if not ANTHROPIC_KEY:
        print("[경고] ANTHROPIC_API_KEY 없음 → 핵심 단어 기준으로만 거름", file=sys.stderr)
        strict_filter(arts)
        return

    blocks = []
    for i, a in enumerate(arts):
        body = a["body"] or "(본문 없음 - 제목만으로 판단)"
        blocks.append(f"<article id=\"{i}\">\n제목: {a['title']}\n본문: {body}\n</article>")

    prompt = (
        "다음은 중대범죄수사청(중수청) 언론동향 모니터링을 위해 수집한 기사들이다.\n"
        "각 기사마다 공무원 내부 보고용으로 핵심 요지를 1~2문장, 개조식 '~함/~임' 체로 요약하라. "
        "추측이나 평가는 넣지 말고 기사에 나온 사실만 쓴다.\n"
        "관련성 판단: 다음 중 하나에 해당하면 relevant=true, 아니면 false로 표시하라.\n"
        "① 중수청·공소청(본청·지방청, 청장·간부 포함)의 조직·인사·수사·사건·논란\n"
        "② 수사·기소 분리 등 형사사법체계 개편, 수사권·관할 조정, 보완수사, 형소법 개정의 시행·혼란·평가\n"
        "③ 경찰(국수본)·공수처·특검·특별감찰관 등 유관 수사기관 기사 중 중수청·공소청이나 형사사법체계 개편과 연결된 것\n"
        "④ 위 주제를 다룬 사설·칼럼\n"
        "단어만 우연히 겹친 기사(다른 나라 기관, 일반 사건사고 등)와 동명이인 기사(예: 촬영감독·연예인·운동선수 김지용)는 false.\n"
        "출력은 JSON 배열만: [{\"id\": 0, \"summary\": \"...\", \"relevant\": true}, ...]\n\n"
        + "\n\n".join(blocks)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 4000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        r.raise_for_status()
        text = "".join(c.get("text", "") for c in r.json().get("content", []))
        data = json.loads(text[text.index("["): text.rindex("]") + 1])
        for d in data:
            i = int(d.get("id", -1))
            if 0 <= i < len(arts):
                if d.get("summary"):
                    arts[i]["summary"] = d["summary"].strip()
                arts[i]["relevant"] = arts[i]["relevant"] and bool(d.get("relevant", True))
    except Exception as e:
        detail = ""
        try:
            detail = r.text[:300]
        except Exception:
            pass
        print(f"[경고] AI 요약 실패 → 핵심 단어 기준으로만 거름: {e} {detail}", file=sys.stderr)
        strict_filter(arts)


def strict_filter(arts: list) -> None:
    """AI 판단을 못 쓸 때: 제목·본문에 핵심 단어가 없는 기사는 제외"""
    for a in arts:
        if a["relevant"] and not any(t in a["title"] + " " + a["body"] for t in CORE_TERMS):
            a["relevant"] = False


# ── 전송 ─────────────────────────────────────────────────────
def format_messages(arts: list, now: dt.datetime) -> list:
    e = html.escape
    header = f"📰 <b>중수청 등 언론보도</b>\n{now:%m.%d. %H:%M} 기준 신규 {len(arts)}건\n"
    blocks, section = [], None
    for n, a in enumerate(arts, 1):
        sec = "■ 중수청" if a["priority"] else "■ 공소청·형사사법 등"
        prefix = f"\n<b>{sec}</b>\n" if sec != section else ""
        section = sec
        blocks.append(
            f"{prefix}\n{n}. <b>{e(a['title'])}</b>\n"
            f"   {e(a['press'])} · {a['pub']:%H:%M}\n"
            + (f"   ▸ {e(a['summary'])}\n" if a["summary"] else "")
            + f"   <a href=\"{e(a['url'], quote=True)}\">기사 보기</a>"
        )
    msgs, cur = [], header
    for b in blocks:                               # 텔레그램 4096자 제한 대비 분할
        if len(cur) + len(b) > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n"
    msgs.append(cur)
    return msgs


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    chat = TG_CHAT.strip()
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=15)
    if r.ok:
        return
    desc = r.json().get("description", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
    print(f"[텔레그램 오류] {r.status_code}: {desc}", file=sys.stderr)
    if "parse" in desc.lower():                    # 서식 문제면 서식 없이 다시 보냄
        plain = html.unescape(re.sub(r'<a href="([^"]+)">[^<]*</a>', r"\1", text))
        plain = re.sub(r"</?b>", "", plain)
        r = requests.post(url, json={"chat_id": chat, "text": plain,
                                     "disable_web_page_preview": True}, timeout=15)
        if r.ok:
            return
        print(f"[텔레그램 오류] 재시도 실패 {r.status_code}: {r.text}", file=sys.stderr)
    if "chat not found" in desc.lower():
        print("→ TELEGRAM_CHAT_ID가 틀렸거나, 봇에게 먼저 메시지를 보내지 않았습니다.", file=sys.stderr)
    r.raise_for_status()


# ── 실행 ─────────────────────────────────────────────────────
def main() -> int:
    missing = [k for k, v in {"TELEGRAM_BOT_TOKEN": TG_TOKEN, "TELEGRAM_CHAT_ID": TG_CHAT}.items() if not v]
    if missing:
        print("필수 설정 누락: " + ", ".join(missing), file=sys.stderr)
        return 1

    now = dt.datetime.now(KST)
    if in_quiet_hours(now):
        print(f"{now:%H:%M} 방해금지 시간 - 건너뜀")
        return 0

    seen = load_seen()
    arts = collect(seen, now)[: MAX_ITEMS * 2]
    decode_links(arts)
    summarize(arts)

    for a in arts:                                 # 무관 기사도 다시 안 보도록 기록
        seen[a["key"]] = now.isoformat()
    arts = [a for a in arts if a["relevant"]][:MAX_ITEMS]

    if arts:
        for msg in format_messages(arts, now):
            send_telegram(msg)
        print(f"{len(arts)}건 발송")
    else:
        print("새 기사 없음")

    save_seen(seen, now)                           # 발송 성공 후에만 기록
    return 0


if __name__ == "__main__":
    sys.exit(main())
