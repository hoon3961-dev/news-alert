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
# ── 네이버 뉴스 검색(NAVER API HUB). 키가 없거나 NAVER_SEARCH=off 이면 구글만 사용 ──
NAVER_ID = (os.getenv("NAVER_APIHUB_KEY_ID") or "").strip()
NAVER_KEY = (os.getenv("NAVER_APIHUB_KEY") or "").strip()
NAVER_ON = ((os.getenv("NAVER_SEARCH") or "on").strip().lower() != "off") and bool(NAVER_ID and NAVER_KEY)
NAVER_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_DISPLAY = int(os.getenv("NAVER_DISPLAY") or "100")   # 한 번에 받는 건수(최대 100)
_NAVER_FAILS = [0]
BGM_USED = [""]                                     # 이번 브리핑에 쓴 배경음악(기록용)
AUDIO_ERR: list = []                                # 음성 브리핑 중 AI 오류(state.json에 남겨 원인 확인용)                                 # 네이버 호출이 3번 실패하면 이번 실행에서는 더 부르지 않음
DROPPED: list = []                                 # 이번 실행에서 거른 기사 [시각, 이유, 언론사, 제목] → state.json에 보관
def _naver_queries() -> list:
    raw = os.getenv("NAVER_QUERIES")
    qs = [q.strip() for q in raw.split(",") if q.strip()] if raw else \
        [x.strip() for q in QUERIES for x in re.split(r"\s+OR\s+", q) if x.strip()]
    return list(dict.fromkeys(qs))
NAVER_QUERIES = _naver_queries()
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
    # 11.12. 스크랩 대조 후 추가(도메인 미확인은 이름만 등록 — 구글 표기와 일치해야 함)
    "울산신문": "ulsanpress.net", "전라일보": "jeollailbo.com", "광주타임스": "", "JCN울산중앙방송": "", "알쓸신문": "",
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

# ── 음성 브리핑(중수청 기사만, 평일 아침·오후) ──────────────────
# AUDIO=off 로 끄기. AUDIO_TIMES: 만드는 시각(쉼표 구분). 직전 브리핑 이후 보낸 중수청 기사를 담음
AUDIO_ON = (os.getenv("AUDIO") or "on").strip().lower() != "off"
AUDIO_TIMES = os.getenv("AUDIO_TIMES") or "07:10,17:00"
AUDIO_MAX = int(os.getenv("AUDIO_MAX") or "10")          # 한 번에 읽을 최대 사안 수
AUDIO_VOICE = (os.getenv("AUDIO_VOICE") or "").strip()  # 비우면 기본 목소리
AUDIO_TEST = (os.getenv("AUDIO_TEST") or "").strip().lower() == "true"   # Actions에서 수동 시험
GOOGLE_TTS_KEY = (os.getenv("GOOGLE_TTS_KEY") or "").strip()            # 있으면 구글 음성 사용
AUDIO_BGM = (os.getenv("AUDIO_BGM") or "on").strip().lower() != "off"   # 배경음악(직접 합성한 음악이라 저작권 문제 없음)
AUDIO_BGM_STYLE = int(os.getenv("AUDIO_BGM_STYLE") or "1")              # 1 속보 시그널(기본) ~ 5
# 외부 무료 음원 쓰기: AUDIO_BGM_URL(쉼표로 여러 개 → 브리핑마다 돌아가며) 또는 저장소 bgm 폴더의 mp3
# 저장소가 공개라 음원 파일을 올리면 남이 내려받을 수 있음 → 라이선스상 재배포 금지 음원은 URL 방식으로
AUDIO_BGM_URL = os.getenv("AUDIO_BGM_URL") or ""
AUDIO_RATE = float(os.getenv("AUDIO_RATE") or "0.95")    # 읽는 속도(1.0 보통, 0.9 느리게)
# 발음 사전: '바꿀말=읽을말'을 쉼표·줄바꿈으로 구분. 예) NDFAS=엔디파스, 이드러그=이 드러그
AUDIO_DICT = os.getenv("AUDIO_DICT") or ""

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


SCOOP_RE = re.compile(r"^\s*(?:[\[\(【〈<][^\]\)】〉>]{0,10}단독[^\]\)】〉>]{0,10}[\]\)】〉>]|단독\s*[:：\-·])")


def is_scoop(title: str) -> bool:
    """제목 맨 앞에 [단독]·(단독)·[속보·단독] 같은 표시가 붙은 기사 → 같은 사안이어도 중복으로 거르지 않음"""
    return bool(SCOOP_RE.match(title or ""))


def drop(reason: str, press: str, title: str, now: dt.datetime) -> None:
    """걸러낸 기사와 이유를 기록(state.json의 dropped, 나중에 '왜 안 왔나' 확인용)"""
    DROPPED.append([now.isoformat(), reason, press or "", (title or "")[:90]])


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


