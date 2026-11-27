"""Claude가 브리핑 원고를 직접 쓸 때 쓰는 도구(AI 호출 없음)

  python brief_tool.py prepare [아침|오후]        → 이번 브리핑에 들어갈 기사 목록(본문 포함)을 brief_input.json으로
  python brief_tool.py prepare 아침 --test 24     → 시험: 최근 24시간 기사로("test": true를 원고 파일에 그대로 넣으면 발송 기록을 안 바꿈)
  python brief_tool.py check briefs/파일.json     → 원고·카드를 원문과 기계적으로 대조해 문제 목록 출력(문제 있으면 종료 코드 1)
  python brief_tool.py preview briefs/파일.json   → GitHub과 같은 틀로 카드 그림을 미리 만들어 preview/sheet.png로(눈으로 확인)
  python brief_tool.py publish briefs/파일.json   → 점검 통과 시 저장소에 올리고 GitHub에 발송 요청

원고 파일 형식(briefs/YYYYMMDD_HHMM.json):
{
  "label": "아침",                      # 아침 | 오후
  "since": "...", "cut": "...",          # prepare가 알려 준 값 그대로
  "paras": [{"text": "사안 문단(넘김말·첫머리·맺음말 없이)", "sources": ["a3", "a7"]}],
  "cards": [{"tag": "수사", "headline": "30자 이내", "lead": "무슨 일인지 한 문장(80자 이내)",
             "points": ["보충 요점 1~3개(50자 이내)"],
             "stat": {"big": "900", "unit": "명+", "desc": "설명", "bar": 76, "after": 2},   # 선택: 숫자 강조 칸(after: 그 숫자를 말한 문장 아래 — "lead" 또는 요점 번호)
             "viz": {"type": "ratio", "title": "시각화 장 제목", "data": {...}},                         # 선택: 시각화 1장 추가
             "sources": ["a3"]}],   # 사안당 글 카드 1장 + (viz가 있으면) 시각화 1장 = 최대 2장. viz 형식은 cards_v2.py 머리말
  "articles": {"a3": {"title": "...", "press": "...", "url": "..."}},   # prepare의 기사 정보 그대로
  "title_only": ["본문을 못 읽어 원고에서 뺀 기사 제목"],
  "more": 0,
  "check": "점검 결과 한 줄(예: 기사 9건 원문 대조 · 고친 문장 2개)"
}
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_alert as n  # noqa: E402

REPO = "hoon3961-dev/news-alert"
BANNED = [  # 지금까지 지적된 표현(평가·전망·해설, 소속 변화 등)
    (r"관할하게 (?:됩니다|된|됨)|산하(?:가|로) (?:됐|되었|됩니다|놓이)", "소속·관할이 새로 바뀌는 표현(원래 행안부 소속)"),
    (r"떠올랐습니다|부상했습니다|도마에 올랐습니다|셈입니다|전망입니다|것으로 보입니다|중요해질 것", "앵커가 단정하는 평가·전망"),
    (r"그쳤습니다|그치고 있습니다|불과했습니다", "평가가 섞인 끝맺음('~였습니다'로)"),
    (r"(?<![가-힣])(?:드문|이례적)", "원문에 없는 평가일 가능성"),
    (r"(?<![가-힣])(?:오늘|어제|내일|그제|지난주|이번 주)(?![가-힣])", "상대 날짜(날짜로 쓸 것)"),
    (r"퍼센트인 [\d,]+\s*[억만조]", "숫자 겹치기('~인 금액')"),
    (r"^(?:먼저|이어서|다음 소식|마지막 소식)", "넘김말은 프로그램이 붙임"),
    (r"[一-鿿]", "한자(한글로)"),
    (r"[()\[\]「」『』\"“”]", "괄호·따옴표"),
    (r"내용입니다|로 보도됐습니다", "기사 설명투"),
]


def _state():
    return json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"), encoding="utf-8"))


def prepare(label="", hours=0, test=False):
    now = dt.datetime.now(n.KST)
    st = _state()
    holiday = now.weekday() >= 5 or now.date() in n._kr_holidays()
    label = label or ("아침" if now.hour < 12 else "오후")
    since = dt.datetime.fromisoformat(st["audio_cut"]) if st.get("audio_cut") else now - dt.timedelta(hours=14)
    if hours:                                       # 시험: 최근 N시간 기사로(발송 범위·시각 기록은 바꾸지 않음)
        since = now - dt.timedelta(hours=hours)
    recs = [n.from_record(r) for r in st.get("audio_log", []) if dt.datetime.fromisoformat(r["found"]) > since]
    recs = [r for r in recs if not n.is_photo(r)]
    arts, seen_u = {}, set()
    for k, r in enumerate(sorted(recs, key=lambda x: x["found"]), 1):
        if r["url"] in seen_u:
            continue
        seen_u.add(r["url"])
        body = (r.get("body") or "").strip()
        words = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", re.sub(r"\[[^\]]*\]", " ", r["title"])) if w not in n._STOP]
        head_ = re.sub(r"(\.\.\.|…)$", "", re.sub(r"\[[^\]]*\]", "", r["title"])).strip()
        rest = body.replace(head_, " ", 1) if head_ else body   # 본문 맨 앞에 제목만 붙고 나머지는 딴 내용인 경우 제목은 빼고 봄
        hit = sum(1 for w in set(words) if w in rest)
        if body and hit < min(2, len(set(words))):                      # 본문 자리에 다른 기사·광고가 잡힌 경우(11.27. 세계일보) → 본문 없음으로
            body = ""
        arts[f"a{k}"] = {"title": r["title"], "press": r["press"], "url": r["url"], "pub": r["pub"].isoformat(),
                         "summary": r.get("summary") or "", "body": body}
    out = {"label": label, "test": test, "now": now.isoformat(), "since": since.isoformat(), "cut": now.isoformat(),
           "briefing_day": not holiday, "intro": n.intro_text(label, now),
           "articles": arts,
           "title_only_ids": [i for i, a in arts.items() if not a["body"]]}
    json.dump(out, open("brief_input.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"브리핑 날: {'예' if not holiday else '아니오(주말·공휴일)'} · {label} · 기사 {len(arts)}건"
          f"(본문 없음 {len(out['title_only_ids'])}건) · 범위 {since:%m-%d %H:%M}~{now:%m-%d %H:%M} → brief_input.json")


def check(path):
    d = json.load(open(path, encoding="utf-8"))
    st = _state()
    by_url = {r["url"]: r for r in st.get("audio_log", [])}
    arts = d.get("articles", {})
    now = dt.datetime.now(n.KST)
    probs = []

    def src_of(ids):
        g = []
        for i in ids:
            a = arts.get(i)
            if not a:
                probs.append(f"없는 기사 id: {i}")
                continue
            r = by_url.get(a["url"]) or {}
            g.append({"title": a["title"], "summary": r.get("summary") or "", "body": r.get("body") or a.get("body") or ""})
        return g

    def lint(where, text, ids):
        g = src_of(ids)
        if not g:
            probs.append(f"{where}: 근거 기사(sources) 없음")
            return
        if not any(x["body"] for x in g):
            probs.append(f"{where}: 근거 기사 본문이 없음(제목뿐) → 원고에서 빼고 title_only로")
        src = n._src_text(g)
        for sent in re.split(r"(?<=[.?!])\s+", text):
            bad = n.unsupported(sent, src, now)
            if bad:
                probs.append(f"{where}: 원문에 없는 숫자·기관명 {', '.join(bad)} | {sent[:60]}")
        for pat, why in BANNED:
            for sent in re.split(r"(?<=[.?!])\s+", text):
                if re.search(pat, sent) and not (why.startswith("앵커") and n._SAID.search(sent)):
                    probs.append(f"{where}: {why} | {sent[:60]}")
    if d.get("label") not in ("아침", "오후"):
        probs.append("label은 아침 또는 오후")
    for k, p in enumerate(d.get("paras", []), 1):
        lint(f"원고 {k}문단", p.get("text", ""), p.get("sources", []))
        for sent in re.split(r"(?<=[.?!])\s+", p.get("text", "").strip()):
            if sent and not re.search(r"(니다|니까)[.?!]?$", sent):
                probs.append(f"원고 {k}문단: '~습니다/~입니다'로 끝나지 않음 | {sent[:50]}")
            if len(sent) > 95:
                probs.append(f"원고 {k}문단: 문장이 김({len(sent)}자) | {sent[:40]}")
    import cards_v2
    cs = d.get("cards", [])
    n_pg = 3 + sum(2 if (c.get("viz") or {}).get("type") in cards_v2.RENDER else 1 for c in cs)
    if len(cs) > n.CARD_MAX:
        probs.append(f"카드: 사안은 {n.CARD_MAX}개까지(지금 {len(cs)}개)")
    elif n_pg > cards_v2.ALBUM_MAX and n.CARD_SEND != "pdf":   # PDF로 보낼 땐 장수 제한 없음
        probs.append(f"카드: 표지·목차·마무리 포함 {n_pg}장 → 10장 넘음. 덜 중요한 사안의 viz를 빼서 10장 이내로"
                     "(그대로 두면 뒤 사안 시각화부터 자동으로 빠짐)")
    for k, c in enumerate(cs, 1):
        src = n._src_text(src_of(c.get("sources", [])))
        if len(c.get("headline", "")) > 30:
            probs.append(f"카드 {k}: 제목이 김({len(c['headline'])}자)")
        if not 1 <= len(c.get("points", [])) <= 3:
            probs.append(f"카드 {k}: 요점은 1~3개")
        if c.get("tag") not in n.CARD_TAGS:
            probs.append(f"카드 {k}: tag는 {', '.join(n.CARD_TAGS)} 중 하나")
        if len(c.get("lead", "")) > 80:
            probs.append(f"카드 {k}: 핵심 문장이 김({len(c['lead'])}자)")
        texts = [c.get("headline", ""), c.get("lead", "")] + c.get("points", [])
        st = c.get("stat") or {}
        if st:
            texts += [str(st.get("big", "")) + str(st.get("unit", "")), st.get("desc", "")]
        v = c.get("viz") or {}
        if v:
            if not v.get("lead"):
                probs.append(f"카드 {k}: 시각화 장에 그림 위 한 문장(viz.lead)이 없음")
            elif len(v["lead"]) > 70:
                probs.append(f"카드 {k}: 시각화 한 문장이 김({len(v['lead'])}자)")
            if v.get("type") not in cards_v2.RENDER:
                probs.append(f"카드 {k}: viz.type은 {', '.join(cards_v2.RENDER)} 중 하나")
            texts += cards_v2.viz_texts(v)
        for t in texts:
            if not t:
                continue
            if t in c.get("points", []) and len(t) > 50:
                probs.append(f"카드 {k}: 요점이 김({len(t)}자) | {t[:30]}")
            bad = n.unsupported(re.sub(r"<[^>]+>", " ", t), src, now)
            if bad:
                probs.append(f"카드 {k}: 원문에 없는 숫자·기관명 {', '.join(bad)} | {t[:40]}")
            if re.search(r"관할하게|산하(?:가|로) 됨|[一-鿿]", t):
                probs.append(f"카드 {k}: 소속 변화 표현·한자 | {t[:40]}")
    if not d.get("paras"):
        probs.append("원고 문단이 없음")
    for x in probs:
        print("문제 " + x)
    print("점검 통과" if not probs else f"문제 {len(probs)}건 — 고친 뒤 다시 check")
    return not probs


def preview(path):
    """GitHub이 만들 것과 같은 틀로 카드 그림을 미리 만들고 한 장으로 모은 그림(preview/sheet.png)을 만듦 → 눈으로 확인"""
    d = json.load(open(path, encoding="utf-8"))
    arts = d.get("articles", {})
    now = dt.datetime.now(n.KST)
    use, cards = [], []
    for c in d.get("cards", [])[:n.CARD_MAX]:
        g = [arts[i] for i in c.get("sources", []) if i in arts]
        if not g:
            continue
        use.append(g)
        cards.append(n.card_fields(c))
    if not cards:
        print("카드 없음")
        return
    sources = [n.press_name(g[0]["press"]) + (f" 외 {len(g) - 1}건" if len(g) > 1 else "") for g in use]
    import cards_v2
    since = dt.datetime.fromisoformat(d["since"]) if d.get("since") else now - dt.timedelta(hours=14)
    until = min(dt.datetime.fromisoformat(d["cut"]), now) if d.get("cut") else now
    pages = cards_v2.build(cards, sources, now, d["label"], since, until)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")
    for f in os.listdir(out) if os.path.isdir(out) else []:
        if f.startswith("card_"):
            os.remove(os.path.join(out, f))
    paths = n.render_cards(pages, out, 1080)
    try:                                            # 한눈에 보도록 4장씩 한 줄로 모은 그림
        from PIL import Image
        ims = [Image.open(p_).resize((540, 540)) for p_ in paths]
        cols = 4
        rows = (len(ims) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * 560 + 20, rows * 560 + 20), "#cfd6df")
        for i, im in enumerate(ims):
            sheet.paste(im, (20 + (i % cols) * 560, 20 + (i // cols) * 560))
        sheet.save(os.path.join(out, "sheet.png"))
        print(f"미리보기: {os.path.join(out, 'sheet.png')} (카드 {len(paths)}장, 낱장은 {out}/card_N.png)")
    except Exception as e:
        print(f"미리보기 낱장: {out} ({type(e).__name__})")


def publish(path):
    if not check(path):
        sys.exit(1)
    root = os.path.dirname(os.path.abspath(__file__))
    rel = os.path.relpath(os.path.abspath(path), root)
    run = lambda *a: subprocess.run(a, cwd=root, check=True)  # noqa: E731
    run("git", "add", rel)
    run("git", "commit", "-q", "-m", f"브리핑 원고 {os.path.basename(rel)} [skip ci]")
    for _ in range(3):
        if subprocess.run(["git", "pull", "-q", "--rebase", "--autostash", "origin", "main"], cwd=root).returncode == 0 and \
                subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=root).returncode == 0:
            break
    else:
        sys.exit("올리기 실패")
    run("gh", "api", "-X", "POST", f"repos/{REPO}/actions/workflows/news-alert.yml/dispatches",
        "-f", "ref=main", "-f", f"inputs[brief_file]={rel}")
    print(f"발송 요청 완료: {rel} (1~3분 뒤 텔레그램 도착)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare":                            # prepare [아침|오후] [--test 시간]
        args = sys.argv[2:]
        hrs = int(args[args.index("--test") + 1]) if "--test" in args else 0
        prepare(next((x for x in args if x in ("아침", "오후")), ""), hrs, bool(hrs))
    elif cmd == "check":
        sys.exit(0 if check(sys.argv[2]) else 1)
    elif cmd == "preview":
        preview(sys.argv[2])
    elif cmd == "publish":
        publish(sys.argv[2])
    else:
        print(__doc__)
