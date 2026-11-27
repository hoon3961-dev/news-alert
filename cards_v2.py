"""카드뉴스 2판(정사각 1080×1080): 표지 → 목차 → 사안 카드(사안당 1~2장) → 마무리.

사안 카드 데이터(원고 파일 cards[] 한 개):
  {"tag": "수사", "headline": "제목", "lead": "무슨 일인지 한 문장", "points": ["보충 요점", "..."],
   "stat": {"big": "900", "unit": "명+", "desc": "설명", "bar": 76, "after": 2},   # 선택
   "viz": {"type": "ratio|hbar|vs|timeline|flow|route|tree|grid|quote|waffle", "title": "시각화 장 제목", "data": {...}},  # 선택
   "sources": ["a3"]}
- 사안마다 글 카드 1장이 기본. viz가 있으면 바로 뒤에 시각화 1장을 더 붙임(사안당 최대 2장).
- 글자에는 <br>, <b>만 허용(나머지는 그대로 글자로 표시).
"""
import base64
import html
import os
import re

B1, B2, DEEP, PALE, BG, LINE, TXT, SUB, WARN = (
    "#3A74FF", "#1534C4", "#0E2148", "#DCE6FF", "#E9EEF6", "#D5DDEB", "#1B2A3D", "#5B6B82", "#E4572E")
ACC = "#BFD3FF"
COLORS = {"main": B2, "light": B1, "warn": WARN, "soft": "#9DB4E8", "pale": PALE}
FONT = 'font-family:Pretendard,"Noto Sans KR","Noto Sans CJK KR",sans-serif;letter-spacing:-0.5px;word-break:keep-all;overflow-wrap:break-word;'
VIZ_TYPES = ("ratio", "hbar", "vs", "timeline", "flow", "route", "tree", "grid", "quote", "waffle")
KIND = dict(ratio="비율", hbar="막대그래프", vs="비교", timeline="타임라인", flow="흐름도", route="연결도",
            tree="구조도", grid="항목 정리", quote="발언", waffle="한눈에 보기")
WD = "월화수목금토일"


def t(s) -> str:
    """글자 안전 처리: <br>, <b>만 살림"""
    s = html.escape(str(s or ""))
    return re.sub(r"&lt;(/?b|br\s*/?)&gt;", r"<\1>", s)


def _asset(name: str) -> str:
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)
    return "data:image/png;base64," + base64.b64encode(open(fp, "rb").read()).decode() if os.path.exists(fp) else ""


def page(body: str) -> str:
    return ("<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'><style>*{margin:0;padding:0;box-sizing:border-box}"
            "html,body{width:1080px;height:1080px;overflow:hidden}</style></head><body>" + body + "</body></html>")


def band(date_label: str, pg: str) -> str:
    return (f"<div style='background:linear-gradient(90deg,{B1},{B2});color:#fff;border-radius:32px 32px 0 0;padding:28px 48px 68px;display:flex;align-items:center'>"
            f"<div style='border-left:3px solid #fff;padding-left:16px;font-size:22px;font-weight:500;letter-spacing:2px;line-height:1.5'>PRESS BRIEFING<br>{t(date_label)}</div>"
            f"<div style='margin-left:auto;font-size:24px;font-weight:700;opacity:.9'>{pg}</div></div>")


def shell(date_label: str, pg: str, inner: str, pad: str = "36px 52px 26px") -> str:
    return page(f"<div style='width:1080px;height:1080px;background:{BG};color:{DEEP};{FONT}padding:40px 48px 48px;display:flex;flex-direction:column'>"
                + band(date_label, pg) +
                f"<div style='flex:1;min-height:0;margin-top:-40px;background:#fff;border-radius:32px;padding:{pad};display:flex;flex-direction:column;box-shadow:0 8px 24px rgba(14,33,72,.08)'>"
                + inner + "</div></div>")


