#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cập nhật Bản tin Chứng khoán LPS — chạy hoàn toàn bằng script, không dùng AI.

Việc script làm mỗi lần chạy:
  1. Công bố thông tin: đọc mục https://www.lpbs.com.vn/cong-bo-thong-tin-cat19 (tất cả các trang),
     lấy NGUYÊN VĂN tiêu đề, ngày đăng, nội dung và file đính kèm của từng văn bản.
  2. Tin tức LPS: đọc mục "Tin tức LPS" trên lpbs.com.vn (nguyên văn).
  3. Báo chí: đọc RSS của các báo tài chính, giữ bài có nhắc tới LPS / LPBS / Chứng khoán LPBank;
     lấy nguyên văn tiêu đề + đoạn mô tả (sapo) do chính tòa soạn đưa vào RSS hoặc thẻ meta.
     Không sao chép thân bài của báo; người đọc bấm link để đọc toàn văn trên trang gốc.
  4. Ghi cơ sở dữ liệu lps_db.json và dựng lại file ban-tin-lps.html (một file duy nhất, mở được offline).

Cách chạy:
  python update_lps.py            # cập nhật phần mới (nhanh)
  python update_lps.py --full     # quét lại toàn bộ mục Công bố thông tin
  python update_lps.py --render   # chỉ dựng lại HTML từ lps_db.json, không truy cập mạng

