#!/usr/bin/env python3
"""
国家中小学智慧教育平台 - 电子教材数字图书馆
Flask Web 应用
"""

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

# ─── 配置 ───────────────────────────────────────────────

DB_PATH = Path("tch_material.db")
PDF_DIR = Path("textbook_pdfs")
COVER_DIR = Path("textbook_covers")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB

# Jinja2 自定义过滤器
@app.template_filter("basename")
def basename_filter(path):
    return os.path.basename(path) if path else ""


# ─── 排序辅助函数 ───────────────────────────────────────

def _sort_phases(phases):
    order = {
        "小学": 1,
        "小学（五四学制）": 2,
        "初中": 3,
        "初中（五四学制）": 4,
        "高中": 5,
        "特殊教育": 6,
    }
    return sorted(phases, key=lambda x: order.get(x, 99))


def _sort_grades(grades):
    mapping = {
        "一年级": 1,
        "二年级": 2,
        "三年级": 3,
        "四年级": 4,
        "五年级": 5,
        "六年级": 6,
        "七年级": 7,
        "八年级": 8,
        "九年级": 9,
        "高一年级": 10,
        "高二年级": 11,
        "高中年级": 12,
        "三至四年级": 13,
        "五至六年级": 14,
        "七至九年级": 15,
        "学生用书": 80,
        "教师用书": 81,
        "人工智能专册": 82,
        "中国历史": 90,
        "世界历史": 91,
    }
    return sorted(grades, key=lambda x: mapping.get(x, 99))


def _sort_subjects(subjects):
    priority = {
        "语文": 1,
        "数学": 2,
        "英语": 3,
        "英语（三年级起点）": 4,
        "物理": 5,
        "化学": 6,
        "生物": 7,
        "历史": 8,
        "地理": 9,
        "道德与法治": 10,
        "思想政治": 11,
        "科学": 12,
        "体育与健康": 13,
        "音乐": 14,
        "美术": 15,
        "信息技术": 16,
        "信息科技": 17,
        "通用技术": 18,
        "语文·书法练习指导": 19,
    }
    return sorted(subjects, key=lambda x: (priority.get(x, 50), x))


# ─── 数据库 ─────────────────────────────────────────────

def get_db():
    if "db" not in g:
        if not DB_PATH.exists():
            raise RuntimeError(
                f"数据库 {DB_PATH} 不存在，请先运行: python tch_material_crawler.py --collect"
            )
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


# ─── 路由 ───────────────────────────────────────────────

