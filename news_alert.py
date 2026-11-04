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
STATE_FILE = os.getenv("STATE_FILE", "state.json") # 공소청·형사사법 모음 대기 목록
DIGEST_MINUTES = int(os.getenv("DIGEST_MINUTES", "60"))   # 공소청·형사사법 기사 모음 간격(분)
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "17"))   # 이 시각 이후 첫 실행 때 '오늘의 대응 검토 기사' 발송
AREAS = ("법령검토", "지침제정", "기관협의", "수사지원", "타부서 소관")   # 수사정책담당관실 업무 구분
LEAD_TERMS = ("중수청", "중대범죄수사청")             # 본문 첫머리에 있으면 중수청 기사로 봄
KEEP_DAYS = 7                                      # 기록 보관 기간
MAX_AGE_HOURS = 24                                 # 이보다 오래된 기사는 무시(최대)
BUFFER_HOURS = 2                                   # 직전 실행보다 이만큼 앞선 기사까지 받음(포털 반영 지연 대비)
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


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("pending", [])
    st.setdefault("last_digest", "")
    return st


def save_state(st: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


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


def collect(seen: dict, now: dt.datetime, last_run: str = "") -> list:
    first_run = not seen
    if first_run:
        max_age = dt.timedelta(hours=FIRST_RUN_HOURS)
    elif last_run:
        # 직전 실행 시각 - 2시간 이후 기사만 (밤사이 쉬었다면 그만큼 넓어짐), 최대 24시간
        gap = now - dt.datetime.fromisoformat(last_run) + dt.timedelta(hours=BUFFER_HOURS)
        max_age = min(gap, dt.timedelta(hours=MAX_AGE_HOURS))
    else:
        max_age = dt.timedelta(hours=MAX_AGE_HOURS)
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
                seen[key] = now.isoformat()        # 지난 기사는 이후에도 보내지 않음
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
        # 중수청 기사 판별(기본): 제목에 우선 단어, 또는 본문 첫머리 300자에 중수청
        a["priority"] = a["priority"] or any(t in a["body"][:300] for t in LEAD_TERMS)
        a["group"] = None
        a["action"], a["area"], a["reason"] = "", "", ""
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
        "jsc 판단: 기사의 주된 대상이 중수청(중대범죄수사청 본청·지방청, 김지용 청장 등 중수청 간부, 중수청 수사·사건·조직)이면 true. "
        "공소청·경찰·공수처·형사사법체계 일반이 주된 대상이고 중수청은 곁가지로 언급되면 false.\n"
        "group: 같은 사안(같은 사건·발언·발표)을 다룬 기사끼리 같은 숫자를 주고, 다른 사안은 다른 숫자를 준다.\n"
        "action 판단: 이 알림은 중수청 본청 '수사정책담당관실'(업무: 법령검토, 지침제정, 기관협의, 수사지원)이 받는다. "
        "기사 내용으로 볼 때 이 부서의 대응 검토가 필요한지 판단하라.\n"
        "- 법령검토: 중수청법·형사소송법 등 개정 논의, 수사 대상·관할 규정 공백 지적, 국회 입법 발의\n"
        "- 지침제정: 현장 혼란 보도(고소장 접수, 이첩·이관 기준, 조서 작성, 영장 절차 등), 기관별 기준 불일치 지적\n"
        "- 기관협의: 공소청·경찰·공수처·금감원 등과의 관할·이첩·보완수사·리니언시 갈등, 협의체 관련 보도\n"
        "- 수사지원: 장비·시스템 미비(킥스, 포렌식, 유치장 등), 수사 인력·인프라 문제\n"
        "- 타부서 소관: 해명·홍보 대응이 필요한 비판·오보, 인사·감찰 등 다른 부서 업무\n"
        "action은 \"red\"(위 업무와 직접 연결되어 사실확인·조치 검토가 필요), \"yellow\"(관련은 있으나 참고 수준), \"\"(해당 없음) 중 하나. "
        "단순 동정·일반 사건 보도는 \"\". red는 꼭 필요한 경우에만 쓴다.\n"
        "area는 위 업무 구분 중 해당하는 것(둘이면 '지침제정·기관협의'처럼), reason은 검토 포인트를 25자 내외 명사형으로(예: '조서 작성 기준 정합성 확인').\n"
        "출력은 JSON 배열만: [{\"id\": 0, \"summary\": \"...\", \"relevant\": true, \"jsc\": true, \"group\": 1, "
        "\"action\": \"red\", \"area\": \"법령검토\", \"reason\": \"...\"}, ...]\n\n"
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
                if "jsc" in d:
                    arts[i]["priority"] = bool(d["jsc"]) or any(t in arts[i]["title"] for t in PRIORITY_TERMS)
                if d.get("group") is not None:
                    arts[i]["group"] = str(d["group"])
                if d.get("action") in ("red", "yellow"):
                    arts[i]["action"] = d["action"]
                    arts[i]["area"] = str(d.get("area") or "").strip()
                    arts[i]["reason"] = str(d.get("reason") or "").strip()
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
def fmt_time(t: dt.datetime, now: dt.datetime) -> str:
    return f"{t:%H:%M}" if t.date() == now.date() else f"{t:%m.%d. %H:%M}"


def group_articles(arts: list) -> list:
    """같은 사안 기사 묶기 → [[대표기사, 관련기사...], ...] (AI group, 없으면 제목 유사도)"""
    groups, by_key = [], {}
    for a in arts:
        key = a.get("group")
        if key is None:
            for g in groups:                       # AI 묶음이 없으면 제목이 비슷한 것끼리
                if similar(g[0]["title"], a["title"]):
                    g.append(a)
                    break
            else:
                groups.append([a])
            continue
        if key in by_key:
            by_key[key].append(a)
        else:
            by_key[key] = [a]
            groups.append(by_key[key])
    return groups


def similar(t1: str, t2: str) -> bool:
    b = lambda t: {t[i:i + 2] for i in range(len(t) - 1)}
    x, y = b(norm_title(t1)), b(norm_title(t2))
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.6


def action_line(g: list) -> str:
    """묶음 중 가장 높은 대응 표시 한 줄"""
    best = next((x for x in g if x.get("action") == "red"), None) or \
        next((x for x in g if x.get("action") == "yellow"), None)
    if not best:
        return ""
    mark = "🔴 대응 검토" if best["action"] == "red" else "🟡 참고"
    tail = " — " + html.escape(best["reason"]) if best.get("reason") else ""
    area = " · " + html.escape(best["area"]) if best.get("area") else ""
    return f"   {mark}{area}{tail}\n"


def to_record(a: dict, now: dt.datetime) -> dict:
    return {"url": a["url"], "title": a["title"], "press": a["press"], "pub": a["pub"].isoformat(),
            "summary": a["summary"], "group": a.get("group"), "found": now.isoformat(),
            "action": a.get("action", ""), "area": a.get("area", ""), "reason": a.get("reason", "")}


def from_record(r: dict) -> dict:
    r = dict(r)
    r["pub"] = dt.datetime.fromisoformat(r["pub"])
    r.setdefault("action", ""), r.setdefault("area", ""), r.setdefault("reason", "")
    return r


def format_daily(items: list, now: dt.datetime) -> list:
    """오늘의 대응 검토 기사: 업무별로 정리"""
    e = html.escape
    reds = [x for x in items if x["action"] == "red"]
    yellows = [x for x in items if x["action"] == "yellow"]
    head = f"📋 <b>오늘의 대응 검토 기사</b> ({now:%m.%d.})\n"
    if not items:
        return [head + "오늘 대응 검토 기사 없음"]
    head += f"🔴 {len(reds)}건 · 🟡 {len(yellows)}건\n"
    sections = []
    for area in AREAS + ("기타",):
        mine = [x for x in reds if (x["area"].split("·")[0].strip() if x["area"] else "기타") == area
                or (area == "기타" and x["area"] and x["area"].split("·")[0].strip() not in AREAS)]
        if mine:
            label = area
            sections.append((label, mine))
    if yellows:
        sections.append(("참고", yellows))
    blocks = []
    for label, xs in sections:
        b = f"\n<b>■ {e(label)}</b>"
        for g in group_articles(xs):
            a = g[0]
            mark = "🔴" if a["action"] == "red" else "🟡"
            area = f" [{e(a['area'])}]" if a["area"] and label != a["area"] else ""
            b += (f"\n{mark} {e(a['title'])} ({e(a['press'])} {fmt_time(a['pub'], now)}){area}\n"
                  + (f"   → {e(a['reason'])}\n" if a["reason"] else "")
                  + f"   <a href=\"{e(a['url'], quote=True)}\">기사 보기</a>"
                  + (f" · 같은 내용 {len(g) - 1}건" if len(g) > 1 else ""))
        blocks.append(b)
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n"
    msgs.append(cur)
    return msgs


def format_messages(arts: list, now: dt.datetime, header: str) -> list:
    e = html.escape
    blocks = []
    for n, g in enumerate(group_articles(arts), 1):
        a = g[0]
        link = f"<a href=\"{e(a['url'], quote=True)}\">기사 보기</a>"
        if len(g) > 1:
            more = ", ".join(f"<a href=\"{e(x['url'], quote=True)}\">{e(x['press'])}</a>" for x in g[1:])
            link += f" · 같은 내용 {len(g) - 1}건: {more}"
        blocks.append(
            f"\n{n}. <b>{e(a['title'])}</b>\n"
            f"   {e(a['press'])} · {fmt_time(a['pub'], now)}\n"
            + (f"   ▸ {e(a['summary'])}\n" if a["summary"] else "")
            + action_line(g)
            + f"   {link}"
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
    state = load_state()
    arts = collect(seen, now, state.get("last_run", ""))[: MAX_ITEMS * 2]
    decode_links(arts)
    summarize(arts)

    for a in arts:                                 # 무관 기사도 다시 안 보도록 기록
        seen[a["key"]] = now.isoformat()
    arts = [a for a in arts if a["relevant"]]

    # 대응 표시(🔴·🟡) 기사는 하루 목록용으로 기록
    today = now.date().isoformat()
    state.setdefault("daily", [])
    state["daily"] = [x for x in state["daily"] if x.get("found", "")[:10] == today]
    for a in arts:
        if a.get("action"):
            state["daily"].append(to_record(a, now))

    # ① 중수청 기사 + 🔴 대응 검토 기사: 바로 발송
    urgent = [a for a in arts if a["priority"]][:MAX_ITEMS]
    red_now = [a for a in arts if not a["priority"] and a.get("action") == "red"]
    if urgent:
        head = f"📰 <b>중수청 등 언론보도</b> · 중수청\n{now:%m.%d. %H:%M} 기준 신규 {len(urgent)}건\n"
        for msg in format_messages(urgent, now, head):
            send_telegram(msg)
        print(f"중수청 {len(urgent)}건 즉시 발송")
    if red_now:
        head = f"📰 <b>중수청 등 언론보도</b> · 🔴 대응 검토\n{now:%m.%d. %H:%M} 기준 {len(red_now)}건\n"
        for msg in format_messages(red_now, now, head):
            send_telegram(msg)
        print(f"🔴 대응 검토 {len(red_now)}건 즉시 발송")

    # ② 공소청·형사사법 기사(🔴 제외): 대기 목록에 쌓았다가 마지막 모음 발송 후 DIGEST_MINUTES 지나면 발송
    for a in arts:
        if not a["priority"] and a.get("action") != "red":
            state["pending"].append(to_record(a, now))
    last = dt.datetime.fromisoformat(state["last_digest"]) if state["last_digest"] else None
    due = last is None or now - last >= dt.timedelta(minutes=DIGEST_MINUTES)
    if state["pending"] and due:
        items = []
        for p_ in state["pending"]:
            p_ = from_record(p_)
            p_["priority"] = False
            # 대기 중 들어온 기사끼리는 AI 묶음 번호가 실행마다 달라서 제목 유사도로 묶음
            p_["group"] = None if len({x.get("found") for x in state["pending"]}) > 1 else p_.get("group")
            items.append(p_)
        since = last.strftime("%H:%M") if last and last.date() == now.date() else "직전"
        head = (f"📰 <b>중수청 등 언론보도</b> · 공소청·형사사법 등 모음\n"
                f"{since}~{now:%H:%M} 신규 {len(items)}건\n")
        for msg in format_messages(items, now, head):
            send_telegram(msg)
        print(f"공소청·형사사법 {len(items)}건 모음 발송")
        state["pending"] = []
        state["last_digest"] = now.isoformat()
    elif state["pending"]:
        print(f"공소청·형사사법 {len(state['pending'])}건 대기 중 (모음 발송까지 "
              f"{DIGEST_MINUTES - int((now - last).total_seconds() // 60)}분)")
    if not arts:
        print("새 기사 없음")

    # ③ 오늘의 대응 검토 기사: DAILY_HOUR 이후 첫 실행 때 하루 1회
    if now.hour >= DAILY_HOUR and state.get("daily_sent") != today:
        items = [from_record(x) for x in state["daily"]]
        for msg in format_daily(items, now):
            send_telegram(msg)
        state["daily_sent"] = today
        print(f"오늘의 대응 검토 기사 발송 (🔴·🟡 {len(items)}건)")

    state["last_run"] = now.isoformat()
    save_seen(seen, now)                           # 발송 성공 후에만 기록
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
