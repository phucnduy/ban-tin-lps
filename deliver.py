#!/usr/bin/env python3
"""Đóng gói bản tin và tệp công bố thông tin để chuyển về máy cơ quan. Không dùng AI.

Kết quả trong thư mục --out (mặc định /mnt/user-data/outputs/tin-tuc):
  ban-tin-lps.html                                  -> Tin tức\\ban-tin-lps.html
  Cong bo thong tin/<YYYY-MM-DD>/<tệp>              -> Tin tức\\Công bố thông tin\\<YYYY-MM-DD>\\<tệp>
  manifest.json  : danh sách tệp, chia lô (<=20 tệp, <=25 MB mỗi lô) để ghi về máy.

Mỗi ngày có một tệp "_Danh sách văn bản.html" liệt kê toàn bộ văn bản của ngày đó
(kể cả văn bản chỉ có nội dung, không có tệp đính kèm), kèm link gốc và mã SHA-256.
"""
import argparse, hashlib, html, json, re, shutil, sys, unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import requests

BASE = Path(__file__).resolve().parent
VN = timezone(timedelta(hours=7))
MAX_FILE = 30 * 1024 * 1024          # giới hạn mỗi tệp khi ghi về máy
BATCH_FILES, BATCH_BYTES = 20, 25 * 1024 * 1024   # lô nhỏ để tránh quá thời gian ghi về máy
CBTT_DIR = "Công bố thông tin"
UA = {"User-Agent": "Mozilla/5.0 (ban-tin-lps deliver)"}


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def safe_name(name):
    name = nfc(unquote(name)).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + "." + ext) if dot else name[:150]
    return name or "tep"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(sess, urls, dest):
    last = ""
    for u in urls:
        if not u:
            continue
        try:
            with sess.get(u, stream=True, timeout=90, headers=UA) as r:
                if r.status_code != 200:
                    last = f"{u} -> HTTP {r.status_code}"
                    continue
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                tmp.replace(dest)
                return u, ""
        except requests.RequestException as e:
            last = f"{u} -> {type(e).__name__}"
    return None, last