def head(no: int, tag: str, right: str = "") -> str:
    return (f"<div style='display:flex;align-items:center;gap:14px'>"
            f"<div style='width:56px;height:56px;border-radius:50%;background:{B2};color:#fff;display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:500'>{no:02d}</div>"
            f"<span style='font-size:23px;font-weight:700;color:{B2};background:{PALE};border-radius:999px;padding:5px 18px'>{t(tag)}</span>"
            + (f"<span style='margin-left:auto;font-size:20px;font-weight:700;color:{SUB}'>{t(right)}</span>" if right else "") + "</div>")


def _hsize(s: str, big=52, mid=46, small=40) -> int:
    n = len(re.sub(r"<[^>]+>", "", s))
    return big if n <= 18 else mid if n <= 24 else small


# ───────── 표지 ─────────
_PAPER = ("<svg viewBox='0 0 420 380' width='450' height='407' aria-hidden='true' style='display:block'>"
          "<rect x='70' y='30' width='270' height='300' rx='16' fill='#DCE7FF' transform='rotate(-9 205 180)'/>"
          "<rect x='60' y='50' width='280' height='300' rx='16' fill='#FFFFFF'/>"
          "<rect x='90' y='84' width='170' height='24' rx='6' fill='#1F5BD8'/>"
          "<rect x='90' y='124' width='220' height='10' rx='5' fill='#C9D6EE'/><rect x='90' y='144' width='200' height='10' rx='5' fill='#C9D6EE'/>"
          "<rect x='90' y='174' width='100' height='86' rx='8' fill='#BFD3FF'/>"
          "<rect x='206' y='178' width='104' height='10' rx='5' fill='#C9D6EE'/><rect x='206' y='198' width='90' height='10' rx='5' fill='#C9D6EE'/>"
          "<rect x='206' y='218' width='100' height='10' rx='5' fill='#C9D6EE'/>"
          "<rect x='90' y='282' width='220' height='10' rx='5' fill='#C9D6EE'/><rect x='90' y='302' width='160' height='10' rx='5' fill='#C9D6EE'/>"
          "<circle cx='318' cy='262' r='60' fill='rgba(255,255,255,0.35)' stroke='#0E2148' stroke-width='18'/>"
          "<line x1='362' y1='306' x2='404' y2='348' stroke='#F2B630' stroke-width='28' stroke-linecap='round'/></svg>")


def cover(day: str, label: str, period: tuple) -> str:
    """day='11.27.', 요일은 day_wd로 분리해 크기 보정 / period=(시작 문자열, 끝 문자열)"""
    mi = _asset("sci_mi_white.png")
    d, wd = day
    return page(
        f"<div style='width:1080px;height:1080px;background:linear-gradient(160deg,{B1} 0%,{B2} 100%);color:#fff;{FONT}position:relative;overflow:hidden;padding:60px 72px 56px;display:flex;flex-direction:column'>"
        "<div style='position:absolute;right:-140px;bottom:-140px;width:760px;height:760px;border-radius:50%;background:rgba(255,255,255,.08)'></div>"
        f"<div style='position:absolute;right:36px;bottom:44px'>{_PAPER}</div>"
        + (f"<div style='position:relative;display:flex;justify-content:flex-end'><img src='{mi}' style='height:62px;width:auto;display:block'></div>" if mi else "<div style='height:62px'></div>") +
        "<div style='position:relative;margin-top:90px'>"
        f"<h1 style='font-size:120px;font-weight:900;line-height:1.22;letter-spacing:-1px'>"
        f"<span style='font-size:77px;letter-spacing:-2px'>{t(d)}<span style='font-size:.84em'>({t(wd)})</span></span><br>언론보도<br>{t(label)} 브리핑</h1>"
        f"<div style='margin-top:40px;font-size:26px;font-weight:700;line-height:1.6;color:{ACC}'>"
        f"<span style='margin-left:-.45em'>〈보도기간〉</span><br>{t(period[0])} 부터<br>{t(period[1])} 까지</div></div>"
        f"<div style='position:relative;margin-top:auto;font-size:22px;color:{ACC}'>중대범죄수사청 관련 언론보도를 요약한 자료입니다</div></div>")


