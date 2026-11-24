# -*- coding: utf-8 -*-
"""법령 뷰어 모바일: 브라우저(Pyodide) 안에서 lawcore 를 그대로 돌리는 얇은 층.
worker.js 가 call(함수이름, JSON 인자) 로 부르고, 결과는 JSON 글로 돌려준다.
접속은 브라우저의 동기 XMLHttpRequest(워커 안에서만 허용)로 법제처 API 를 직접 부른다."""
import json
import re
import urllib.parse
import concurrent.futures as _cf

import lawcore as L

L.API = "https://www.law.go.kr/DRF/"
_API_BASE = {"v": None}


# ---- 쓰레드가 없는 환경이라 ThreadPoolExecutor 를 차례로 실행하는 것으로 바꾼다
class _Fut:
    def __init__(self, fn, a, k):
        try:
            self._v, self._e = fn(*a, **k), None
        except BaseException as e:  # noqa: BLE001
            self._v, self._e = None, e

    def result(self, timeout=None):
        if self._e is not None:
            raise self._e
        return self._v


class _SyncPool:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *a, **k):
        return _Fut(fn, a, k)

    def map(self, fn, it):
        return [fn(x) for x in it]


_cf.ThreadPoolExecutor = _SyncPool

_NET = {}
_NETS = {}          # 기기에 저장해 둔 응답(글 그대로) — 쓸 때 한 번만 해석


def getter(url, params, timeout=25, tries=2):
    """법제처 JSON 조회(법령 본문은 이 접속 동안 다시 받지 않는다)"""
    from js import XMLHttpRequest
    if _API_BASE["v"]:
        url = url.replace("https://www.law.go.kr/DRF/", _API_BASE["v"])
    full = url + "?" + urllib.parse.urlencode(params)
    ck = full.replace(params.get("OC", ""), "")
    if ck in _NET:
        return _NET[ck]
    if ck in _NETS:
        try:
            _NET[ck] = json.loads(_NETS.pop(ck))
            return _NET[ck]
        except ValueError:
            _NETS.pop(ck, None)
    last = None
    for _ in range(tries):
        try:
            x = XMLHttpRequest.new()
            x.open("GET", full, False)
            x.send()
            if x.status != 200:
                last = L.LawError("법제처 접속 오류(HTTP %s)" % x.status)
                continue
            d = json.loads(x.responseText)
            if str(url).endswith("lawService.do") or len(_NET) < 200:
                _NET[ck] = d
            return d
        except ValueError:
            last = L.LawError("법제처 응답을 읽을 수 없음(JSON 아님) — 인증키(OC)와 접속 허용 도메인을 확인하세요")
        except Exception as e:  # noqa: BLE001
            last = L.LawError("법제처에 접속하지 못했습니다: %s" % e)
    raise last


def set_api_base(base):
    _API_BASE["v"] = base or None
    return "ok"


# ---- 보내는 모양 만들기
_ANNEX_RE = re.compile(r"별표\s*(\d+)(?:\s*의\s*(\d+))?|별지\s*제\s*(\d+)\s*호(?:\s*의\s*(\d+))?\s*서식|서식\s*제?\s*(\d+)\s*호")
_CODE_RE = re.compile(r"(?<![\d-])\d{1,2}(?:-\d{1,2}){2}(?![\d-])")


def _spans(text, selfname, law=None, lv=0):
    out = []
    taken = []
    for c in L.cite_spans(text, selfname):
        out.append([c["start"], c["end"], c["law"], c["n"], c["b"], c["sub"]])
        taken.append((c["start"], c["end"]))
    hm = L.HEADER_RE.match(text) if lv == 0 else None
    hdr = hm.end() if hm else 0
    if law is not None:                    # '별표 3'·'서식 제2호'(그 법령에 있으면) / 지침의 '2-3-12' -> 눌러서 이동
        try:
            labs = law.get("_axl")
            if labs is None:
                labs = law["_axl"] = {x["label"] for x in (law.get("annex") or [])}
            last_other = {}                                              # 한 문장에서 '「건축법 시행령」 별표 1 … 다만 별표 1의 …' 처럼 이름 없이 되풀이되는 별표는 같은 법령 것
            for m in _ANNEX_RE.finditer(text):
                if m.start() < hdr:
                    continue
                if m.group(1):
                    lab = "별표 %s%s" % (m.group(1), ("의" + m.group(2)) if m.group(2) else "")
                elif m.group(3):
                    lab = "서식 %s%s" % (m.group(3), ("의" + m.group(4)) if m.group(4) else "")          # '별지 제3호서식'·'별지 제3호의2서식'
                else:
                    lab = "서식 %s" % m.group(5)
                if re.search(r"같은\s*법(?:\s*시행령|\s*시행규칙)?\s*$", text[:m.start()]):
                    continue                                              # '같은 법 시행령 별표 1' — 어느 법인지 앞 문장에 달려 있어 잘못 잇는 것보다 잇지 않는다
                other = _annex_owner(text, m.start(), selfname)          # '「건축법 시행령」 별표 1'·'영 별표 1' -> 보고 있는 법령이 아니라 그 법령의 별표
                if other:
                    last_other[lab] = other[1]
                elif lab in last_other:
                    other = (m.start(), last_other[lab])
                if other:
                    out.append([other[0], m.end(), "@j", other[1] + "|" + lab, 0, None]); taken.append((other[0], m.end()))
                elif lab in labs:
                    out.append([m.start(), m.end(), "@x", lab, 0, None]); taken.append((m.start(), m.end()))
            if law.get("kind") == "adm":
                codes = law.get("_cdm")
                if codes is None:
                    codes = law["_cdm"] = {x["label"]: k for k, x in law["arts"].items()}
                for m in _CODE_RE.finditer(text):
                    if m.start() < hdr or m.group(0) not in codes or any(x <= m.start() < y for x, y in taken):
                        continue
                    if text[:m.start()].strip() == "" and text[m.end():m.end() + 1] == ".":
                        continue
                    nmk = _named_before_code(text, m.start(), selfname)       # '「광역도시계획수립지침」 3-5-3' -> 그 지침의 3-5-3 (이 지침 안의 번호가 아님)
                    if nmk:
                        out.append([nmk[0], m.end(), "@k", nmk[1] + "|" + m.group(0), 0, None]); taken.append((nmk[0], m.end()))
                        continue
                    out.append([m.start(), m.end(), "@c", codes[m.group(0)], 0, None]); taken.append((m.start(), m.end()))
        except Exception:  # noqa: BLE001
            pass
    try:                                   # '대통령령으로'·'정하여 고시'·'조례' — 눌러서 관련 조문 목록을 만든다(PC 와 같음)
        for a, b, to in L.deleg_spans(text, selfname, taken):
            out.append([a, b, "@d", to, 0, None]); taken.append((a, b))
        for a, b in L.adm_spans(text, taken):
            nm = _adm_name_before(text, a, b)           # '지구단위계획수립지침' 처럼 이름이 붙어 있으면 그 지침 자체를 연다
            if nm and not any(a - len(nm) < e and b > s0 for s0, e in taken):
                out.append([a - len(nm), b, "@n", nm + "지침", 0, None]); taken.append((a - len(nm), b))
            else:
                out.append([a, b, "@a", "", 0, None]); taken.append((a, b))
        for a, b in L.ordin_spans(text, taken):
            out.append([a, b, "@o", "", 0, None]); taken.append((a, b))
    except Exception:  # noqa: BLE001
        pass
    return out


