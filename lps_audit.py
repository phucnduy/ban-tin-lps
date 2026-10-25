# -*- coding: utf-8 -*-
"""
Các chức năng kiểm soát cho Bản tin LPS (không dùng AI):
  1. Phát hiện văn bản bị sửa, gỡ, thay file      → verify_documents()
  2. Mã băm SHA-256 và lưu trữ file đính kèm       → trong verify_documents()
  3. Số hiệu nghị quyết HĐQT bị nhảy cóc           → resolution_numbering()
  4. Lịch công bố thông tin định kỳ               → periodic_calendar()
  5. Gộp tin báo cùng sự kiện                      → cluster_news()
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata

import requests

VN_TZ = dt.timezone(dt.timedelta(hours=7))


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def _norm_text(s):
    return re.sub(r"\s+", " ", nfc(s)).strip()


def _h(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def now_vn():
    return dt.datetime.now(VN_TZ).isoformat(timespec="minutes")


# =============================================================== 1–2. VĂN BẢN THAY ĐỔI + MÃ BĂM
CHANGE_LABELS = {
    "doi_tieu_de": "Tiêu đề thay đổi",
    "doi_ngay": "Ngày đăng thay đổi",
    "sua_noi_dung": "Nội dung thay đổi",
    "doi_tep": "Danh sách tệp đính kèm thay đổi",
    "thay_noi_dung_tep": "Tệp đính kèm bị thay (cùng tên, khác nội dung)",
    "tep_loi": "Tệp đính kèm không còn tải được",
    "go_khoi_web": "Trang văn bản không còn trên website",
    "an_khoi_danh_sach": "Không còn trong danh sách mục (trang vẫn mở được)",
    "khoi_phuc": "Văn bản xuất hiện trở lại",
}

# Cấu hình kiểm tra
VERIFY_RECENT_DAYS = 45          # mỗi lần chạy: kiểm tra lại văn bản đăng trong 45 ngày gần nhất
DETAIL_WORKERS = 4
HEAD_WORKERS = 6
HASH_WORKERS = 3
HASH_TIME_BUDGET = int(os.environ.get("LPS_HASH_BUDGET", 25 * 60))   # giây cho việc tải + băm tệp mỗi lần chạy
ARCHIVE_FROM = "2026-01-01"      # lưu bản sao PDF cho văn bản đăng từ ngày này (mã băm thì tính cho mọi file)
ARCHIVE_MAX_BYTES = 1_900_000_000


def fingerprint(title, body, files, date):
    return {"title": _h(_norm_text(title)), "body": _h(_norm_text(body)),
            "files": _h("|".join(sorted(f["url"] for f in files or []))), "date": date or ""}


def _add_change(rec, kind, detail=""):
    rec.setdefault("changes", []).append({"at": now_vn(), "kind": kind, "label": CHANGE_LABELS[kind], "detail": detail})


def _snapshot(rec):
    return {"at": rec.get("checked") or rec.get("first_seen") or "", "title": rec["title"], "date": rec.get("date", ""),
            "body": rec.get("body", ""), "files": [{"label": f["label"], "url": f["url"], "sha256": f.get("sha256")}
                                                    for f in rec.get("files", [])]}


class Verifier:
    def __init__(self, session, parse_detail, log, delay=0.3, timeout=30):
        self.s = session
        self.parse_detail = parse_detail
        self.log = log
        self.delay = delay
        self.timeout = timeout
        self.gh_token = os.environ.get("GITHUB_TOKEN")
        self.gh_repo = os.environ.get("GITHUB_REPOSITORY")
        self._rel_cache = {}
        self._lock = threading.Lock()

    # ---------------- tải trang chi tiết
    def fetch_detail(self, url):
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=self.timeout)
                time.sleep(self.delay)
                if r.status_code == 404:
                    return "404", None, None
                r.raise_for_status()
                r.encoding = "utf-8"
                body, files = self.parse_detail(r.text)
                if not body and not files:
                    return "empty", None, None
                return "ok", body, files
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
        return "error", None, None

    def head(self, url):
        for attempt in range(2):
            try:
                r = self.s.head(url, timeout=self.timeout, allow_redirects=True)
                et = r.headers.get("ETag", "")
                m = re.search(r'"([0-9a-f]+)-', et)
                size = int(r.headers.get("Content-Length") or 0) or (int(m.group(1), 16) if m else 0)
                return r.status_code, et, r.headers.get("Last-Modified", ""), size
            except requests.RequestException:
                time.sleep(2)
        return "error", "", "", 0

    # ---------------- lưu trữ trên GitHub Releases (chỉ khi chạy trên GitHub Actions)
    def _release_id(self, tag):
        if tag in self._rel_cache:
            return self._rel_cache[tag]
        api = f"https://api.github.com/repos/{self.gh_repo}/releases"
        hdr = {"Authorization": f"Bearer {self.gh_token}", "Accept": "application/vnd.github+json"}
        r = requests.get(f"{api}/tags/{tag}", headers=hdr, timeout=30)
        if r.status_code == 404:
            r = requests.post(api, headers=hdr, timeout=30, json={
                "tag_name": tag, "name": f"Lưu trữ tệp công bố thông tin {tag[-4:]}",
                "body": "Bản sao tệp đính kèm trên lpbs.com.vn, tạo tự động. Tên tệp bắt đầu bằng 12 ký tự đầu mã SHA-256.",
                "draft": False, "prerelease": False})
        r.raise_for_status()
        self._rel_cache[tag] = r.json()["id"]
        return self._rel_cache[tag]

    def archive(self, path, sha, url, year):
        if not (self.gh_token and self.gh_repo):
            return None
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", requests.utils.unquote(url.rsplit("/", 1)[-1]))[-120:] or "file"
        name = f"{sha[:12]}_{base}"
        with self._lock:
            rid = self._release_id(f"luu-tru-{year}")
        up = f"https://uploads.github.com/repos/{self.gh_repo}/releases/{rid}/assets?name={requests.utils.quote(name)}"
        with open(path, "rb") as fh:
            r = requests.post(up, data=fh, timeout=600, headers={
                "Authorization": f"Bearer {self.gh_token}", "Content-Type": "application/octet-stream"})
        if r.status_code == 422:   # đã có tệp cùng tên (cùng mã băm) → coi như đã lưu
            return f"https://github.com/{self.gh_repo}/releases/download/luu-tru-{year}/{name}"
        r.raise_for_status()
        return r.json().get("browser_download_url")

    def hash_file(self, url, want_archive, year):
        h = hashlib.sha256()
        size = 0
        tmp = tempfile.NamedTemporaryFile(delete=False) if want_archive else None
        try:
            with self.s.get(url, stream=True, timeout=120) as r:
                if r.status_code == 404:
                    return {"status": 404}
                r.raise_for_status()
                for chunk in r.iter_content(1 << 16):
                    h.update(chunk)
                    size += len(chunk)
                    if tmp:
                        tmp.write(chunk)
            sha = h.hexdigest()
            out = {"status": 200, "sha256": sha, "size": size, "hashed_at": now_vn()}
            if tmp:
                tmp.close()
                if size <= ARCHIVE_MAX_BYTES:
                    try:
                        out["archive"] = self.archive(tmp.name, sha, url, year)
                    except Exception as e:
                        self.log(f"  Không lưu trữ được {url}: {e}")
            return out
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}
        finally:
            if tmp:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass


def verify_documents(db, session, parse_detail, log, listing=None, full_day=False):
    """So sánh văn bản trên website với lần lưu trước. listing: {url: (mục, tiêu đề, ngày)} của toàn bộ danh sách
    (chỉ có khi đã đọc trọn vẹn mọi trang) — dùng để phát hiện văn bản bị gỡ khỏi danh sách."""
    v = Verifier(session, parse_detail, log)
    today = dt.datetime.now(VN_TZ).date()
    recent = (today - dt.timedelta(days=VERIFY_RECENT_DAYS)).isoformat()
    recs = db["cbtt"]
    targets = [r for r in recs if full_day or (r.get("date") or "") >= recent or "fp" not in r]
    stats = {"checked": 0, "changed": 0, "files_checked": 0, "hashed": 0, "archived": 0}

    # (a) trang chi tiết
    def work(rec):
        return rec, v.fetch_detail(rec["url"])
    with cf.ThreadPoolExecutor(DETAIL_WORKERS) as ex:
        results = list(ex.map(work, targets))
    for rec, (status, body, files) in results:
        if status == "error":
            continue
        stats["checked"] += 1
        rec["checked"] = now_vn()
        listed = listing is None or rec["url"] in listing
        if listing is not None and rec["url"] in listing:
            sec, ltitle, ldate = listing[rec["url"]]
        else:
            ltitle, ldate = rec["title"], rec.get("date", "")
        if status in ("404", "empty"):
            if rec.get("status") != "removed":
                rec["status"] = "removed"
                _add_change(rec, "go_khoi_web", "Trang chi tiết trả lỗi 404" if status == "404" else "Trang chi tiết không còn nội dung")
                stats["changed"] += 1
            continue
        if rec.get("status") == "removed":
            _add_change(rec, "khoi_phuc")
            stats["changed"] += 1
        new_fp = fingerprint(ltitle, body, files, ldate)
        old_fp = rec.get("fp")
        if old_fp:
            kinds = []
            if new_fp["title"] != old_fp["title"]:
                kinds.append(("doi_tieu_de", f"Trước: “{rec['title']}”"))
            if new_fp["date"] != old_fp["date"] and new_fp["date"]:
                kinds.append(("doi_ngay", f"Trước: {old_fp['date']} → nay: {new_fp['date']}"))
            if new_fp["body"] != old_fp["body"]:
                kinds.append(("sua_noi_dung", ""))
            if new_fp["files"] != old_fp["files"]:
                before = {f["url"] for f in rec.get("files", [])}
                after = {f["url"] for f in files}
                added = [requests.utils.unquote(u.rsplit('/', 1)[-1]) for u in after - before]
                gone = [requests.utils.unquote(u.rsplit('/', 1)[-1]) for u in before - after]
                kinds.append(("doi_tep", "; ".join(filter(None, [("Thêm: " + ", ".join(added)) if added else "",
                                                                   ("Bỏ: " + ", ".join(gone)) if gone else ""]))))
            if kinds:
                rec.setdefault("versions", []).append(_snapshot(rec))
                for k, d in kinds:
                    _add_change(rec, k, d)
                stats["changed"] += 1
                # giữ thông tin băm của tệp cũ còn trùng link
                oldf = {f["url"]: f for f in rec.get("files", [])}
                for f in files:
                    for key in ("sha256", "size", "etag", "lm", "archive", "hashed_at", "status"):
                        if key in oldf.get(f["url"], {}):
                            f[key] = oldf[f["url"]][key]
                rec["title"], rec["body"], rec["files"] = ltitle, body, files
                if ldate:
                    rec["date"] = ldate
        rec["fp"] = new_fp
        if listing is not None:
            if not listed and rec.get("status") != "unlisted":
                rec["status"] = "unlisted"
                _add_change(rec, "an_khoi_danh_sach")
                stats["changed"] += 1
            elif listed and rec.get("status") in ("unlisted", "removed"):
                rec["status"] = "active"
            elif listed:
                rec["status"] = "active"
        else:
            rec.setdefault("status", "active")

    # (b) tệp đính kèm: kiểm tra nhanh bằng ETag / Last-Modified, chỉ tải lại khi có dấu hiệu đổi hoặc chưa có mã băm
    file_jobs = []
    for rec in targets:
        if rec.get("status") == "removed":
            continue
        for f in rec.get("files", []):
            if "lpbs.com.vn" in f["url"]:
                file_jobs.append((rec, f))
    with cf.ThreadPoolExecutor(HEAD_WORKERS) as ex:
        heads = list(ex.map(lambda rf: (rf, v.head(rf[1]["url"])), file_jobs))
    to_hash = []
    for (rec, f), (code, et, lm, size) in heads:
        if code == "error":
            continue
        stats["files_checked"] += 1
        if code == 404:
            if f.get("status") != 404:
                f["status"] = 404
                _add_change(rec, "tep_loi", requests.utils.unquote(f["url"].rsplit("/", 1)[-1]))
                stats["changed"] += 1
            continue
        if f.get("status") == 404 and code == 200:
            f["status"] = 200
        changed_hdr = (f.get("etag") and et and et != f["etag"]) or (f.get("lm") and lm and lm != f["lm"])
        f["etag"], f["lm"] = et, lm
        if size:
            f["size"] = size
        need_archive = (rec.get("date") or "") >= ARCHIVE_FROM and not f.get("archive")
        if "sha256" not in f or changed_hdr or (need_archive and v.gh_token):
            to_hash.append((rec, f, bool(changed_hdr)))
    # văn bản mới nhất trước; dừng khi hết ngân sách thời gian
    to_hash.sort(key=lambda x: x[0].get("date") or "", reverse=True)
    t0 = time.time()

    def do_hash(item):
        rec, f, changed_hdr = item
        if time.time() - t0 > HASH_TIME_BUDGET:
            return item, None
        want = (rec.get("date") or "") >= ARCHIVE_FROM and bool(v.gh_token)
        return item, v.hash_file(f["url"], want, (rec.get("date") or "0000")[:4])
    with cf.ThreadPoolExecutor(HASH_WORKERS) as ex:
        for (rec, f, changed_hdr), res in ex.map(do_hash, to_hash):
            if not res or res.get("status") != 200:
                continue
            old = f.get("sha256")
            if old and old != res["sha256"]:
                rec.setdefault("versions", []).append(_snapshot(rec))
                _add_change(rec, "thay_noi_dung_tep",
                            f"{requests.utils.unquote(f['url'].rsplit('/', 1)[-1])}: SHA-256 cũ {old[:16]}… → mới {res['sha256'][:16]}…")
                stats["changed"] += 1
                f.pop("archive", None)
            f.update({k: res[k] for k in ("sha256", "size", "hashed_at") if k in res})
            if res.get("archive"):
                f["archive"] = res["archive"]
                stats["archived"] += 1
            stats["hashed"] += 1
    remaining = sum(1 for r in recs for f in r.get("files", []) if "lpbs.com.vn" in f["url"] and "sha256" not in f and f.get("status") != 404)
    db["meta"]["hash_remaining"] = remaining
    log(f"Kiểm tra văn bản: {stats['checked']} trang, {stats['files_checked']} tệp, {stats['changed']} thay đổi; "
        f"băm {stats['hashed']} tệp, lưu trữ {stats['archived']} tệp; còn {remaining} tệp chưa có mã băm")
    return stats


# =============================================================== 3. SỐ HIỆU NGHỊ QUYẾT
_NQ_FULL = re.compile(r"(?<![\d/])(\d{1,4})\s*([A-Za-z]?)\s*/\s*(20\d\d)\s*/\s*NQ\s*[-–]\s*(HĐQT|LPBS|LPS)", re.I)
_NQ_SO = re.compile(r"nghị quyết(?:\s+(?:của\s+)?(?:hđqt|hội đồng quản trị))?\s+số\s+(\d{1,4})\s*([A-Za-z]?)\b", re.I)


MIN_NUMBERS_FOR_GAPS = 10


def resolution_numbering(cbtt, hose=None, min_year=2024):
    """Thu thập số hiệu nghị quyết HĐQT đã công bố, theo năm; liệt kê các số chưa thấy giữa số nhỏ nhất và lớn nhất."""
    found = {}
    sources = [(r["title"], r.get("body", "")[:800], r.get("date", ""), r["url"], "web") for r in cbtt]
    sources += [(h["title"], h.get("summary", ""), h["posted"][:10], h["url"], "hose") for h in (hose or [])]
    for title, body, date, url, where in sources:
        text = nfc(title + "\n" + body)
        if re.search(r"đhđcđ|đại hội đồng cổ đông", nfc(title), re.I) and not re.search(r"hđqt|hội đồng quản trị", nfc(title), re.I):
            pass
        hits = [(int(n), (s or "").upper(), int(y)) for n, s, y, _ in _NQ_FULL.findall(text)]
        if not hits and date:
            hits = [(int(n), (s or "").upper(), int(date[:4])) for n, s in _NQ_SO.findall(text)
                    if re.search(r"hđqt|hội đồng quản trị", text, re.I)]
        for n, suf, y in hits:
            if y < min_year or n == 0 or n > 2000:
                continue
            key = f"{n}{suf}"
            lst = found.setdefault(y, {}).setdefault(n, {})
            lst.setdefault(key, [])
            if not any(e["url"] == url for e in lst[key]):
                lst[key].append({"title": nfc(title), "url": url, "date": date, "where": where})
    out = []
    for y in sorted(found, reverse=True):
        nums = found[y]
        if len(nums) < 3:
            continue
        lo, hi = min(nums), max(nums)
        # cần đủ dữ liệu mới liệt kê khoảng trống (năm có ít số hiệu thường do định dạng số khác, không có ý nghĩa)
        enough = len(nums) >= MIN_NUMBERS_FOR_GAPS
        missing = [n for n in range(lo, hi + 1) if n not in nums] if enough else []
        out.append({"year": y, "min": lo, "max": hi, "count": len(nums), "missing": missing, "enough": enough,
                    "missing_ranges": _ranges(missing),
                    "items": [{"number": k, "base": n, "docs": docs} for n in sorted(nums)
                              for k, docs in sorted(nums[n].items())]})
    return out


def _ranges(nums):
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append([start, prev])
            start = prev = n
    if start is not None:
        out.append([start, prev])
    return out


# =============================================================== 4. LỊCH CÔNG BỐ ĐỊNH KỲ
# Thời hạn theo Thông tư 96/2020/TT-BTC (Điều 10, 14, 22), cần đối chiếu sửa đổi tại TT 68/2024 và TT 08/2026.
# Áp dụng thời hạn chung (không phải công ty mẹ): quý 20 ngày; bán niên 45 ngày; năm 90 ngày; thường niên 110 ngày;
# quản trị 30 ngày; báo cáo tỷ lệ an toàn tài chính công bố cùng lúc với báo cáo tài chính bán niên / năm.
PERIODIC_FROM_YEAR = 2025
ROMAN = {1: "i", 2: "ii", 3: "iii", 4: "iv"}


def _q_re(q):
    return rf"(quý|quy|q)\s*0?({q}|{ROMAN[q]})\b"


def periodic_calendar(docs, today=None):
    today = today or dt.datetime.now(VN_TZ).date()
    T = [(nfc(d["title"]).lower(), d) for d in docs if d.get("date")]

    def find(rx_all, rx_none, after, until_extra=200):
        res = []
        for t, d in T:
            if all(re.search(r, t, re.I) for r in rx_all) and not any(re.search(r, t, re.I) for r in rx_none):
                if d["date"] >= after.isoformat() and d["date"] <= (after + dt.timedelta(days=until_extra)).isoformat():
                    res.append(d)
        return sorted(res, key=lambda d: d["date"])

    BCTC = r"báo cáo tài chính|\bbctc\b"
    rows = []
    for Y in range(PERIODIC_FROM_YEAR, today.year + 1):
        specs = []
        for q, (m, dd) in {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}.items():
            end = dt.date(Y, m, dd)
            specs.append((f"BCTC quý {q}/{Y}", "Báo cáo tài chính quý", end, end + dt.timedelta(days=20),
                          [BCTC, _q_re(q), str(Y)], [r"^\s*(cbtt:?\s*)?(thư|công văn)\s+giải trình"]))
        h_end = dt.date(Y, 6, 30)
        specs.append((f"BCTC bán niên {Y} (soát xét)", "Báo cáo tài chính bán niên", h_end, h_end + dt.timedelta(days=45),
                      [BCTC, r"bán niên|6 tháng|giữa niên độ|30/0?6", str(Y)], [_q_re(2) + r"(?!.*bán niên)"]))
        specs.append((f"Tỷ lệ an toàn tài chính 30/6/{Y}", "Báo cáo tỷ lệ an toàn tài chính", h_end, h_end + dt.timedelta(days=45),
                      [r"an toàn tài chính", r"bán niên|6 tháng|30/0?6|30 tháng 6|soát xét", str(Y)], []))
        specs.append((f"Báo cáo quản trị 6 tháng {Y}", "Báo cáo tình hình quản trị", h_end, h_end + dt.timedelta(days=30),
                      [r"quản tr[iị]", r"6 tháng|bán niên", str(Y)], []))
        y_end = dt.date(Y, 12, 31)
        specs.append((f"BCTC năm {Y} (kiểm toán)", "Báo cáo tài chính năm", y_end, y_end + dt.timedelta(days=90),
                      [BCTC, rf"năm\s*(tài chính\s*)?{Y}|31/12/{Y}"], [r"quý|bán niên|6 tháng|giữa niên độ|hợp đồng kiểm toán|ký hợp đồng"]))
        specs.append((f"Tỷ lệ an toàn tài chính 31/12/{Y}", "Báo cáo tỷ lệ an toàn tài chính", y_end, y_end + dt.timedelta(days=90),
                      [r"an toàn tài chính", rf"năm\s*{Y}|31/12/{Y}"], [r"bán niên|6 tháng|30/0?6"]))
        specs.append((f"Báo cáo thường niên {Y}", "Báo cáo thường niên", y_end, y_end + dt.timedelta(days=110),
                      [r"báo cáo thường n", str(Y)], [r"đại hội|đhđcđ"]))
        specs.append((f"Báo cáo quản trị năm {Y}", "Báo cáo tình hình quản trị", y_end, y_end + dt.timedelta(days=30),
                      [r"quản tr[iị]", rf"năm\s*{Y}|kết thúc năm"], [r"6 tháng|bán niên"]))
        for name, kind, end, deadline, rx_all, rx_none in specs:
            if end > today + dt.timedelta(days=1):
                continue
            hits = find(rx_all, rx_none, end - dt.timedelta(days=5))
            if hits:
                d = hits[0]
                late = (dt.date.fromisoformat(d["date"]) - deadline).days
                status = "on_time" if late <= 0 else "late"
            else:
                d, late = None, None
                status = "missing" if today > deadline else "pending"
            rows.append({"name": name, "kind": kind, "period_end": end.isoformat(), "deadline": deadline.isoformat(),
                         "status": status, "late_days": late,
                         "doc": {"title": nfc(d["title"]), "url": d["url"], "date": d["date"], "where": d.get("where", "web")} if d else None})
    rows.sort(key=lambda r: (r["deadline"], r["name"]), reverse=True)
    return rows


# =============================================================== 5. GỘP TIN CÙNG SỰ KIỆN
_STOP = set(("của về việc và tại cho các công ty cổ phần chứng khoán lps lpbs lpbank với trong khi đã được "
             "có là một những này sau trước từ đến theo the of on in to").split())


def _tok(s):
    s = nfc(s).lower()
    return {t for t in re.findall(r"[\w%.,]+", s) if t not in _STOP and len(t) > 1}


def cluster_news(items, days=3, threshold=0.42):
    """Gán trường 'cluster' (id bài đại diện) cho các bài cùng sự kiện. Bài mới nhất trong nhóm làm đại diện."""
    items = sorted(items, key=lambda r: r.get("published", ""), reverse=True)
    leads = []
    for it in items:
        it.pop("cluster", None)
        d = (it.get("published") or "")[:10]
        if not d:
            continue
        tk = _tok(it["title"])
        nums = {t for t in tk if re.search(r"\d", t)}
        best, score = None, 0.0
        for ld in leads:
            if abs((dt.date.fromisoformat(ld["d"]) - dt.date.fromisoformat(d)).days) > days:
                continue
            j = len(tk & ld["tk"]) / max(1, len(tk | ld["tk"]))
            if nums and len(nums & ld["nums"]) >= 2:
                j += 0.15
            if j > score:
                best, score = ld, j
        if best and score >= threshold:
            it["cluster"] = best["id"]
            best["tk"] |= tk
            best["nums"] |= nums
        else:
            leads.append({"id": it["id"], "d": d, "tk": set(tk), "nums": set(nums)})
    return items
