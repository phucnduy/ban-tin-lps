# -*- coding: utf-8 -*-
"""
Quy tắc phân loại và sàng lọc cho Bản tin LPS. Sửa file này để thêm/bớt nhãn mà không động vào update_lps.py.

Mỗi văn bản có thể mang NHIỀU nhãn (ví dụ: "Nghị quyết HĐQT" + "Tín dụng ngân hàng").
Quy tắc so khớp trên tiêu đề; một số nhãn xét thêm vài dòng đầu nội dung (cột "body").
Biểu thức chính quy viết thường, không phân biệt hoa/thường.
"""
import re
import unicodedata
import datetime as dt


def nfc(s):
    """Website dùng lẫn chữ dựng sẵn và chữ tổ hợp (NFD); chuẩn hóa để so khớp ổn định."""
    return unicodedata.normalize("NFC", s or "")

# (Tên nhãn, biểu thức áp lên tiêu đề, biểu thức áp lên đoạn đầu nội dung hoặc None)
CATEGORY_RULES = [
    ("Nghị quyết HĐQT",
     r"nghị quyết\s*(của\s*)?(hđqt|hội đồng quản trị)|hđqt\s*(đã\s*)?(ban hành|thông qua)\s*nghị quyết|nq[-/ ]?hđqt|"
     r"quyết định\s*(của\s*)?(hđqt|hội đồng quản trị)",
     r"^(ngày[^\n]{0,30},\s*)?(hội đồng quản trị|hđqt)[^\n]{0,120}(ban hành|thông qua)[^\n]{0,20}(nghị quyết|quyết định)"),
    ("Báo cáo tài chính",
     r"báo cáo tài chính|\bbctc\b|giải trình[^,]{0,40}(lợi nhuận|kết quả kinh doanh|báo cáo tài chính|bctc|chênh lệch)|"
     r"báo cáo kết quả (hoạt động )?kinh doanh|(tỷ lệ |báo cáo )an toàn tài chính|báo cáo thường n[iê]+n|soát xét|kiểm toán",
     None),
    ("Trái phiếu", r"trái phiếu", None),
    ("Chứng quyền", r"chứng quyền|\bcw\b|covered warrant", None),
    ("Giải chấp & giao dịch người nội bộ",
     r"giải chấp|người nội bộ|người có liên quan|cổ đông lớn|cổ đông sáng lập|"
     r"(thông báo|báo cáo)( kết quả)? giao dịch( cổ phiếu)?( của)?|thay đổi sở hữu|sở hữu (cổ phiếu|chứng chỉ)",
     None),
    ("Nhân sự",
     r"bổ nhiệm|miễn nhiệm|từ nhiệm|bãi nhiệm|\bbầu\b|thay đổi nhân sự|nhân sự|tổng giám đốc|giám đốc|chủ tịch|"
     r"phó chủ tịch|thành viên (hđqt|hội đồng quản trị|ban kiểm soát)|kế toán trưởng|ban kiểm soát|"
     r"người phụ trách quản trị|người (được )?ủy quyền công bố|người đại diện",
     None),
    ("Đại hội cổ đông",
     r"đại hội đồng cổ đông|đhđcđ|đhcđ|đại hội cổ đông|lấy ý kiến cổ đông|"
     r"chốt danh sách cổ đông|ngày đăng ký cuối cùng|thư mời họp|tài liệu họp|biên bản họp đại hội",
     None),
    ("Tín dụng ngân hàng",
     r"tín dụng|hạn mức (vay|tín dụng|cấp tín dụng)|khoản vay|vay vốn|thấu chi|hợp đồng vay|vay hạn mức",
     None),
    # Các nhãn bổ sung (ngoài 8 nhãn yêu cầu ban đầu), hữu ích cho kiểm toán và giảm số văn bản rơi vào "Khác":
    ("Giao dịch với bên liên quan",
     r"bên (có )?liên quan|với ngân hàng tmcp lộc phát|với lpbank|với lpb\b|(lpbank|lpb)\s*[–-]\s*bên|"
     r"hợp đồng tiền gửi|hợp đồng quản lý cổ đông|ngân hàng tmcp bưu điện liên việt|của ngân hàng tmcp lộc phát",
     None),
    ("Cơ quan quản lý & xử phạt",
     r"xử phạt|vi phạm|ủy ban chứng khoán|\bubck|sở giao dịch chứng khoán|\bhose\b|\bhnx\b|công văn|"
     r"quyết định số|chấp thuận",
     None),
    ("Quản trị công ty",
     r"tình hình quản trị|quản trị công ty|báo cáo quản tr[iị]|(thực hiện|ủy quyền|được ủy quyền) công bố thông tin|"
     r"quyền biểu quyết|tỷ lệ sở hữu nước ngoài|công ty đại chúng",
     None),
    ("Giấy phép, vận hành & mạng lưới",
     r"giấy chứng nhận|giấy phép|chi nhánh|phòng giao dịch|văn phòng đại diện|(mẫu )?con dấu|mẫu dấu|trụ sở|đổi tên|"
     r"điều lệ|quy chế|ngành nghề|nghiệp vụ|thành viên (giao dịch|lưu ký|bù trừ)|tài khoản (chuyên dụng|phong tỏa)|"
     r"\btk chuyên dụng|tài khoản tiền gửi[^\n]{0,30}chuyên dụng|kết nối giao dịch|giao dịch (chứng khoán )?trực tuyến|cung cấp dịch vụ",
     None),
    ("Vốn, cổ phiếu & niêm yết",
     r"tăng vốn|vốn điều lệ|phát hành (thêm )?cổ phiếu|chào bán cổ phiếu|\bipo\b|niêm yết|đăng ký giao dịch|"
     r"lưu ký cổ phiếu|cổ tức|cổ phiếu quỹ|quyền mua|chào bán|chuyển nhượng cổ phần",
     None),
]
OTHER_LABEL = "Khác"