_ADM_NAME_BAD = ("이", "그", "같은", "해당", "다음", "관계", "관련", "각종", "동", "별도", "위", "아래", "각", "이하", "상기")


def _adm_name_before(text, a, b):
    """'지침' 글자(text[a:b]) 바로 앞에 공백 없이 붙은 이름 조각('지구단위계획수립') -> 있으면 그 글, 없으면 ''"""
    import re as _re
    if text[a:b] != "지침":
        return ""
    m = _re.search(r"([가-힣ㆍ·]{2,30})$", text[:a])
    if not m or m.group(1) in _ADM_NAME_BAD:
        return ""
    return m.group(1)


def _annex_owner(text, pos, selfname):
    """'별표 n' 바로 앞에 다른 법령 이름이 붙어 있으면 (시작 위치, 법령 이름). 없거나 지금 보는 법령이면 None.
    「건축법 시행령」 별표 1 / 건축법 시행령 별표 1 / 영 별표 1 · 시행령 별표 1 · 규칙 별표 1 · 법 별표 1 (지금 보는 법령 기준)"""
    before = text[:pos]
    nz = lambda x: re.sub(r"\s+", "", x)
    m = re.search(r"「([^」]{2,60})」\s*$", before)
    if m:
        nm = m.group(1).strip()
        return None if nz(nm) == nz(selfname) else (m.start(), nm)
    m = re.search(r"((?:[가-힣ㆍ·A-Za-z0-9]+\s)*[가-힣ㆍ·A-Za-z0-9]*(?:법|법률)\s*(?:시행령|시행규칙))\s*$", before)   # 괄호 없이 쓴 '건축법 시행령'
    if m:
        nm = re.sub(r"\s+", " ", m.group(1)).strip()
        nm = re.sub(r"^(?:이|같은|위|해당|그)\s+", "", nm)
        if len(nz(nm)) >= 6 and nz(nm) != nz(selfname) and not nm.startswith("법 "):
            return (m.start(1) + (len(m.group(1)) - len(m.group(1).lstrip())), nm)
    m = re.search(r"(?:^|(?<=[\s(「,·]))(시행령|시행규칙|영|규칙|법)\s*$", before)
    if m:
        base, sfx = L._split_name(selfname)
        tok = m.group(1)
        tgt = base + " 시행령" if tok in ("영", "시행령") else base + " 시행규칙" if tok in ("규칙", "시행규칙") else base
        if nz(tgt) != nz(selfname) and base:
            return (m.start(1), tgt)
    return None


def _named_before_code(text, pos, selfname):
    """번호(text[pos:]) 바로 앞에 다른 지침 이름이 있으면 (이름 시작 위치, 이름) — '「광역도시계획수립지침」 3-5-3', '광역도시계획수립지침 3-5-3', '…지침」의 3-5-3'"""
    import re as _re
    m = _re.search(r"(?:「([^」]{2,40})」|([가-힣ㆍ·]{2,30}))(?:의)?\s*$", text[:pos])
    if not m:
        return None
    nm = m.group(1) or m.group(2)
    if not nm.endswith("지침") or nm in _ADM_NAME_BAD:
        return None
    norm = lambda x: _re.sub(r"[\sㆍ·]", "", x or "")
    me = norm(selfname)
    if me and (norm(nm) == me or me.endswith(norm(nm))):         # 지금 보는 지침 자신이면 보통 번호 링크
        return None
    return (m.start(), nm)


