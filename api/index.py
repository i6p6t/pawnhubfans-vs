import base64, hashlib, hmac, html, ipaddress, json, os, re, socket, time
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)
SECRET = os.getenv("SECRET_KEY", "change-me-in-vercel-env").encode()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
PH, DESI, HS, KJ, RGB = ("https://www.pornhub.com/", "https://desidekho.wiki/", "https://hstream.moe/",
                         "https://vjav.com/", "https://www.redgifs.com/")
DESI_HOSTS = tuple(h.strip().lower() for h in
                   os.getenv("DESI_MEDIA_HOSTS", "pvtcdn.com,desidekho.wiki,fileview.cfd").split(",") if h.strip())
MEDIA_RE = re.compile(r"""https?://[^"'\s<>\\]+?\.(?:mp4|m3u8)(?![A-Za-z0-9])(?:\?[^"'\s<>\\]*)?""", re.I)
DUR_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?![\d:])")


# ---------------------------------------------------------------- helpers
def public_host(host):
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return False
    return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)


def safe(u):
    p = urlparse(u or "")
    return p.scheme == "https" and bool(p.hostname) and public_host(p.hostname)


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, newurl) if safe(newurl) else None


OPENER = build_opener(SafeRedirect)


def get(url, ref=None, limit=4 << 20, headers=None, timeout=15):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/json,*/*;q=0.8"}
    if ref:
        h["Referer"] = ref
    if "pornhub.com" in url:
        h["Cookie"] = "accessAgeDisclaimerPH=1; age_verified=1"
    h.update(headers or {})
    with OPENER.open(Request(url, headers=h), timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "ignore")


def sig(s):
    return hmac.new(SECRET, s.encode(), hashlib.sha256).hexdigest()[:24]


def purl(u, ref=""):
    if not u or not u.startswith("https://"):
        return ""
    return "/api/p?" + urlencode({"u": u, "r": ref, "s": sig(u + "|" + ref)})


def attr(tag, name):
    m = re.search(r'[\s"\']' + name + r'=["\']([^"\']*)["\']', tag, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def norm(u):
    u = (u or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("http://"):
        u = "https://" + u[7:]
    return u if u.startswith("https://") else ""


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fmt_dur(sec):
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pretty(slug):
    return re.sub(r"[-_.]+", " ", slug).strip().title()


def item(key, title, thumb, ref, dur="", views=""):
    return {"key": key, "title": (title or "Untitled")[:200], "thumb": purl(thumb, ref), "dur": dur, "views": views}


def probe(url, ref):
    try:
        req = Request(url, headers={"User-Agent": UA, "Referer": ref, "Range": "bytes=0-1"})
        with OPENER.open(req, timeout=10) as r:
            return r.status in (200, 206) and "text/html" not in (r.headers.get("Content-Type") or "").lower()
    except Exception:
        return False


class Cards(HTMLParser):
    """Generic listing parser: every <a href=video> that contains an <img> becomes a card."""

    def __init__(self, slugfn):
        super().__init__(convert_charrefs=True)
        self.f, self.items, self.cur = slugfn, {}, None

    def handle_starttag(self, tag, a):
        a = dict((k, v or "") for k, v in a)
        if tag == "a":
            sl = self.f(a.get("href", ""))
            self.cur = self.items.setdefault(sl, {"title": "", "thumb": "", "dur": "", "views": ""}) if sl else None
            if self.cur is not None and a.get("title") and not self.cur["title"]:
                self.cur["title"] = a["title"].strip()
        elif tag == "img" and self.cur is not None:
            for k in ("data-src", "data-lazy-src", "data-original", "data-thumb", "src"):
                u = norm(urljoin(self.base, a.get(k, ""))) if a.get(k) and not a[k].startswith("data:") else ""
                if u and not self.cur["thumb"]:
                    self.cur["thumb"] = u
            if a.get("alt") and not self.cur["title"]:
                self.cur["title"] = a["alt"].strip()

    def handle_endtag(self, tag):
        if tag == "a":
            self.cur = None

    def handle_data(self, d):
        if self.cur is None:
            return
        d = d.strip()
        if DUR_RE.fullmatch(d):
            self.cur["dur"] = d
        elif len(d) > 6 and not self.cur["title"]:
            self.cur["title"] = d


def parse_cards(body, slugfn, base):
    p = Cards(slugfn)
    p.base = base
    try:
        p.feed(body)
    except Exception:
        pass
    return [(sl, v) for sl, v in p.items.items() if v["thumb"]]


# ---------------------------------------------------------------- Pornhub
def ph_list(q, o, pg):
    qs = {}
    if q:
        qs["search"] = q
    if o in ("ht", "cm", "mv", "tr"):
        qs["o"] = o
    if pg > 1:
        qs["page"] = pg
    body = get(PH + ("video/search" if q else "video") + ("?" + urlencode(qs) if qs else ""), ref=PH)
    hits = list(re.finditer(r'data-video-vkey=["\']([A-Za-z0-9_-]{1,100})["\']', body))
    out, seen = [], set()
    for i, m in enumerate(hits):
        key = m.group(1)
        if key in seen:
            continue
        stop = hits[i + 1].start() if i + 1 < len(hits) else len(body)
        chunk = body[m.end():min(stop, m.end() + 14000)]
        title = ""
        for tag in re.findall(r"<a\b[^>]*>", chunk, re.I):
            if "viewkey=" in tag and attr(tag, "title"):
                title = attr(tag, "title")
                break
        thumb = ""
        for tag in re.findall(r"<img\b[^>]*>", chunk, re.I):
            blob = attr(tag, "class") + " " + attr(tag, "src")
            if re.search(r"avatar|usernames|/channels/|/pornstars/|/model", blob, re.I):
                continue
            for a in ("data-mediumthumb", "data-thumb_url", "data-src", "src"):
                if norm(attr(tag, a)):
                    thumb = norm(attr(tag, a))
                    break
            if thumb:
                title = title or attr(tag, "alt")
                break
        if not title:
            continue
        d = re.search(r'class=["\']duration["\'][^>]*>\s*([0-9]{1,2}(?::[0-9]{2}){1,2})', chunk)
        v = re.search(r'class=["\']views["\'][^>]*>(.*?)</', chunk, re.S | re.I)
        vv = re.search(r"([0-9][0-9.,]*\s*[KMBkmb]?)", clean(v.group(1))) if v else None
        seen.add(key)
        out.append(item("ph_" + key, title, thumb, PH, d.group(1) if d else "", vv.group(1).replace(" ", "") if vv else ""))
    return out


def ph_watch(key):
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20,
            "http_headers": {"User-Agent": UA}}
    with YoutubeDL(opts) as y:
        info = y.extract_info(PH + "view_video.php?viewkey=" + key, download=False)
    best, bs = None, None
    for f in info.get("formats") or []:
        u = f.get("url") or ""
        if not u.startswith("https://") or f.get("vcodec") == "none":
            continue
        proto = str(f.get("protocol") or "")
        prog = proto in ("https", "http") and f.get("acodec") != "none"
        hls = proto.startswith("m3u8")
        if not (prog or hls):
            continue
        h = int(f.get("height") or 0)
        fit = 1 if 0 < h <= 720 else 0
        score = (fit, 1 if prog else 0, h if fit else -h)
        if bs is None or score > bs:
            best, bs = f, score
    if not best:
        raise ValueError("no stream")
    t = "mp4" if str(best.get("protocol")) in ("https", "http") else "hls"
    u = best["url"]
    return [{"u": u, "t": t}, {"u": purl(u, PH), "t": t}]


# ---------------------------------------------------------------- Desidekho
DESI_SLUG = re.compile(r"[A-Za-z0-9_-]{3,190}")
DESI_RES = {"latest", "categories", "category", "tags", "tag", "exclusive", "page", "search", "about", "contact",
            "dmca", "privacy", "privacy-policy", "terms", "feed", "author", "login", "register", "sitemap", "random"}


def desi_slug(href):
    u = urlparse((href or "").strip())
    if u.hostname and not u.hostname.lower().endswith("desidekho.wiki"):
        return ""
    parts = [x for x in u.path.split("/") if x]
    if len(parts) != 1 or not DESI_SLUG.fullmatch(parts[0]) or parts[0].lower() in DESI_RES:
        return ""
    return parts[0]


def desi_list(q, pg):
    try:
        p = {"per_page": 24, "page": pg, "_embed": "wp:featuredmedia", "_fields": "slug,title,_links,_embedded"}
        if q:
            p["search"] = q
        data = json.loads(get(DESI + "wp-json/wp/v2/posts?" + urlencode(p), ref=DESI))
        out = []
        for x in data:
            slug = str(x.get("slug", ""))
            if not DESI_SLUG.fullmatch(slug):
                continue
            media = ((x.get("_embedded") or {}).get("wp:featuredmedia") or [{}])[0]
            th = norm(str(media.get("source_url", ""))) if isinstance(media, dict) else ""
            out.append(item("desi_" + slug, clean((x.get("title") or {}).get("rendered", "")) or pretty(slug), th, DESI))
        return out
    except HTTPError as e:
        if e.code == 400 and pg > 1:
            return []
    except Exception:
        pass
    url = DESI + ("page/%d/" % pg if pg > 1 else "") + ("?" + urlencode({"s": q}) if q else "")
    return [item("desi_" + s, v["title"] or pretty(s), v["thumb"], DESI, v["dur"])
            for s, v in parse_cards(get(url, ref=DESI), desi_slug, DESI)]


def desi_ok(u):
    h = (urlparse(u).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in DESI_HOSTS)


def desi_watch(slug):
    url = DESI + slug + "/"
    body = get(url, ref=DESI, limit=3 << 20)
    flat = html.unescape(body.replace("\\/", "/"))
    cands = []
    for tag in re.findall(r"<meta\b[^>]*>", flat[:300000], re.I):
        if (attr(tag, "property") or attr(tag, "name")).lower() in ("og:video", "og:video:url", "og:video:secure_url"):
            cands.append(attr(tag, "content"))
    cands += [m.group(0) for m in MEDIA_RE.finditer(flat)]
    cands += [urljoin(url, m) for m in re.findall(r"""(?:src|href|data-src)=["']((?:/|\./)[^"']*?\.(?:mp4|m3u8)[^"']*)["']""", flat)]
    media = [c for c in dict.fromkeys(norm(c) for c in cands) if c and desi_ok(c)]
    if not media:
        raise ValueError("no media")
    media.sort(key=lambda c: 0 if ("_video" in c or "/videos/" in c) else 1)
    u = media[0]
    return [{"u": purl(u, DESI), "t": "hls" if ".m3u8" in u.lower() else "mp4"}]


# ---------------------------------------------------------------- VJAV
KJL = 86400
CYR = {"\u0410": "A", "\u0412": "B", "\u0421": "C", "\u0415": "E", "\u041c": "M"}


def kj_dur(t):
    parts = [int(x) for x in re.findall(r"\d+", str(t or ""))][:3]
    s = 0
    for x in parts:
        s = s * 60 + x
    return fmt_dur(s)


def kj_list(q, pg):
    path = (f"api/videos2.php?params={KJL}/str/relevance/48/search..{pg}.all..&s={quote(q, safe='')}" if q
            else f"api/json/videos2/{KJL}/str/latest-updates/48/..{pg}.all...json")
    try:
        data = json.loads(get(KJ + path, ref=KJ))
    except HTTPError as e:
        if e.code == 404 and pg > 1:
            return []
        raise
    out = []
    for v in data.get("videos") or []:
        vid = str(v.get("video_id") or "")
        if vid.isdigit():
            out.append(item("kj_" + vid, re.sub(r"\s+", " ", str(v.get("title") or "")).strip() or "Video " + vid,
                            norm(str(v.get("scr") or "")), KJ, kj_dur(v.get("duration")), str(v.get("video_viewed") or "")))
    return out


def kj_decode(raw):
    t = "".join(CYR.get(c, c) for c in str(raw))
    t = re.sub(r"[^A-Za-z0-9.,~]", "", t).replace(".", "+").replace(",", "/").replace("~", "=")
    t += "=" * (-len(t) % 4)
    return base64.b64decode(t).decode("utf-8", "ignore")


def kj_watch(vid):
    files = json.loads(get(KJ + f"api/videofile.php?lifetime=8640000&video_id={vid}", ref=KJ))
    cands = []
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or not f.get("video_url"):
            continue
        try:
            path = kj_decode(f["video_url"]).strip()
        except Exception:
            continue
        full = norm(path if path.startswith("http") else KJ.rstrip("/") + (path if path.startswith("/") else "/" + path))
        fmt = str(f.get("format") or "")
        if full:
            cands.append((0 if ("hq" in fmt or "hd" in fmt) else (2 if "tr" in fmt else 1), 0 if f.get("is_default") else 1, full))
    cands.sort()
    for _, _, u in cands[:4]:
        if probe(u, KJ):
            return [{"u": purl(u, KJ), "t": "mp4"}]
    raise ValueError("no media")


# ---------------------------------------------------------------- HStream
def hs_slug(href):
    u = urlparse((href or "").strip())
    if u.hostname and not u.hostname.lower().endswith("hstream.moe"):
        return ""
    m = re.fullmatch(r"/hentai/([a-z0-9-]{3,190})/?", u.path)
    return m.group(1) if m and not m.group(1).isdigit() else ""


def hs_list(q, pg):
    bases = [{"search": q}] if q else [{"order": "recently-uploaded"}, {}]
    for b in bases:
        qs = dict(b)
        if pg > 1:
            qs["page"] = pg
        try:
            cards = parse_cards(get(HS + "search" + ("?" + urlencode(qs) if qs else ""), ref=HS), hs_slug, HS)
        except Exception:
            continue
        if cards:
            return [item("hs_" + s, v["title"] or pretty(s), v["thumb"], HS, v["dur"]) for s, v in cards]
    return []


def hs_watch(slug):
    from yt_dlp import YoutubeDL
    url = HS + "hentai/" + slug
    mp4 = []
    try:
        flat = html.unescape(get(url, ref=HS, limit=900000).replace("\\/", "/"))
        mp4 += [m.group(0) for m in MEDIA_RE.finditer(flat) if urlparse(m.group(0)).path.lower().endswith(".mp4")]
    except Exception:
        pass
    manifest = ""
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                        "socket_timeout": 20, "http_headers": {"User-Agent": UA}}) as y:
            info = y.extract_info(url, download=False)
        for f in info.get("formats") or []:
            u = f.get("url") or ""
            if u and urlparse(u).path.lower().endswith(".mp4") and not f.get("fragments"):
                mp4.append(u)
            if f.get("manifest_url") and not manifest and (f.get("height") or 0) <= 1080:
                manifest = f["manifest_url"]
    except Exception:
        pass
    if manifest:
        base = manifest.rsplit("/", 2)[0] + "/"
        for q in (720, 1080):
            mp4 += [f"{base}x264.{q}p.mp4", f"{base}x264.{q}.mp4"]
    for u in sorted(dict.fromkeys(norm(x) for x in mp4 if x), key=lambda c: (0 if "720" in c else 1, 0 if "x264" in c else 1))[:6]:
        if probe(u, HS):
            return [{"u": purl(u, HS), "t": "mp4"}]
    raise ValueError("no mp4")


# ---------------------------------------------------------------- RedGifs
RG_API = "https://api.redgifs.com/v2/"
RG_ORD = {"": "trending", "new": "latest", "top": "top"}
RG_BLOCK = frozenset("gay gays femboy femboys femboi trap traps sissy sissies twink twinks trans transgender "
                     "transsexual transwoman transgirl tgirl tgirls shemale shemales ladyboy ladyboys crossdresser "
                     "crossdressers crossdressing gaysex gayporn".split())
RG_H = {"User-Agent": UA, "Referer": RGB, "Origin": "https://www.redgifs.com", "Accept": "application/json"}
RGT = {"v": "", "ts": 0.0}


def rg_ok(u):
    h = (urlparse(u or "").hostname or "").lower()
    return u.startswith("https://") and (h == "redgifs.com" or h.endswith(".redgifs.com"))


def rg_call(ep, params=None):
    url = RG_API + ep + ("?" + urlencode(params) if params else "")
    for attempt in (0, 1):
        if attempt or not RGT["v"] or time.time() - RGT["ts"] > 3000:
            with OPENER.open(Request(RG_API + "auth/temporary", headers=RG_H), timeout=12) as r:
                RGT.update(v=str(json.loads(r.read(65536)).get("token", "")), ts=time.time())
        try:
            with OPENER.open(Request(url, headers={**RG_H, "Authorization": "Bearer " + RGT["v"]}), timeout=15) as r:
                return json.loads(r.read(4 << 20))
        except HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                continue
            raise


def rg_blocked(g):
    words = set()
    for p in [str(t) for t in g.get("tags") or []] + [str(n) for n in g.get("niches") or []] + \
             [str(g.get("userName") or ""), str(g.get("description") or "")]:
        words.update(re.findall(r"[a-z0-9]+", p.lower()))
    return bool(words & RG_BLOCK)


def rg_list(q, o, pg):
    if set(re.findall(r"[a-z0-9]+", q.lower())) & RG_BLOCK:
        return []
    p = {"order": RG_ORD.get(o, "trending"), "count": 40, "page": pg, "type": "g"}
    if q:
        p["search_text"] = q
    data = rg_call("gifs/search", p)
    out = []
    for g in data.get("gifs") or []:
        if not isinstance(g, dict) or rg_blocked(g):
            continue
        slug = str(g.get("id") or "").lower()
        urls = g.get("urls") or {}
        poster = urls.get("poster") or urls.get("thumbnail") or ""
        if not re.fullmatch(r"[a-z0-9]{3,60}", slug) or not rg_ok(urls.get("sd") or urls.get("hd") or ""):
            continue
        tags = [str(t) for t in g.get("tags") or [] if isinstance(t, str)]
        out.append(item("rg_" + slug, (" · ".join(tags[:3]) or "RedGifs"), poster if rg_ok(poster) else "", RGB,
                        fmt_dur(g.get("duration")), "@" + str(g.get("userName") or "")[:40]))
    return out


def rg_watch(slug):
    gif = (rg_call("gifs/" + slug) or {}).get("gif") or {}
    urls = gif.get("urls") or {}
    if rg_blocked(gif):
        raise ValueError("blocked")
    out = [{"u": purl(urls[k], RGB), "t": "mp4"} for k in ("hd", "sd") if rg_ok(urls.get(k) or "")]
    if not out:
        raise ValueError("no media")
    return out


# ---------------------------------------------------------------- routes
@app.get("/api/catalog")
def catalog():
    s = request.args.get("s", "")
    q = re.sub(r"\s+", " ", request.args.get("q", "")).strip()[:100]
    o = request.args.get("o", "")
    try:
        pg = max(1, min(50, int(request.args.get("p", "1"))))
    except ValueError:
        pg = 1
    fn = {"ph": lambda: ph_list(q, o, pg), "desi": lambda: desi_list(q, pg), "kissjav": lambda: kj_list(q, pg),
          "hstream": lambda: hs_list(q, pg), "redgifs": lambda: rg_list(q, o, pg)}.get(s)
    if not fn:
        return jsonify(error="Unknown source."), 404
    try:
        items = fn()
    except Exception:
        return jsonify(error="This source is temporarily unavailable. Try again in a few minutes."), 502
    resp = jsonify(items=items, more=bool(items))
    resp.headers["Cache-Control"] = "public, s-maxage=300, stale-while-revalidate=600"
    return resp


@app.get("/api/watch")
def watch():
    k = request.args.get("k", "")
    m = re.fullmatch(r"(ph|desi|kj|hs|rg)_([A-Za-z0-9_-]{1,190})", k)
    if not m:
        return jsonify(error="Bad video id."), 400
    kind, ident = m.groups()
    try:
        srcs = {"ph": ph_watch, "desi": desi_watch, "kj": kj_watch, "hs": hs_watch, "rg": rg_watch}[kind](ident)
    except Exception:
        return jsonify(error="Couldn't load this video right now."), 502
    resp = jsonify(sources=srcs)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def rewrite(text, base, ref):
    def fix(u):
        return purl(urljoin(base, u), ref) or u
    out = []
    for line in text.splitlines():
        if line.startswith("#"):
            line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="%s"' % fix(m.group(1)), line)
        elif line.strip():
            line = fix(line.strip())
        out.append(line)
    return "\n".join(out)


@app.get("/api/p")
def proxy():
    u, r = request.args.get("u", ""), request.args.get("r", "")
    if not hmac.compare_digest(sig(u + "|" + r), request.args.get("s", "")) or not safe(u):
        return "", 403
    h = {"User-Agent": UA, "Accept": "*/*"}
    if r:
        h["Referer"] = r
    rng = request.headers.get("Range", "")
    if re.fullmatch(r"bytes=\d*-\d*", rng):
        h["Range"] = rng
    try:
        up = OPENER.open(Request(u, headers=h), timeout=20)
    except HTTPError as e:
        resp = Response(status=416 if e.code == 416 else 502)
        if e.code == 416 and e.headers.get("Content-Range"):
            resp.headers["Content-Range"] = e.headers["Content-Range"]
        return resp
    except Exception:
        return "", 502
    ctype = up.headers.get("Content-Type", "")
    if ".m3u8" in urlparse(u).path.lower() or "mpegurl" in ctype.lower():
        text = up.read(2 << 20).decode("utf-8", "ignore")
        up.close()
        return Response(rewrite(text, u, r), mimetype="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    def gen():
        try:
            while True:
                c = up.read(65536)
                if not c:
                    break
                yield c
        except Exception:
            pass
        finally:
            up.close()

    resp = Response(stream_with_context(gen()), status=up.status if up.status in (200, 206) else 200,
                    content_type=ctype or "application/octet-stream")
    for hd in ("Content-Length", "Content-Range"):
        if up.headers.get(hd):
            resp.headers[hd] = up.headers[hd]
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = ("public, max-age=86400, s-maxage=86400" if ctype.startswith("image/")
                                     else "private, max-age=600")
    return resp
