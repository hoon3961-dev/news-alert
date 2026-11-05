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
import time
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
    "검찰총장 대행", "수사기관장 국무회의", "법왜곡죄",
    # 유관기관
    "공수처",
])
QUERIES = [q.strip() for q in (os.getenv("QUERIES") or DEFAULT_QUERIES).split(",") if q.strip()]
PRIORITY_TERMS = ("중수청", "중대범죄수사청", "김지용", "임은정")   # 이 단어가 제목에 있으면 맨 위에
SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")    # 이미 보낸 기사 기록
STATE_FILE = os.getenv("STATE_FILE", "state.json") # 공소청·형사사법 모음 대기 목록
DIGEST_MINUTES = int(os.getenv("DIGEST_MINUTES", "60"))   # 공소청·형사사법 기사 모음 간격(분)
LEAD_TERMS = ("중수청", "중대범죄수사청")             # 본문 첫머리에 있으면 중수청 기사로 봄
KEEP_DAYS = 7                                      # 기록 보관 기간
MAX_AGE_HOURS = 24                                 # 이보다 오래된 기사는 무시(최대)
BUFFER_HOURS = 2                                   # 직전 실행보다 이만큼 앞선 기사까지 받음(포털 반영 지연 대비)
FIRST_RUN_HOURS = 2                                # 첫 실행 때는 최근 2시간만
BATCH = int(os.getenv("BATCH", "12"))              # AI에 한 번에 보내는 기사 수(많으면 나눠서 처리, 버리는 기사 없음)
QUIET_HOURS = os.getenv("QUIET_HOURS", "")         # 예: "0-6" → 0~6시엔 보내지 않고 모아뒀다가 7시에 발송
# 받지 않을 언론사(쉼표 구분). 언론사 이름이나 주소 일부가 들어가면 제외
EXCLUDE_PRESS = [p.strip() for p in (os.getenv("EXCLUDE_PRESS") or "중수청일보").split(",") if p.strip()]