def _art_json(a, selfname, spans=True, law=None):
    lines = []
    for lv, t in a["lines"]:
        if lv == 9:                                                  # 조문 속 표 한 행: 칸마다 인용 위치를 붙인다
            import json
            try:
                row = json.loads(t)
                lines.append([9, json.dumps([[c[0], c[1], (c[2] if len(c) > 2 else 1), _ax_spans(c[1], selfname, law) if spans else []] for c in row], ensure_ascii=False), []])
            except Exception:  # noqa: BLE001
                lines.append([9, t, []])
            continue
        lines.append([lv, t, _spans(t, selfname, law, lv) if spans else []])
    return {"key": a["key"], "n": a["n"], "b": a["b"], "label": a["label"], "title": a["title"], "lines": lines,
            "inn": list(a.get("in_keys") or [])}


_AX_QUICK = re.compile(r"제\s*\d|「|별표|서식|\d-\d|지침")


def _ax_spans(t, refname, law):
    """별표·서식 글 한 줄의 인용 위치(조문 인용·별표 이동·지침 이름). 위임·조례 목록(@d/@a/@o)은 조문 키가 있어야 해서 뺀다"""
    if not t or not _AX_QUICK.search(t):
        return []
    try:
        return [s for s in _spans(t, refname, law, 4) if str(s[2])[:2] not in ("@d", "@a", "@o")]
    except Exception:  # noqa: BLE001
        return []


def _ax_lines(law, refname, x):
    import json
    out = []
    for lv, t in x["text"]:
        if lv == 9:
            try:
                row = json.loads(t)
            except Exception:  # noqa: BLE001
                out.append([lv, t])
                continue
            out.append([9, json.dumps([[c[0], c[1], (c[2] if len(c) > 2 else 1), _ax_spans(c[1], refname, law)] for c in row], ensure_ascii=False)])
        else:
            out.append([lv, t, _ax_spans(t, refname, law) if lv else []])
    return out


def _doc_json(law, selfname_target):
    refname = law.get("refname") or law["name"]
    arts = {}
    for k in law["order"]:
        arts[k] = _art_json(law["arts"][k], refname, True, law)
    items = []
    for it in law["items"]:
        items.append({"k": "h", "t": it["text"]} if it["kind"] == "head" else {"k": "a", "key": it["key"]})
    base = L._split_name(refname)[0] if law.get("kind") == "adm" else None
    annex = []
    for x in (law.get("annex") or []):
        url = L.annex_url(x.get("pdf") or x.get("link") or "")
        web = False
        if not url and law.get("kind") == "ordin" and not x.get("text") and not x.get("deleted"):      # 조례 별표는 본문·파일 링크가 안 올 때가 있어 법제처 화면으로 안내
            url, web = L.ordin_web_url(law.get("name") or refname), True
        annex.append({"label": x["label"], "title": x["title"], "lines": _ax_lines(law, refname, x),
                      "url": url, "web": web, "deleted": bool(x.get("deleted"))})
    return {"name": law["name"], "kind": law.get("kind", "law"), "mst": law["mst"], "date": law.get("date", ""),
            "refname": refname, "adm_base": base, "items": items, "arts": arts, "annex": annex}


# ---- 화면에서 부르는 함수들
def _hits(r):
    return [{"kind": h["kind"], "lawname": h["lawname"], "rawmst": h["rawmst"], "mst": h["mst"], "short": h["short"],
             "info": h.get("snippet", ""), "date": h.get("date", "")} for h in r["hits"]]


def search(oc, q):
    """데스크톱 run()과 같은 순서: 조 번호가 있으면 바로 열기 / 법 이름·약칭이면 법·시행령·시행규칙·지침 목록 / 그 밖엔 이름 검색."""
    q = (q or "").strip()
    kind = "art" if "조례" in q else L.query_kind(q)
    if kind == "empty":
        kind = "name"
    msg = ""
    if "조례" in q:                                         # 지자체 조례: '남양주시 도시계획 조례' (이름 목록) · '남양주시 도시계획 조례 제55조' (그 조문 바로 열기)
        name, jo, ji = L.parse_ordin_query(q)
        if not name:
            return {"hits": [], "total": 0, "msg": "조례 이름을 알 수 없습니다. 예) 고양시 도시계획 조례 55조"}
        if jo:
            try:
                f = L.find_ordin(oc, name, getter)
            except Exception as e:  # noqa: BLE001
                return {"hits": [], "total": 0, "msg": "'%s' 조례를 찾지 못했습니다 — 이름만 쳐서 목록에서 골라 보세요" % name}
            return {"hits": [], "total": 0, "open": {"kind": "ordin", "rawmst": f["mst"], "name": f["name"], "date": f["date"], "key": "%d:%d" % (jo, ji or 0)}}
        kind = "name"
    elif kind == "art":
        p = L.parse_query(q)
        if p.get("jo"):
            return {"hits": [], "total": 0, "open": {"name": L.full_name(p), "key": "%d:%d" % (p["jo"], p.get("ji") or 0)}}
        kind = "law"
    if kind == "law":
        names, rest = L.split_query(q)
        if names and len(names) == 3 and not rest:
            try:
                r = L.family_search(oc, q, getter)
            except Exception as e:  # noqa: BLE001
                r = None
                msg = "목록을 만들지 못했습니다: %s" % e
            if r and r["hits"]:
                return {"hits": _hits(r), "total": r["total"], "msg": r.get("note", "")}
        if names:
            return {"hits": [], "total": 0, "open": {"name": names[0] if len(names) == 1 else L.full_name(L.parse_query(q)), "key": ""}}
        return {"hits": [], "total": 0, "open": {"name": L.full_name(L.parse_query(q)), "key": ""}}
    r = L.name_or_text_search(oc, q, getter)
    return {"hits": _hits(r), "total": r["total"], "msg": msg or r.get("note", "") or ("'%s' 이름의 법령·행정규칙·자치법규를 찾지 못했고, 조례 본문에서도 없습니다." % q if not r["hits"] else "")}