def naver_search(query: str) -> list:
    """네이버 뉴스 검색(최신순) → 구글 결과와 같은 형식의 목록"""
    if _NAVER_FAILS[0] >= 3:
        return []
    r = None
    try:
        r = requests.get(
            NAVER_URL,
            params={"query": query, "display": NAVER_DISPLAY, "start": 1, "sort": "date"},
            headers={"X-NCP-APIGW-API-KEY-ID": NAVER_ID, "X-NCP-APIGW-API-KEY": NAVER_KEY,
                     "Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        _NAVER_FAILS[0] += 1
        detail = ""
        if r is not None:
            detail = f" (응답 {r.status_code}: {r.text[:150]!r})"
        print(f"[경고] 네이버 검색 실패 {_NAVER_FAILS[0]}/3{detail}: {e}", file=sys.stderr)
        if _NAVER_FAILS[0] >= 3:
            print("[경고] 네이버 검색이 계속 실패해 이번 실행에서는 구글만 사용합니다. "
                  "(키·한도·API HUB 신청 상태 확인)", file=sys.stderr)
        raise
    items = []
    for it in data.get("items", []) if isinstance(data, dict) else []:
        orig = (it.get("originallink") or "").strip()
        link = orig or (it.get("link") or "").strip()
        if not link:
            continue
        items.append({
            "title": clean(it.get("title", "")),
            "link": link,
            "guid": "nv:" + link,
            "pubDate": it.get("pubDate", ""),
            "source": press_name(link),
            "source_url": link,
        })
    return items


def decode_links(arts: list) -> None:
    """구글 뉴스 중계 링크를 실제 언론사 주소로 변환 (실패하면 구글 링크 유지)"""
    arts = [a for a in arts if "news.google.com" in a["url"]]   # 네이버 결과는 이미 원문 주소
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
    sources = [(google_search, q) for q in QUERIES]
    if NAVER_ON:
        sources += [(naver_search, q) for q in NAVER_QUERIES]
    for fn, q in sources:
        try:
            items = fn(q)
        except Exception as e:
            if fn is google_search:
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
                if not key.startswith("nv:"):
                    seen[key] = now.isoformat()    # 지난 기사는 이후에도 보내지 않음(네이버는 기록 안 함: 매번 걸러져 용량만 커짐)
                continue
            press = it["source"]
            title = html.unescape(it["title"]).strip()
            if (any(x in press or x in it["source_url"] or title.endswith(x) for x in EXCLUDE_PRESS)
                    or not press_allowed(press, it["source_url"])):
                if not key.startswith("nv:"):
                    seen[key] = now.isoformat()    # 제외 언론사
                if any(t in title for t in CORE_TERMS + PRIORITY_TERMS):
                    drop(f"언론사 제외({press_name(it['source_url']) or press})", press or press_name(it["source_url"]), title, now)
                continue
            if press and title.endswith(" - " + press):   # 구글 뉴스 제목 끝의 ' - 언론사' 제거
                title = title[: -len(press) - 3].strip()
            if press and title.endswith(":" + press):     # 제목 끝 ':언론사' 제거
                title = title[: -len(press) - 1].strip()
            nt = norm_title(title)
            if nt in titles or ("t:" + nt) in seen:  # 같은 제목 중복 제거(이전 실행에서 처리한 제목 포함)
                if not key.startswith("nv:"):
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
    n_nv = sum(1 for k in found if k.startswith("nv:"))
    print(f"검색 후보 {len(found)}건 (구글 {len(found) - n_nv}건 · 네이버 {n_nv}건)"
          + ("" if NAVER_ON else " · 네이버 검색 꺼짐"))
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
            try:                                   # 기사 페이지에 적힌 작성일(구글 날짜가 틀릴 때 대비)
                meta = trafilatura.extract_metadata(r.text)
                art["page_date"] = (meta.date if meta and meta.date else "")[:10]
            except Exception:
                art["page_date"] = ""
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
            a["why"] = "제외 단어"
        elif a["body"] and not any(t in text for t in CONTEXT_TERMS):
            a["relevant"] = False                  # 수사·사법 관련 단어가 전혀 없는 기사(동명이인 등)
            a["why"] = "수사·사법 단어 없음"
        # AI 키가 없을 때: 본문 첫머리 120자 정도를 요지로 사용
        first = re.sub(r"\s+", " ", a["body"])[:120]
        a["summary"] = (first.rsplit(" ", 1)[0] + "…") if len(first) == 120 else first
        # 중수청 기사 판별(기본): 제목에 우선 단어, 또는 본문 첫머리 300자에 중수청
        a["priority"] = a["priority"] or any(t in a["body"][:300] for t in LEAD_TERMS)
        a["group"] = None
        a["dup"] = False
        pd_ = a.get("page_date", "")
        # 기사 페이지에서 '월'만 읽고 일을 1일로 채운 날짜(예: 2026-11-01)가 많아 1일은 믿지 않음. 네이버는 시각이 정확해 검사 안 함
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pd_ or "") and not pd_.endswith("-01") \
                and not a["key"].startswith("nv:") and \
                dt.date.fromisoformat(pd_) < (dt.datetime.now(KST).date() - dt.timedelta(days=2)):
            a["relevant"] = False                  # 기사 페이지상 작성일이 오래된 기사(구글 날짜 오류)
            a["old"] = True
            a["why"] = f"작성일 오래됨({pd_})"
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
        f"오늘은 {dt.datetime.now(KST):%Y-%m-%d}이다. 중수청·공소청은 2026-10-02 출범, 김지용 청장은 2026-10-16 임명재가·10-19 임명장 수여.\n"
        "old 판단: 기사 내용이 이미 며칠 지난 사건을 처음 알리는 당시 보도(예: 청장 임명 재가, 출범 첫날 소식 등)여서 "
        "오늘 기준 2일 넘게 지난 기사로 보이면 old=true. 지난 일을 다루더라도 오늘 새로 쓴 분석·후속 기사면 false.\n"
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
        "표현만 다른 같은 내용(예: '고발건 배당'과 '수사 착수')은 dup=true. 단, 제목이 [단독]으로 시작하는 기사는 항상 dup=false.\n"
        "출력은 JSON 배열만: [{\"id\": 0, \"summary\": \"...\", \"relevant\": true, \"jsc\": true, \"group\": 1, "
        "\"dup\": false, \"old\": false}, ...]\n\n"
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
            if arts[i]["relevant"] and not bool(d.get("relevant", True)):
                arts[i]["why"] = "AI 관련 없음"
            arts[i]["relevant"] = arts[i]["relevant"] and bool(d.get("relevant", True))
            arts[i]["dup"] = bool(d.get("dup", False)) and not is_scoop(arts[i]["title"])   # [단독]은 중복 판단 제외
            if d.get("old"):
                arts[i]["relevant"] = False
                arts[i]["old"] = True
                arts[i]["why"] = "AI 지난 기사"
            if "jsc" in d:
                arts[i]["priority"] = bool(d["jsc"]) or any(t in arts[i]["title"] for t in PRIORITY_TERMS)
            if d.get("group") is not None:
                arts[i]["group"] = str(d["group"])


def strict_filter(arts: list) -> None:
    """AI 판단을 못 쓸 때: 제목·본문에 핵심 단어가 없는 기사는 제외"""
    for a in arts:
        if a["relevant"] and not any(t in a["title"] + " " + a["body"] for t in CORE_TERMS):
            a["relevant"] = False
            a["why"] = "핵심 단어 없음(AI 실패 시)"


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
        th = 0.95 if is_scoop(a["title"]) else 0.75   # [단독]은 사실상 같은 제목(재검색)일 때만 중복 처리
        dup = (a["url"] in urls or ("u:" + a["url"]) in seen
               or any(similar(a["title"], t, th) for t in recent))
        if dup:
            seen[a["key"]] = now.isoformat()
            drop("중복(같은 주소·비슷한 제목)", a["press"], a["title"], now)
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
            "summary": a["summary"], "group": a.get("group"), "found": now.isoformat(),
            "jsc": bool(a.get("priority"))}


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



# ── 음성 브리핑 ───────────────────────────────────────────────
def _kr_holidays():
    try:
        import holidays
        return holidays.KR(years=range(dt.date.today().year - 1, dt.date.today().year + 2))
    except Exception:
        return {}


def audio_due(state: dict, now: dt.datetime):
    """만들 차례면 '아침'/'오후'(시험이면 '시험'), 아니면 None. 평일·비공휴일만, 예정 시각부터 3시간 안"""
    if AUDIO_TEST:
        return "시험"
    if not AUDIO_ON or now.weekday() >= 5 or now.date() in _kr_holidays():
        return None
    last = dt.datetime.fromisoformat(state["last_audio"]) if state.get("last_audio") else None
    for t in AUDIO_TIMES.split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", t)
        if not m:
            continue
        slot = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if slot <= now < slot + dt.timedelta(hours=3) and (last is None or last < slot):
            return "아침" if slot.hour < 12 else "오후"
    return None


def ask_claude(prompt: str, max_tokens: int = 8000, timeout: int = 150) -> str:
    """음성 브리핑용 AI 호출. 답의 글 부분만 돌려줌(생각 블록 등은 건너뜀). 실패하면 오류 기록 후 ''
    답이 길이 제한에 걸려 잘리면 제한을 늘려 다시 요청함"""
    no_think = True                                # '생각' 단계를 끄고(답이 잘리는 원인) 안 되면 기본으로
    for attempt in range(3):
        try:
            body = {"model": CLAUDE_MODEL, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            if no_think:
                body["thinking"] = {"type": "disabled"}
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body, timeout=timeout,
            )
            if not r.ok:
                if no_think and r.status_code == 400 and "thinking" in r.text:
                    no_think = False
                    continue
                raise RuntimeError(f"{r.status_code} {r.text[:300]}")
            res = r.json()
            text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type", "text") == "text").strip()
            if res.get("stop_reason") == "max_tokens":
                AUDIO_ERR.append(f"답변 잘림(max_tokens={max_tokens}, 글 {len(text)}자, 블록={[c.get('type') for c in res.get('content', [])]})")
                max_tokens = min(max_tokens * 2, 32000)
                continue
            if not text:
                raise RuntimeError(f"빈 답변 stop={res.get('stop_reason')} 블록={[c.get('type') for c in res.get('content', [])]}")
            return text
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            AUDIO_ERR.append(msg)
            print(f"[음성 AI 오류] {msg}", file=sys.stderr)
            time.sleep(5)
    return ""


_STOP = {"중수청", "공소청", "중대범죄수사청", "단독", "속보", "종합", "기자", "뉴스", "관련", "의혹", "사건", "논란"}


def _words(t: str) -> set:
    return {w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", re.sub(r"\[[^\]]*\]", " ", t)) if w not in _STOP}