# ── 받을 언론사 (중수청 뉴스스크랩 33회분 분석: 1·2군 41개사 + 지방지) ──
# PRESS_FILTER=off 로 설정하면 언론사 제한 없이 모두 받음
PRESS_FILTER = os.getenv("PRESS_FILTER", "on").strip().lower() != "off"
ALLOWED_PRESS = {
    # 1군: 석간
    "문화일보": "munhwa.com", "내일신문": "naeil.com", "아시아경제": "asiae.co.kr", "헤럴드경제": "heraldcorp.com",
    # 1군: 종합 일간지
    "조선일보": "chosun.com", "서울신문": "seoul.co.kr", "경향신문": "khan.co.kr", "세계일보": "segye.com",
    "중앙일보": "joongang.co.kr", "국민일보": "kmib.co.kr", "한국일보": "hankookilbo.com", "한겨레": "hani.co.kr",
    # 1군: 통신사
    "뉴스1": "news1.kr", "뉴시스": "newsis.com", "연합뉴스": "yna.co.kr",
    # 1군: 경제지
    "서울경제": "sedaily.com", "매일경제": "mk.co.kr", "파이낸셜뉴스": "fnnews.com", "한국경제": "hankyung.com",
    "아시아투데이": "asiatoday.co.kr",
    # 1군: 방송
    "KBS": "kbs.co.kr", "JTBC": "jtbc.co.kr", "연합뉴스TV": "yonhapnewstv.co.kr", "MBN": "mbn.co.kr",
    "SBS": "sbs.co.kr", "MBC": "imbc.com",
    # 2군
    "동아일보": "donga.com", "TV조선": "tvchosun.com", "YTN": "ytn.co.kr", "아주경제": "ajunews.com",
    "이데일리": "edaily.co.kr", "머니투데이": "mt.co.kr", "뉴스토마토": "newstomato.com", "매일신문": "imaeil.com",
    "노컷뉴스": "nocutnews.co.kr", "대구신문": "idaegu.co.kr", "더팩트": "tf.co.kr", "법률신문": "lawtimes.co.kr",
    "뉴스핌": "newspim.com", "경상매일신문": "ksmnews.co.kr",
    # 추가 요청
    "오마이뉴스": "ohmynews.com", "데일리안": "dailian.co.kr", "채널A": "ichannela.com", "시사저널": "sisajournal.com",
    # 11.5. 조간 스크랩 반영
    "프레시안": "pressian.com", "천지일보": "newscj.com", "조선비즈": "biz.chosun.com", "호남신문": "honamnews.co.kr",
    # 지방지
    "부산일보": "busan.com", "국제신문": "kookje.co.kr", "영남일보": "yeongnam.com", "대구일보": "idaegu.com",
    "경북일보": "kyongbuk.co.kr", "경북신문": "kbsm.net", "경북매일": "kbmaeil.com", "대경일보": "dkilbo.com",
    "일간경북신문": "", "경북연합일보": "", "대구광역일보": "", "경남신문": "knnews.co.kr", "경남도민일보": "idomin.com",
    "경상일보": "ksilbo.co.kr", "울산매일": "iusm.co.kr", "광주일보": "kwangju.co.kr", "광주매일신문": "kjdaily.com",
    "남도일보": "namdonews.com", "무등일보": "mdilbo.com", "전남일보": "jnilbo.com", "전북일보": "jjan.kr",
    "전북도민일보": "domin.co.kr", "새전북신문": "sjbnews.com", "대전일보": "daejonilbo.com", "중도일보": "joongdo.co.kr",
    "충청투데이": "cctoday.co.kr", "충남일보": "chungnamilbo.co.kr", "충북일보": "inews365.com", "충청매일": "ccdn.co.kr",
    "충청타임즈": "cctimes.kr", "중부매일": "jbnews.com", "충청일보": "ccdailynews.com", "경인일보": "kyeongin.com",
    "경기일보": "kyeonggi.com", "중부일보": "joongboo.com", "기호일보": "kihoilbo.co.kr", "인천일보": "incheonilbo.com",
    "강원일보": "kwnews.co.kr", "강원도민일보": "kado.net", "제주일보": "jejunews.com", "한라일보": "ihalla.com",
    "제민일보": "jemin.com", "산경일보": "", "동방일보": "", "대한일보": "",
}
# 이름이 다르게 표시되는 경우(구글 뉴스 표기)
PRESS_ALIASES = {"서울Pn": "서울신문", "JoongAng": "중앙일보", "Yonhap": "연합뉴스", "TV조선뉴스": "TV조선",
                 "news1": "뉴스1", "NEWSIS": "뉴시스", "한겨레신문": "한겨레", "The Fact": "더팩트",
                 "OhmyNews": "오마이뉴스", "ChosunBiz": "조선비즈", "Chosunbiz": "조선비즈", "뉴스천지": "천지일보", "오마이TV": "오마이뉴스", "Channel A": "채널A", "채널A 뉴스": "채널A"}
BLOCKED_HOSTS = ("weekly.chosun.com", "biz.sbs.co.kr")   # 같은 주소 아래 다른 매체


def press_allowed(press: str, url: str) -> bool:
    if not PRESS_FILTER:
        return True
    host = urlparse(url).netloc.lower()
    if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
        return False
    name = PRESS_ALIASES.get(press.strip(), press.strip())
    for p, dom in ALLOWED_PRESS.items():
        if name == p or name.startswith(p + " ") or (p in ("KBS", "SBS", "MBC", "MBN", "JTBC", "YTN") and name.startswith(p)):
            return True
        if dom and (host == dom or host.endswith("." + dom)):
            return True
    return False
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
            if (any(x in press or x in it["source_url"] or title.endswith(x) for x in EXCLUDE_PRESS)
                    or not press_allowed(press, it["source_url"])):
                seen[key] = now.isoformat()        # 제외 언론사
                continue
            if press and title.endswith(" - " + press):   # 구글 뉴스 제목 끝의 ' - 언론사' 제거
                title = title[: -len(press) - 3].strip()
            if press and title.endswith(":" + press):     # 제목 끝 ':언론사' 제거
                title = title[: -len(press) - 1].strip()
            nt = norm_title(title)
            if nt in titles or ("t:" + nt) in seen:  # 같은 제목 중복 제거(이전 실행에서 처리한 제목 포함)
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


RECENT_SENT: list = []                             # 최근 12시간 동안 보냈거나 모음 대기 중인 기사 제목(AI 중복 판단용)