def suggest(oc, q):
    """검색창 자동완성: 약칭 풀이 + 법제처 이름 검색 -> {q, items, tail}"""
    return {"q": q, "items": L.suggest(oc, q, getter), "tail": L.suggest_split(q)[2]}


def open_doc(oc, kind, rawmst, name, date=""):
    if kind == "ordin":
        law = L.load_ordin(oc, rawmst, name, getter)
        pre = "o"
    elif kind == "adm":
        law = L.load_adm(oc, rawmst, name, date, getter)
        pre = "a"
    else:
        law = L.load_whole(oc, rawmst, name, getter)
        pre = ""
    law["name"], law["mst"], law["date"] = name, pre + rawmst, date
    L._LAW_CACHE[law["mst"]] = law
    return _doc_json(law, name)


def open_by_name(oc, name):
    f = L.find_law(oc, name, getter)
    return open_doc(oc, "law", f["mst"], f["name"], f["date"])


def _find_art(law, n, b):
    k = "%d:%d" % (n, b)
    if k in law["arts"]:
        return law["arts"][k]
    return law["arts"].get("%d:0" % n)


def open_cite(oc, lawname, n, b, subs, ctx_mst=""):
    """인용 조문 미리보기 -> {lawname, mst, art, hl:[[첫줄,끝줄)…], summary}"""
    law = L._LAW_CACHE.get(ctx_mst) if ctx_mst else None
    if law is None or L._nospace(law["name"]) != L._nospace(lawname):
        f = L.find_law(oc, lawname, getter)
        law = L._LAW_CACHE.get(f["mst"]) or L.load_whole(oc, f["mst"], f["name"], getter)
        law["name"], law["mst"] = f["name"], f["mst"]
        L._LAW_CACHE[f["mst"]] = law
    a = _find_art(law, n, b)
    if a is None:
        return {"lawname": law["name"], "mst": law["mst"], "art": None, "label": L.jo_label(n, b)}
    hl = []
    for sb in L.expand_subs(subs):
        r = L.find_sub(a["lines"], sb)
        if r:
            hl.append([r[0], r[1]])
    return {"lawname": law["name"], "mst": law["mst"], "art": _art_json(a, law.get("refname") or law["name"], True, law), "hl": hl,
            "summary": L.cite_summary(a, subs), "label": a["label"]}


def cite_titles(items):
    """[[법령명, 조, 가지]…] -> 이미 받아 둔 법령에서 조문 제목을 찾아 같은 순서로 돌려준다(새로 내려받지 않음)"""
    out = []
    for name, n, b in items:
        t = ""
        for law in list(L._LAW_CACHE.values()):
            try:
                if L._nospace(law["name"]) == L._nospace(name) or L._nospace(law.get("refname") or "") == L._nospace(name):
                    ar = _find_art(law, int(n), int(b or 0))
                    if ar is not None:
                        t = ar["title"]
                        break
            except Exception:
                continue
        out.append(t)
    return out


def fav_rel(oc, base):
    """본법 이름 -> 시행령·시행규칙 [{name,mst,date}] (없으면 건너뜀)"""
    out = []
    for suf in ("시행령", "시행규칙"):
        try:
            f = L.find_law(oc, base + " " + suf, getter)
        except Exception:  # noqa: BLE001
            continue
        if L._nospace(f["name"]) == L._nospace(base + " " + suf):
            out.append({"name": f["name"], "mst": f["mst"], "date": f.get("date", "")})
    return out


def tt_open(oc, base, cur_name, cur_mst, cur_key):
    """3단 비교표 줄 목록과 시작 줄"""
    tt = L.three_tier(oc, base, getter)
    law = L._LAW_CACHE.get(cur_mst)
    start = L.tt_start(tt, law, cur_key, cur_name) if (law and cur_key) else 0
    rows = []
    for r in tt["rows"]:
        a = tt["laws"][0]["arts"][r["key"]]
        rows.append({"key": r["key"], "label": a["label"], "title": a["title"], "nd": len(r["dec"]), "nr": len(r["rule"]), "dec": list(r["dec"]), "rule": list(r["rule"])})
    return {"names": tt["names"], "rows": rows, "start": start, "have": [x is not None for x in tt["laws"]]}


def tt_row(base, i):
    tt = L._TT_CACHE[L._split_name(base)[0]]
    r = tt["rows"][i]
    rc = tt.get("rcite") or {}
    cols = []
    for ci, keys in enumerate(([r["key"]], r["dec"], r["rule"])):
        law = tt["laws"][ci]
        arts = []
        if law:
            for k in keys:
                aj = _art_json(law["arts"][k], tt["names"][ci])
                if ci == 0:                                     # 법 조문 -> 이 줄의 시행령·시행규칙 조문 모두
                    rel = ["1:" + x for x in r["dec"]] + ["2:" + x for x in r["rule"]]
                elif ci == 1:                                   # 시행령 조문 -> 법 조문 + 그 조를 인용한 시행규칙 조문
                    rel = ["0:" + r["key"]] + ["2:" + x for x in r["rule"] if k in rc.get(x, [])]
                else:                                           # 시행규칙 조문 -> 법 조문 + 그 조문이 인용한 시행령 조문
                    rel = ["0:" + r["key"]] + ["1:" + x for x in rc.get(k, []) if x in r["dec"]]
                aj["rel"] = rel
                arts.append(aj)
        cols.append({"name": tt["names"][ci], "mst": (law or {}).get("mst", ""), "arts": arts, "none": law is None})
    return {"cols": cols}