def audio_cluster(groups: list) -> list:
    """여러 실행에 걸쳐 들어온 같은 사안 기사들을 하나로 묶음 → [[그룹번호...], ...] (중요한 사안부터)
    AI로 묶고, 실패하면 제목·요약에 겹치는 단어가 많은 것끼리 묶음"""
    n = len(groups)
    if ANTHROPIC_KEY and n > 1:
        lines = "\n".join(f"[{i}] {g[0]['title']} / {(g[0]['summary'] or '')[:120]}" for i, g in enumerate(groups))
        prompt = ("아래 기사 목록에서 같은 사안(같은 사건·발표·인사·조치)을 다룬 기사 번호끼리 묶어라. "
                  "표현이 달라도 같은 일이면 같은 묶음이다. 같은 기관·같은 주제라도 다른 일(예: 장비 이관과 청사 이전)은 따로 둔다. 애매하면 따로 둔다. "
                  "묶음 순서는 중요도 순(기사 수가 많을수록, 중수청 조직·청장·주요 사건일수록 앞). "
                  "모든 번호가 정확히 한 번씩 들어가야 한다. JSON 배열만 출력: [[0,3],[1],[2,4]]\n\n" + lines)
        try:
            text = ask_claude(prompt, 4000)
            data, _ = json.JSONDecoder().raw_decode(text[text.index("["):])   # 뒤에 설명이 붙어도 첫 배열만
            out, used = [], set()
            for c in data:
                c = [int(i) for i in (c if isinstance(c, list) else [c]) if 0 <= int(i) < n and int(i) not in used]
                used.update(c)
                if c:
                    out.append(c)
            out += [[i] for i in range(n) if i not in used]
            return out
        except Exception as e:
            AUDIO_ERR.append(f"사안 묶기: {type(e).__name__}: {str(e)[:200]}")
            print(f"[음성 사안 묶기 AI 오류] {type(e).__name__}: {e}", file=sys.stderr)
    ws = [_words(g[0]["title"] + " " + (g[0]["summary"] or "")[:80]) for g in groups]
    out, where = [], {}
    for i in range(n):
        for j in range(i):
            inter = ws[i] & ws[j]
            jac = len(inter) / max(1, len(ws[i] | ws[j]))
            if jac >= 0.3 or (len(inter) >= 3 and jac >= 0.15):
                out[where[j]].append(i)
                where[i] = where[j]
                break
        else:
            where[i] = len(out)
            out.append([i])
    out.sort(key=lambda c: -sum(len(groups[i]) for i in c))
    return out


def audio_script(groups: list, label: str, since: dt.datetime, now: dt.datetime, more: int) -> str:
    """AI가 뉴스 앵커 원고로 정리. 실패하면 요약을 그대로 이어 붙임"""
    items = []
    for n, g in enumerate(groups, 1):
        sums = []
        for x in g[:4]:                            # 같은 사안 여러 기사의 요약을 합쳐 한 번에 전하게
            if x["summary"] and x["summary"] not in sums:
                sums.append(x["summary"])
        items.append(f"[사안 {n}] 제목: {g[0]['title']}\n관련 기사 {len(g)}건 요약:\n" + "\n".join(sums or ["(요약 없음)"]))
    wd = "월화수목금토일"[now.weekday()]
    intro = f"{now.month}월 {now.day}일 {wd}요일 {label}, 중수청 언론보도 브리핑입니다."
    outro = (f"이 밖에도 중수청 관련 소식이 {more}가지 더 있으니, 텔레그램 알림에서 확인해 주십시오. " if more else "") + \
        "지금까지 중수청 언론보도 브리핑이었습니다."
    if ANTHROPIC_KEY:
        prompt = (
            "너는 아침·저녁 라디오 뉴스 앵커다. 아래 기사들로 3분 안팎의 라디오 뉴스 원고를 써라. "
            "듣는 사람은 출근길이나 퇴근길에 귀로만 듣는 중대범죄수사청(중수청) 직원이다.\n"
            f"- 첫머리는 그대로: {intro}\n- 맺음말은 그대로: {outro}\n"
            "[구성]\n"
            "- 사안 번호 순서대로, 사안 하나당 한 문단으로 전한다(같은 사안 안의 반응·발언은 그 문단 안에서). "
            "다만 한 사안 묶음 안에 서로 다른 일이 섞여 있으면 억지로 이어 붙이지 말고 별도 문단으로 나눈다. "
            "'첫 번째, 두 번째' 같은 순서 숫자는 쓰지 않는다. "
            "'사설 소식', '단독 소식'처럼 기사 형식을 소식 이름으로 쓰지 않는다. 사설·칼럼은 '한 신문은 사설에서 ~라고 지적했습니다'처럼 전한다.\n"
            "- 한 사안은 딱 한 번만 다룬다. 같은 사안의 여러 요약은 합쳐서 겹치는 내용 없이 전한다. "
            "앞에서 이미 말한 사실을 뒤 사안에서 되풀이하지 않는다.\n"
            "- 사안마다 핵심을 한 문장으로 먼저 말하고(무슨 일인지), 이어서 배경이나 의미를 1~2문장 덧붙인다.\n"
            "- 사안을 시작할 때 '먼저', '이어서', '다음 소식입니다', '한편', '마지막 소식입니다'처럼 자연스럽게 넘긴다. 매번 같은 말을 반복하지 않는다.\n"
            "- 언론사 이름이나 '보도에 따르면' 같은 출처 표현은 말하지 않는다. 사실 자체를 바로 전한다.\n"
            "[말투]\n"
            "- 모든 문장은 반드시 '~습니다', '~입니다'로 끝낸다(방송 뉴스 말투). 요약에 있는 '~함, ~임, ~됨, ~했음, ~예정' 같은 개조식 끝맺음을 그대로 옮기지 않는다. "
            "예: '회의에 불참함' → '회의에 불참했습니다', '조직이 개편되었음' → '조직이 개편되었습니다', '첫 사례임' → '첫 사례입니다', '발표할 예정' → '발표할 예정입니다'.\n"
            "- 한 문장은 40자 안팎으로 짧게 끊는다. 한 문장에 정보는 하나만.\n"
            "- 신문 제목식 표현(명사로 끝나는 문장, 생략된 조사, '…' 줄임)을 쓰지 말고 완전한 문장으로 풀어 말한다.\n"
            "- 사람 직함은 요약에 나온 그대로만 쓴다. 요약에 없는 직함·소속을 붙이지 않는다(기억에 의존한 직함은 틀릴 수 있음. "
            "예: 요약에 '한동훈 딸'이라고만 있으면 '한동훈 씨의 딸'로 쓰고 장관·대표 같은 직함을 붙이지 않는다). 어려운 법률 용어는 쉬운 말로 한 번 풀어 준다(예: 불송치, 즉 경찰이 사건을 검찰로 넘기지 않기로 한 것).\n"
            "- 숫자는 듣기 쉽게: '87건', '3억 원', '11월 16일'. 퍼센트는 '퍼센트'로.\n"
            "- 괄호, 따옴표, 대괄호, 기호(·, →, ~, /), 영문 약어는 쓰지 않는다. [단독] 표시는 읽지 않는다.\n"
            "[금지]\n"
            "- 제목·요약에 없는 사실, 숫자, 평가, 전망을 지어내지 않는다. 앵커의 개인 의견을 넣지 않는다. "
            "'~한 셈입니다', '도마에 올랐습니다', '당면 과제로 떠올랐습니다'처럼 해석·평가를 덧붙이는 표현도 쓰지 않는다.\n"
            "- 앞 문단에서 이미 설명한 용어나 사실(예: 시스템 이름 설명)은 뒤에서 다시 설명하지 않고 이름만 쓴다.\n"
            "- 한자 약칭(李, 尹, 故, 與, 野, 檢)은 한글로 풀어 쓴다(이재명 대통령, 고, 여당, 야당, 검찰). 영문 약어는 한글 소리로 쓴다(NDFAS는 엔디파스).\n"
            "- 첫머리, 각 사안, 맺음말은 각각 한 문단으로 쓰고 문단 사이는 빈 줄로 구분한다(읽을 때 쉬는 자리).\n"
            "- 제목, 번호, 소제목, 마크다운 기호 없이 소리 내어 읽을 문장만 출력한다. 전체 1,800자 이내.\n\n"
            + "\n\n".join(items)
        )
        text = ask_claude(prompt)
        if len(text) > 50:
            if "브리핑이었습니다" not in text[-60:]:      # 끝이 잘렸으면 마지막 완성 문장까지만 + 맺음말
                AUDIO_ERR.append(f"대본 끝 누락 보정(원래 {len(text)}자)")
                cut = max(text.rfind("니다."), text.rfind("니다\n"))
                text = (text[:cut + 3] if cut > 0 else text) + "\n\n" + outro
            return text
    parts = [intro]
    for n, g in enumerate(groups, 1):
        a = g[0]
        lead = "먼저, " if n == 1 else ("마지막 소식입니다. " if n == len(groups) else "다음 소식입니다. ")
        parts.append(lead + tidy_summary(a["summary"] or a["title"]))
    parts.append(outro)
    return "\n".join(parts)