_COMPILED = [(name, re.compile(t, re.I), re.compile(b, re.I | re.M) if b else None) for name, t, b in CATEGORY_RULES]


def classify(title, body=""):
    title = nfc(title)
    head = "\n".join(nfc(body).split("\n")[:3])
    tags = []
    for name, rt, rb in _COMPILED:
        if rt.search(title) or (rb is not None and rb.search(head)):
            tags.append(name)
    return tags or [OTHER_LABEL]


# ------------------------------------------------------------------ SÀNG LỌC TRỄ HẠN
# Thời hạn công bố thông tin bất thường: 24 giờ (Điều 11 – công ty đại chúng, Điều 23 – công ty chứng khoán,
# Thông tư 96/2020/TT-BTC, đã sửa đổi bởi Thông tư 68/2024/TT-BTC và Thông tư 08/2026/TT-BTC).
# Cần tự đối chiếu văn bản đang có hiệu lực. Vì chỉ biết NGÀY ban hành (không biết giờ), script chỉ đánh dấu khi
# ngày đăng cách ngày ban hành từ 2 ngày lịch trở lên — khi đó chắc chắn đã quá 24 giờ.
DEADLINE_DAYS = 2
# Nhãn có thời hạn riêng (báo cáo định kỳ) hoặc ngày nêu trong nội dung không phải ngày phát sinh sự kiện:
LATE_CHECK_EXCLUDE = {"Báo cáo tài chính", "Đại hội cổ đông"}
LATE_CHECK_EXCLUDE_TITLE = re.compile(r"giải chấp|báo cáo tình hình quản trị|báo cáo quản trị|danh mục|định kỳ", re.I)

_DATE_PATTERNS = [
    re.compile(r"ngày\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})", re.I),
    re.compile(r"ngày\s*(\d{1,2})\s*tháng\s*(\d{1,2})\s*năm\s*(\d{4})", re.I),
]
# Động từ cho thấy ngày đó là ngày phát sinh sự kiện (ban hành, ký, thông qua, nhận được…)
_EVENT_VERB = re.compile(r"ban hành|thông qua|ký|nhận được|cấp|chấp thuận|quyết định|phê duyệt|bổ nhiệm|miễn nhiệm|"
                         r"phát hành|hoàn tất|công bố|issued|approved|signed|received", re.I)


_EXTERNAL_DOC = re.compile(r"ủy ban chứng khoán|\bubck|sở giao dịch|sgdhcm|sgdhn|tổng cục|bộ tài chính|"
                           r"/qđ-|/gp|/gcn|/tb-sgd|sở kế hoạch|vsdc|tổng công ty lưu ký", re.I)


def _to_date(d, m, y):
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def extract_event_date(title, body, posted):
    """Trả về (ngày phát sinh, đoạn trích làm căn cứ) hoặc (None, None).
    Chỉ nhận ngày nằm trong 60 ngày trước ngày đăng và không sau ngày đăng."""
    if not posted:
        return None, None
    title, body = nfc(title), nfc(body)
    try:
        p = dt.date.fromisoformat(posted[:10])
    except ValueError:
        return None, None
    candidates = []
    # 1) Ngày trong tiêu đề (vd. "Nghị quyết HĐQT số 117A ngày 15/10/2026")
    for rx in _DATE_PATTERNS:
        for m in rx.finditer(title or ""):
            candidates.append((_to_date(*m.groups()), (title or "")[max(0, m.start() - 40): m.end() + 40]))
    # 2) Câu đầu nội dung có động từ sự kiện
    for line in (body or "").split("\n")[:4]:
        for rx in _DATE_PATTERNS:
            for m in rx.finditer(line):
                window = line[m.start(): m.start() + 220]
                if _EVENT_VERB.search(window):
                    candidates.append((_to_date(*m.groups()), line[max(0, m.start() - 20): m.end() + 140]))
    for d, ctx in candidates:
        if d and d <= p and (p - d).days <= 60:
            return d, re.sub(r"\s+", " ", ctx).strip()
    return None, None


def late_check(title, body, posted, tags, hose_posted=None):
    """Trả về dict sàng lọc trễ hạn, hoặc None nếu không áp dụng / không xác định được ngày phát sinh."""
    if set(tags) & LATE_CHECK_EXCLUDE or LATE_CHECK_EXCLUDE_TITLE.search(nfc(title)):
        return None
    ev, ctx = extract_event_date(title, body, posted)
    if not ev:
        return None
    web_gap = (dt.date.fromisoformat(posted[:10]) - ev).days
    res = {"event_date": ev.isoformat(), "evidence": ctx, "web_gap_days": web_gap,
           "web_flag": web_gap >= DEADLINE_DAYS,
           # Ngày ghi là ngày ký của cơ quan bên ngoài: thời hạn 24 giờ tính từ khi LPS NHẬN văn bản, có thể muộn hơn
           "external": bool(_EXTERNAL_DOC.search(ctx or ""))}
    if hose_posted:
        hose_gap = (dt.date.fromisoformat(hose_posted[:10]) - ev).days
        res.update({"hose_gap_days": hose_gap, "hose_flag": hose_gap >= DEADLINE_DAYS})
    return res