def _hit(h):
    mst = str(h["mst"])
    lw = L._LAW_CACHE.get(mst)
    if lw is not None:
        lw.setdefault("name", h["lawname"]); lw.setdefault("mst", mst)
    kind = "adm" if h.get("adm") or mst.startswith("a") else "ordin" if h.get("ord") or mst.startswith("o") else "law"
    return {"kind": kind, "mst": mst, "rawmst": mst[1:] if kind != "law" else mst, "lawname": h["lawname"], "key": h.get("key", ""),
            "label": h.get("label", ""), "title": h.get("title", ""), "short": h.get("short", ""), "snippet": h.get("snippet", ""),
            "ext": bool(h.get("ext"))}


def _hitres(r, note=""):
    return {"hits": [_hit(h) for h in r["hits"]], "total": r["total"], "note": note or r.get("note", ""), "toks": r.get("toks", []),
            "searched": r.get("searched", [])}


def kw_search(oc, q, city=""):
    """키워드로 조문 찾기(PC 의 '키워드 검색')"""
    r = L.keyword_search(oc, q, None, getter, 80, True, city or "")
    return _hitres(r)


def open_cached(mst, key=""):
    """키워드·위임 결과를 누르면, 이미 받아 둔 법령을 본문으로 연다"""
    law = L._LAW_CACHE.get(mst)
    if law is None:
        raise L.LawError("이 법령은 다시 검색해서 열어 주세요")
    d = _doc_json(law, law["name"])
    d["rawmst"] = mst[1:] if mst[:1] in ("a", "o") else mst
    d["kind"] = "adm" if mst.startswith("a") else "ordin" if mst.startswith("o") else "law"
    return d


def _cur_art(mst, key):
    law = L._LAW_CACHE.get(mst)
    a = law["arts"].get(key) if law else None
    if not a:
        raise L.LawError("조문을 찾지 못했습니다")
    return law, a


def list_deleg(oc, mst, key, to):
    """'대통령령으로'를 누르면: 이 조문을 위임받은 시행령(시행규칙) 조문 목록"""
    law, a = _cur_art(mst, key)
    r = L.deleg_lookup(oc, law["name"], to, a["n"], a["b"], a.get("title", ""), getter)
    return _hitres(r)


def list_adm(oc, mst, key, line=""):
    """'정하여 고시한다'를 누르면: 관련 지침·고시"""
    law, a = _cur_art(mst, key)
    r = L.adm_search(oc, a.get("title", ""), law["name"], a.get("scan", ""), line=line, cite=(law["name"], a["n"], a["b"]), getter=getter)
    return _hitres(r)


def find_adm(oc, name):
    """본문에 이름이 적힌 지침('지구단위계획수립지침')을 이름으로 찾는다 -> 검색결과 꼴 + exact(이름이 같은 첫 건이면 그 건)"""
    import re as _re
    r = L.adm_search(oc, name, "", "", typed=name, getter=getter)
    out = _hitres(r, "'%s' 을(를) 이름으로 찾은 행정규칙" % name)
    want = _re.sub(r"[\sㆍ·]", "", name)
    hs = out["hits"]
    hs.sort(key=lambda h: 0 if _re.sub(r"[\sㆍ·]", "", str(h.get("lawname") or "")) == want else 1)
    out["exact"] = bool(hs and _re.sub(r"[\sㆍ·]", "", str(hs[0].get("lawname") or "")) == want)
    return out


def list_ordin(oc, mst, key, city=""):
    """'조례'를 누르면: 기본 자치단체 조례에서 이 조문 제목 낱말이 든 조문"""
    import re as _re
    law, a = _cur_art(mst, key)
    toks = []
    for w in _re.split(r"[\s·ㆍ,()]+", a.get("title", "")):
        w = _re.sub(r"(에서의|에서|으로의|의)$", "", w)
        if len(w) >= 2 and w not in ("및", "경우", "기준", "대한", "관한", "관련") and w not in toks:
            toks.append(w)
    if not toks:
        toks = _re.findall(r"[가-힣]{2,}", a.get("scan", ""))[:3]
    r = L.ordin_lookup(oc, city or "고양시", toks, getter)
    return _hitres(r, "%s — '%s' 관련 조문" % (r.get("ordname", ""), " ".join(toks)))


def rev_check(oc, items):
    """즐겨찾기 법령들의 현재 시행본(일련번호·시행일)을 이름으로 다시 찾는다. items = [[kind, name], ...] -> {kind|name: {rawmst, date}}"""
    import re as _re
    norm = lambda x: _re.sub(r"[\sㆍ·]", "", _re.sub(r"\[[^\]]*\]", "", x or ""))
    out = {}
    for kind, name in items:
        k = "%s|%s" % (kind, name)
        if k in out:
            continue
        try:
            r = L.name_search(oc, name, getter)
        except Exception:  # noqa: BLE001
            continue
        for h in r["hits"]:
            if h["kind"] == kind and norm(h["lawname"]) == norm(name):
                out[k] = {"rawmst": h["rawmst"], "date": h.get("date", "")}
                break
    return out


def _digits(x):
    return re.sub(r"\D", "", str(x or ""))


def _ymd(x):
    p = re.findall(r"\d+", str(x or ""))
    if len(p) >= 3:
        return "%04d%02d%02d" % (int(p[0]), int(p[1]), int(p[2]))
    return _digits(x)