_POLITE = [  # 문장 끝 개조식·해라체 → 합니다체 (AI가 놓쳐도 음성은 존댓말로)
    (r"(였|었|았|했|됐|겠)(음|다)", r"\1습니다"), (r"되었(음|다)", "되었습니다"),
    (r"(있|없|같|않)(음|다)", r"\1습니다"), (r"(하|되)?였음", r"\1였습니다"),
    (r"함", "했습니다"), (r"됨", "됐습니다"), (r"임", "입니다"), (r"이다", "입니다"),
    (r"한다", "합니다"), (r"된다", "됩니다"),
    (r"(예정|전망|방침|계획|입장|상황|상태|방안|취지|계획| 중)", r"\1입니다"),
]


_NOUN_M = ("처음", "다음", "지금", "이름", "사람", "마음", "느낌", "요금", "기금", "소금", "바람", "고름", "기름", "모임", "구름", "얼음", "걸음")
_PAST = {"ㅣ": "ㅕ", "ㅗ": "ㅘ", "ㅜ": "ㅝ", "ㅡ": "ㅓ", "ㅏ": "ㅏ", "ㅓ": "ㅓ", "ㅐ": "ㅐ", "ㅕ": "ㅕ", "ㅔ": "ㅔ", "ㅚ": "ㅙ"}
_V = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"


def _nominal_past(body: str) -> str:
    """'밝힘'→'밝혔습니다', '나옴'→'나왔습니다', '전해짐'→'전해졌습니다' (받침 ㅁ 명사형 어미)"""
    if len(body) < 2 or not ("가" <= body[-1] <= "힣") or body.endswith(_NOUN_M) or body.endswith(("함", "됨", "임", "음")):
        return body
    code = ord(body[-1]) - 0xAC00
    if code % 28 != 16:                            # 받침 ㅁ이 아니면 그대로
        return body
    cho, jung = code // 588, (code % 588) // 28
    v = _PAST.get(_V[jung])
    if not v:
        return body
    return body[:-1] + chr(0xAC00 + cho * 588 + _V.index(v) * 28 + 20) + "습니다"   # 받침 ㅆ


def polite(text: str) -> str:
    out = []
    for sent in re.split(r"(?<=[.!?\n])", text):
        m = re.match(r"(.*?)([.!?]?)(\s*)$", sent, re.S)
        body, punct, sp = m.group(1), m.group(2), m.group(3)
        if body and not re.search(r"(니다|니까|세요|시오|까요|어요|에요|예요|해요|죠)$", body):
            last2 = body[-2:]
            if len(last2) == 2 and last2[1] in "다음" and "가" <= last2[0] <= "힣" and (ord(last2[0]) - 0xAC00) % 28 == 20:
                body = body[:-1] + "습니다"            # 밝혔다·밝혔음·보였다 등 받침 ㅆ + 다/음
            body = _nominal_past(body)
            for pat, sub in ([] if body.endswith("니다") else _POLITE):
                new = re.sub(pat + r"$", sub, body)
                if new != body:
                    body = new
                    break
            if body and punct == "" and re.search(r"[가-힣]$", body):
                punct = "."
        out.append(body + punct + sp)
    return "".join(out)


_PRON = [  # (정규식, 바꿀 말) — 귀로 들을 때 어색한 표기를 소리 나는 대로
    (r"5\s?[·.]\s?18", "오일팔"), (r"4\s?[·.]\s?19", "사일구"), (r"6\s?[·.]\s?25", "육이오"),
    (r"12\s?[·.]\s?12", "십이 십이"), (r"12\s?[·.]\s?3(?!\d)", "십이 삼"), (r"3\s?[·.]\s?1절", "삼일절"),
    (r"(\d+)\s?·\s?(\d+)", r"\1 \2"),                                # 그 밖의 숫자·숫자
    (r"([A-Za-z]{2,})\s?\(([가-힣\s]{1,20})\)", r"\2"),           # NDFAS(엔디파스) → 엔디파스
    (r"([가-힣]+)\s?\(([A-Za-z0-9\s\-]{1,20})\)", r"\1"),          # 엔디파스(NDFAS) → 엔디파스
    (r"\(([\u4e00-\u9fff]{1,4})\)", ""),                           # 고(故) → 고
    (r"李\s*대통령", "이재명 대통령"), (r"李", "이재명 대통령"), (r"尹\s*전\s*대통령", "윤석열 전 대통령"),
    (r"尹", "윤석열 전 대통령"), (r"故\s*", "고 "), (r"與", "여당"), (r"野", "야당"), (r"檢", "검찰"),
    (r"警", "경찰"), (r"靑", "청와대"), (r"軍", "군"), (r"美", "미국"), (r"中(?=[\s,])", "중국"), (r"日(?=[\s,])", "일본"),
    (r"\b([A-Z])\s?씨", lambda m: {"A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "에프", "G": "지", "H": "에이치", "K": "케이", "L": "엘", "M": "엠", "P": "피", "S": "에스", "Y": "와이"}.get(m.group(1), m.group(1)) + " 씨"),
    (r"NDFAS", "엔디파스"), (r"KOLAS", "콜라스"), (r"\bAI\b", "에이아이"), (r"\bCCTV\b", "씨씨티비"),
    (r"\bTF\b", "티에프"), (r"\bSNS\b", "에스엔에스"), (r"\bMOU\b", "업무협약"),
    (r"(\d{1,2})\.\s?(\d{1,2})\.(?!\d)", r"\1월 \2일"),          # 11.17. → 11월 17일
    (r"(\d{1,2}):(\d{2})", r"\1시 \2분"),                         # 9:20 → 9시 20분
    (r"(\d)\s?%", r"\1퍼센트"), (r"(\d)\s?~\s?(\d)", r"\1에서 \2"),
    (r"\([^)]{0,40}\)", " "), (r"(^|[\s.])[-–•]\s+", r"\1 "), (r"[/|]", ", "), (r"&", " 그리고 "),
]


def pronounce(text: str) -> str:
    pairs = [x.split("=", 1) for x in re.split(r"[,\n]", AUDIO_DICT) if "=" in x]
    for a, b in pairs:                             # 사용자 사전 먼저
        if a.strip():
            text = text.replace(a.strip(), b.strip())
    for pat, sub in _PRON:
        text = re.sub(pat, sub, text)
    return re.sub(r"\s*,\s*,", ",", text)


def tidy_summary(t: str) -> str:
    """AI 요약(개조식)을 대체 원고용 문장으로: 글머리 '-' 없애고, '(제목 기준…)' 같은 메모 삭제, 줄마다 문장으로"""
    t = re.sub(r"\((?:[^)]*(?:기준|본문|확인|추정)[^)]*)\)", "", t)
    lines = [re.sub(r"^\s*[-*•·]\s*", "", x).strip() for x in t.split("\n")]
    lines = [x if re.search(r"[.!?]$", x) else x + "." for x in lines if x]
    return " ".join(lines)


def speakable(text: str) -> str:
    text = pronounce(polite(text))
    text = re.sub(r"(?m)^\s*(#+|[-*•]|\d+[.)])\s+", "", text).replace("**", "")
    text = re.sub(r"[\[(（]\s*(단독|속보|종합|사설|칼럼|기고|포토|영상)\s*[\])）]", " ", text)   # 머리표는 읽지 않음
    text = re.sub(r"[\"“”‘’'「」『』]", "", text)                 # 따옴표는 붙여서('이드러그'도 → 이드러그도)
    text = re.sub(r"[\[\]【】〈〉<>]", " ", text)
    text = text.replace("·", " ").replace("→", ", ").replace("…", ", ")
    return re.sub(r"[ \t]+", " ", text).strip()


SENT_GAP = float(os.getenv("AUDIO_SENT_GAP") or "0.35")   # 문장 사이 쉼(초)
PARA_GAP = float(os.getenv("AUDIO_PARA_GAP") or "0.9")    # 문단(사안) 사이 쉼(초)


def _google_by_sentence(paras: list, voice: str, path: str) -> str:
    """문장마다 따로 읽혀서 사이에 정해진 길이의 무음을 넣음.
    한 번에 긴 글을 보내면 음성 엔진이 마침표·문단을 무시하고 이어 읽거나 엉뚱한 곳에서 쉬는 일이 있어,
    마침표와 문단 경계의 쉼을 프로그램이 직접 보장한다(쉼표 안쪽 억양은 엔진에 맡김)"""
    import base64
    import io
    import subprocess
    import wave
    sr, pcm = 24000, bytearray()
    for pi, p in enumerate(paras):
        sents = [x.strip() for x in re.split(r"(?<=[.?!])\s+", p) if x.strip()]
        for si, sent in enumerate(sents):
            r = requests.post(
                "https://texttospeech.googleapis.com/v1/text:synthesize",
                params={"key": GOOGLE_TTS_KEY},
                json={"input": {"text": sent}, "voice": {"languageCode": "ko-KR", "name": voice},
                      "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": sr,
                                      "speakingRate": AUDIO_RATE}},
                timeout=60,
            )
            if not r.ok:
                raise RuntimeError(f"구글 음성 오류 {r.status_code}: {r.text[:200]}")
            with wave.open(io.BytesIO(base64.b64decode(r.json()["audioContent"]))) as w:
                pcm += w.readframes(w.getnframes())
            last = si == len(sents) - 1
            if not (last and pi == len(paras) - 1):
                pcm += b"\x00\x00" * int(sr * (PARA_GAP if last else SENT_GAP))
    wav = path[:-4] + ".wav"
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(pcm))
    ff = _ffmpeg()
    if ff:
        subprocess.run([ff, "-y", "-v", "error", "-i", wav, "-b:a", "96k", path], check=True, capture_output=True, timeout=120)
    else:
        os.replace(wav, path)
    return f"구글 {voice} 문장별 쉼{SENT_GAP}/{PARA_GAP}초 속도{AUDIO_RATE}"