# ───────── 목차 ─────────
def toc(date_label: str, pg: str, items: list) -> str:
    """items=[(tag, headline)]"""
    k = len(items)
    fs = 30 if k <= 6 else 26 if k <= 8 else 23
    circ = 68 if k <= 6 else 56 if k <= 8 else 48
    rows = ""
    for i, (tag, hl) in enumerate(items, 1):
        style = (f"background:{B2};color:#fff;border:3px solid {B2}" if i % 2 == 0 else f"background:#fff;color:{DEEP};border:3px solid {DEEP}")
        rows += (f"<div style='flex:1;display:flex;align-items:center;gap:24px;border-bottom:2px solid {DEEP}'>"
                 f"<div style='flex:none;width:{circ}px;height:{circ}px;border-radius:50%;{style};display:flex;align-items:center;justify-content:center;font-size:{int(circ * .44)}px;font-weight:500'>{i:02d}</div>"
                 f"<div style='flex:1;min-width:0;font-size:{fs}px;font-weight:700;line-height:1.3;color:{DEEP}'>{t(hl)}</div>"
                 f"<div style='flex:none;width:120px;text-align:right;font-size:22px;font-weight:500;color:{SUB}'>{t(tag)}</div></div>")
    return shell(date_label, pg,
                 f"<h1 style='text-align:center;font-size:72px;font-weight:900;color:{DEEP}'>오늘의 언론보도</h1>"
                 f"<div style='flex:1;margin-top:18px;display:flex;flex-direction:column'>{rows}</div>", "40px 52px 28px")


# ───────── 사안 글 카드 ─────────
def text_card(date_label: str, pg: str, no: int, c: dict, src: str) -> str:
    st = c.get("stat") or {}
    has = bool(st.get("big"))
    lead = c.get("lead") or ""
    pts = [p for p in (c.get("points") or []) if p][:3]
    statb = ""
    if has:
        bar = ""
        if st.get("bar") not in (None, ""):
            try:
                w = max(0.0, min(100.0, float(st["bar"])))
                bar = (f"<div style='height:10px;background:rgba(255,255,255,.25);border-radius:5px;margin-top:12px;overflow:hidden'>"
                       f"<div style='height:100%;width:{w}%;background:#fff;border-radius:5px'></div></div>")
            except (TypeError, ValueError):
                pass
        # 읽는 순서대로: 무엇인지(설명) → 숫자. 비율 막대는 숫자 아래
        lab = (f"<div style='font-size:21px;font-weight:700;opacity:.85;margin-bottom:6px'>{t(st['label'])}</div>" if st.get("label") else "")
        statb = (f"<div style='margin-top:22px;display:flex;align-items:center;gap:30px;background:linear-gradient(120deg,{B1},{B2});color:#fff;border-radius:24px;padding:22px 32px'>"
                 f"<div style='flex:1;min-width:0'>{lab}<div style='font-size:29px;font-weight:700;line-height:1.4'>{t(st.get('desc'))}</div></div>"
                 f"<div style='flex:none;white-space:nowrap;text-align:right;min-width:240px'><span style='font-size:88px;font-weight:900;line-height:1'>{t(st['big'])}</span>"
                 f"<span style='font-size:34px;font-weight:700;margin-left:4px'>{t(st.get('unit'))}</span>{bar}</div></div>")
    fs = 33 if has else 38
    # 숫자 칸은 그 숫자를 말한 문장 바로 아래에 둠: stat.after = "lead"(기본) 또는 요점 번호(1부터)
    after = str(st.get("after") or "lead") if has else ""
    rows = []
    for k, p in enumerate(pts, 1):
        rows.append(f"<div style='display:flex;align-items:flex-start;gap:18px;padding:{16 if has else 22}px 0;border-bottom:2px solid {LINE}'>"
                    f"<span style='flex:none;width:12px;height:12px;border-radius:50%;background:{B2};margin-top:{int(fs * .62)}px'></span>"
                    f"<p style='font-size:{fs}px;line-height:1.45;font-weight:500;color:#33415C'>{t(p)}</p></div>")
        if has and after == str(k):
            rows.append(statb.replace("margin-top:22px", "margin:6px 0 10px", 1))
    bl = "".join(rows)
    leadb = (f"<p style='margin-top:22px;border-left:6px solid {B2};padding:4px 0 4px 22px;font-size:{34 if has else 38}px;line-height:1.5;font-weight:700;color:{TXT}'>{t(lead)}</p>" if lead else "")
    inner = (head(no, c.get("tag", "기타")) +
             f"<h1 style='margin-top:22px;font-size:{_hsize(c['headline'])}px;font-weight:900;line-height:1.28;color:{DEEP}'>{t(c['headline'])}</h1>"
             + leadb + (statb if after == "lead" else "") + f"<div style='margin-top:10px;display:flex;flex-direction:column'>{bl}</div>"
             f"<div style='margin-top:auto;font-size:20px;color:#6B7A94'>출처: {t(src)}</div>")
    return shell(date_label, pg, inner, "40px 52px 30px")