def _versions(oc, name, nw="", pages=6):
    """같은 법령의 시행본 목록(연혁·현행·시행예정 모두) -> [{mst, date(시행일), pub(공포일), kind}] 시행일 순.
    법제처 '시행일 법령' 목록(target=eflaw)을 쪽(100개)씩 넘기며 모은다. nw=2 면 시행예정만."""
    nm = lambda x: re.sub(r"[\sㆍ·]", "", x or "")
    out, seen = [], set()
    for pg in range(1, pages + 1):
        prm = {"OC": oc, "target": "eflaw", "type": "JSON", "query": name, "display": 100, "page": pg}
        if nw:
            prm["nw"] = nw
        try:
            d = L._get("lawSearch.do", prm, getter)
        except Exception:  # noqa: BLE001
            break
        rows = []
        L._dicts_with(d, "법령명한글", rows)
        for r in rows:
            mst = str(r.get("법령일련번호") or "")
            if not mst or mst in seen or nm(r.get("법령명한글")) != nm(name):
                continue
            seen.add(mst)
            out.append({"mst": mst, "date": _digits(r.get("시행일자")), "pub": _digits(r.get("공포일자")), "kind": str(r.get("제개정구분명") or "")})
        if len(rows) < 100:
            break
    out.sort(key=lambda v: (v["date"], v["mst"]))
    return out


_VCACHE = {}


def _adm_versions(oc, name):
    """행정규칙의 연혁 목록(현행 + 연혁) -> [{mst, date(시행일), pub(발령일), kind}] 날짜 순. 현행은 기본 목록, 옛 것은 nw=2."""
    nm = lambda x: re.sub(r"[\sㆍ·]", "", x or "")
    out, seen = [], set()
    for nw in ("", "2"):
        for pg in range(1, 4):
            prm = {"OC": oc, "target": "admrul", "type": "JSON", "query": name, "display": 100, "page": pg}
            if nw:
                prm["nw"] = nw
            try:
                d = L._get("lawSearch.do", prm, getter)
            except Exception:  # noqa: BLE001
                break
            rows = []
            L._dicts_with(d, "행정규칙명", rows)
            for r in rows:
                mst = str(r.get("행정규칙일련번호") or "")
                if not mst or mst in seen or nm(r.get("행정규칙명")) != nm(name):
                    continue
                seen.add(mst)
                pub = _digits(r.get("발령일자"))
                out.append({"mst": mst, "date": _digits(r.get("시행일자")) or pub, "pub": pub, "kind": str(r.get("제개정구분명") or "")})
            if len(rows) < 100:
                break
    out.sort(key=lambda v: (v["date"] or v["pub"], v["mst"]))
    return out


def _vers_cached(oc, kind, name):
    import time
    k = (kind, name)
    c = _VCACHE.get(k)
    if c and time.time() - c[0] < 600:
        return c[1]
    v = _adm_versions(oc, name) if kind == "adm" else _versions(oc, name)
    if v:
        _VCACHE[k] = (time.time(), v)
    return v


def art_hist(oc, kind, name, key, rev_date, mst="", dummy=""):
    """본문의 '<개정 2021.1.5>' 를 누르면: 그 개정 때 이 조문이 어떻게 바뀌었는지(개정 직전 시행본 ↔ 그 개정을 담은 시행본).
    mst 를 주면 그 시행본을 '개정 후' 로 삼는다(이전·다음 개정 단추). 법령·행정규칙 모두."""
    if kind not in ("law", "adm"):
        return {"err": "개정 비교는 법령(법·시행령·시행규칙)과 행정규칙만 됩니다"}
    rd = _ymd(rev_date)
    vers = _vers_cached(oc, kind, name)
    if len(vers) < 2:
        return {"err": "이 %s의 이전 시행본 목록을 가져오지 못했습니다(연혁이 조회되지 않았습니다)" % ("행정규칙" if kind == "adm" else "법령")}
    if mst == "last":
        idx = len(vers) - 1
    elif mst:
        idx = next((i for i, v in enumerate(vers) if v["mst"] == str(mst)), -1)
    else:
        idx = next((i for i, v in enumerate(vers) if v["pub"] == rd), -1)
        if idx < 0 and kind == "adm":
            idx = next((i for i, v in enumerate(vers) if v["date"] == rd), -1)
    if idx < 0:
        return {"err": "%s 개정을 담은 시행본을 연혁에서 찾지 못했습니다" % rev_date}
    if idx == 0:
        return {"err": "이 개정이 조회되는 가장 오래된 시행본입니다(비교할 이전 시행본이 없습니다)", "next_mst": vers[1]["mst"] if len(vers) > 1 else "", "next_pub": vers[1]["pub"] if len(vers) > 1 else ""}
    A, B = vers[idx - 1], vers[idx]
    ld = (lambda m, d: L.load_adm(oc, m, name, d, getter)) if kind == "adm" else (lambda m, d: L.load_whole(oc, m, name, getter))
    la = ld(A["mst"], A["date"])
    lb = ld(B["mst"], B["date"])
    a, b = la["arts"].get(key), lb["arts"].get(key)
    flat = lambda x: re.sub(r"\s+", " ", " ".join(t for _, t in x["lines"])).strip()
    res = {"old_date": A["date"], "new_date": B["date"], "pub": B["pub"], "kind": B["kind"], "label": (b or a or {}).get("label", ""), "title": (b or a or {}).get("title", ""),
           "old_mst": A["mst"], "new_mst": B["mst"], "reason": lb.get("reason", ""), "idx": idx, "total": len(vers),
           "prev_mst": vers[idx - 1]["mst"] if idx >= 2 else "", "prev_pub": vers[idx - 1]["pub"] if idx >= 2 else "",
           "next_mst": vers[idx + 1]["mst"] if idx + 1 < len(vers) else "", "next_pub": vers[idx + 1]["pub"] if idx + 1 < len(vers) else ""}
    if a and b:
        ta, tb = flat(a), flat(b)
        if ta == tb and (a["title"] or "") == (b["title"] or ""):
            res.update({"st": "동일", "old": [["=", ta]], "new": [["=", tb]]})
        else:
            oa, ob = _tok_diff(ta, tb)
            res.update({"st": "수정", "old": oa, "new": ob, "otitle": a["title"] if (a["title"] or "") != (b["title"] or "") else ""})
    elif b:
        res.update({"st": "신설", "old": [], "new": [["+", flat(b)]]})
    elif a:
        res.update({"st": "삭제", "old": [["-", flat(a)]], "new": []})
    else:
        res.update({"st": "없음", "old": [], "new": []})
    return res