def tts_mp3(text: str, path: str) -> str:
    """원고 → mp3. GOOGLE_TTS_KEY가 있으면 구글, 없으면 edge-tts(무료). 쓴 엔진 이름을 돌려줌"""
    if GOOGLE_TTS_KEY:
        import base64
        voice = AUDIO_VOICE or "ko-KR-Neural2-A"
        paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
        pieces = []                                # 긴 문단은 문장 단위로 쪼갬(구글은 한 번에 5,000바이트까지)
        for p in paras:
            if len(p.encode()) <= 3500:
                pieces.append(p)
                continue
            cur = ""
            for sent in re.split(r"(?<=[.?!])\s+", p):
                if cur and len((cur + " " + sent).encode()) > 3500:
                    pieces.append(cur)
                    cur = sent
                else:
                    cur = (cur + " " + sent).strip()
            if cur:
                pieces.append(cur)
        chunks, cur = [], []                       # 문단 경계에서만 나눠 보냄(억양이 중간에 끊기지 않게)
        for p in pieces:
            if cur and len(" [pause long] ".join(cur + [p]).encode()) > 4200:
                chunks.append(cur)
                cur = [p]
            else:
                cur.append(p)
        if cur:
            chunks.append(cur)
        # 쉼 표시([pause long])는 Chirp 3 HD가 그 자리에 '음·어' 같은 군말을 넣는 경우가 있어 기본은 끔(AUDIO_PAUSE=on으로 켜기)
        use_markup = "chirp3" in voice.lower() and (os.getenv("AUDIO_PAUSE") or "off").strip().lower() == "on"
        if (os.getenv("AUDIO_SPLIT") or "on").strip().lower() != "off" and not use_markup:
            return _google_by_sentence(paras, voice, path)
        with open(path, "wb") as f:
            for c in chunks:
                for mode in (["markup", "text"] if use_markup else ["text"]):
                    body = " [pause long] ".join(c) if mode == "markup" else "\n\n".join(c)
                    r = requests.post(
                        "https://texttospeech.googleapis.com/v1/text:synthesize",
                        params={"key": GOOGLE_TTS_KEY},
                        json={"input": {mode: body}, "voice": {"languageCode": "ko-KR", "name": voice},
                              "audioConfig": {"audioEncoding": "MP3", "speakingRate": AUDIO_RATE}},
                        timeout=90,
                    )
                    if r.ok:
                        break
                    print(f"[구글 음성 {mode} 실패] {r.status_code}: {r.text[:150]}", file=sys.stderr)
                if not r.ok:
                    raise RuntimeError(f"구글 음성 오류 {r.status_code}: {r.text[:200]}")
                f.write(base64.b64decode(r.json()["audioContent"]))
        return f"구글 {voice}" + (" 쉼표시" if use_markup and mode == "markup" else "") + f" 속도{AUDIO_RATE}"
    import asyncio
    import edge_tts
    voice = AUDIO_VOICE or "ko-KR-SunHiNeural"
    rate = f"{round((AUDIO_RATE - 1) * 100):+d}%"
    asyncio.run(edge_tts.Communicate(re.sub(r"\n\s*\n", "\n", text), voice, rate=rate).save(path))
    if os.path.getsize(path) < 1000:
        raise RuntimeError("음성 파일이 비어 있음")
    return f"edge {voice}"



# ── 배경음악: 외부 음원 없이 코드로 직접 합성(저작권 걱정 없음) ──────────────
# AUDIO_BGM_STYLE: 1 속보 시그널 · 2 수사 드라마 · 3 탐사보도 · 4 긴박 리포트 · 5 정보 브리핑
_SR = 22050


def _write_wav(path: str, data: list, sr: int = _SR) -> None:
    import struct
    import wave
    peak = max(1e-9, max(abs(x) for x in data))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(x / peak * 0.9 * 32767)) for x in data))