# ───────── 시각화 카드 ─────────
def _num(v, u="", size=96, color=B2):
    return (f"<span style='font-size:{size}px;font-weight:900;line-height:1;color:{color}'>{t(v)}</span>"
            f"<span style='font-size:{int(size * .38)}px;font-weight:700;color:{color};margin-left:4px'>{t(u)}</span>")


def _note(d, mt="12px"):
    return f"<div style='margin-top:{mt};font-size:24px;color:{SUB};line-height:1.5'>{t(d['note'])}</div>" if d.get("note") else ""


def _f(x, default=0.0) -> float:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return default


def v_ratio(d):
    part, total = _f(d.get("part")), _f(d.get("total"), 1) or 1
    pct = part / total * 100
    rows = "".join(f"<div style='display:flex;justify-content:space-between;gap:20px;font-size:26px;padding:12px 0;border-bottom:2px solid {LINE}'><span>{t(k)}</span><b style='text-align:right'>{t(v)}</b></div>"
                   for k, v in d.get("rows", []))
    return (f"<div style='display:flex;gap:28px'>"
            f"<div style='flex:1;background:{PALE};border-radius:24px;padding:26px 30px'><div style='font-size:23px;font-weight:700;color:{SUB}'>{t(d.get('total_label'))}</div>{_num(d.get('total_txt'), d.get('unit'), 84, DEEP)}</div>"
            f"<div style='flex:1;background:linear-gradient(120deg,{B1},{B2});border-radius:24px;padding:26px 30px;color:#fff'><div style='font-size:23px;font-weight:700;opacity:.85'>{t(d.get('part_label'))}</div>{_num(d.get('part_txt'), d.get('unit'), 84, '#fff')}</div></div>"
            f"<div style='margin-top:26px;display:flex;align-items:center;gap:20px'><div style='flex:1;height:34px;background:{PALE};border-radius:17px;overflow:hidden'>"
            f"<div style='height:100%;width:{max(pct, 1.2):.2f}%;background:{B2};border-radius:17px'></div></div><b style='font-size:40px;color:{B2}'>{t(d.get('pct_label'))}</b></div>"
            f"<div style='margin-top:8px;font-size:22px;color:{SUB}'>{t(d.get('bar_note'))}</div><div style='margin-top:10px'>{rows}</div>")


def v_hbar(d):
    items = d.get("items", [])
    mx = max([_f(it.get("value")) for it in items] or [1]) or 1
    lw = d.get("lw", 200)
    out = ""
    for it in items:
        col = COLORS.get(it.get("color") or "light", B1)
        out += (f"<div style='display:grid;grid-template-columns:{lw}px 1fr auto;align-items:center;gap:18px;padding:12px 0'>"
                f"<span style='font-size:27px;font-weight:700'>{t(it.get('label'))}</span>"
                f"<div style='height:38px;background:{BG};border-radius:10px;overflow:hidden'><div style='height:100%;width:{max(_f(it.get('value')) / mx * 100, 1.5):.1f}%;background:{col};border-radius:10px'></div></div>"
                f"<b style='font-size:27px;text-align:right;color:{B2 if it.get('color') in ('main', 'warn') else DEEP}'>{t(it.get('text'))}</b></div>")
    ttl = f"<div style='font-size:23px;font-weight:700;color:{SUB};margin-bottom:8px'>{t(d['title'])}</div>" if d.get("title") else ""
    return ttl + out + _note(d)