def upcoming(oc, items):
    """즐겨찾기 법령 중 곧 시행될(시행일이 오늘 이후인) 시행본이 있는 것 -> {kind|name: {rawmst, date}}"""
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    out = {}
    for kind, name in items[:25]:
        if kind != "law":
            continue
        try:
            vs = _versions(oc, name, "2", 1)
        except Exception:  # noqa: BLE001
            continue
        fut = [v for v in vs if v["date"] and v["date"] > today]
        if fut:
            out["%s|%s" % (kind, name)] = {"rawmst": fut[0]["mst"], "date": fut[0]["date"]}
    return out


def tt_ord(oc, base, i, city=""):
    """3단 비교의 '조례' 열: 이 줄의 법 조문 제목 낱말로 기본 자치단체 조례에서 조문 찾기"""
    tt = L._TT_CACHE[L._split_name(base)[0]]
    r = tt["rows"][i]
    law0 = tt["laws"][0]
    a = law0["arts"][r["key"]] if law0 else None
    if not a:
        return {"arts": []}
    toks = []
    for w in re.split(r"[\s·ㆍ,()]+", a.get("title", "")):
        w = re.sub(r"(에서의|에서|으로의|의)$", "", w)
        if len(w) >= 2 and w not in ("및", "경우", "기준", "대한", "관한", "관련") and w not in toks:
            toks.append(w)
    if not toks:
        return {"arts": []}
    res = L.ordin_lookup(oc, city or "고양시", toks, getter)
    arts, seen = [], set()
    for h in res["hits"]:
        lw = L._LAW_CACHE.get(str(h["mst"]))
        ar = lw["arts"].get(h.get("key")) if lw else None
        if not ar or (h["mst"], h["key"]) in seen:
            continue
        seen.add((h["mst"], h["key"]))
        aj = _art_json(ar, h["lawname"], True, lw)
        aj["mst"] = str(h["mst"])
        arts.append(aj)
        if len(arts) >= 6:
            break
    return {"ordname": res.get("ordname", ""), "arts": arts, "toks": toks, "total": res.get("total", 0)}