def _mf(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


_SINE = (1.0, 0.25, 0.08)
_SAW = (1.0, 0.5, 0.33, 0.25, 0.2, 0.16, 0.12)
_SQR = (1.0, 0.0, 0.33, 0.0, 0.2, 0.0, 0.14)
_BRASS = (1.0, 0.7, 0.5, 0.35, 0.25, 0.15)


def _tone(buf, start, dur, m, amp, harm=_SINE, attack=0.005, decay=0.0, release=0.03, sr=_SR):
    import math
    i0, n = int(start * sr), int(dur * sr)
    w = 2 * math.pi * _mf(m) / sr
    hs = [(h + 1, a) for h, a in enumerate(harm) if a]
    for k in range(n):
        i = i0 + k
        if i >= len(buf):
            break
        t = k / sr
        env = min(1.0, t / attack) if attack else 1.0
        if decay:
            env *= math.exp(-t / decay)
        if t > dur - release:
            env *= max(0.0, (dur - t) / release)
        if env < 1e-4:
            continue
        buf[i] += amp * env * sum(a * math.sin(h * w * k) for h, a in hs)


def _kick(buf, start, amp, sr=_SR):
    import math
    i0, ph = int(start * sr), 0.0
    for k in range(int(0.3 * sr)):
        if i0 + k >= len(buf):
            break
        t = k / sr
        ph += 2 * math.pi * (45 + 100 * math.exp(-t / 0.025)) / sr
        buf[i0 + k] += amp * math.exp(-t / 0.11) * math.sin(ph)


def _noise(buf, start, amp, decay, rnd, dur=0.15, hp=True, sr=_SR):
    import math
    i0, prev = int(start * sr), 0.0
    for k in range(int(dur * sr)):
        if i0 + k >= len(buf):
            break
        x = rnd.random() * 2 - 1
        y, prev = (x - prev if hp else x), x
        buf[i0 + k] += amp * math.exp(-k / sr / decay) * y


def _snare(buf, start, amp, rnd):
    _noise(buf, start, amp, 0.07, rnd, dur=0.2)
    _tone(buf, start, 0.12, 55, amp * 0.6, attack=0.001, decay=0.04)


def _style(n: int):
    """(시작 시그널, 반복 반주) 두 목록을 돌려줌"""
    import random
    rnd = random.Random(n)
    if n == 1:      # 속보 시그널: 128BPM D단조, 16분음표 베이스 맥박 + 팀파니 + 스타카토 화음
        bpm, prog = 128, [(50, 53, 57), (46, 50, 53), (48, 52, 55), (50, 53, 57)]
    elif n == 2:    # 수사 드라마: 120BPM A단조, 째깍 초침 + 낮은 현악 스타카토 + 긴장 패드
        bpm, prog = 120, [(45, 48, 52), (41, 45, 48), (38, 41, 45), (40, 44, 47)]
    elif n == 3:    # 탐사보도: 132BPM E단조, 킥·하이햇 + 16분 아르페지오 + 저음 드론
        bpm, prog = 132, [(40, 43, 47), (36, 40, 43), (45, 48, 52), (47, 51, 54)]
    elif n == 4:    # 긴박 리포트: 140BPM C단조, 당김음 금관 스탭 + 8분 베이스 + 스네어
        bpm, prog = 140, [(48, 51, 55), (44, 48, 51), (51, 55, 58), (43, 47, 50)]
    else:           # 정보 브리핑: 124BPM G단조, 피아노 8분 반복 + 신스 베이스 + 가벼운 비트
        bpm, prog = 124, [(43, 46, 50), (39, 43, 46), (46, 50, 53), (41, 45, 48)]
    beat = 60.0 / bpm
    bar = 4 * beat
    # ── 반주(4마디 × 2바퀴) ──
    bars = prog * 2
    b = [0.0] * int(len(bars) * bar * _SR)
    for c, ch in enumerate(bars):
        t0 = c * bar
        root = ch[0] - 12
        if n == 1:
            for q in range(16):
                _tone(b, t0 + q * beat / 4, beat / 4 * 0.8, root, 0.30 if q % 4 == 0 else 0.18, _SAW, decay=0.08)
            for q in (0, 2):
                _tone(b, t0 + q * beat, 0.6, root - 12, 0.5, _SINE, attack=0.002, decay=0.35)   # 팀파니
            for q in (1.5, 3.5):
                for m in ch:
                    _tone(b, t0 + q * beat, 0.18, m + 12, 0.07, _BRASS, decay=0.08)
            for q in range(8):
                _noise(b, t0 + q * beat / 2, 0.05, 0.02, rnd, dur=0.04)
        elif n == 2:
            for q in range(16):
                _noise(b, t0 + q * beat / 4, 0.09 if q % 2 == 0 else 0.04, 0.008, rnd, dur=0.02)   # 초침
            pat = [0, 0, 3, 0, 0, 0, 3, 5]
            for q, d in enumerate(pat):
                _tone(b, t0 + q * beat / 2, beat / 2 * 0.6, ch[0] + d - 12, 0.22, _SAW, decay=0.12)
            for m in ch:
                _tone(b, t0, bar, m + 12, 0.05, _SINE, attack=0.4, release=0.2)
            _tone(b, t0, bar, ch[0] + 23, 0.025, _SINE, attack=0.6, release=0.3)               # 높은 긴장음
            _kick(b, t0, 0.35)
        elif n == 3:
            for q in range(4):
                _kick(b, t0 + q * beat, 0.5)
                _noise(b, t0 + q * beat + beat / 2, 0.10, 0.03, rnd, dur=0.06)
            arp = [ch[0], ch[1], ch[2], ch[2] + 5]
            for q in range(16):
                _tone(b, t0 + q * beat / 4, beat / 4 * 0.7, arp[q % 4] + 12, 0.10, _SQR, decay=0.07)
            _tone(b, t0, bar, root, 0.20, _SAW, attack=0.05, release=0.05)
        elif n == 4:
            for q in range(8):
                _tone(b, t0 + q * beat / 2, beat / 2 * 0.7, root, 0.25, _SAW, decay=0.1)
            for q in (0, 1.5, 3, 3.5):
                for m in ch:
                    _tone(b, t0 + q * beat, 0.16, m + 12, 0.08, _BRASS, decay=0.07)
            _kick(b, t0, 0.5)
            _kick(b, t0 + 2.5 * beat, 0.4)
            for q in (1, 3):
                _snare(b, t0 + q * beat, 0.18, rnd)
            for q in range(8):
                _noise(b, t0 + q * beat / 2, 0.04, 0.015, rnd, dur=0.03)
        else:
            pat = [ch[0] + 12, ch[2] + 12, ch[1] + 12, ch[2] + 12] * 2
            for q, m in enumerate(pat):
                _tone(b, t0 + q * beat / 2, beat / 2, m, 0.12, _SINE, decay=0.25)               # 피아노 느낌
            for q in (0, 1.5, 2, 3):
                _tone(b, t0 + q * beat, beat * 0.45, root, 0.22, _SQR, decay=0.15)
            _kick(b, t0, 0.35)
            _kick(b, t0 + 2 * beat, 0.3)
            for q in range(8):
                _noise(b, t0 + q * beat / 2, 0.035, 0.02, rnd, dur=0.04)
    # ── 시작 시그널(약 3초) ──
    j = [0.0] * int(3.4 * _SR)
    ch = prog[0]
    if n in (1, 4):          # 빠른 상승 스탭 3번 + 화음 히트
        for i, m in enumerate((ch[0], ch[1], ch[2])):
            for x in (0, 12):
                _tone(j, i * 0.12, 0.1, m + x, 0.18, _BRASS, decay=0.06)
        for m in ch + (ch[0] + 12, ch[0] - 12, ch[0] - 24):
            _tone(j, 0.42, 2.6, m, 0.13, _BRASS, attack=0.004, decay=0.7, release=0.4)
        _tone(j, 0.42, 2.0, ch[0] - 24, 0.5, _SINE, attack=0.002, decay=0.5)
        _noise(j, 0.42, 0.10, 0.2, rnd, dur=0.6, hp=False)
    elif n == 2:             # 낮은 '둥-' 두 번 + 긴장 화음
        for t in (0.0, 0.6):
            _tone(j, t, 0.9, ch[0] - 24, 0.6, _SINE, attack=0.002, decay=0.4)
            _tone(j, t, 0.9, ch[0] - 12, 0.25, _SAW, attack=0.002, decay=0.25)
        for m in (ch[0], ch[0] + 1, ch[2]):
            _tone(j, 1.2, 2.1, m + 12, 0.08, _SAW, attack=0.3, release=0.5)
    elif n == 3:             # 빠른 아르페지오 상승 + 킥
        for i in range(8):
            _tone(j, i * 0.07, 0.12, ch[i % 3] + 12 * (i // 3), 0.15, _SQR, decay=0.06)
        _kick(j, 0.6, 0.7)
        for m in ch:
            _tone(j, 0.6, 2.5, m + 12, 0.1, _SAW, attack=0.01, decay=0.8, release=0.4)
    else:                    # 피아노 3음 모티프 + 화음
        for i, m in enumerate((ch[2] + 12, ch[1] + 12, ch[0] + 24)):
            _tone(j, i * 0.18, 0.5, m, 0.3, _SINE, decay=0.3)
        _kick(j, 0.6, 0.5)
        for m in ch + (ch[0] + 12,):
            _tone(j, 0.6, 2.6, m + 12, 0.15, _SINE, attack=0.003, decay=0.9, release=0.4)
        _tone(j, 0.6, 2.6, ch[0] - 12, 0.3, _SQR, decay=0.6)
    return j, b


def make_bgm(folder: str, style: int = 1):
    j, b = _style(style)
    jp, bp = os.path.join(folder, f"bgm_intro{style}.wav"), os.path.join(folder, f"bgm_bed{style}.wav")
    _write_wav(jp, j)
    _write_wav(bp, b)
    return jp, bp


def _ffmpeg() -> str:
    """ffmpeg 실행 파일 위치(없으면 pip의 imageio-ffmpeg에 든 것)"""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _duration(ff: str, path: str) -> float:
    import subprocess
    err = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=60).stderr
    h, m, sec = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def pick_music(folder: str, now: dt.datetime) -> str:
    """사용자가 정한 음원(URL 또는 bgm 폴더) 중 이번 브리핑에 쓸 것 하나. 없으면 ''"""
    import glob
    urls = [u.strip() for u in AUDIO_BGM_URL.split(",") if u.strip().startswith("http")]
    files = sorted(glob.glob("bgm/*.mp3") + glob.glob("bgm/*.m4a") + glob.glob("bgm/*.wav"))
    items = urls + files
    if not items:
        return ""
    pick = items[(now.toordinal() * 2 + (now.hour >= 12)) % len(items)]   # 브리핑마다 돌아가며
    if not pick.startswith("http"):
        return pick
    try:
        r = requests.get(pick, headers=UA, timeout=60)
        r.raise_for_status()
        if len(r.content) < 20000:
            raise ValueError(f"파일이 너무 작음({len(r.content)}바이트) — 음원 직접 주소가 아닐 수 있음")
        path = os.path.join(folder, "bgm_download" + os.path.splitext(urlparse(pick).path)[1][:5])
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception as e:
        print(f"[배경음악 내려받기 실패] {pick[:80]} {type(e).__name__}: {e}", file=sys.stderr)
        return ""


def add_bgm(voice: str, now: dt.datetime = None) -> str:
    """음성 앞에 시작 음, 밑에 작은 반주, 끝에 반주가 잠깐 커졌다 사라지게. 실패하면 음성만"""
    import subprocess
    ff = _ffmpeg()
    if not AUDIO_BGM or not ff:
        return voice
    try:
        music = pick_music(os.path.dirname(os.path.abspath(voice)), now or dt.datetime.now(KST))
        if music:                                  # 외부 음원: 처음 3초는 크게 → 목소리 밑에 작게 → 끝에 다시 커졌다 사라짐
            try:
                dur = _duration(ff, voice)
                lead, tail = 3.0, 4.0
                total, end = lead + dur + tail, lead + dur
                out = voice[:-4] + "_bgm.mp3"
                vol = (f"if(lt(t,{lead - 0.8:.2f}),0.6,if(lt(t,{lead:.2f}),0.6-0.48*(t-{lead - 0.8:.2f})/0.8,"
                       f"0.12+0.2*clip((t-{end:.2f})/1.0,0,1)))")
                fc = (f"[0:a]aresample=44100,aformat=channel_layouts=mono,adelay={int(lead * 1000)}:all=1[v];"
                      f"[1:a]aresample=44100,aformat=channel_layouts=mono,atrim=0:{total:.2f},asetpts=PTS-STARTPTS,"
                      f"volume='{vol}':eval=frame,afade=t=in:st=0:d=0.5,afade=t=out:st={total - 2.5:.2f}:d=2.5[b];"
                      f"[v][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[o]")
                subprocess.run([ff, "-y", "-v", "error", "-i", voice, "-stream_loop", "-1", "-i", music,
                                "-filter_complex", fc, "-map", "[o]", "-t", f"{total:.2f}", "-ac", "1", "-b:a", "96k", out],
                               check=True, capture_output=True, timeout=180)
                if os.path.getsize(out) > 1000:
                    BGM_USED[0] = "지정 음원" if AUDIO_BGM_URL else os.path.basename(music)
                    print(f"배경음악: {BGM_USED[0]} 사용")
                    return out
            except Exception as e:
                print(f"[지정 배경음악 실패 → 기본 음악 사용] {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
        folder = os.path.dirname(os.path.abspath(voice))
        jp, bp = make_bgm(folder, AUDIO_BGM_STYLE)
        dur = _duration(ff, voice)
        lead, tail = 2.8, 4.0
        total = lead + dur + tail
        out = voice[:-4] + "_bgm.mp3"
        fc = (f"[0:a]aresample=44100,aformat=channel_layouts=mono,adelay={int(lead * 1000)}:all=1[v];"
              f"[1:a]aresample=44100,volume=0.55[j];"
              f"[2:a]aresample=44100,atrim=0:{total:.2f},"
              f"volume='0.10+0.12*clip((t-{lead + dur:.2f})/0.8,0,1)':eval=frame,"
              f"afade=t=in:st=1.5:d=1.5,afade=t=out:st={total - 2.5:.2f}:d=2.5[b];"
              f"[v][j][b]amix=inputs=3:duration=longest:normalize=0,alimiter=limit=0.95[o]")
        subprocess.run([ff, "-y", "-v", "error", "-i", voice, "-i", jp, "-stream_loop", "-1", "-i", bp,
                        "-filter_complex", fc, "-map", "[o]", "-t", f"{total:.2f}", "-ac", "1", "-b:a", "96k", out],
                       check=True, capture_output=True, timeout=180)
        if os.path.getsize(out) > 1000:
            BGM_USED[0] = f"기본 음악 {AUDIO_BGM_STYLE}번"
            return out
    except Exception as e:
        print(f"[배경음악 생략] {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
    return voice


def to_voice(mp3: str) -> str:
    """텔레그램 '음성 메시지'(ogg/opus)로 변환. 음악 파일(sendAudio)은 끝나면 채팅방의 이전 음악이
    이어서 재생되지만, 음성 메시지는 더 새로운 음성 메시지로만 넘어가므로 최신 브리핑은 끝나면 멈춤"""
    import subprocess
    ff = _ffmpeg()
    if not ff:
        return ""
    out = mp3[:-4] + ".ogg"
    try:
        subprocess.run([ff, "-y", "-v", "error", "-i", mp3, "-ac", "1", "-ar", "48000",
                        "-c:a", "libopus", "-b:a", "48k", "-application", "audio", out],
                       check=True, capture_output=True, timeout=180)
        return out if os.path.getsize(out) > 1000 else ""
    except Exception as e:
        print(f"[음성 메시지 변환 실패] {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
        return ""


def send_audio(path: str, caption: str, title: str) -> int:
    ogg = to_voice(path)
    if ogg:                                        # 음성 메시지로 발송(재생이 끝나면 멈춤)
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendVoice"
        field, fpath, mime = "voice", ogg, "audio/ogg"
        data = {"chat_id": TG_CHAT.strip(), "caption": caption, "parse_mode": "HTML"}
    else:                                          # 변환이 안 되면 예전처럼 음악 파일로
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendAudio"
        field, fpath, mime = "audio", path, "audio/mpeg"
        data = {"chat_id": TG_CHAT.strip(), "caption": caption, "parse_mode": "HTML",
                "title": title, "performer": "중수청 브리핑"}
    for _ in range(2):
        with open(fpath, "rb") as f:
            r = requests.post(url, data=data, files={field: (os.path.basename(fpath), f, mime)}, timeout=120)
        if r.ok:
            try:
                return int(r.json()["result"]["message_id"])
            except Exception:
                return 0
        print(f"[텔레그램 음성 오류] {r.status_code}: {r.text[:200]}", file=sys.stderr)
        data.pop("parse_mode", None)               # 서식 문제일 수 있어 서식 없이 다시
        data["caption"] = html.unescape(re.sub(r"</?b>", "", caption))
    r.raise_for_status()


def send_script(script: str, reply_to: int, head: str) -> None:
    """음성에 실제로 넣은 원고를 문단 번호와 함께 글로 보냄(음성 메시지에 답장 형태, 접힌 상태로)
    → 들으면서 '3번 문단 ○○ 발음 이상'처럼 바로 짚을 수 있게"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", script) if p.strip()]
    body = "\n\n".join(f"{n}) {html.escape(p)}" for n, p in enumerate(paras, 1))
    parts, cur = [], ""
    for blk in body.split("\n\n"):                # 텔레그램 4,096자 제한 대비 문단 단위로 나눔
        if cur and len(cur) + len(blk) > 3500:
            parts.append(cur)
            cur = blk
        else:
            cur = (cur + "\n\n" + blk) if cur else blk
    if cur:
        parts.append(cur)
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for i, part in enumerate(parts):
        text = (head if i == 0 else "") + f"<blockquote expandable>{part}</blockquote>"
        payload = {"chat_id": TG_CHAT.strip(), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        r = requests.post(url, json=payload, timeout=30)
        if not r.ok:                               # 접기 기능이 안 되면 그냥 글로
            print(f"[원고 발송 오류] {r.status_code}: {r.text[:150]}", file=sys.stderr)
            payload.pop("parse_mode", None)
            payload["text"] = html.unescape(re.sub(r"</?(b|blockquote[^>]*)>", "", text))
            requests.post(url, json=payload, timeout=30)


def audio_briefing(state: dict, now: dt.datetime) -> None:
    label = audio_due(state, now)
    if not label:
        return
    since = dt.datetime.fromisoformat(state["audio_cut"]) if state.get("audio_cut") else now - dt.timedelta(hours=14)
    if label == "시험":                            # 시험은 언제 눌러도 들어 볼 수 있게 최근 24시간 기사로
        since = now - dt.timedelta(hours=24)
    recs = [from_record(r) for r in state.get("audio_log", []) if dt.datetime.fromisoformat(r["found"]) > since]
    for r in recs:                                 # 실행마다 AI 묶음 번호가 달라서 실행 시각과 함께 구분
        r["group"] = f"{r['found']}|{r['group']}" if r.get("group") is not None else None
    wd = "월화수목금토일"
    span = (f"{since:%H:%M}~{now:%H:%M}" if since.date() == now.date() else
            f"{since:%m.%d.}({wd[since.weekday()]}) {since:%H:%M} ~ {now:%m.%d.}({wd[now.weekday()]}) {now:%H:%M}")
    head = f"🎧 <b>중수청 {label} 브리핑</b>\n{span} 중수청 기사 "
    if not recs:
        send_telegram(head + "없음")
        print(f"음성 브리핑({label}): 새 중수청 기사 없음")
    else:
        groups = group_articles(sorted(recs, key=lambda x: x["found"]))
        # 실행마다 따로 들어온 같은 사안(예: 9시·11시·15시에 각각 들어온 같은 사건 기사)을 하나로 합침
        merged = [[a for i in c for a in groups[i]] for c in audio_cluster(groups)]
        use, more = merged[:AUDIO_MAX], max(len(merged) - AUDIO_MAX, 0)
        script = speakable(audio_script(use, "오후" if label == "시험" and now.hour >= 12 else
                                        ("아침" if label == "시험" else label), since, now, more))
        lines = [f"{n}. {html.escape(g[0]['title'][:60])} ({html.escape(g[0]['press'])})"
                 + (f" 외 {len(g) - 1}건" if len(g) > 1 else "") for n, g in enumerate(use, 1)]
        caption = head + f"{len(recs)}건 · {len(use)}개 사안\n" + "\n".join(lines)
        if more:
            caption += f"\n외 {more}개 사안"
        if len(caption) > 1000:                    # 텔레그램 설명란 1,024자 제한
            caption = caption[:990].rsplit("\n", 1)[0] + "\n…"
        # 원고 보관(최근 10회, state.json의 audio_scripts) → 나중에 글로 다시 볼 수 있게
        state["audio_scripts"] = (state.get("audio_scripts", []) + [[now.isoformat(), label, script]])[-10:]
        path = os.path.join(os.getenv("RUNNER_TEMP") or ".", f"중수청브리핑_{now:%m%d_%H%M}.mp3")
        try:
            engine = tts_mp3(script, path)
            mixed = add_bgm(path, now)
            if mixed != path:
                engine += " + 배경음악(" + BGM_USED[0] + ")"
                path = mixed
            state["audio_scripts"][-1].append(engine)
            mid = send_audio(path, caption, f"{now:%m.%d.} {label} 중수청 브리핑")
            if (os.getenv("AUDIO_SCRIPT_MSG") or "on").strip().lower() != "off":
                try:
                    send_script(script, mid, "📝 <b>음성 원고</b> (눌러서 펼치기 · 문단 번호로 지적해 주세요)\n")
                except Exception as e:
                    print(f"[원고 발송 오류] {type(e).__name__}: {e}", file=sys.stderr)
            print(f"음성 브리핑({label}) 발송: {len(recs)}건, 원고 {len(script)}자, {engine}")
        except Exception as e:                     # 음성 실패 시 원고를 글로 대신 보냄
            print(f"[음성 생성 실패] {type(e).__name__}: {e}", file=sys.stderr)
            msg = head + f"{len(recs)}건 (음성 생성 실패, 원고로 대신 보냅니다)\n\n" + html.escape(script)
            for i in range(0, len(msg), 3900):
                send_telegram(msg[i:i + 3900])
    if AUDIO_ERR:
        state["audio_errors"] = (state.get("audio_errors", []) + [[now.isoformat()] + AUDIO_ERR[-4:]])[-10:]
    if label != "시험":                            # 시험 발송은 다음 정기 브리핑 범위에 영향 없음
        state["audio_cut"] = now.isoformat()
        state["last_audio"] = now.isoformat()


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
    olds = [a for a in arts if a.get("old")]
    if olds:
        print(f"지난 기사 {len(olds)}건 제외: " + " / ".join(a["title"][:30] for a in olds))
    dups = [a for a in arts if a["relevant"] and a.get("dup")]
    if dups:
        print(f"이미 보낸 기사와 같은 사안 {len(dups)}건 제외: " + " / ".join(a["title"][:30] for a in dups))
    scoops = [a for a in arts if a["relevant"] and is_scoop(a["title"])]
    if scoops:
        print(f"[단독] {len(scoops)}건은 중복 판단 없이 통과: " + " / ".join(a["title"][:30] for a in scoops))
    for a in arts:                                 # 거른 이유 기록
        if not a["relevant"]:
            drop(a.get("why") or "관련 없음", a["press"], a["title"], now)
        elif a.get("dup"):
            drop("AI 중복(이미 보낸 사안)", a["press"], a["title"], now)
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
        state.setdefault("audio_log", []).extend(to_record(a, now) for a in urgent)
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

    try:
        audio_briefing(state, now)
    except Exception as e:                         # 음성 문제로 알림 전체가 멈추지 않게
        print(f"[음성 브리핑 오류] {type(e).__name__}: {e}", file=sys.stderr)
    keep_a = now - dt.timedelta(days=7)            # 음성용 기록은 7일·최대 300건만 보관(연휴 대비)
    state["audio_log"] = [r for r in state.get("audio_log", [])
                          if dt.datetime.fromisoformat(r["found"]) >= keep_a][-300:]

    state.pop("daily", None), state.pop("daily_sent", None)   # 예전 대응 검토 기록 정리
    state["last_run"] = now.isoformat()
    keep = now - dt.timedelta(hours=48)            # 거른 기사 기록은 48시간·최대 300건만 보관
    old_d = [r for r in state.get("dropped", []) if dt.datetime.fromisoformat(r[0]) >= keep]
    have = {(r[1], r[3]) for r in old_d}
    new_d = []
    for r in DROPPED:                              # 네이버는 매 실행 다시 걸러지므로 같은 기록은 한 번만
        if (r[1], r[3]) not in have:
            have.add((r[1], r[3]))
            new_d.append(r)
    state["dropped"] = (old_d + new_d)[-300:]
    save_seen(seen, now)                           # 발송 성공 후에만 기록
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