def summarize(arts: list, _again: bool = False) -> None:
    """각 기사에 summary(요지)와 relevant(관련 여부)를 채운다."""
    for a in ([] if _again else arts):        # 나눠서 다시 처리할 때는 본문을 다시 받지 않음
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
        a["dup"] = False
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
        "공소청이 개별 형사사건을 송치받거나 기소·구형·상고했다는 단순 사건 보도(선거법 위반 송치, 일반 범죄 기소·재판 등)는 "
        "수사·기소 분리 등 제도 쟁점이나 중수청과 연결되지 않으면 false. 정당 내부·선거 정치 기사, 날씨·생활 기사도 false.\n"
        "단, 정치 기사라도 공소청·검찰 업무와 직결된 쟁점(공소취소, 검찰미래위원회, 조작기소 특검법 등 특검법의 수사·기소 권한, "
        "검찰총장·공소청장 인선)이나 법왜곡죄 고소·고발(판사·검사·수사기관 대상)을 다루면 true.\n"
        "반대로 중수청이 언급된 사설·칼럼·시평, 중수청 관할·이첩·파견 문제를 다룬 기사는 짧게 언급돼도 true.\n"
        "jsc 판단: 기사의 주된 대상이 중수청(중대범죄수사청 본청·지방청, 김지용 청장 등 중수청 간부, 중수청 수사·사건·조직)이면 true. "
        "공소청·경찰·공수처·형사사법체계 일반이 주된 대상이고 중수청은 곁가지로 언급되면 false.\n"
        "group: 같은 사안(같은 사건·발언·발표)을 다룬 기사끼리 같은 숫자를 주고, 다른 사안은 다른 숫자를 준다.\n"
        "dup 판단: 맨 아래 sent 태그 안의 목록은 최근 이미 알림을 보낸 기사 제목이다. 기사가 목록 중 하나와 같은 사안(같은 사건·발표·발언·조치)이고 "
        "새로운 진전(예: 고발→기소처럼 단계가 바뀜, 새 수치·새 당사자 입장 공개)이 없으면 dup=true. "
        "표현만 다른 같은 내용(예: '고발건 배당'과 '수사 착수')은 dup=true.\n"
        "출력은 JSON 배열만: [{\"id\": 0, \"summary\": \"...\", \"relevant\": true, \"jsc\": true, \"group\": 1, "
        "\"dup\": false}, ...]\n\n"
        + "<sent>\n" + ("\n".join("- " + t for t in RECENT_SENT[-80:]) or "(없음)") + "\n</sent>\n\n"
        + "\n\n".join(blocks)
    )
    data, err = None, ""
    for attempt in range(3):                       # 일시 오류(과부하·시간초과·답변 잘림)면 다시 시도
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": CLAUDE_MODEL, "max_tokens": 8000,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=180,
            )
            if r.status_code in (401, 403):        # 키 문제는 다시 해도 안 됨
                err = f"{r.status_code} {r.text[:200]}"
                break
            if r.status_code == 400 and "credit" in r.text.lower():
                err = "크레딧 부족 " + r.text[:200]
                break
            r.raise_for_status()
            res = r.json()
            text = "".join(c.get("text", "") for c in res.get("content", []))
            if res.get("stop_reason") == "max_tokens":
                raise ValueError("답변이 길이 제한에 걸려 잘림")
            data = json.loads(text[text.index("["): text.rindex("]") + 1])
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            try:
                err += " " + r.text[:200]
            except Exception:
                pass
            time.sleep(5 * (attempt + 1))
    if data is None:
        if len(arts) > 4:                          # 실패하면 절반씩 나눠 다시 처리
            print(f"[알림] AI 처리 실패({err[:80]}) → {len(arts)}건을 나눠 다시 시도", file=sys.stderr)
            half = len(arts) // 2
            summarize(arts[:half], _again=True)
            summarize(arts[half:], _again=True)
            return
        print(f"[경고] AI 요약 실패 → 핵심 단어 기준으로만 거름: {err}", file=sys.stderr)
        strict_filter(arts)
        return
    for d in data:
        i = int(d.get("id", -1))
        if 0 <= i < len(arts):
            if d.get("summary"):
                arts[i]["summary"] = d["summary"].strip()
            arts[i]["relevant"] = arts[i]["relevant"] and bool(d.get("relevant", True))
            arts[i]["dup"] = bool(d.get("dup", False))
            if "jsc" in d:
                arts[i]["priority"] = bool(d["jsc"]) or any(t in arts[i]["title"] for t in PRIORITY_TERMS)
            if d.get("group") is not None:
                arts[i]["group"] = str(d["group"])


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