def v_vs(d):
    def side(s, dark):
        bg, col = (f"linear-gradient(150deg,{B1},{B2})", "#fff") if dark else (PALE, DEEP)
        pts = "".join(f"<li style='margin:0 0 16px'>{t(p)}</li>" for p in s.get("points", []))
        big = f"<div style='margin:12px 0 4px'>{_num(s['big'], s.get('unit', ''), 76, col)}</div>" if s.get("big") else ""
        return (f"<div style='flex:1;background:{bg};color:{col};border-radius:26px;padding:30px 40px 24px;display:flex;flex-direction:column;justify-content:center'>"
                f"<div style='font-size:36px;font-weight:900'>{t(s.get('name'))}</div>{big}<ul style='margin-top:16px;padding-left:28px;font-size:29px;line-height:1.5'>{pts}</ul></div>")
    mid = (f"<div style='align-self:center;flex:none;width:64px;height:64px;border-radius:50%;background:#fff;border:3px solid {B2};color:{B2};font-weight:900;font-size:24px;"
           f"display:flex;align-items:center;justify-content:center;margin:0 -32px;z-index:1'>{t(d.get('mid', 'VS'))}</div>")
    return f"<div style='flex:1;display:flex'>{side(d.get('a', {}), False)}{mid}{side(d.get('b', {}), True)}</div>" + _note(d, "16px")


def v_timeline(d):
    steps = d.get("steps", [])
    out = ""
    for i, s in enumerate(steps):
        on, last = bool(s.get("hl")), i == len(steps) - 1
        out += (f"<div style='display:flex;gap:22px;flex:1;min-height:0'>"
                f"<div style='flex:none;width:150px;text-align:right;font-size:28px;font-weight:900;color:{B2 if on else DEEP};padding-top:2px'>{t(s.get('when'))}</div>"
                f"<div style='flex:none;display:flex;flex-direction:column;align-items:center;width:26px'><div style='width:24px;height:24px;border-radius:50%;background:{B2 if on else '#fff'};border:4px solid {B2};margin-top:6px'></div>"
                + ("" if last else f"<div style='flex:1;width:4px;background:{PALE}'></div>") +
                f"</div><div style='flex:1;font-size:30px;line-height:1.45;font-weight:{700 if on else 500};padding-bottom:12px'>{t(s.get('text'))}</div></div>")
    return f"<div style='flex:1;display:flex;flex-direction:column'>{out}</div>" + _note(d, "6px")


def v_flow(d):
    steps = d.get("steps", [])
    out = ""
    for i, s in enumerate(steps):
        dark = bool(s.get("hl"))
        out += (f"<div style='display:flex;align-items:center;gap:20px;background:{('linear-gradient(90deg,' + B1 + ',' + B2 + ')') if dark else PALE};color:{'#fff' if dark else DEEP};border-radius:20px;padding:18px 26px'>"
                f"<b style='flex:none;font-size:24px;min-width:120px;opacity:.9'>{t(s.get('who'))}</b><span style='font-size:27px;line-height:1.4;font-weight:{700 if dark else 500}'>{t(s.get('text'))}</span></div>")
        if i < len(steps) - 1:
            out += f"<div style='text-align:center;color:{B2};font-size:26px;line-height:1;margin:6px 0'>▼</div>"
    return out + _note(d)