def day_index(day, recs, file_rows):
    rows = []
    for r in recs:
        files = "".join(
            f'<li>{html.escape(fr["name"])}'
            + (f' <span class="m">SHA-256 {fr["sha256"][:16]}…</span>' if fr.get("sha256") else "")
            + (f' — <b>{html.escape(fr["note"])}</b>' if fr.get("note") else "")
            + f' · <a href="{html.escape(fr["url"])}">link gốc</a></li>'
            for fr in file_rows.get(r["id"], []))
        body = html.escape(nfc(r.get("body", ""))).replace("\n", "<br>")
        rows.append(f'''<article><h2><a href="{html.escape(r.get("url", ""))}">{html.escape(nfc(r.get("title", "")))}</a></h2>
<p class="m">Ngày đăng {r.get("date", "")} · Trạng thái: {html.escape(r.get("status", ""))}</p>
{f"<ul>{files}</ul>" if files else '<p class="m">Không có tệp đính kèm.</p>'}
<details><summary>Nội dung nguyên văn</summary><p>{body}</p></details></article>''')
    return f'''<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CBTT LPS {day}</title>
<style>body{{font:15px/1.55 Roboto,Arial,sans-serif;color:#222;background:#fff;max-width:900px;margin:0 auto;padding:16px}}
h1{{color:#6e361a;font-size:22px}}h2{{font-size:16px;margin:0 0 4px}}a{{color:#6e361a}}
article{{border:1px solid #eadfce;border-radius:6px;padding:12px 14px;margin:12px 0}}.m{{color:#777;font-size:13px}}
summary{{cursor:pointer;color:#6e361a;font-size:13px}}</style></head><body>
<h1>Công bố thông tin LPS — {day}</h1><p class="m">Nguồn: lpbs.com.vn · Tạo lúc {datetime.now(VN):%H:%M %d/%m/%Y}</p>
{"".join(rows)}</body></html>'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/mnt/user-data/outputs/tin-tuc")
    ap.add_argument("--days", type=int, default=3, help="số ngày gần nhất (theo ngày đăng)")
    ap.add_argument("--cbtt-dir", default="Công bố thông tin", help="tên thư mục con đúng như trên máy (giữ nguyên dạng Unicode)")
    ap.add_argument("--skip", default="", help="tệp JSON danh sách đường dẫn tương đối đã có trên máy, bỏ qua")
    a = ap.parse_args()
    cbtt_dir = a.cbtt_dir

    db = json.loads((BASE / "lps_db.json").read_text(encoding="utf-8"))
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    skip = set()
    if a.skip and Path(a.skip).exists():
        skip = {nfc(x).replace("\\", "/") for x in json.loads(Path(a.skip).read_text(encoding="utf-8"))}

    today = datetime.now(VN).date()
    since = (today - timedelta(days=a.days - 1)).isoformat()
    recs = [r for r in db.get("cbtt", []) if (r.get("date") or "") >= since]
    recs.sort(key=lambda r: (r.get("date", ""), r.get("title", "")), reverse=True)

    items, notes = [], []
    shutil.copy2(BASE / "ban-tin-lps.html", out / "ban-tin-lps.html")
    items.append({"rel": "ban-tin-lps.html", "size": (out / "ban-tin-lps.html").stat().st_size})

    sess = requests.Session()
    by_day, file_rows = {}, {}
    for r in recs:
        day = r.get("date") or (r.get("fetched") or "")[:10] or today.isoformat()
        by_day.setdefault(day, []).append(r)
        used = set()
        for f in r.get("files", []):
            if "lpbs.com.vn" not in f.get("url", "") and not f.get("archive"):
                continue
            name = safe_name(f["url"].rsplit("/", 1)[-1])
            if name.lower() in used:
                continue
            used.add(name.lower())
            rel = f"{cbtt_dir}/{day}/{name}"
            row = {"name": name, "url": f["url"], "sha256": f.get("sha256")}
            file_rows.setdefault(r["id"], []).append(row)
            if f.get("status") == 404:
                row["note"] = "tệp đã bị gỡ khỏi website"
                continue
            if (f.get("size") or 0) > MAX_FILE:
                row["note"] = f"tệp {f['size']/1048576:.0f} MB, quá giới hạn chuyển — mở bằng link gốc"
                notes.append(f"Bỏ qua (quá lớn): {rel}")
                continue
            if nfc(rel) in skip or nfc(rel).split("/", 1)[-1] in skip:
                continue
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src, err = download(sess, [f["url"]], dest)
            if not src:
                row["note"] = "chưa tải được"
                notes.append(f"Lỗi tải {rel}: {err}")
                continue
            sha = sha256_file(dest)
            if f.get("sha256") and sha != f["sha256"]:
                row["note"] = f"SHA-256 khác cơ sở dữ liệu ({sha[:16]}…)"
                notes.append(f"Lệch SHA-256: {rel}")
            size = dest.stat().st_size
            if size > MAX_FILE:
                dest.unlink()
                row["note"] = "tệp quá giới hạn chuyển — mở bằng link gốc"
                continue
            items.append({"rel": rel, "size": size, "sha256": sha})

    for day, rs in by_day.items():
        rel = f"{cbtt_dir}/{day}/_Danh sách văn bản.html"
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(day_index(day, rs, file_rows), encoding="utf-8")
        items.append({"rel": rel, "size": p.stat().st_size})

    batches, cur, cur_b = [], [], 0
    for it in items:
        if cur and (len(cur) >= BATCH_FILES or cur_b + it["size"] > BATCH_BYTES):
            batches.append(cur); cur, cur_b = [], 0
        cur.append({"stagedPath": str(out / it["rel"]), "rel": it["rel"].replace("/", "\\"), "size": it["size"]})
        cur_b += it["size"]
    if cur:
        batches.append(cur)

    manifest = {"generated": datetime.now(VN).isoformat(timespec="minutes"), "since": since,
                "documents": len(recs), "files": len(items), "batches": batches, "notes": notes,
                "db_last_updated": db.get("meta", {}).get("last_updated")}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "batches"} | {"batch_count": len(batches)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