Yêu cầu: Python 3.9+, gói requests và beautifulsoup4 (pip install requests beautifulsoup4).
"""
import argparse
import base64
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Thiếu thư viện. Chạy: pip install requests beautifulsoup4")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lps_rules import classify, late_check, nfc   # quy tắc phân loại và sàng lọc (sửa trong lps_rules.py)
import lps_audit                                  # phát hiện thay đổi, mã băm, số hiệu nghị quyết, lịch định kỳ, gộp tin

# ----------------------------------------------------------------- CẤU HÌNH
BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "lps_db.json"
TEMPLATE_FILE = BASE_DIR / "template.html"
OUTPUT_HTML = BASE_DIR / "ban-tin-lps.html"      # đổi sang thư mục dùng chung nếu muốn, vd. r"\\server\share\ban-tin-lps.html"
LOGO_CACHE = BASE_DIR / "logo_lps.png"
LOG_FILE = BASE_DIR / "update_lps.log"
SEED_FILE = BASE_DIR / "news.json"               # dữ liệu cũ (chỉ dùng ở lần chạy đầu tiên)

LPBS = "https://www.lpbs.com.vn"
# Các mục văn bản của nhà đầu tư trên lpbs.com.vn: (mã, tên hiển thị, trang đầu, mẫu trang tiếp theo)
# Trang 2 = n=1, trang 3 = n=2 … Báo cáo tài chính KHÔNG nằm trong mục Công bố thông tin mà ở mục Thông tin tài chính.
IR_SECTIONS = [
    ("cbtt", "Công bố thông tin", LPBS + "/cong-bo-thong-tin-cat19", LPBS + "/news/19/{n}/cong-bo-thong-tin.html"),
    ("ttc", "Thông tin tài chính", LPBS + "/thong-tin-tai-chinh-cat17", LPBS + "/news/17/{n}/thong-tin-tai-chinh.html"),
    ("bctn", "Báo cáo thường niên", LPBS + "/bao-cao-thuong-nien-cat18", LPBS + "/news/18/{n}/bao-cao-thuong-nien.html"),
    ("dieule", "Điều lệ và quy chế", LPBS + "/dieu-le-va-quy-che-cong-ty-cat12", LPBS + "/news/12/{n}/dieu-le-va-quy-che-cong-ty.html"),
]
LPS_NEWS_FIRST = LPBS + "/tin-tuc-lps-news24"
LPS_NEWS_PAGE = LPBS + "/news/24/{n}/tin-tuc-lps.html"
LOGO_URL = LPBS + "/images/logo.png"

RSS_FEEDS = [
    ("cafef.vn", "https://cafef.vn/thi-truong-chung-khoan.rss"),
    ("cafef.vn", "https://cafef.vn/doanh-nghiep.rss"),
    ("cafef.vn", "https://cafef.vn/tai-chinh-ngan-hang.rss"),
    ("vietstock.vn", "https://vietstock.vn/830/chung-khoan/co-phieu.rss"),
    ("vietstock.vn", "https://vietstock.vn/737/doanh-nghiep/hoat-dong-kinh-doanh.rss"),
    ("vietstock.vn", "https://vietstock.vn/739/chung-khoan/giao-dich-noi-bo.rss"),
    ("vneconomy.vn", "https://vneconomy.vn/chung-khoan.rss"),
    ("tinnhanhchungkhoan.vn", "https://www.tinnhanhchungkhoan.vn/rss/chung-khoan-1.rss"),
]

# Google News RSS giúp bắt tin từ nhiều báo hơn. Lưu ý điều khoản của Google: feed này dành cho
# "personal, non-commercial use". Đặt False nếu bộ phận pháp chế/IT không chấp thuận.
USE_GOOGLE_NEWS = True
GOOGLE_NEWS_QUERIES = ['"Chứng khoán LPS"', '"cổ phiếu LPS"', '"LPBS"', '"Chứng khoán LPBank"']

# Bài báo được giữ nếu tiêu đề/sapo khớp biểu thức này (phân biệt hoa thường với "LPS"/"LPBS")
KEYWORD_RE = re.compile(r"\bLPS\b|\bLPBS\b|Chứng khoán LPBank|Chứng khoán LPS|LPBank Securities|LPS Securities")
# Loại tin không liên quan: LPS của Indonesia, tin bóng đá giải "Cúp Quốc gia Chứng khoán LPBank"
EXCLUDE_RE = re.compile(r"Lembaga Penjamin Simpanan|Cúp Quốc gia|V-League|tứ kết|bán kết|chung kết", re.I)

KNOWN_SOURCES = {
    "lpbs.com.vn": "LPS – lpbs.com.vn", "cafef.vn": "CafeF", "vietstock.vn": "Vietstock",
    "vneconomy.vn": "VnEconomy", "tinnhanhchungkhoan.vn": "Đầu tư Chứng khoán",
    "mekongasean.vn": "Mekong ASEAN", "doanhnhan.baophapluat.vn": "Doanh nhân Pháp luật",
    "doanhnghiepkinhtexanh.vn": "Doanh nghiệp & Kinh tế xanh", "etime.danviet.vn": "Dân Việt",
    "danviet.vn": "Dân Việt", "nguoiduatin.vn": "Người Đưa Tin", "vietbao.vn": "Việt Báo",
    "viettimes.vn": "VietTimes", "thitruongtaichinhtiente.vn": "Thị trường Tài chính Tiền tệ",
    "tapchikinhtetaichinh.vn": "Tạp chí Kinh tế Tài chính", "vietnambiz.vn": "VietnamBiz",
    "vietnamfinance.vn": "VietnamFinance", "nguoiquansat.vn": "Người Quan Sát",
}

# Đối chiếu với HOSE: API công khai mà website hsx.vn dùng (không có tài liệu chính thức, có thể đổi).
USE_HOSE = True
HOSE_API = "https://api.hsx.vn/n/api/v1/1"
HOSE_SEC_ID = 8399            # mã nội bộ của LPS trên HOSE (tra từ api.hsx.vn/l/api/v1/1/securities/stock)
HOSE_START = dt.date(2026, 1, 1)   # LPS bắt đầu có tin trên HOSE từ tháng 7/2026
HOSE_NEWS_TYPES = {1: "Tin tổ chức niêm yết", 3: "Tin của Sở về LPS"}
HOSE_MATCH_DAYS = 5           # văn bản trên web và trên HOSE được coi là cùng một văn bản nếu đăng cách nhau ≤ 5 ngày

# Bên liên quan theo dõi trong tab riêng. hose_code: mã trên HOSE (lấy tin công bố thông tin);
# queries: truy vấn Google News; kw: biểu thức bắt buộc có trong tiêu đề để giữ bài.
RELATED_PARTIES = [
    {"key": "LPB", "name": "LPBank (LPB)", "hose_code": "LPB",
     "queries": ['"LPBank"', '"Ngân hàng Lộc Phát"'], "kw": r"LPBank|Lộc Phát|\bLPB\b"},
    {"key": "STB", "name": "Sacombank (STB)", "hose_code": "STB",
     "queries": ['"Sacombank"'], "kw": r"Sacombank|\bSTB\b"},
    {"key": "SCEX", "name": "SCEX – Sàn GD tài sản mã hóa Sacom", "hose_code": None,
     "queries": ['"SCEX"', '"tài sản mã hóa Sacom"'], "kw": r"\bSCEX\b|mã hóa Sacom"},
    {"key": "SACOMLIFE", "name": "Sacom Life", "hose_code": None,
     "queries": ['"Sacom Life"', '"Nhân thọ Sacom"'], "kw": r"Sacom\s*Life|Nhân thọ Sacom"},
]
RELATED_DAYS = 120          # giữ tin bên liên quan trong 120 ngày gần nhất
RELATED_MAX_PER_PARTY = 250
MAX_RELATED_DECODE = 60

# Giải mã link Google News sang link gốc của báo (số lượng tối đa mỗi lần chạy)
MAX_GOOGLE_DECODE = 80

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BanTinLPS/1.0",
           "Accept-Language": "vi-VN,vi;q=0.9"}
TIMEOUT = 25
DELAY = 0.4                 # nghỉ giữa các lượt tải để không gây tải cho máy chủ
MAX_META_FETCH = 40         # số bài báo tối đa được lấy sapo từ thẻ meta mỗi lần chạy
VN_TZ = dt.timezone(dt.timedelta(hours=7))

session = requests.Session()
session.headers.update(HEADERS)
LOG = []


def log(msg):
    line = f"[{dt.datetime.now(VN_TZ):%Y-%m-%d %H:%M:%S}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def get(url, **kw):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = "utf-8"
            time.sleep(DELAY)
            return r
        except requests.RequestException as e:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def norm_title(s):
    s = re.sub(r"\s+", " ", s or "").strip().lower()
    return re.sub(r"[^\w]+", "", s)


def domain_of(url):
    h = urlparse(url).hostname or ""
    return h[4:] if h.startswith("www.") else h


def make_id(prefix, url):
    return prefix + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def vn_date(ddmmyyyy):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", ddmmyyyy or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


# ------------------------------------------------------------- TRÍCH NỘI DUNG lpbs.com.vn
BLOCK_TAGS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}


def node_text(node):
    out = []
    for c in node.children:
        name = getattr(c, "name", None)
        if name is None:
            out.append(str(c))
        elif name == "br":
            out.append("\n")
        elif name in ("script", "style"):
            continue
        else:
            blk = name in BLOCK_TAGS
            if blk:
                out.append("\n")
            out.append(node_text(c))
            if blk:
                out.append("\n")
    return "".join(out)


def clean_text(s):
    s = s.replace("\u00a0", " ").replace("\r", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_lpbs_list(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for art in soup.select(".row article.pbmit-blog-style-3"):
        a = art.select_one("h3 a[href]")
        meta = art.select_one(".pbmit-meta-line")
        if not a:
            continue
        items.append({
            "title": re.sub(r"\s+", " ", a.get_text()).strip(),
            "url": urljoin(LPBS, a["href"]),
            "date": vn_date(meta.get_text() if meta else ""),
        })
    return items


def parse_lpbs_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one(".content-j")
    if not box:
        return "", []
    files = []
    for a in box.select("a[href]"):
        href = a["href"].strip()
        if not href or href.lower().startswith(("javascript", "mailto", "#")) or "cdn-cgi" in href or "email-protection" in href:
            continue
        files.append({"label": re.sub(r"\s+", " ", a.get_text()).strip() or "Tài liệu",
                      "url": urljoin(LPBS, href)})
    return clean_text(node_text(box)), files


def crawl_lpbs_section(first_url, page_tpl, known_urls, full, label):
    """Đọc các trang danh sách. Chế độ thường: dừng khi gặp trang không có mục mới.
    Chế độ full: đọc hết mọi trang. Trả về (mục mới, toàn bộ mục đã thấy, đọc trọn vẹn hay không)."""
    found, n, seen, all_items, complete = [], 0, set(), [], True
    while True:
        url = first_url if n == 0 else page_tpl.format(n=n)
        try:
            items = parse_lpbs_list(get(url).text)
        except Exception as e:
            log(f"{label}: lỗi tải {url}: {e}")
            complete = False
            break
        items = [i for i in items if i["url"] not in seen]
        if not items:
            break
        seen.update(i["url"] for i in items)
        all_items.extend(items)
        new = [i for i in items if i["url"] not in known_urls]
        found.extend(new)
        if not new and not full:
            complete = False      # không đọc hết danh sách → không dùng để kết luận văn bản bị gỡ
            break
        n += 1
        if n > 300:
            break
    log(f"{label}: quét {n + 1} trang, {len(found)} mục mới")
    return found, all_items, complete


def update_lpbs(db, full):
    """Đọc 4 mục văn bản trên lpbs.com.vn. Trả về (số văn bản mới, số tin LPS mới, {url: (mục, tiêu đề, ngày)} nếu đọc trọn vẹn)."""
    known = {x["url"] for x in db["cbtt"]}
    total_new, listing, listing_ok = 0, {}, True
    for key, label, first, tpl in IR_SECTIONS:
        sec_full = full or not db["meta"].get(f"section_done_{key}")
        new_items, all_items, complete = crawl_lpbs_section(first, tpl, known, sec_full, label)
        if sec_full and complete:
            db["meta"][f"section_done_{key}"] = True
        listing_ok = listing_ok and complete
        for it in all_items:
            listing.setdefault(it["url"], (key, it["title"], it["date"]))
        for it in new_items:
            try:
                body, files = parse_lpbs_detail(get(it["url"]).text)
            except Exception as e:
                log(f"  Không đọc được chi tiết {it['url']}: {e}")
                body, files = "", []
            db["cbtt"].append({"id": make_id("cbtt", it["url"]), "title": it["title"], "url": it["url"],
                               "date": it["date"], "body": body, "files": files, "section": key,
                               "first_seen": dt.datetime.now(VN_TZ).isoformat(timespec="minutes")})
            known.add(it["url"])
            total_new += 1
            if total_new % 50 == 0:
                save_db(db)
                log(f"  … đã lưu {len(db['cbtt'])} văn bản")
    for x in db["cbtt"]:
        x.setdefault("section", "cbtt")
        if not x.get("body") and not x.get("files"):
            try:
                x["body"], x["files"] = parse_lpbs_detail(get(x["url"]).text)
            except Exception:
                pass

    # Tin tức LPS (đăng trên lpbs.com.vn)
    known_news = {x["url"] for x in db["news"]}
    new_news, _, _ = crawl_lpbs_section(LPS_NEWS_FIRST, LPS_NEWS_PAGE, known_news, full, "Tin tức LPS")
    for it in new_news:
        try:
            body, files = parse_lpbs_detail(get(it["url"]).text)
        except Exception:
            body, files = "", []
        db["news"].append({"id": make_id("lps", it["url"]), "title": it["title"], "url": it["url"],
                           "source": "lpbs.com.vn", "published": it["date"], "date_precision": "day",
                           "excerpt": first_paragraph(body), "excerpt_origin": "lpbs",
                           "body": body, "files": files, "via": "lpbs"})
    return total_new, len(new_news), (listing if (full and listing_ok) else None)


def first_paragraph(body, limit=320):
    for p in (body or "").split("\n"):
        p = p.strip()
        if len(p) > 40:
            return p if len(p) <= limit else p[:limit].rsplit(" ", 1)[0] + "…"
    return ""


# ------------------------------------------------------------------- BÁO CHÍ
def parse_pubdate(s):
    if not s:
        return "", "day"
    try:
        d = email.utils.parsedate_to_datetime(s.strip())
        if d.tzinfo is None:
            d = d.replace(tzinfo=VN_TZ)
        return d.astimezone(VN_TZ).isoformat(timespec="minutes"), "minute"
    except Exception:
        return "", "day"


def strip_html(s):
    if not s:
        return ""
    return clean_text(BeautifulSoup(s, "html.parser").get_text(" "))


def rss_items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        src = it.find("source")
        yield {
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "desc": it.findtext("description") or "",
            "pub": it.findtext("pubDate") or "",
            "source_name": src.text.strip() if src is not None and src.text else "",
            "source_url": src.get("url") if src is not None else "",
        }


def add_news(db, item):
    keys = {x["url"] for x in db["news"]}
    titles = {norm_title(x["title"]) for x in db["news"]}
    if item["url"] in keys or norm_title(item["title"]) in titles:
        return False
    db["news"].append(item)
    return True


def update_press(db):
    added = 0
    for dom, url in RSS_FEEDS:
        try:
            items = list(rss_items(get(url).content))
        except Exception as e:
            log(f"RSS lỗi {url}: {e}")
            continue
        for it in items:
            desc = strip_html(it["desc"])
            hay = it["title"] + " " + desc
            if not KEYWORD_RE.search(hay) or EXCLUDE_RE.search(hay):
                continue
            pub, prec = parse_pubdate(it["pub"])
            rec = {"id": make_id("news", it["link"]), "title": it["title"], "url": it["link"],
                   "source": domain_of(it["link"]) or dom, "published": pub, "date_precision": prec,
                   "excerpt": desc, "excerpt_origin": "rss" if desc else "", "via": "rss"}
            if add_news(db, rec):
                added += 1
                log(f"  + [{rec['source']}] {rec['title']}")

    if USE_GOOGLE_NEWS:
        for q in GOOGLE_NEWS_QUERIES:
            u = f"https://news.google.com/rss/search?q={quote(q)}&hl=vi&gl=VN&ceid=VN:vi"
            try:
                items = list(rss_items(get(u).content))
            except Exception as e:
                log(f"Google News lỗi ({q}): {e}")
                continue
            for it in items:
                title = it["title"]
                if it["source_name"] and title.endswith(" - " + it["source_name"]):
                    title = title[: -len(" - " + it["source_name"])].strip()
                if not KEYWORD_RE.search(title) or EXCLUDE_RE.search(title):
                    continue
                pub, prec = parse_pubdate(it["pub"])
                src_dom = domain_of(it["source_url"]) if it["source_url"] else "news.google.com"
                rec = {"id": make_id("gn", it["link"]), "title": title, "url": it["link"],
                       "source": src_dom, "source_name": it["source_name"], "published": pub,
                       "date_precision": prec, "excerpt": "", "excerpt_origin": "", "via": "google"}
                if add_news(db, rec):
                    added += 1
                    log(f"  + [{src_dom}] {title}")
    return added


def decode_google_link(gurl):
    """Đổi link news.google.com/rss/articles/... thành link bài gốc. Trả về None nếu không giải mã được.
    Dựa trên cách trang Google News tự chuyển hướng; Google có thể đổi cơ chế này bất kỳ lúc nào."""
    gid = urlparse(gurl).path.rstrip("/").split("/")[-1]
    if not gid:
        return None
    html = get(f"https://news.google.com/rss/articles/{gid}").text
    sig = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not (sig and ts):
        return None
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, int(ts.group(1)), sig.group(1)]
    payload = [[["Fbv4je", json.dumps(inner), None, "generic"]]]
    r = session.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                     data={"f.req": json.dumps(payload)}, timeout=TIMEOUT,
                     headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    r.raise_for_status()
    time.sleep(DELAY)
    arr = json.loads(r.text.split("\n\n", 1)[1])
    url = json.loads(arr[0][2])[1]
    return url if isinstance(url, str) and url.startswith("http") else None


def resolve_google_links(db):
    done = fails = 0
    for x in sorted(db["news"], key=lambda r: r.get("published", ""), reverse=True):
        if done + fails >= MAX_GOOGLE_DECODE:
            break
        if "news.google.com" not in x["url"] or x.get("decode_failed", 0) >= 3:
            continue
        try:
            real = decode_google_link(x["url"])
        except Exception:
            real = None
        if real:
            x["google_url"], x["url"] = x["url"], real
            x["source"] = domain_of(real) or x.get("source", "")
            done += 1
        else:
            x["decode_failed"] = x.get("decode_failed", 0) + 1
            fails += 1
    # gộp bản trùng sau khi đã có link gốc (cùng bài báo đến từ RSS và Google News)
    seen, out = set(), []
    for x in db["news"]:
        if x["url"] in seen:
            continue
        seen.add(x["url"])
        out.append(x)
    db["news"] = out
    if done or fails:
        log(f"Google News: giải mã {done} link, {fails} link chưa giải mã được")


def fill_meta_descriptions(db):
    """Lấy sapo nguyên văn từ thẻ meta description của bài gốc (chỉ với link trực tiếp tới báo)."""
    n = 0
    for x in sorted(db["news"] + db.get("related", []), key=lambda r: r.get("published", ""), reverse=True):
        if n >= MAX_META_FETCH:
            break
        if x.get("excerpt") or x.get("meta_tried") or x.get("via") == "lpbs":
            continue
        if "news.google.com" in x["url"]:
            continue
        x["meta_tried"] = True
        n += 1
        try:
            soup = BeautifulSoup(get(x["url"]).text, "html.parser")
        except Exception:
            continue
        tag = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
        if tag and tag.get("content"):
            x["excerpt"] = clean_text(tag["content"])
            x["excerpt_origin"] = "meta"


# ------------------------------------------------------------------- HOSE
def hose_time(ts):
    # HOSE lưu giờ Việt Nam dưới dạng dấu thời gian UTC (đã đối chiếu với giờ hiển thị trên hsx.vn)
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")


def update_hose(db):
    known = {x["id"] for x in db.setdefault("hose", [])}
    today = dt.datetime.now(VN_TZ).date()
    start = max(HOSE_START, today - dt.timedelta(days=40)) if db["meta"].get("hose_full_done") else HOSE_START
    added = 0
    for ntype, label in HOSE_NEWS_TYPES.items():
        a = start
        while a <= today:
            b = min(a + dt.timedelta(days=364), today)
            page = 1
            while True:
                url = (f"{HOSE_API}/news/securities/{HOSE_SEC_ID}/{ntype}?pageIndex={page}&pageSize=100"
                       f"&startDate={a:%Y-%m-%d}&endDate={b:%Y-%m-%d}")
                data = (get(url, headers={"Origin": "https://www.hsx.vn", "Referer": "https://www.hsx.vn/"}).json()
                        .get("data") or {})
                for it in data.get("list") or []:
                    hid = f"hose-{it['id']}"
                    if hid in known:
                        continue
                    known.add(hid)
                    title = re.sub(r"^\s*LPS\s*:\s*", "", nfc(it.get("title") or "")).strip()
                    db["hose"].append({
                        "id": hid, "hose_id": it["id"], "title": title, "type": label,
                        "posted": hose_time(it["postedDate"]),
                        "summary": strip_html(it.get("summary") or ""),
                        "url": f"https://www.hsx.vn/vi/tin-tuc/tin-to-chuc-niem-yet/{it['id']}",
                    })
                    added += 1
                paging = data.get("paging") or {}
                if page >= (paging.get("totalPages") or 1):
                    break
                page += 1
            a = b + dt.timedelta(days=1)
    # file đính kèm của các tin HOSE mới (nguyên văn tên file)
    for x in db["hose"]:
        if "files" in x:
            continue
        try:
            j = get(f"https://api.hsx.vn/m/api/v1/1/mediafiles/1/{x['hose_id']}?pageIndex=1&pageSize=100&year=0",
                    headers={"Origin": "https://www.hsx.vn"}).json()
            lst = ((j.get("data") or {}).get("list")) or []
            x["files"] = [{"label": nfc(f.get("fileName") or "Tài liệu"),
                           "url": "https://staticfile.hsx.vn/" + quote(f["filePath"].lstrip("~/"), safe="/")}
                          for f in lst if f.get("filePath")]
        except Exception:
            pass
    db["meta"]["hose_full_done"] = True
    log(f"HOSE: {added} tin mới, tổng {len(db['hose'])} tin")
    return added


_STOP = set("của về việc và tại cho các công ty cổ phần chứng khoán lps lpbs lpbank thông báo công bố thông tin cbtt "
            "số ngày năm the of on".split())


def _tokens(s):
    s = nfc(s).lower()
    s = re.sub(r"[^\w/]+", " ", s)
    return {t for t in s.split() if t not in _STOP and len(t) > 1}


def _numbers(s):
    """Số hiệu văn bản (118/2026, 117A, 1865/TB…), bỏ qua ngày tháng dạng dd/mm/yyyy."""
    s = re.sub(r"\b\d{1,2}/\d{1,2}/\d{4}\b", " ", nfc(s).lower())
    out = set()
    for a, b, c in re.findall(r"số\s*(\d{2,5}[a-z]?)\b|\b(\d{2,5}[a-z]?)/(?=\d{4}|[a-zđ])|\b(\d{2,5}[a-z])\b", s):
        v = a or b or c
        if not re.fullmatch(r"(19|20)\d\d", v):   # bỏ số năm
            out.add(v)
    return out


# Tin trên HOSE không phải do LPS công bố (thông báo của chính Sở) — không cần có trên website LPS
HOSE_OWN_NOTICE = re.compile(r"^\s*(HOSE|SGDHCM|Sở Giao dịch)\b", re.I)
# Văn bản trên website không đăng ở mục "Tin tổ chức niêm yết" của HOSE (đăng ở mục khác hoặc không thuộc diện)
WEB_NOT_EXPECTED_ON_HOSE = {"Giải chấp & giao dịch người nội bộ", "Chứng quyền"}


def match_hose(db):
    """Ghép từng tin HOSE với văn bản trên website: cùng khoảng ngày + tiêu đề gần giống hoặc trùng số văn bản."""
    for c in db["cbtt"]:
        c.pop("hose", None)
    if not db.get("hose"):
        return
    for h in db["hose"]:
        if not h.get("title"):
            h["title"] = first_paragraph(h.get("summary", ""), 160) or "(HOSE không ghi tiêu đề)"
        h["own_notice"] = bool(HOSE_OWN_NOTICE.search(h["title"])) or h.get("type") != HOSE_NEWS_TYPES[1]
    own_lps = [h["posted"][:10] for h in db["hose"] if not h["own_notice"]]
    first_hose = min(own_lps) if own_lps else min(h["posted"][:10] for h in db["hose"])
    used = set()
    for h in sorted(db["hose"], key=lambda r: r["posted"]):
        if h["own_notice"]:
            h["match"] = None
            continue
        hd = dt.date.fromisoformat(h["posted"][:10])
        ht, hn = _tokens(h["title"]), _numbers(h["title"] + " " + h.get("summary", ""))
        best, best_score = None, 0.0
        for c in db["cbtt"]:
            if c["id"] in used or not c.get("date"):
                continue
            cd = dt.date.fromisoformat(c["date"])
            if abs((hd - cd).days) > HOSE_MATCH_DAYS:
                continue
            ct = _tokens(c["title"])
            score = len(ht & ct) / max(1, len(ht | ct))
            if hn and hn & _numbers(c["title"] + " " + (c.get("body") or "")[:400]):
                score += 0.5
            score -= abs((hd - cd).days) * 0.02
            if score > best_score:
                best, best_score = c, score
        if best and best_score >= 0.3:
            used.add(best["id"])
            h["match"] = best["id"]
            best["hose"] = {"id": h["id"], "posted": h["posted"], "url": h["url"], "title": h["title"]}
        else:
            h["match"] = None
    db["meta"]["hose_first"] = first_hose


def analyse_cbtt(db):
    first_hose = db["meta"].get("hose_first")
    for c in db["cbtt"]:
        c["tags"] = classify(c["title"], c.get("body", ""))
        hp = (c.get("hose") or {}).get("posted")
        c["late"] = late_check(c["title"], c.get("body", ""), c.get("date", ""), c["tags"], hp)
        # Trạng thái đối chiếu HOSE cho văn bản đăng từ ngày LPS có tin đầu tiên trên HOSE
        if not first_hose or not c.get("date") or c["date"] < first_hose:
            c["recon"] = None
        elif c.get("hose"):
            c["recon"] = "matched"
        elif set(c["tags"]) & WEB_NOT_EXPECTED_ON_HOSE:
            c["recon"] = "web_only_expected"
        else:
            c["recon"] = "web_only"


# ------------------------------------------------------------- BÊN LIÊN QUAN
def hose_sec_id(db, code):
    cache = db["meta"].setdefault("hose_ids", {})
    if code in cache:
        return cache[code]
    data = get(f"https://api.hsx.vn/l/api/v1/1/securities/stock?pageIndex=1&pageSize=1000&alphabet={code[0]}&sectorId=",
               headers={"Origin": "https://www.hsx.vn"}).json()
    for it in (data.get("data") or {}).get("list") or []:
        if it.get("code") == code:
            cache[code] = it["id"]
            return it["id"]
    return None


def update_related(db):
    rel = db.setdefault("related", [])
    known = {r["url"] for r in rel}
    known_titles = {(r["party"], norm_title(r["title"])) for r in rel}
    today = dt.datetime.now(VN_TZ).date()
    start = today - dt.timedelta(days=RELATED_DAYS)
    added = 0
    for p in RELATED_PARTIES:
        kw = re.compile(p["kw"], re.I)
        # (a) công bố thông tin trên HOSE
        if p.get("hose_code"):
            try:
                sid = hose_sec_id(db, p["hose_code"])
                if sid:
                    url = (f"{HOSE_API}/news/securities/{sid}/1?pageIndex=1&pageSize=100"
                           f"&startDate={start:%Y-%m-%d}&endDate={today:%Y-%m-%d}")
                    data = (get(url, headers={"Origin": "https://www.hsx.vn"}).json().get("data") or {})
                    for it in data.get("list") or []:
                        link = f"https://www.hsx.vn/vi/tin-tuc/tin-to-chuc-niem-yet/{it['id']}"
                        if link in known:
                            continue
                        known.add(link)
                        rel.append({"id": f"rel-hose-{it['id']}", "party": p["key"], "kind": "hose",
                                    "title": nfc(it.get("title") or ""), "url": link, "source": "hsx.vn",
                                    "published": hose_time(it["postedDate"]), "date_precision": "minute",
                                    "excerpt": strip_html(it.get("summary") or ""), "excerpt_origin": "hose"})
                        added += 1
            except Exception as e:
                log(f"HOSE ({p['hose_code']}) lỗi: {e}")
        # (b) báo chí qua Google News (chỉ 30 ngày gần nhất mỗi lần đọc)
        if USE_GOOGLE_NEWS:
            for q in p["queries"]:
                u = f"https://news.google.com/rss/search?q={quote(q + ' when:30d')}&hl=vi&gl=VN&ceid=VN:vi"
                try:
                    items = list(rss_items(get(u).content))
                except Exception as e:
                    log(f"Google News lỗi ({q}): {e}")
                    continue
                for it in items:
                    title = it["title"]
                    if it["source_name"] and title.endswith(" - " + it["source_name"]):
                        title = title[: -len(" - " + it["source_name"])].strip()
                    if not kw.search(title) or EXCLUDE_RE.search(title):
                        continue
                    if it["link"] in known or (p["key"], norm_title(title)) in known_titles:
                        continue
                    pub, prec = parse_pubdate(it["pub"])
                    known.add(it["link"]); known_titles.add((p["key"], norm_title(title)))
                    rel.append({"id": make_id("rel", it["link"]), "party": p["key"], "kind": "news", "title": title,
                                "url": it["link"], "source": domain_of(it["source_url"]) if it["source_url"] else "news.google.com",
                                "source_name": it["source_name"], "published": pub, "date_precision": prec,
                                "excerpt": "", "excerpt_origin": "", "via": "google"})
                    added += 1
    # giải mã link Google, bỏ tin cũ, giới hạn số lượng mỗi bên
    n = 0
    for r in sorted(rel, key=lambda x: x.get("published", ""), reverse=True):
        if n >= MAX_RELATED_DECODE:
            break
        if "news.google.com" in r["url"] and r.get("decode_failed", 0) < 3:
            n += 1
            try:
                real = decode_google_link(r["url"])
            except Exception:
                real = None
            if real:
                r["google_url"], r["url"] = r["url"], real
                r["source"] = domain_of(real) or r["source"]
            else:
                r["decode_failed"] = r.get("decode_failed", 0) + 1
    cutoff = start.isoformat()
    keep = []
    for p in RELATED_PARTIES:
        mine = sorted([r for r in rel if r["party"] == p["key"] and (r.get("published") or "")[:10] >= cutoff],
                      key=lambda x: x.get("published", ""), reverse=True)
        keep += mine[:RELATED_MAX_PER_PARTY]
    seen, out = set(), []
    for r in keep:
        if r["url"] not in seen:
            seen.add(r["url"]); out.append(r)
    db["related"] = out
    log(f"Bên liên quan: {added} tin mới, đang giữ {len(out)} tin")
    return added


# ------------------------------------------------------------------ DỮ LIỆU
def empty_db():
    return {"meta": {"company": "Công ty Cổ phần Chứng khoán LPS", "ticker": "LPS", "exchange": "HOSE",
                     "website": LPBS, "update_schedule": ["08:00", "11:00", "13:00", "16:00"], "schema_version": 2},
            "news": [], "cbtt": []}


def load_db():
    if DB_FILE.exists():
        db = json.loads(DB_FILE.read_text(encoding="utf-8"))
        db["news"] = [x for x in db["news"] if x.get("via") == "lpbs" or not EXCLUDE_RE.search(x["title"])]
        return db
    db = empty_db()
    if SEED_FILE.exists():   # nhập danh sách bài cũ: chỉ giữ tiêu đề, nguồn, ngày, link (bỏ phần tóm tắt biên tập)
        seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        for a in seed.get("articles", []):
            if a.get("source") == "lpbs.com.vn":
                continue
            db["news"].append({"id": a["id"], "title": a["title"], "url": a["url"], "source": a["source"],
                               "published": a["published"], "date_precision": a.get("date_precision", "day"),
                               "excerpt": "", "excerpt_origin": "", "via": "seed"})
        log(f"Nhập {len(db['news'])} bài từ {SEED_FILE.name}")
    return db


def save_db(db):
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DB_FILE)


def logo_data_uri():
    if not LOGO_CACHE.exists():
        try:
            LOGO_CACHE.write_bytes(get(LOGO_URL).content)
        except Exception as e:
            log(f"Không tải được logo: {e}")
            return ""
    return "data:image/png;base64," + base64.b64encode(LOGO_CACHE.read_bytes()).decode()


def _strip(rows, drop=("meta_tried", "decode_failed", "google_url")):
    return [{k: v for k, v in r.items() if k not in drop} for r in rows]


def render(db):
    match_hose(db)
    analyse_cbtt(db)
    today = dt.datetime.now(VN_TZ).date()
    docs = [dict(r, where="web") for r in db["cbtt"]]
    docs += [{"title": h["title"], "url": h["url"], "date": h["posted"][:10], "where": "hose"}
             for h in db.get("hose", []) if not h.get("own_notice")]
    news = lps_audit.cluster_news(_strip(db["news"]))
    related = lps_audit.cluster_news(_strip(db.get("related", [])))
    cbtt_out = []
    for r in db["cbtt"]:
        x = {k: v for k, v in r.items() if k not in ("fp", "versions")}
        x["n_versions"] = len(r.get("versions", []))
        cbtt_out.append(x)
    changes = []
    for r in db["cbtt"]:
        for ch in r.get("changes", []):
            changes.append({**ch, "doc_id": r["id"], "title": r["title"], "url": r["url"], "date": r.get("date", ""),
                            "previous": (r.get("versions") or [None])[-1]})
    audit = {
        "changes": sorted(changes, key=lambda c: c["at"], reverse=True),
        "numbering": lps_audit.resolution_numbering(db["cbtt"], db.get("hose")),
        "calendar": lps_audit.periodic_calendar(docs, today),
        "hash_remaining": db["meta"].get("hash_remaining"),
        "baseline": db["meta"].get("verify_baseline"),
        "sections": {k: n for k, n, *_ in IR_SECTIONS},
    }
    payload = {"meta": db["meta"], "sources": KNOWN_SOURCES,
               "news": news,
               "cbtt": sorted(cbtt_out, key=lambda r: (r.get("date", ""), r["url"]), reverse=True),
               "hose": sorted(db.get("hose", []), key=lambda r: r["posted"], reverse=True),
               "related": related,
               "parties": [{"key": p["key"], "name": p["name"]} for p in RELATED_PARTIES],
               "audit": audit}
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = tpl_text().replace("__LPS_DATA__", data).replace("__LPS_LOGO__", logo_data_uri())
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_HTML.with_name(OUTPUT_HTML.name + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, OUTPUT_HTML)
    log(f"Đã dựng {OUTPUT_HTML} ({len(html) // 1024} KB)")


def tpl_text():
    return TEMPLATE_FILE.read_text(encoding="utf-8")


def step(name, fn, default=-1):
    """Chạy một bước; lỗi ở bước này không làm hỏng các bước khác."""
    try:
        return fn()
    except Exception as e:
        log(f"Lỗi ở bước {name}: {e}")
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="Quét lại toàn bộ các trang trên lpbs.com.vn")
    ap.add_argument("--light", action="store_true",
                    help="Lần chạy nhanh (dùng cho lịch chuyển file về máy): không kiểm tra lại toàn bộ văn bản")
    ap.add_argument("--render", action="store_true", help="Chỉ dựng lại HTML, không tải dữ liệu")
    args = ap.parse_args()

    db = load_db()
    if not args.render:
        today = dt.datetime.now(VN_TZ).date().isoformat()
        # Mỗi ngày một lần (lần chạy đầu tiên trong ngày): đọc trọn danh sách 4 mục và kiểm tra lại MỌI văn bản.
        full_day = args.full or (not args.light and db["meta"].get("last_full_verify") != today)
        news_before = len(db["news"])
        res = step("đọc lpbs.com.vn", lambda: update_lpbs(db, full_day), (-1, 0, None))
        c, n, listing = res
        first_verify = not any("fp" in r for r in db["cbtt"])
        v = step("kiểm tra văn bản", lambda: lps_audit.verify_documents(
            db, session, parse_lpbs_detail, log, listing=listing, full_day=full_day), None)
        if v is not None and full_day:
            db["meta"]["last_full_verify"] = today
        if first_verify and v is not None:
            db["meta"]["verify_baseline"] = dt.datetime.now(VN_TZ).isoformat(timespec="minutes")
        save_db(db)
        step("RSS báo chí", lambda: update_press(db), 0)
        step("giải mã link Google News", lambda: resolve_google_links(db), None)
        p = len(db["news"]) - news_before - n
        h = step("HOSE", lambda: update_hose(db)) if USE_HOSE else 0
        r = step("bên liên quan", lambda: update_related(db))
        step("lấy sapo", lambda: fill_meta_descriptions(db), None)
        now = dt.datetime.now(VN_TZ)
        db["meta"]["last_updated"] = now.isoformat(timespec="minutes")
        chg = (v or {}).get("changed", 0)
        db["meta"]["last_result"] = (f"{max(c, 0)} văn bản mới, {n + p} tin mới, {max(h, 0)} tin HOSE mới, "
                                     f"{max(r, 0)} tin bên liên quan, {chg} thay đổi văn bản"
                                     + (" (lỗi khi đọc lpbs.com.vn)" if c < 0 else "") + (" (lỗi khi đọc HOSE)" if h < 0 else "")
                                     + (" (lỗi khi kiểm tra văn bản)" if v is None else ""))
        db["meta"]["last_errors"] = [l for l in LOG if "Lỗi" in l or "lỗi" in l][-10:]
        save_db(db)
        log(f"Hoàn tất: {db['meta']['last_result']}. Tổng: {len(db['cbtt'])} văn bản, {len(db['news'])} tin.")
    render(db)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