def v_route(d):
    rows = ""
    for r in d.get("rows", []):
        warn = r.get("warn")
        rows += (f"<div style='display:grid;grid-template-columns:1fr 54px 1fr;align-items:center;gap:8px'>"
                 f"<div style='background:{PALE};border-radius:16px;padding:14px 18px;font-size:24px;font-weight:700;line-height:1.35'>{t(r.get('from'))}</div>"
                 f"<div style='text-align:center;color:{B2};font-size:30px;font-weight:900'>→</div>"
                 f"<div style='background:{'#FDE7E0' if warn else B2};color:{WARN if warn else '#fff'};border-radius:16px;padding:14px 18px;font-size:24px;font-weight:700;line-height:1.35'>{t(r.get('to'))}</div></div>")
    hd = (f"<div style='display:grid;grid-template-columns:1fr 54px 1fr;gap:8px;font-size:21px;font-weight:700;color:{SUB};margin-bottom:4px'>"
          f"<span>{t(d.get('left'))}</span><span></span><span>{t(d.get('right'))}</span></div>")
    return hd + f"<div style='display:flex;flex-direction:column;gap:12px'>{rows}</div>" + _note(d)


def v_tree(d):
    kids = d.get("kids", [])
    n = max(len(kids), 1)
    kid = "".join(f"<div style='flex:1;display:flex;flex-direction:column;align-items:center'><div style='width:4px;height:30px;background:{B2}'></div>"
                  f"<div style='width:100%;background:{PALE};border-radius:20px;padding:18px 14px;text-align:center;flex:1;display:flex;flex-direction:column;justify-content:center'>"
                  f"<div style='font-size:{30 if n <= 3 else 26}px;font-weight:900;color:{B2};line-height:1.3'>{t(k.get('name'))}</div>"
                  f"<div style='margin-top:8px;font-size:{25 if n <= 3 else 22}px;line-height:1.45;color:{TXT}'>{t(k.get('text'))}</div></div></div>" for k in kids)
    bar = f"<div style='height:4px;background:{B2};margin:0 calc(100%/{2 * n})'></div>" if n > 1 else ""
    sub = f"<div style='font-size:22px;opacity:.9;margin-top:4px'>{t(d['root_sub'])}</div>" if d.get("root_sub") else ""
    return (f"<div style='align-self:center;background:linear-gradient(90deg,{B1},{B2});color:#fff;border-radius:20px;padding:18px 40px;text-align:center'>"
            f"<div style='font-size:34px;font-weight:900'>{t(d.get('root'))}</div>{sub}</div>"
            f"<div style='align-self:center;width:4px;height:26px;background:{B2}'></div>{bar}<div style='display:flex;gap:16px;flex:1'>{kid}</div>" + _note(d, "16px"))


def v_grid(d):
    cells = d.get("cells", [])
    cols = int(d.get("cols") or (3 if len(cells) in (3, 6, 9) else 2))
    many = len(cells) > 4
    out = ""
    for c in cells:
        hl = bool(c.get("hl"))
        out += (f"<div style='background:{B2 if hl else PALE};color:{'#fff' if hl else DEEP};border-radius:22px;padding:22px 26px;display:flex;flex-direction:column;justify-content:center'>"
                f"<div style='font-size:{27 if many else 32}px;font-weight:900;line-height:1.3'>{t(c.get('title'))}</div>"
                + (f"<div style='margin-top:6px'>{_num(c['big'], c.get('unit', ''), 72 if many else 84, '#fff' if hl else B2)}</div>" if c.get("big") else "")
                + (f"<div style='margin-top:8px;font-size:{24 if many else 27}px;line-height:1.45;opacity:.92'>{t(c.get('text'))}</div>" if c.get("text") else "") + "</div>")
    return f"<div style='flex:1;display:grid;grid-template-columns:repeat({cols},minmax(0,1fr));gap:16px'>{out}</div>" + _note(d, "14px")


def v_quote(d):
    facts = "".join(f"<div style='flex:1;background:{PALE};border-radius:20px;padding:18px 20px'><div style='font-size:21px;color:{SUB};font-weight:700'>{t(k)}</div>"
                    f"<div style='font-size:30px;font-weight:900;color:{B2};margin-top:4px'>{t(v)}</div></div>" for k, v in d.get("facts", []))
    return (f"<div style='background:linear-gradient(140deg,{B1},{B2});color:#fff;border-radius:28px;padding:30px 40px'>"
            f"<div style='font-size:90px;line-height:.6;font-weight:900;opacity:.5'>“</div><div style='font-size:38px;font-weight:900;line-height:1.4'>{t(d.get('quote'))}</div>"
            f"<div style='margin-top:16px;font-size:24px;opacity:.9'>— {t(d.get('who'))}</div></div>"
            + (f"<div style='margin-top:20px;display:flex;gap:14px'>{facts}</div>" if facts else "") + _note(d, "16px"))