@app.route("/")
def index():
    """首页：搜索 + 分类筛选 + 卡片列表"""
    db = get_db()

    keyword = request.args.get("q", "").strip()
    phase = request.args.get("phase", "").strip()
    grade = request.args.get("grade", "").strip()
    subject = request.args.get("subject", "").strip()
    publisher = request.args.get("publisher", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 20

    # 构建查询
    conds, vals = [], []
    if keyword:
        conds.append("(t.title LIKE ? OR t.subject LIKE ? OR t.publisher LIKE ?)")
        vals.extend([f"%{keyword}%"] * 3)
    if phase:
        conds.append("t.phase = ?")
        vals.append(phase)
    if grade:
        conds.append("t.grade = ?")
        vals.append(grade)
    if subject:
        conds.append("t.subject = ?")
        vals.append(subject)
    if publisher:
        conds.append("t.publisher = ?")
        vals.append(publisher)

    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    total = db.execute(
        f"SELECT COUNT(*) FROM textbooks t{where}", vals
    ).fetchone()[0]

    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    books = [
        dict(r) for r in db.execute(
            f"SELECT t.* FROM textbooks t{where} ORDER BY t.is_pinned DESC, t.id LIMIT ? OFFSET ?",
            vals + [per_page, offset],
        ).fetchall()
    ]

    # ── 联动筛选选项 ──
    # 学段：始终全部
    phases = [r[0] for r in db.execute(
        "SELECT DISTINCT phase FROM textbooks WHERE phase IS NOT NULL AND phase != ''"
    ).fetchall()]
    phases = _sort_phases(phases)

    # 年级：受学段约束
    grade_conds, grade_vals = ["grade IS NOT NULL AND grade != ''"], []
    if phase:
        grade_conds.append("phase = ?")
        grade_vals.append(phase)
    grades = [r[0] for r in db.execute(
        f"SELECT DISTINCT grade FROM textbooks WHERE {' AND '.join(grade_conds)}",
        grade_vals,
    ).fetchall()]
    grades = _sort_grades(grades)

    # 学科：受学段+年级约束
    subj_conds, subj_vals = ["subject IS NOT NULL AND subject != ''"], []
    if phase:
        subj_conds.append("phase = ?")
        subj_vals.append(phase)
    if grade:
        subj_conds.append("grade = ?")
        subj_vals.append(grade)
    subjects = [r[0] for r in db.execute(
        f"SELECT DISTINCT subject FROM textbooks WHERE {' AND '.join(subj_conds)}",
        subj_vals,
    ).fetchall()]
    subjects = _sort_subjects(subjects)

    # 出版社：受学段+年级+学科约束
    pub_conds, pub_vals = ["publisher IS NOT NULL AND publisher != ''"], []
    if phase:
        pub_conds.append("phase = ?")
        pub_vals.append(phase)
    if grade:
        pub_conds.append("grade = ?")
        pub_vals.append(grade)
    if subject:
        pub_conds.append("subject = ?")
        pub_vals.append(subject)
    publishers = [r[0] for r in db.execute(
        f"SELECT DISTINCT publisher FROM textbooks WHERE {' AND '.join(pub_conds)} ORDER BY publisher",
        pub_vals,
    ).fetchall()]

    # 统计
    stats = {
        "total": db.execute("SELECT COUNT(*) FROM textbooks").fetchone()[0],
        "downloaded": db.execute("SELECT COUNT(*) FROM textbooks WHERE download_status = 'downloaded'").fetchone()[0],
    }

    return render_template(
        "index.html",
        books=books,
        keyword=keyword,
        current_phase=phase,
        current_grade=grade,
        current_subject=subject,
        current_publisher=publisher,
        phases=phases,
        grades=grades,
        subjects=subjects,
        publishers=publishers,
        page=page,
        total_pages=total_pages,
        total=total,
        stats=stats,
    )


@app.route("/book/<content_id>")
def book_detail(content_id):
    """教材详情页：信息 + 图片阅读器"""
    db = get_db()
    book = db.execute(
        "SELECT * FROM textbooks WHERE content_id = ?", (content_id,)
    ).fetchone()

    if not book:
        abort(404)

    book = dict(book)

    # 获取本地页面图片列表
    pages = []
    if book.get("pdf_path") and Path(book["pdf_path"]).is_dir():
        for img in sorted(Path(book["pdf_path"]).glob("page_*.jpg")):
            pages.append(img.name)

    return render_template(
        "viewer.html",
        book=book,
        pages=pages,
        oss_base="http://image.caizhidao.cc/",
        oss_pages_dir=Path(book["pdf_path"]).name if book.get("pdf_path") else "",
    )


@app.route("/pages/<content_id>/<path:filename>")
def serve_page(content_id, filename):
    """页面图片服务"""
    db = get_db()
    book = db.execute(
        "SELECT pdf_path FROM textbooks WHERE content_id = ?", (content_id,)
    ).fetchone()
    if not book or not book["pdf_path"]:
        abort(404)
    book_dir = Path(book["pdf_path"])
    if not book_dir.is_dir():
        abort(404)
    return send_from_directory(str(book_dir.absolute()), filename)


@app.route("/cover/<path:filename>")
def serve_cover(filename):
    """封面图片服务"""
    if COVER_DIR.exists():
        return send_from_directory(str(COVER_DIR.absolute()), filename)
    abort(404)


@app.route("/api/img-proxy")
def img_proxy():
    """图片代理（解决跨域）"""
    url = request.args.get("url", "").strip()
    if not url:
        abort(400)

    allowed_domains = ["caizhidao.cc"]
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not any(parsed.netloc.endswith(d) for d in allowed_domains):
        abort(403)

    cache_dir = Path("img_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(url.encode()).hexdigest()
    cache_path = cache_dir / f"{cache_key}.jpg"

    if cache_path.exists():
        return send_from_directory(str(cache_dir.absolute()), f"{cache_key}.jpg",
                                   mimetype="image/jpeg")

    try:
        import requests as req
        resp = req.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        resp.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(resp.content)
    except Exception as e:
        print(f"[img-proxy] 请求失败: {url} | 错误: {e}")
        abort(502)

    return send_from_directory(str(cache_dir.absolute()), f"{cache_key}.jpg",
                               mimetype="image/jpeg")


@app.route("/api/books")
def api_books():
    """JSON 接口：教材列表"""
    db = get_db()
    keyword = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(50, max(1, int(request.args.get("per_page", 20))))

    conds, vals = [], []
    if keyword:
        conds.append("(title LIKE ? OR subject LIKE ? OR publisher LIKE ?)")
        vals.extend([f"%{keyword}%"] * 3)

    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = db.execute(f"SELECT COUNT(*) FROM textbooks{where}", vals).fetchone()[0]
    offset = (page - 1) * per_page
    books = [
        dict(r) for r in db.execute(
            f"SELECT * FROM textbooks{where} ORDER BY is_pinned DESC, id LIMIT ? OFFSET ?",
            vals + [per_page, offset],
        ).fetchall()
    ]

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "books": books,
    })


# ─── 启动 ───────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    debug = os.environ.get("FLASK_ENV") == "development"

    if DB_PATH.exists():
        db = sqlite3.connect(str(DB_PATH))
        count = db.execute("SELECT COUNT(*) FROM textbooks").fetchone()[0]
        db.close()
        print(f"数据库已加载: {count} 本教材")
    else:
        print(f"警告: 数据库 {DB_PATH} 不存在")
        print(f"请先运行: python tch_material_crawler.py --collect")

    app.run(debug=debug, host=host, port=port)