def _tok_diff(a, b):
    """두 글을 낱말 단위로 비교 -> (옛 글 조각, 새 글 조각). 조각 = [종류('=','-','+'), 글]"""
    import difflib
    ta, tb = re.findall(r"\S+|\s+", a), re.findall(r"\S+|\s+", b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    oa, ob = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        x, y = "".join(ta[i1:i2]), "".join(tb[j1:j2])
        if op == "equal":
            oa.append(["=", x]); ob.append(["=", y])
        else:
            if x:
                oa.append(["-", x])
            if y:
                ob.append(["+", y])
    return oa, ob


def _load_ver(oc, kind, rawmst, name, date):
    if kind == "ordin":
        return L.load_ordin(oc, rawmst, name, getter)
    if kind == "adm":
        return L.load_adm(oc, rawmst, name, date, getter)
    return L.load_whole(oc, rawmst, name, getter)


def compare_ver(oc, kind, name, old_mst, old_date, new_mst, new_date, key=""):
    """같은 법령의 옛 시행본과 새 시행본을 조문별로 비교 -> 바뀐 조문만(수정·신설·삭제) 낱말 단위 차이와 함께"""
    A = _load_ver(oc, kind, old_mst, name, old_date)
    B = _load_ver(oc, kind, new_mst, name, new_date)
    flat = lambda a: re.sub(r"\s+", " ", " ".join(t for _, t in a["lines"])).strip()
    order = list(B["order"]) + [k for k in A["order"] if k not in B["arts"]]
    out, same = [], 0
    for k in order:
        a, b = A["arts"].get(k), B["arts"].get(k)
        if a and b:
            ta, tb = flat(a), flat(b)
            if ta == tb and (a["title"] or "") == (b["title"] or ""):
                same += 1
                continue
            oa, ob = _tok_diff(ta, tb)
            out.append({"key": k, "label": b["label"], "title": b["title"], "st": "수정", "old": oa, "new": ob,
                        "otitle": a["title"] if (a["title"] or "") != (b["title"] or "") else ""})
        elif b:
            out.append({"key": k, "label": b["label"], "title": b["title"], "st": "신설", "old": [], "new": [["+", flat(b)]], "otitle": ""})
        else:
            out.append({"key": k, "label": a["label"], "title": a["title"], "st": "삭제", "old": [["-", flat(a)]], "new": [], "otitle": ""})
    if key:
        out.sort(key=lambda x: 0 if x["key"] == key else 1)
    return {"changes": out[:300], "total": len(out), "same": same, "truncated": len(out) > 300,
            "old_date": old_date, "new_date": new_date, "reason": B.get("reason", "")}


def law_hist(oc, kind, name):
    """법령 전체의 제·개정 이력 -> [{mst, date(시행일), pub(공포일), kind(제정·일부개정·타법개정…)}] 최신 순"""
    v = _vers_cached(oc, kind if kind in ("adm",) else "law", name)
    if not v:
        return {"err": "이 법령의 제·개정 이력을 가져오지 못했습니다"}
    return {"rows": sorted(v, key=lambda x: (x["pub"] or x["date"], x["mst"]), reverse=True)}


def ver_reason(oc, kind, rawmst, name, date=""):
    """한 시행본의 제·개정 이유(전체 법령 분석 없이 이유만)"""
    if kind in ("law", "") or kind is None:
        d = L._get("lawService.do", {"OC": oc, "target": "law", "type": "JSON", "MST": rawmst}, getter)
        return {"reason": L.extract_reason(d)}
    return {"reason": _load_ver(oc, kind, rawmst, name, date).get("reason", "")}


def chain(oc, mst, key):
    """조문 하나가 어디서 구체화되는지 한눈에: 이 조문 -> (시행령 -> 시행규칙) 위임 조문 + 관련 지침·고시"""
    law, a = _cur_art(mst, key)
    nm = law["name"]
    out = {"root": {"lawname": nm, "label": a["label"], "title": a.get("title", ""), "mst": mst, "key": key}, "deleg": [], "adm": [], "note": ""}
    isrule = nm.endswith("시행규칙")
    isdec = nm.endswith("시행령")
    if not isrule:
        try:
            r1 = L.deleg_lookup(oc, nm, "시행규칙" if isdec else "시행령", a["n"], a["b"], a.get("title", ""), getter)
        except Exception:  # noqa: BLE001
            r1 = {"hits": []}
        seen = set()
        for h in r1["hits"]:
            if str(h["mst"]) == str(mst) or (h["mst"], h.get("key")) in seen:
                continue
            seen.add((h["mst"], h.get("key")))
            out["deleg"].append(_hit(h))
            if len(out["deleg"]) >= 12:
                break
        if not isdec:                                                    # 법 -> 시행령 조문 밑에 다시 시행규칙
            for node in [x for x in out["deleg"] if x["lawname"].endswith("시행령")][:4]:
                node["kids"] = {"deleg": [], "adm": []}
                try:
                    r2 = L.deleg_lookup(oc, node["lawname"], "시행규칙", int(node["key"].split(":")[0]), int(node["key"].split(":")[1] or 0), node["title"], getter)
                except Exception:  # noqa: BLE001
                    continue
                for h in r2["hits"]:
                    if len(node["kids"]["deleg"]) >= 6:
                        break
                    if h["lawname"].endswith("시행규칙"):
                        node["kids"]["deleg"].append(_hit(h))
    try:
        ra = L.adm_search(oc, a.get("title", ""), nm, a.get("scan", ""), line="", cite=(nm, a["n"], a["b"]), getter=getter)
        out["adm"] = [_hit(h) for h in ra["hits"][:6]]
    except Exception:  # noqa: BLE001
        pass
    if not out["deleg"] and not out["adm"]:
        out["note"] = "이 조문에서 이어지는 하위 법령·지침을 찾지 못했습니다"
    return out


def web_url(kind, name, key=""):
    """법제처에서 열기 주소"""
    n = b = None
    if key:
        n, b = [int(x) for x in key.split(":")]
    if kind == "adm":
        return L.adm_web_url(name)
    if kind == "ordin":
        return L.ordin_web_url(name, n, b or 0)
    q = name + ((" " + L.jo_label(n, b)) if n else "")
    return L.web_url(L.parse_query(q))


def net_export(known):
    """이번 접속에서 새로 받은 법령 본문 응답(일련번호마다 내용이 정해져 있음) -> {키: JSON 글}. 기기에 저장하는 용도"""
    ks = set(known or [])
    return {k: json.dumps(v, ensure_ascii=False) for k, v in list(_NET.items()) if "lawService.do" in k and k not in ks}


def net_preload(items):
    """기기에 저장해 둔 응답을 미리 올려 둔다(해석은 쓸 때)"""
    for k, v in (items or {}).items():
        if k not in _NET:
            _NETS[k] = v
    return len(items or {})


FUNCS = {"law_hist": law_hist, "ver_reason": ver_reason, "net_export": net_export, "net_preload": net_preload, "art_hist": art_hist, "upcoming": upcoming, "tt_ord": tt_ord, "cite_titles": cite_titles, "web_url": web_url, "kw_search": kw_search, "open_cached": open_cached, "list_deleg": list_deleg, "list_adm": list_adm, "find_adm": find_adm, "rev_check": rev_check, "compare_ver": compare_ver, "chain": chain, "list_ordin": list_ordin,"search": search, "suggest": suggest, "open_doc": open_doc, "open_by_name": open_by_name, "open_cite": open_cite,
         "tt_open": tt_open, "fav_rel": fav_rel, "tt_row": tt_row, "set_api_base": set_api_base}


def call(fn, args_json):
    try:
        res = FUNCS[fn](*json.loads(args_json))
        return json.dumps({"ok": res}, ensure_ascii=False)
    except L.LawError as e:
        return json.dumps({"err": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        import traceback
        return json.dumps({"err": "프로그램 내부 오류(%s: %s)" % (type(e).__name__, e), "tb": traceback.format_exc()[-1500:]}, ensure_ascii=False)