def v_waffle(d):
    total = max(1, min(int(_f(d.get("total"), 100)), 100))
    parts = [(int(_f(p.get("count"))), COLORS.get(p.get("color") or "main", B2), p.get("label", "")) for p in d.get("parts", [])]
    cols = int(d.get("cols") or (10 if total > 30 else 4 if total <= 16 else 7))
    cells, acc_list = "", []
    acc = 0
    for cnt, col, _ in parts:
        acc += cnt
        acc_list.append((acc, col))
    for i in range(total):
        col = next((c for a, c in acc_list if i < a), PALE)
        cells += f"<div style='aspect-ratio:1;border-radius:{10 if total > 30 else 14}px;background:{col}'></div>"
    leg = "".join(f"<div style='display:flex;align-items:center;gap:12px;font-size:30px'><span style='width:26px;height:26px;border-radius:7px;background:{c}'></span><b>{t(lab)}</b></div>"
                  for _, c, lab in parts)
    if d.get("rest_label"):
        leg += f"<div style='display:flex;align-items:center;gap:12px;font-size:30px'><span style='width:26px;height:26px;border-radius:7px;background:{PALE}'></span><b>{t(d['rest_label'])}</b></div>"
    side = f"<div style='font-size:24px;color:{SUB};font-weight:700'>{t(d['caption'])}</div>" if d.get("caption") else ""
    w = 440 if total > 30 else 400
    return (f"<div style='display:flex;gap:34px;flex:1;align-items:center'><div style='width:{w}px;display:grid;grid-template-columns:repeat({cols},1fr);gap:{7 if total > 30 else 14}px'>{cells}</div>"
            f"<div style='flex:1;display:flex;flex-direction:column;gap:16px'>{side}{leg}</div></div>" + _note(d))


RENDER = dict(ratio=v_ratio, hbar=v_hbar, vs=v_vs, timeline=v_timeline, flow=v_flow, route=v_route,
              tree=v_tree, grid=v_grid, quote=v_quote, waffle=v_waffle)
FIT = {"ratio", "hbar", "flow", "route", "quote"}
_FIT_JS = ("<script>(function(){var f=document.getElementById('fit'),i=document.getElementById('inner'),z=1;"
           "while(z<1.45){i.style.zoom=z+0.03;if(i.scrollHeight*(z+0.03)>f.clientHeight*0.97||i.scrollWidth>f.clientWidth+2){i.style.zoom=z;break}z+=0.03}})()</script>")


def viz_card(date_label: str, pg: str, no: int, c: dict, src: str) -> str:
    v = c["viz"]
    typ = v.get("type")
    body = RENDER[typ](v.get("data") or {})
    if typ in FIT:
        body = (f"<div id='fit' style='flex:1;min-height:0;margin-top:22px;display:flex;flex-direction:column;justify-content:center'>"
                f"<div id='inner'>{body}</div></div>" + _FIT_JS)
    else:
        body = f"<div style='flex:1;min-height:0;margin-top:24px;display:flex;flex-direction:column'>{body}</div>"
    title = v.get("title") or c["headline"]
    lead = f"<p style='margin-top:14px;font-size:28px;line-height:1.5;font-weight:500;color:{SUB}'>{t(v['lead'])}</p>" if v.get("lead") else ""
    inner = (head(no, c.get("tag", "기타"), KIND.get(typ, "")) +
             f"<h1 style='margin-top:18px;font-size:{_hsize(title, 48, 44, 40)}px;font-weight:900;line-height:1.25;color:{DEEP}'>{t(title)}</h1>"
             + lead + body + f"<div style='margin-top:14px;font-size:19px;color:#7A889E'>출처: {t(src)}</div>")
    return shell(date_label, pg, inner)