def dedupe(arts: list, seen: dict, state: dict, now: dt.datetime) -> list:
    """같은 기사가 출처 이름·주소만 바뀌어 다시 검색되는 경우 제거"""
    recent = [r[0] for r in state.get("recent", [])] + [p["title"] for p in state.get("pending", [])]
    out, urls = [], set()
    for a in arts:
        dup = (a["url"] in urls or ("u:" + a["url"]) in seen
               or any(similar(a["title"], t, 0.75) for t in recent))
        if dup:
            seen[a["key"]] = now.isoformat()
            continue
        urls.add(a["url"])
        out.append(a)
    return out


def similar(t1: str, t2: str, th: float = 0.6) -> bool:
    b = lambda t: {t[i:i + 2] for i in range(len(t) - 1)}
    x, y = b(norm_title(t1)), b(norm_title(t2))
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= th


def to_record(a: dict, now: dt.datetime) -> dict:
    return {"url": a["url"], "title": a["title"], "press": a["press"], "pub": a["pub"].isoformat(),
            "summary": a["summary"], "group": a.get("group"), "found": now.isoformat()}


def from_record(r: dict) -> dict:
    r = dict(r)
    r["pub"] = dt.datetime.fromisoformat(r["pub"])
    return r


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
    arts = collect(seen, now, state.get("last_run", ""))
    decode_links(arts)
    arts = dedupe(arts, seen, state, now)
    cutoff = now - dt.timedelta(hours=12)
    RECENT_SENT[:] = [r[0] for r in state.get("recent", []) if dt.datetime.fromisoformat(r[1]) >= cutoff] + \
        [p["title"] for p in state.get("pending", [])]
    for i in range(0, len(arts), BATCH):           # 기사가 많으면 나눠서 요약·판단
        summarize(arts[i:i + BATCH])

    for a in arts:                                 # 무관 기사도 다시 안 보도록 기록(번호·제목·주소 모두)
        seen[a["key"]] = now.isoformat()
        seen["t:" + norm_title(a["title"])] = now.isoformat()
        seen["u:" + a["url"]] = now.isoformat()
    dups = [a for a in arts if a["relevant"] and a.get("dup")]
    if dups:
        print(f"이미 보낸 기사와 같은 사안 {len(dups)}건 제외: " + " / ".join(a["title"][:30] for a in dups))
    arts = [a for a in arts if a["relevant"] and not a.get("dup")]
    state["recent"] = [r for r in state.get("recent", [])
                       if now - dt.datetime.fromisoformat(r[1]) < dt.timedelta(hours=MAX_AGE_HOURS)]
    state["recent"] += [[a["title"], now.isoformat()] for a in arts]

    # ① 중수청 기사: 바로 발송
    urgent = [a for a in arts if a["priority"]]    # 건수 제한 없음(긴 경우 여러 통으로 나눠 발송)
    if urgent:
        head = f"⚡ <b>중수청 언론보도</b>\n{now:%m.%d. %H:%M} 기준 신규 {len(urgent)}건\n"
        for msg in format_messages(urgent, now, head):
            send_telegram(msg)
        print(f"중수청 {len(urgent)}건 즉시 발송")
    # ② 공소청·형사사법 기사: 대기 목록에 쌓았다가 마지막 모음 발송 후 DIGEST_MINUTES 지나면 발송
    shown = urgent
    for a in arts:
        if not a["priority"]:
            if any(similar(a["title"], b["title"]) for b in shown):
                continue                           # 방금 즉시 발송한 기사와 같은 내용이면 모음에서 제외
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
        head = (f"📚 <b>공소청·형사사법 모음</b>\n"
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

    state.pop("daily", None), state.pop("daily_sent", None)   # 예전 대응 검토 기록 정리
    state["last_run"] = now.isoformat()
    save_seen(seen, now)                           # 발송 성공 후에만 기록
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