# ───────── 마무리 ─────────
def closing(pg: str) -> str:
    mi = _asset("sci_mi_white.png")
    return page(
        f"<div style='width:1080px;height:1080px;background:linear-gradient(160deg,{B1} 0%,{B2} 100%);color:#fff;{FONT}position:relative;overflow:hidden;padding:60px 72px 56px;display:flex;flex-direction:column'>"
        "<div style='position:absolute;right:-140px;bottom:-140px;width:760px;height:760px;border-radius:50%;background:rgba(255,255,255,.08)'></div>"
        f"<div style='position:relative;display:flex;align-items:center'><span style='font-size:24px;font-weight:700;opacity:.9'>{pg}</span>"
        + (f"<img src='{mi}' style='margin-left:auto;height:62px;width:auto;display:block'>" if mi else "") + "</div>"
        "<div style='position:relative;margin:auto 0'>"
        "<div style='width:96px;height:96px;border-radius:28px;background:rgba(255,255,255,.16);display:flex;align-items:center;justify-content:center'>"
        "<svg width='52' height='52' viewBox='0 0 64 64' fill='none' stroke='#FFFFFF' stroke-width='5' stroke-linecap='round'><path d='M27 37a10 10 0 0 0 14 0l9-9a10 10 0 0 0-14-14l-3 3'/><path d='M37 27a10 10 0 0 0-14 0l-9 9a10 10 0 0 0 14 14l3-3'/></svg></div>"
        f"<h1 style='margin-top:40px;font-size:76px;font-weight:900;line-height:1.3'>기사 원문은<br>텔레그램 메시지의<br><span style='color:{ACC}'>링크</span>에서 확인하세요</h1>"
        f"<div style='margin-top:40px;padding-top:30px;border-top:2px solid rgba(255,255,255,.3);font-size:28px;line-height:1.7;color:{ACC}'>언론 보도를 요약한 것으로 사실관계가 확인되지 않은<br>내용이 포함될 수 있습니다. 외부 배포를 금합니다.</div></div>"
        f"<div style='position:relative;font-size:22px;color:{ACC}'>중대범죄수사청 관련 언론보도를 요약한 자료입니다</div></div>")


# ───────── 전체 묶음 ─────────
def build(cards: list, sources: list, now, label: str, since, until) -> list:
    """cards: 사안 카드 목록, sources: 사안별 출처 문구. 반환: 페이지 HTML 목록(표지·목차·사안·마무리)"""
    date_label = f"{now:%m. %d.}({WD[now.weekday()]}) {label}"
    period = (f"{since:%Y. %m. %d.}({WD[since.weekday()]}) {since:%H:%M}", f"{until:%Y. %m. %d.}({WD[until.weekday()]}) {until:%H:%M}")
    n_pages = 3 + sum(2 if (c.get("viz") or {}).get("type") in RENDER else 1 for c in cards)
    pages = [cover((f"{now:%m.%d.}", WD[now.weekday()]), label, period),
             toc(date_label, f"2 / {n_pages}", [(c.get("tag", "기타"), c["headline"]) for c in cards])]
    k = 2
    for i, (c, s) in enumerate(zip(cards, sources), 1):
        k += 1
        pages.append(text_card(date_label, f"{k} / {n_pages}", i, c, s))
        if (c.get("viz") or {}).get("type") in RENDER:
            k += 1
            pages.append(viz_card(date_label, f"{k} / {n_pages}", i, c, s))
    pages.append(closing(f"{n_pages} / {n_pages}"))
    return pages


def viz_texts(v: dict) -> list:
    """원문 대조용: 시각화 데이터 안의 모든 글자"""
    out = []

    def walk(x):
        if isinstance(x, dict):
            for k, y in x.items():
                if k not in ("type", "color", "hl", "warn", "cols", "lw", "value", "part", "total", "count", "bar"):
                    walk(y)
        elif isinstance(x, (list, tuple)):
            for y in x:
                walk(y)
        elif isinstance(x, str):
            out.append(re.sub(r"<[^>]+>", " ", x))
    walk(v)
    return out
