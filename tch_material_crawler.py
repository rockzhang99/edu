#!/usr/bin/env python3
"""
国家中小学智慧教育平台电子教材采集器
基于 Playwright 模拟浏览器采集教材元数据和PDF文件
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ─── 配置 ───────────────────────────────────────────────

BASE_URL = "https://basic.smartedu.cn/tchMaterial"
LIST_URL_TPL = "https://s-file-{}.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/part_{}.json"
DETAIL_URL_TPL = "https://s-file-{}.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{}.json"
THEMATIC_RES_LIST_TPL = "https://s-file-{}.ykt.cbern.com.cn/zxx/ndrs/special_edu/thematic_course/{}/resources/list.json"
THEMATIC_DETAIL_TPL = "https://s-file-{}.ykt.cbern.com.cn/zxx/ndrs/special_edu/resources/details/{}.json"
CDN_SERVERS = [1, 2, 3]
LIST_PART_MIN = 101
LIST_PART_MAX = 130  # part_101 ~ part_130，按需调整

MIN_DELAY = 1.0
MAX_DELAY = 3.0
BATCH_SIZE = 50
BATCH_REST_MIN = 10
BATCH_REST_MAX = 30
LARGE_BATCH_SIZE = 200
LARGE_REST_MIN = 60
LARGE_REST_MAX = 180

DB_PATH = Path("tch_material.db")
PDF_DIR = Path("textbook_pdfs")
COVER_DIR = Path("textbook_covers")

DB_PATH = Path("tch_material.db")
PDF_DIR = Path("textbook_pdfs")
COVER_DIR = Path("textbook_covers")
LOG_DIR = Path("logs")
PLACEHOLDER_WECHAT_URL = "https://pub-e612e869d3b1475bbff4637b6351fee7.r2.dev/wechat.png"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _setup_file_logger() -> logging.FileHandler:
    """创建按日期滚动的文件日志 Handler"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"crawl_{datetime.now().strftime('%Y%m%d')}.log"
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    return fh

# ─── 数据库管理 ─────────────────────────────────────────

class TchMaterialDB:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS textbooks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id      TEXT UNIQUE,
                title           TEXT NOT NULL,
                subject         TEXT,
                phase           TEXT,
                grade           TEXT,
                semester        TEXT,
                publisher       TEXT,
                cover_path      TEXT,
                pdf_path        TEXT,
                pdf_url         TEXT,
                r2_pdf_url      TEXT,
                page_count      INTEGER,
                file_size       INTEGER,
                download_status TEXT DEFAULT 'pending',
                detail_json     TEXT,
                is_pinned       INTEGER DEFAULT 0,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS categories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                parent_id   INTEGER REFERENCES categories(id),
                level       INTEGER,
                tag_code    TEXT UNIQUE,
                sort_order  INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_textbooks_content_id ON textbooks(content_id);
            CREATE INDEX IF NOT EXISTS idx_textbooks_subject    ON textbooks(subject);
            CREATE INDEX IF NOT EXISTS idx_textbooks_phase      ON textbooks(phase);
            CREATE INDEX IF NOT EXISTS idx_textbooks_grade      ON textbooks(grade);
            CREATE INDEX IF NOT EXISTS idx_textbooks_status     ON textbooks(download_status);
            CREATE INDEX IF NOT EXISTS idx_categories_tag_code  ON categories(tag_code);
        """)
        self.conn.commit()

    # ── 教材 CRUD ──

    def upsert_textbook(self, **kwargs) -> int:
        content_id = kwargs.get("content_id")
        if not content_id:
            raise ValueError("content_id is required")
        existing = self.conn.execute(
            "SELECT id FROM textbooks WHERE content_id = ?", (content_id,)
        ).fetchone()
        if existing:
            kwargs["updated_at"] = datetime.now().isoformat()
            sets = ", ".join(f"{k} = ?" for k in kwargs if k != "content_id")
            vals = [kwargs[k] for k in kwargs if k != "content_id"]
            vals.append(content_id)
            self.conn.execute(f"UPDATE textbooks SET {sets} WHERE content_id = ?", vals)
            self.conn.commit()
            return existing["id"]
        else:
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            vals = list(kwargs.values())
            cur = self.conn.execute(
                f"INSERT INTO textbooks ({cols}) VALUES ({placeholders})", vals
            )
            self.conn.commit()
            return cur.lastrowid

    def get_textbook(self, content_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM textbooks WHERE content_id = ?", (content_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_pending_textbooks(self, limit: int = 0) -> list[dict]:
        sql = "SELECT * FROM textbooks WHERE download_status = 'pending' ORDER BY id"
        if limit:
            sql += f" LIMIT {limit}"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def update_download_status(self, content_id: str, status: str, **extra):
        parts = ["download_status = ?", "updated_at = ?"]
        vals = [status, datetime.now().isoformat()]
        for k, v in extra.items():
            parts.append(f"{k} = ?")
            vals.append(v)
        vals.append(content_id)
        self.conn.execute(
            f"UPDATE textbooks SET {', '.join(parts)} WHERE content_id = ?", vals
        )
        self.conn.commit()

    def search(self, keyword: str = "", phase: str = "", subject: str = "",
               grade: str = "", page: int = 1, per_page: int = 20) -> tuple[list[dict], int]:
        conds, vals = [], []
        if keyword:
            conds.append("(title LIKE ? OR subject LIKE ? OR publisher LIKE ?)")
            vals.extend([f"%{keyword}%"] * 3)
        if phase:
            conds.append("phase = ?")
            vals.append(phase)
        if subject:
            conds.append("subject = ?")
            vals.append(subject)
        if grade:
            conds.append("grade = ?")
            vals.append(grade)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM textbooks{where}", vals
        ).fetchone()[0]
        offset = (page - 1) * per_page
        rows = self.conn.execute(
            f"SELECT * FROM textbooks{where} ORDER BY is_pinned DESC, id LIMIT ? OFFSET ?",
            vals + [per_page, offset],
        ).fetchall()
        return [dict(r) for r in rows], total

    def get_distinct(self, column: str) -> list[str]:
        rows = self.conn.execute(
            f"SELECT DISTINCT {column} FROM textbooks WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
        ).fetchall()
        return [r[0] for r in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM textbooks").fetchone()[0]

    def count_by_status(self, status: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM textbooks WHERE download_status = ?", (status,)
        ).fetchone()[0]

    # ── 分类 CRUD ──

    def upsert_category(self, name: str, tag_code: str, level: int,
                        parent_id: int = 0, sort_order: int = 0) -> int:
        existing = self.conn.execute(
            "SELECT id FROM categories WHERE tag_code = ?", (tag_code,)
        ).fetchone()
        if existing:
            return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO categories (name, parent_id, level, tag_code, sort_order) VALUES (?, ?, ?, ?, ?)",
            (name, parent_id, level, tag_code, sort_order),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_categories(self, parent_id: int = 0) -> list[dict]:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM categories WHERE parent_id = ? ORDER BY sort_order, id",
                (parent_id,),
            ).fetchall()
        ]

    def close(self):
        if self.conn:
            self.conn.close()


# ─── 采集器 ─────────────────────────────────────────────

class TchMaterialCrawler:
    def __init__(self):
        self.db = TchMaterialDB()
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.intercepted_data: list[dict] = []
        self.discovered_list_api: str | None = None
        self._ensure_dirs()

    @staticmethod
    def _ensure_dirs():
        for d in (PDF_DIR, COVER_DIR):
            d.mkdir(parents=True, exist_ok=True)

    # ── 浏览器管理 ──

    def start_browser(self, headless: bool = True, force_channel: str = None):
        from playwright.sync_api import sync_playwright

        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass

        self.pw = sync_playwright().start()

        channels = ["chrome", "msedge", "chromium"]
        if force_channel:
            channels.insert(0, force_channel)

        for ch in channels:
            try:
                launch_args = {
                    "headless": headless,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                }
                if ch != "chromium":
                    launch_args["channel"] = ch
                self.browser = self.pw.chromium.launch(**launch_args)
                break
            except Exception as e:
                logger.warning(f"启动 {ch} 失败: {e}")
                continue

        if not self.browser:
            raise RuntimeError("无法启动任何浏览器")

        self.context = self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=self._random_ua(),
            locale="zh-CN",
        )

        self.page = self.context.new_page()

        # 反检测脚本
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'chrome', { get: () => ({ runtime: {} }) });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        # 拦截响应
        self.page.on("response", self._on_response)
        logger.info(f"浏览器已启动")

    @staticmethod
    def _random_ua() -> str:
        uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        ]
        return random.choice(uas)

    @staticmethod
    def _delay(min_s=MIN_DELAY, max_s=MAX_DELAY):
        time.sleep(random.uniform(min_s, max_s))

    # ── 响应拦截 ──

    def _on_response(self, response):
        url = response.url
        try:
            if "tch_material" in url and url.endswith(".json"):
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    data = response.json()
                    self._process_intercepted(url, data)
        except Exception:
            pass

    def _process_intercepted(self, url: str, data):
        """处理拦截到的JSON数据，提取教材信息"""
        # 检测教材列表数据
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # 可能的字段名：list, items, data, content, rows, records
            for key in ("list", "items", "data", "content", "rows", "records", "result"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break

        if not items:
            # 可能是单本教材详情
            if "ti_items" in data or "content_id" in data or "global_content_id" in data:
                self._process_detail(data, url)
            return

        for item in items:
            if isinstance(item, dict):
                self._process_list_item(item)

        logger.info(f"拦截到列表数据 [{url[:80]}...] 共 {len(items)} 条")

    def _process_list_item(self, item: dict):
        """从列表项提取教材元数据"""
        content_id = (
            item.get("id")
            or item.get("global_resource_id")
            or item.get("version_id")
            or ""
        )
        # 确保是 UUID 格式
        uuid_pattern = re.compile(
            r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
        )
        if not uuid_pattern.match(str(content_id)):
            return

        title = item.get("title") or ""
        if not title:
            return

        # 通过 tag_list + tag_dimension_id 提取分类信息
        subject = phase = grade = semester = publisher = ""
        tag_list = item.get("tag_list", []) or []
        tag_dim_map = {
            "zxxxd": "phase",     # 学段
            "zxxxk": "subject",   # 学科
            "zxxnj": "grade",     # 年级
            "zxxbb": "publisher", # 版本/出版社简写
            "zxxcc": "semester",  # 册别
        }
        for tag in tag_list:
            if isinstance(tag, dict):
                dim_id = tag.get("tag_dimension_id", "")
                tag_name = tag.get("tag_name", "")
                field = tag_dim_map.get(dim_id)
                if field and tag_name:
                    locals_dict = {"phase": phase, "subject": subject,
                                  "grade": grade, "publisher": publisher,
                                  "semester": semester}
                    if not locals_dict[field]:
                        if field == "phase":
                            phase = tag_name
                        elif field == "subject":
                            subject = tag_name
                        elif field == "grade":
                            grade = tag_name
                        elif field == "publisher":
                            publisher = tag_name
                        elif field == "semester":
                            semester = tag_name

        # provider_list 中的出版社名称更完整
        provider_list = item.get("provider_list", []) or []
        if provider_list and isinstance(provider_list[0], dict):
            provider_name = provider_list[0].get("name", "")
            if provider_name:
                publisher = provider_name

        # 文件大小
        file_size = item.get("size") or 0

        try:
            self.db.upsert_textbook(
                content_id=content_id,
                title=title,
                subject=subject,
                phase=phase,
                grade=grade,
                semester=semester,
                publisher=publisher,
                file_size=file_size or None,
            )
        except Exception as e:
            logger.warning(f"保存教材失败 [{title}]: {e}")

    def _process_detail(self, data: dict, url: str):
        """处理单本教材详情数据"""
        content_id = ""
        for candidate in ("content_id", "global_content_id", "id"):
            if candidate in data:
                content_id = str(data[candidate])
                break
        if not content_id:
            # 从 URL 提取
            match = re.search(
                r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", url
            )
            if match:
                content_id = match.group(0)
        if content_id:
            # 保存详情 JSON
            existing = self.db.get_textbook(content_id)
            if existing:
                self.db.upsert_textbook(
                    content_id=content_id,
                    detail_json=json.dumps(data, ensure_ascii=False),
                    page_count=data.get("page_count") or data.get("ti_pages"),
                )

    # ── Phase 1: 元数据采集 ──

    def collect_metadata(self, max_books: int = 0):
        """Phase 1: 采集全部教材元数据
        优先使用直接请求 part_*.json（高效），Playwright 作为备选"""
        logger.info("=" * 60)
        logger.info("开始采集教材元数据...")
        logger.info("=" * 60)

        initial_count = self.db.count()
        logger.info(f"采集前数据库已有 {initial_count} 本教材")

        # 策略1（优先）: 直接请求 part_*.json 文件
        self._collect_from_part_files(max_books)

        final_count = self.db.count()
        new_count = final_count - initial_count
        logger.info(f"元数据采集完成！新增 {new_count} 本，总计 {final_count} 本")

    def _collect_from_part_files(self, max_books: int = 0):
        """直接请求 part_*.json 分片文件采集元数据"""
        logger.info("通过 part_*.json 文件采集元数据...")

        for part_num in range(LIST_PART_MIN, LIST_PART_MAX + 1):
            for cdn in CDN_SERVERS:
                url = LIST_URL_TPL.format(cdn, part_num)
                try:
                    resp = requests.get(url, timeout=30, headers={
                        "User-Agent": self._random_ua(),
                        "Referer": BASE_URL,
                    })
                    if resp.status_code != 200:
                        logger.debug(f"  part_{part_num} CDN-{cdn} 返回 {resp.status_code}")
                        continue

                    data = resp.json()
                    items = data if isinstance(data, list) else []
                    if not items:
                        logger.debug(f"  part_{part_num} 为空，跳过")
                        break  # 此 CDN 无此分片，换下一个也大概率没有

                    for item in items:
                        if isinstance(item, dict):
                            self._process_list_item(item)

                    current = self.db.count()
                    logger.info(f"  part_{part_num}: {len(items)} 条，累计 {current} 本")

                    # 达到上限
                    if max_books and current >= max_books:
                        logger.info(f"  已达上限 {max_books}，停止采集")
                        return

                    self._delay(0.3, 0.8)
                    break  # 此 CDN 成功，不需要尝试其他 CDN

                except requests.exceptions.RequestException as e:
                    logger.debug(f"  part_{part_num} CDN-{cdn} 请求失败: {e}")
                    continue
                except Exception as e:
                    logger.warning(f"  part_{part_num} 解析失败: {e}")
                    break  # 解析失败，换下一个 part

        logger.info("part_*.json 采集完毕")

    def _collect_from_playwright(self, max_books: int = 0):
        """使用 Playwright 采集（备选方案）"""
        if not self.page:
            self.start_browser()

        logger.info(f"正在打开 {BASE_URL} ...")
        self.page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        self._delay(3, 5)

        try:
            self.page.wait_for_selector("[class*='card'], [class*='item']", timeout=15000)
        except Exception:
            logger.warning("未检测到教材卡片，尝试继续...")

        self._scroll_to_load_all()
        self._click_through_filters(max_books)

    def _scroll_to_load_all(self):
        """滚动页面触发懒加载"""
        logger.info("滚动页面加载更多教材...")
        prev_count = 0
        no_new_count = 0

        for i in range(500):  # 最多滚动500次
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self._delay(0.5, 1.5)

            current_count = self.db.count()
            if current_count > prev_count:
                if i % 20 == 0:
                    logger.info(f"  滚动第 {i} 次，当前 {current_count} 本")
                prev_count = current_count
                no_new_count = 0
            else:
                no_new_count += 1
                if no_new_count >= 10:
                    logger.info(f"  滚动 {i} 次后无新数据，停止滚动")
                    break

    def _click_through_filters(self, max_books: int = 0):
        """遍历分类筛选器（学段/学科/年级/版本）"""
        logger.info("遍历分类筛选器...")

        # 尝试找到筛选器按钮/标签
        filter_selectors = [
            ".filter-item",
            ".tag-item",
            "[class*='filter']",
            "[class*='tag']",
            "[class*='tab']",
            ".ant-tag",
            ".fish-tag",
        ]

        for selector in filter_selectors:
            try:
                elements = self.page.query_selector_all(selector)
                if elements and len(elements) > 2:
                    logger.info(f"  找到筛选器 [{selector}]，共 {len(elements)} 个选项")
                    for idx, el in enumerate(elements):
                        try:
                            el.click()
                            self._delay(2, 4)
                            self.page.wait_for_load_state("networkidle", timeout=10000)
                            current = self.db.count()
                            if max_books and current >= max_books:
                                logger.info(f"  已达上限 {max_books}，停止遍历")
                                return
                        except Exception as e:
                            logger.debug(f"  点击筛选器选项失败: {e}")
                            continue
                    break
            except Exception:
                continue

        # 尝试下拉选择器方式
        select_selectors = ["select", ".ant-select", ".fish-select"]
        for selector in select_selectors:
            try:
                elements = self.page.query_selector_all(selector)
                if elements:
                    logger.info(f"  找到下拉选择器 [{selector}]，共 {len(elements)} 个")
                    # 对每个下拉框，获取所有选项
                    for el in elements:
                        try:
                            el.click()
                            self._delay(1, 2)
                            options = self.page.query_selector_all(".ant-select-item, option, li")
                            for opt in options:
                                try:
                                    opt.click()
                                    self._delay(2, 4)
                                    self.page.wait_for_load_state("networkidle", timeout=10000)
                                    if max_books and self.db.count() >= max_books:
                                        return
                                except Exception:
                                    continue
                        except Exception:
                            continue
                    break
            except Exception:
                continue

    # ── Phase 2: 下载页面图片 ──

    def download_pdfs(self, max_books: int = 0, start_from: int = 0):
        """Phase 2: 下载教材页面图片（PDF已不可直接下载，改为下载转码图片）"""
        logger.info("=" * 60)
        logger.info("开始下载教材页面图片...")
        logger.info("=" * 60)

        pending = self.db.get_pending_textbooks()
        if start_from:
            pending = pending[start_from:]
        if max_books:
            pending = pending[:max_books]

        logger.info(f"待下载: {len(pending)} 本")

        for i, book in enumerate(pending):
            content_id = book["content_id"]
            title = book["title"]
            logger.info(f"[{i+1}/{len(pending)}] 下载: {title} ({content_id})")

            try:
                page_urls, reason = self._get_page_urls(content_id)
                if not page_urls:
                    if reason == "no_thematic_doc":
                        logger.warning(f"  专题课程无独立文档资源（疑似重复条目），跳过")
                        self.db.update_download_status(content_id, "skipped")
                    else:
                        logger.warning(f"  未找到页面图片链接，跳过")
                        self.db.update_download_status(content_id, "failed")
                    continue

                # 清理文件名中的非法字符，并用 content_id 确保唯一性
                safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
                book_dir = PDF_DIR / f"{safe_title}_{content_id[:8]}"
                book_dir.mkdir(parents=True, exist_ok=True)

                # 下载所有页面图片
                total_size = 0
                failed_pages = []
                for page_num, img_url in page_urls.items():
                    img_path = book_dir / f"page_{page_num:04d}.jpg"
                    if img_path.exists():
                        total_size += img_path.stat().st_size
                        continue
                    try:
                        self._download_file(img_url, img_path)
                        file_size = img_path.stat().st_size if img_path.exists() else 0
                        # 检测是否为有效图片（非 HTML 错误页）
                        if file_size < 500 or not self._is_valid_jpg(img_path):
                            logger.warning(f"  页面 {page_num} 下载内容无效({file_size}B)，使用占位图")
                            self._generate_placeholder(img_path, page_num)
                            failed_pages.append(page_num)
                        else:
                            total_size += file_size
                    except Exception as e:
                        logger.warning(f"  页面 {page_num} 下载失败: {e}，使用占位图")
                        self._generate_placeholder(img_path, page_num)
                        failed_pages.append(page_num)

                if failed_pages:
                    logger.warning(f"  共 {len(failed_pages)} 页下载失败并已替换为占位图: {failed_pages}")

                # 下载封面（第1页），用 content_id 确保唯一
                cover_path = COVER_DIR / f"{safe_title}_{content_id[:8]}.jpg"
                first_page = book_dir / "page_0001.jpg"
                if first_page.exists():
                    import shutil
                    shutil.copy2(first_page, cover_path)

                downloaded_pages = len(list(book_dir.glob("*.jpg")))
                self.db.update_download_status(
                    content_id,
                    "downloaded",
                    pdf_path=str(book_dir),  # 存储目录路径
                    page_count=downloaded_pages,
                    cover_path=str(cover_path) if cover_path.exists() else None,
                    file_size=total_size or None,
                )
                logger.info(f"  下载完成: {downloaded_pages} 页, {total_size/1024/1024:.1f}MB")

            except Exception as e:
                logger.error(f"  下载失败: {e}")
                self.db.update_download_status(content_id, "failed")

            # 延迟与休息策略
            self._delay()

            if (i + 1) % BATCH_SIZE == 0:
                rest = random.uniform(BATCH_REST_MIN, BATCH_REST_MAX)
                logger.info(f"  已处理 {i+1} 本，休息 {rest:.0f}s ...")
                time.sleep(rest)

        logger.info("页面图片下载完成！")

    def _get_page_urls(self, content_id: str) -> tuple[dict[int, str], str]:
        """通过详情 API 获取所有页面图片URL
        
        策略：
        1. 从 ti_items 中找 ti_file_flag="image" 的文件夹路径，推导全部页码
        2. preview 中的 Slide 仅含部分缩略图，不可靠
        3. 用 HEAD 请求逐页验证，遇到 403 停止
        """
        # 先尝试标准 API，失败则降级到 thematic_course API
        for x in CDN_SERVERS:
            url = DETAIL_URL_TPL.format(x, content_id)
            try:
                resp = requests.get(url, timeout=15, headers={
                    "User-Agent": self._random_ua(),
                    "Referer": BASE_URL,
                })
                if resp.status_code != 200:
                    continue

                data = resp.json()

                # 从 ti_items 获取图片文件夹基础路径
                image_folder_base = None
                for item in data.get("ti_items", []):
                    if item.get("ti_file_flag") == "image" and item.get("ti_format") == "folder":
                        storages = item.get("ti_storages", [])
                        if storages:
                            raw_url = storages[0]
                            # 替换 ndr-private 为 ndr（公开访问）
                            image_folder_base = raw_url.replace("ndr-private", "ndr")
                            break

                if not image_folder_base:
                    # 回退到 preview（不完整，仅作为最后手段）
                    logger.debug(f"  未找到 image 文件夹路径，回退到 preview")
                    preview = data.get("custom_properties", {}).get("preview", {})
                    page_urls = {}
                    for key, img_url in preview.items():
                        if key.startswith("Slide"):
                            try:
                                page_num = int(key.replace("Slide", ""))
                                page_urls[page_num] = img_url
                            except ValueError:
                                continue
                    if page_urls:
                        return page_urls, "ok"  # 标准教材有预览页
                    # 预览也为空 → 可能为 thematic_course，跳出循环降级
                    break

                # 从文件夹路径推导全部页面 URL
                # 用 HEAD 请求逐页探测，直到遇到 403 为止
                page_urls = {}
                # 先从 preview 获取预估页数（作为上限参考）
                preview = data.get("custom_properties", {}).get("preview", {})
                preview_max = max(
                    (int(k.replace("Slide", "")) for k in preview if k.startswith("Slide")),
                    default=50
                )
                # 预估总页数：preview 的 3 倍，但至少 200
                # 实际不会逐个请求到上限，连续 3 次 403 就会停止
                estimated_max = max(preview_max * 3, 200)

                headers = {
                    "User-Agent": self._random_ua(),
                    "Referer": BASE_URL,
                }

                consecutive_403 = 0
                for page_num in range(1, estimated_max + 1):
                    img_url = f"{image_folder_base}/{page_num}.jpg"
                    try:
                        head_resp = requests.head(img_url, timeout=10, allow_redirects=True, headers=headers)
                        if head_resp.status_code == 200:
                            page_urls[page_num] = img_url
                            consecutive_403 = 0
                        else:
                            consecutive_403 += 1
                            if consecutive_403 >= 3:
                                # 连续 3 页 403，认为已到末尾
                                break
                    except requests.exceptions.RequestException:
                        consecutive_403 += 1
                        if consecutive_403 >= 3:
                            break

                logger.info(f"  探测到 {len(page_urls)} 页（preview 仅 {len(preview)} 张）")
                return page_urls, "ok"

            except Exception as e:
                logger.debug(f"  CDN-{x} 详情请求失败: {e}")
                continue

        # 标准 API 未能获取到页面图片，尝试 thematic_course 接口降级
        logger.debug(f"  标准 API 未获取到图片数据，尝试 thematic_course 接口降级...")
        thematic_urls, reason = self._get_thematic_page_urls(content_id)
        if thematic_urls:
            return thematic_urls, reason
        return {}, reason

    def _get_thematic_page_urls(self, content_id: str) -> tuple[dict[int, str], str]:
        """通过 thematic_course API 获取专题课程文档的页面图片URL
        
        结构：
        1. 获取资源列表 → 找到 assets_document 类型的资源
        2. 从其 ti_items 中提取图片文件夹模板路径
        3. 逐页 HEAD 探测
        
        返回: (page_urls, reason)
          reason="ok" → 正常获取
          reason="no_thematic_doc" → 专题课程有资源但无文档（可能是重复条目）
        """
        # Step 1: 获取资源的文档列表
        doc_resource_id = None
        image_folder_base = None
        preview_slides = {}

        for cdn in CDN_SERVERS:
            list_url = THEMATIC_RES_LIST_TPL.format(cdn, content_id)
            try:
                resp = requests.get(list_url, timeout=15, headers={
                    "User-Agent": self._random_ua(),
                    "Referer": BASE_URL,
                })
                if resp.status_code != 200:
                    continue

                resources = resp.json()
                # 找 assets_document 类型的资源
                for res in resources:
                    if res.get("resource_type_code") == "assets_document":
                        doc_resource_id = res.get("id", "")
                        # 从 ti_items 中找 image 类型（含 jpg 路径）
                        for ti in res.get("ti_items", []):
                            storage = ti.get("ti_storage", "")
                            if "/transcode/image" in storage or storage.endswith(".jpg"):
                                # ti_storage 格式：cs_path:${ref-path}/edu_product/esp/assets/{id}.t/zh-CN/{ts}/transcode/image
                                # 替换占位符，去掉 cs_path: 前缀
                                path = storage
                                if path.startswith("cs_path:"):
                                    path = path[len("cs_path:"):]
                                path = path.replace("${ref-path}", "")
                                # 如果是具体文件（如 1.jpg），提取目录
                                if path.endswith(".jpg"):
                                    path = "/".join(path.split("/")[:-1])
                                # 确保以 / 结尾
                                if not path.endswith("/"):
                                    path += "/"
                                # 尝试多个公开 CDN host
                                for host in [
                                    "https://r1-ndr.ykt.cbern.com.cn",
                                    "https://r2-ndr.ykt.cbern.com.cn",
                                    "https://r3-ndr.ykt.cbern.com.cn",
                                ]:
                                    test_url = host + path + "1.jpg"
                                    try:
                                        hr = requests.head(test_url, timeout=10, headers={
                                            "User-Agent": self._random_ua(),
                                            "Referer": BASE_URL,
                                        })
                                        if hr.status_code == 200:
                                            image_folder_base = host + path
                                            logger.info(f"  thematic_course 图片路径: {image_folder_base}")
                                            break
                                    except Exception:
                                        continue
                                if image_folder_base:
                                    break
                        # 收集 preview slides
                        preview = res.get("custom_properties", {}).get("preview", {})
                        for key, url in preview.items():
                            if key.startswith("Slide"):
                                try:
                                    pn = int(key.replace("Slide", ""))
                                    preview_slides[pn] = url
                                except ValueError:
                                    pass
                        break  # 找到第一个文档即可
                break  # 找到可用的 CDN
            except Exception as e:
                logger.debug(f"  thematic_course CDN-{cdn} 请求失败: {e}")
                continue

        if not image_folder_base:
            # 回退到 preview（不完整）
            if preview_slides:
                logger.warning(f"  未找到图片路径，仅能使用 preview 的 {len(preview_slides)} 张缩略图")
                return preview_slides, "ok"
            logger.warning(f"  未找到 thematic_course 文档的图片路径")
            return {}, "no_thematic_doc"

        # Step 2: 逐页探测
        preview_max = max(preview_slides.keys(), default=50)
        estimated_max = max(preview_max * 3, 200)

        headers = {
            "User-Agent": self._random_ua(),
            "Referer": BASE_URL,
        }

        page_urls = {}
        consecutive_403 = 0
        for page_num in range(1, estimated_max + 1):
            img_url = f"{image_folder_base}{page_num}.jpg"
            try:
                head_resp = requests.head(img_url, timeout=10, allow_redirects=True, headers=headers)
                if head_resp.status_code == 200:
                    page_urls[page_num] = img_url
                    consecutive_403 = 0
                else:
                    consecutive_403 += 1
                    if consecutive_403 >= 3:
                        break
            except requests.exceptions.RequestException:
                consecutive_403 += 1
                if consecutive_403 >= 3:
                    break

        logger.info(f"  thematic_course 探测到 {len(page_urls)} 页（preview 含 {len(preview_slides)} 张）")
        return page_urls, "ok"

    @staticmethod
    def _download_file(url: str, save_path: Path, chunk_size: int = 8192):
        """下载文件"""
        uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ]
        headers = {
            "User-Agent": random.choice(uas),
            "Referer": BASE_URL,
        }
        resp = requests.get(url, headers=headers, stream=True, timeout=120)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)

    @staticmethod
    def _is_valid_jpg(path: Path) -> bool:
        """检查文件是否为有效的 JPEG 图片"""
        try:
            with open(path, "rb") as f:
                header = f.read(3)
            return header[:2] == b"\xff\xd8"  # JPEG magic bytes
        except Exception:
            return False

    @staticmethod
    def _generate_placeholder(img_path: Path, page_num: int):
        """生成占位图：提示用户该页原图缺失，联系作者纠正"""
        try:
            from PIL import Image, ImageDraw, ImageFont

            W, H = 1437, 1006
            img = Image.new("RGB", (W, H), "#f3f4f6")
            draw = ImageDraw.Draw(img)

            # 尝试加载中文字体
            font_paths = [
                "C:/Windows/Fonts/msyh.ttc",    # 微软雅黑
                "C:/Windows/Fonts/simhei.ttf",   # 黑体
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            ]
            font_large = font_small = None
            for fp in font_paths:
                if Path(fp).exists():
                    try:
                        font_large = ImageFont.truetype(fp, 36)
                        font_small = ImageFont.truetype(fp, 24)
                        break
                    except Exception:
                        continue

            if not font_large:
                font_large = ImageFont.load_default()
                font_small = font_large

            # 绘制文字内容
            lines = [
                (f"第 {page_num} 页 - 原图缺失", font_large, "#374151"),
                ("", font_small, "#9ca3af"),
                ("该页面原图无法下载，已用占位图替代", font_small, "#6b7280"),
                ("如需纠正，请联系作者", font_small, "#6b7280"),
            ]

            # 下载并绘制微信二维码
            qr_size = 160
            qr_img = None
            try:
                resp = requests.get(PLACEHOLDER_WECHAT_URL, timeout=10)
                if resp.status_code == 200 and len(resp.content) > 500:
                    import io
                    qr_img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
                    qr_img = qr_img.resize((qr_size, qr_size), Image.LANCZOS)
            except Exception:
                pass

            # 计算总内容高度
            total_h = 0
            for text, fnt, _ in lines:
                if text:
                    bbox = draw.textbbox((0, 0), text, font=fnt)
                    total_h += bbox[3] - bbox[1] + 16
                else:
                    total_h += 20

            if qr_img:
                total_h += qr_size + 20

            y = (H - total_h) // 2

            for text, fnt, color in lines:
                if text:
                    bbox = draw.textbbox((0, 0), text, font=fnt)
                    tw = bbox[2] - bbox[0]
                    draw.text(((W - tw) // 2, y), text, fill=color, font=fnt)
                    y += bbox[3] - bbox[1] + 16
                else:
                    y += 20

            if qr_img:
                qr_x = (W - qr_size) // 2
                img.paste(qr_img, (qr_x, y))
                y += qr_size + 10
                label = "扫码联系作者"
                bbox = draw.textbbox((0, 0), label, font=font_small)
                lw = bbox[2] - bbox[0]
                draw.text(((W - lw) // 2, y), label, fill="#3b82f6", font=font_small)

            img.save(str(img_path), "JPEG", quality=85)

        except ImportError:
            # 没有 Pillow，生成最简占位图
            _generate_simple_placeholder(img_path, page_num)
        except Exception as e:
            logger.debug(f"  生成占位图失败: {e}")
            # 确保文件存在（即使是空的）
            if not img_path.exists():
                img_path.write_bytes(b"")

    # ── 清理 ──

    def close(self):
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.pw:
            try:
                self.pw.stop()
            except Exception:
                pass
        self.db.close()
        logger.info("资源已释放")


def _generate_simple_placeholder(img_path: Path, page_num: int):
    """无 Pillow 时的最简占位图生成"""
    # 生成一个最小的有效 JPEG（1x1 灰色像素）
    # JPEG: FFD8FFE0 + JFIF header + minimal image data + FFD9
    minimal_jpg = bytes([
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
        0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
        0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
        0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
        0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
        0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
        0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
        0x00, 0x00, 0x3F, 0x00, 0x7B, 0x94, 0x11, 0x00, 0x00, 0x00, 0x00, 0xFF,
        0xD9,
    ])
    img_path.write_bytes(minimal_jpg)


class TchMaterialHelper:
    """独立工具类，用于修复/统计等操作（不需要浏览器）"""

    def __init__(self):
        self.db = TchMaterialDB()

    # ── 统计 ──

    def show_stats(self):
        total = self.db.count()
        downloaded = self.db.count_by_status("downloaded")
        pending = self.db.count_by_status("pending")
        failed = self.db.count_by_status("failed")

        print("\n" + "=" * 50)
        print("  教材采集统计")
        print("=" * 50)
        print(f"  总计:   {total}")
        print(f"  已下载: {downloaded}")
        print(f"  待下载: {pending}")
        print(f"  失败:   {failed}")
        print("=" * 50)

        if total > 0:
            subjects = self.db.get_distinct("subject")
            phases = self.db.get_distinct("phase")
            try:
                print(f"\n  学段: {', '.join(phases)}")
                print(f"  学科: {', '.join(subjects)}")
            except UnicodeEncodeError:
                print(f"\n  学段: {len(phases)} 个")
                print(f"  学科: {len(subjects)} 个")

            print("\n  按学段统计:")
            for p in phases:
                cnt = self._conn_execute_count("phase", p)
                try:
                    print(f"    {p}: {cnt} 本")
                except UnicodeEncodeError:
                    print(f"    [学段]: {cnt} 本")

    def _conn_execute_count(self, column: str, value: str) -> int:
        return self.db.conn.execute(
            f"SELECT COUNT(*) FROM textbooks WHERE {column} = ?", (value,)
        ).fetchone()[0]

    # ── 重置 ──

    def reset_pending(self):
        failed = self.db.count_by_status("failed")
        skipped = self.db.count_by_status("skipped")
        self.db.conn.execute(
            "UPDATE textbooks SET download_status = 'pending' WHERE download_status IN ('failed', 'skipped')"
        )
        self.db.conn.commit()
        logger.info(f"已将 {failed} 本失败 + {skipped} 本跳过 教材重置为待下载状态")

    def fix_duplicate_paths(self):
        """修复因相同标题导致的下载路径冲突
        
        策略：
        1. 找出所有 download_status='downloaded' 的记录
        2. 按 pdf_path 分组，统计每个路径对应多少本不同的 content_id
        3. 如果一个路径对应多本书，说明发生了覆盖冲突 → 删除旧文件夹，重置为 pending
        4. 对无冲突但使用旧路径（不含 content_id）的单本书 → 实际重命名文件夹，更新 DB
        5. 同时为所有无冲突书生成封面文件
        6. 将 failed 状态的书也重置为 pending
        """
        import shutil
        from collections import defaultdict

        logger.info("=" * 60)
        logger.info("开始修复下载路径...")
        logger.info("=" * 60)

        # ── Step 0: 确保 covers 目录存在 ──
        COVER_DIR.mkdir(parents=True, exist_ok=True)

        # ── Step 1: 分析路径冲突 ──
        rows = self.db.conn.execute(
            "SELECT content_id, title, pdf_path, cover_path FROM textbooks WHERE download_status = 'downloaded'"
        ).fetchall()

        path_to_books = defaultdict(list)
        for content_id, title, pdf_path, cover_path in rows:
            if pdf_path:
                path_to_books[pdf_path].append((content_id, title, cover_path))

        conflict_books = []      # 路径冲突，需重新下载
        old_format_books = []    # 旧格式路径，可原地重命名
        new_format_books = []    # 已是新格式，只补封面

        for pdf_path, books in path_to_books.items():
            if len(books) > 1:
                for content_id, title, cover_path in books:
                    conflict_books.append((content_id, title, pdf_path))
            else:
                content_id, title, cover_path = books[0]
                if content_id[:8] not in pdf_path:
                    old_format_books.append((content_id, title, pdf_path, cover_path))
                else:
                    new_format_books.append((content_id, title, pdf_path, cover_path))

        # ── Step 2: 处理冲突书籍 ──
        if conflict_books:
            logger.warning(f"发现 {len(conflict_books)} 本因标题重复导致路径冲突，将重置为待下载：")
            # 收集需要删除的冲突文件夹（去重）
            conflict_dirs_to_delete = set()
            for content_id, title, pdf_path in conflict_books:
                logger.warning(f"  [{content_id[:8]}] {title}")
                self.db.conn.execute(
                    "UPDATE textbooks SET download_status = 'pending', pdf_path = NULL, cover_path = NULL WHERE content_id = ?",
                    (content_id,)
                )
                conflict_dirs_to_delete.add(pdf_path)
            self.db.conn.commit()

            # 删除冲突的文件夹（里面的图片是混着的，不可用）
            for old_dir in conflict_dirs_to_delete:
                old_path = Path(old_dir)
                if old_path.is_dir():
                    try:
                        shutil.rmtree(old_path)
                        logger.info(f"  已删除冲突文件夹: {old_path.name}")
                    except Exception as e:
                        logger.warning(f"  删除文件夹失败 {old_path.name}: {e}")

        # ── Step 3: 处理旧格式书籍 — 实际重命名文件夹 ──
        renamed_count = 0
        rename_fail_count = 0
        if old_format_books:
            logger.info(f"发现 {len(old_format_books)} 本旧格式教材，开始重命名文件夹...")
            for content_id, title, pdf_path, cover_path in old_format_books:
                safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
                new_pdf_path = PDF_DIR / f"{safe_title}_{content_id[:8]}"
                new_cover_path = COVER_DIR / f"{safe_title}_{content_id[:8]}.jpg"
                old_dir = Path(pdf_path)

                if not old_dir.is_dir():
                    # 文件夹不存在（可能之前被删了），重置为 pending
                    logger.warning(f"  [{content_id[:8]}] {title} — 文件夹不存在，重置为待下载")
                    self.db.conn.execute(
                        "UPDATE textbooks SET download_status = 'pending', pdf_path = NULL, cover_path = NULL WHERE content_id = ?",
                        (content_id,)
                    )
                    rename_fail_count += 1
                    continue

                if new_pdf_path.exists():
                    # 新路径已存在（不太可能，但防御一下）
                    logger.warning(f"  [{content_id[:8]}] {title} — 新路径已存在，重置为待下载")
                    self.db.conn.execute(
                        "UPDATE textbooks SET download_status = 'pending', pdf_path = NULL, cover_path = NULL WHERE content_id = ?",
                        (content_id,)
                    )
                    rename_fail_count += 1
                    continue

                try:
                    old_dir.rename(new_pdf_path)
                    # 生成封面
                    first_page = new_pdf_path / "page_0001.jpg"
                    if first_page.exists() and not new_cover_path.exists():
                        shutil.copy2(first_page, new_cover_path)
                    # 更新数据库
                    self.db.conn.execute(
                        "UPDATE textbooks SET pdf_path = ?, cover_path = ? WHERE content_id = ?",
                        (str(new_pdf_path), str(new_cover_path) if new_cover_path.exists() else None, content_id)
                    )
                    renamed_count += 1
                    if renamed_count % 100 == 0:
                        logger.info(f"  已重命名 {renamed_count}/{len(old_format_books)} ...")
                except Exception as e:
                    logger.error(f"  [{content_id[:8]}] {title} — 重命名失败: {e}，重置为待下载")
                    self.db.conn.execute(
                        "UPDATE textbooks SET download_status = 'pending', pdf_path = NULL, cover_path = NULL WHERE content_id = ?",
                        (content_id,)
                    )
                    rename_fail_count += 1

            self.db.conn.commit()

        # ── Step 4: 为已是新格式的书补封面 ──
        cover_added = 0
        if new_format_books:
            logger.info(f"检查 {len(new_format_books)} 本新格式教材的封面...")
            for content_id, title, pdf_path, cover_path in new_format_books:
                if cover_path and Path(cover_path).exists():
                    continue
                safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
                new_cover_path = COVER_DIR / f"{safe_title}_{content_id[:8]}.jpg"
                first_page = Path(pdf_path) / "page_0001.jpg"
                if first_page.exists() and not new_cover_path.exists():
                    shutil.copy2(first_page, new_cover_path)
                    self.db.conn.execute(
                        "UPDATE textbooks SET cover_path = ? WHERE content_id = ?",
                        (str(new_cover_path), content_id)
                    )
                    cover_added += 1
            if cover_added:
                self.db.conn.commit()

        # ── Step 5: 将 failed / skipped 状态重置为 pending ──
        reset_count = self._conn_execute_count("download_status", "failed") + self._conn_execute_count("download_status", "skipped")
        if reset_count > 0:
            self.db.conn.execute(
                "UPDATE textbooks SET download_status = 'pending' WHERE download_status IN ('failed', 'skipped')"
            )
            self.db.conn.commit()
            logger.info(f"已将 {reset_count} 本失败/跳过教材重置为待下载")

        # ── 汇总 ──
        logger.info("=" * 60)
        logger.info("路径修复完成！")
        logger.info(f"  冲突书籍: {len(conflict_books)} 本 → 已删除旧文件夹，重置为待下载")
        logger.info(f"  旧格式重命名: {renamed_count} 本成功, {rename_fail_count} 本失败(已重置为待下载)")
        logger.info(f"  新格式补封面: {cover_added} 本")
        logger.info(f"  失败重置: {failed_count} 本")
        pending_now = self.db.count_by_status("pending")
        downloaded_now = self.db.count_by_status("downloaded")
        logger.info(f"  当前状态: 已下载 {downloaded_now}, 待下载 {pending_now}")
        logger.info(f"  下一步: 运行 python tch_material_crawler.py --download")

    def verify_downloads(self, fix: bool = False):
        """验证已下载教材的完整性，可选修复

        检查项：
        1. 占位图（非有效 JPEG 的小文件）
        2. 实际页数 vs 数据库记录页数
        3. 文件夹是否存在
        4. page_count=100 的书可能被旧版截断，重新探测真实页数

        fix=True 时：删除占位图，将不完整的书重置为 pending
        """
        import shutil

        logger.info("=" * 60)
        logger.info(f"开始验证已下载教材完整性{'（修复模式）' if fix else ''}...")
        logger.info("=" * 60)

        rows = self.db.conn.execute(
            "SELECT content_id, title, pdf_path, page_count FROM textbooks WHERE download_status = 'downloaded'"
        ).fetchall()

        total_checked = 0
        total_ok = 0
        total_placeholder = 0
        total_missing_dir = 0
        total_page_mismatch = 0
        total_truncated = 0
        placeholder_books = []     # (content_id, title, placeholder_count, total_pages)
        missing_dir_books = []     # (content_id, title)
        mismatch_books = []       # (content_id, title, db_pages, actual_pages)
        truncated_books = []      # (content_id, title, db_pages, real_pages)

        for content_id, title, pdf_path, db_page_count in rows:
            total_checked += 1
            book_dir = Path(pdf_path) if pdf_path else None

            # 检查文件夹是否存在
            if not book_dir or not book_dir.is_dir():
                total_missing_dir += 1
                missing_dir_books.append((content_id, title))
                if fix:
                    self.db.conn.execute(
                        "UPDATE textbooks SET download_status = 'pending', pdf_path = NULL, cover_path = NULL WHERE content_id = ?",
                        (content_id,)
                    )
                continue

            # 检查每个页面图片
            page_files = sorted(book_dir.glob("page_*.jpg"))
            actual_pages = len(page_files)
            placeholder_count = 0

            for pf in page_files:
                # 占位图特征：文件很小（<2KB）或不是有效JPEG
                file_size = pf.stat().st_size
                if file_size < 2000 or not self._is_valid_jpg_static(pf):
                    placeholder_count += 1

            if placeholder_count > 0:
                total_placeholder += 1
                placeholder_books.append((content_id, title, placeholder_count, actual_pages))
                if fix:
                    # 删除占位图，让重新下载时能补上
                    for pf in page_files:
                        file_size = pf.stat().st_size
                        if file_size < 2000 or not self._is_valid_jpg_static(pf):
                            pf.unlink()
                    self.db.conn.execute(
                        "UPDATE textbooks SET download_status = 'pending' WHERE content_id = ?",
                        (content_id,)
                    )

            # 检查页数是否匹配（如果有db记录的话）
            if db_page_count and db_page_count > 0 and actual_pages < db_page_count * 0.8:
                # 实际页数不到数据库记录的80%，视为不完整
                total_page_mismatch += 1
                mismatch_books.append((content_id, title, db_page_count, actual_pages))
                if fix:
                    self.db.conn.execute(
                        "UPDATE textbooks SET download_status = 'pending' WHERE content_id = ?",
                        (content_id,)
                    )

            # 检查是否被截断：db_page_count=100 很可能是旧版 estimated_max 上限截断
            truncated = False
            if db_page_count == 100:
                real_pages = self._probe_real_page_count(content_id)
                if real_pages and real_pages > 100:
                    truncated = True
                    total_truncated += 1
                    truncated_books.append((content_id, title, db_page_count, real_pages))
                    logger.info(f"  [{content_id[:8]}] {title} — 记录100页，实际{real_pages}页，被截断")
                    if fix:
                        self.db.conn.execute(
                            "UPDATE textbooks SET download_status = 'pending' WHERE content_id = ?",
                            (content_id,)
                        )
                elif real_pages == 100:
                    logger.info(f"  [{content_id[:8]}] {title} — 确认100页，未截断")

            if placeholder_count == 0 and not truncated and (not db_page_count or actual_pages >= db_page_count * 0.8):
                total_ok += 1

            if total_checked % 20 == 0:
                logger.info(f"  已检查 {total_checked}/{len(rows)} ...")

        if fix:
            self.db.conn.commit()

        # ── 报告 ──
        logger.info("=" * 60)
        logger.info(f"验证完成！共检查 {total_checked} 本")
        logger.info(f"  完整: {total_ok}")
        logger.info(f"  有占位图: {total_placeholder}")
        logger.info(f"  文件夹缺失: {total_missing_dir}")
        logger.info(f"  页数不足: {total_page_mismatch}")
        logger.info(f"  页数截断(原100页): {total_truncated}")

        if placeholder_books:
            logger.info(f"\n  含占位图的教材（前20本）:")
            for cid, t, pc, tp in placeholder_books[:20]:
                logger.info(f"    [{cid[:8]}] {t} — {pc}/{tp} 页为占位图")
            if len(placeholder_books) > 20:
                logger.info(f"    ... 还有 {len(placeholder_books) - 20} 本")

        if missing_dir_books:
            logger.info(f"\n  文件夹缺失的教材（前10本）:")
            for cid, t in missing_dir_books[:10]:
                logger.info(f"    [{cid[:8]}] {t}")
            if len(missing_dir_books) > 10:
                logger.info(f"    ... 还有 {len(missing_dir_books) - 10} 本")

        if mismatch_books:
            logger.info(f"\n  页数不足的教材（前10本）:")
            for cid, t, dbp, ap in mismatch_books[:10]:
                logger.info(f"    [{cid[:8]}] {t} — 数据库{dbp}页/实际{ap}页")

        if truncated_books:
            logger.info(f"\n  被截断的教材（page_count=100，实际更多）:")
            for cid, t, dbp, rp in truncated_books[:20]:
                logger.info(f"    [{cid[:8]}] {t} — 记录{dbp}页/实际{rp}页")
            if len(truncated_books) > 20:
                logger.info(f"    ... 还有 {len(truncated_books) - 20} 本")

        if fix:
            pending_now = self.db.count_by_status("pending")
            downloaded_now = self.db.count_by_status("downloaded")
            logger.info(f"\n  修复后状态: 已下载 {downloaded_now}, 待下载 {pending_now}")
            logger.info(f"  下一步: 运行 python tch_material_crawler.py --download")
        else:
            need_fix = total_placeholder + total_missing_dir + total_page_mismatch + total_truncated
            if need_fix > 0:
                logger.info(f"\n  发现 {need_fix} 本需要修复，运行以下命令修复:")
                logger.info(f"  python tch_material_crawler.py --fix-incomplete")

    @staticmethod
    def _is_valid_jpg_static(path: Path) -> bool:
        """检查文件是否为有效的 JPEG 图片"""
        try:
            with open(path, "rb") as f:
                header = f.read(3)
            return header[:2] == b"\xff\xd8"
        except Exception:
            return False

    @staticmethod
    def _probe_real_page_count(content_id: str) -> int | None:
        """重新探测书籍的真实总页数（从 page_count 开始往后探测）"""
        try:
            for x in CDN_SERVERS:
                url = DETAIL_URL_TPL.format(x, content_id)
                resp = requests.get(url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
                    "Referer": BASE_URL,
                })
                if resp.status_code != 200:
                    continue
                data = resp.json()
                image_folder_base = None
                for item in data.get("ti_items", []):
                    if item.get("ti_file_flag") == "image" and item.get("ti_format") == "folder":
                        storages = item.get("ti_storages", [])
                        if storages:
                            image_folder_base = storages[0].replace("ndr-private", "ndr")
                            break
                if not image_folder_base:
                    return None

                # 从第100页往后探测
                headers = {"User-Agent": "Mozilla/5.0", "Referer": BASE_URL}
                real_max = 100
                consecutive_miss = 0
                for p in range(101, 300):
                    try:
                        r = requests.head(f"{image_folder_base}/{p}.jpg", timeout=8, headers=headers)
                        if r.status_code == 200:
                            real_max = p
                            consecutive_miss = 0
                        else:
                            consecutive_miss += 1
                            if consecutive_miss >= 3:
                                break
                    except Exception:
                        consecutive_miss += 1
                        if consecutive_miss >= 3:
                            break
                return real_max
        except Exception:
            return None

    def close(self):
        self.db.close()
        logger.info("资源已释放")


# ─── 主入口 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="国家中小学智慧教育平台电子教材采集器")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--collect", action="store_true", help="仅采集元数据")
    group.add_argument("--download", action="store_true", help="仅下载PDF")
    group.add_argument("--all", action="store_true", help="采集元数据 + 下载PDF")
    group.add_argument("--stats", action="store_true", help="查看统计信息")
    group.add_argument("--reset", action="store_true", help="重置失败状态为待下载")
    group.add_argument("--fix-paths", action="store_true", help="修复因标题重复导致的路径冲突")
    group.add_argument("--verify", action="store_true", help="验证已下载教材完整性")
    group.add_argument("--fix-incomplete", action="store_true", help="修复不完整的下载（删除占位图并重置为待下载）")

    parser.add_argument("--max-books", type=int, default=0, help="限制处理数量")
    parser.add_argument("--start-from", type=int, default=0, help="从第N本开始下载")
    parser.add_argument("--headless", action="store_true", default=True, help="无头模式运行")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="显示浏览器窗口")

    args = parser.parse_args()

    if not any([args.collect, args.download, args.all, args.stats, args.reset, args.fix_paths, args.verify, args.fix_incomplete]):
        parser.print_help()
        return

    crawler = TchMaterialCrawler()
    helper = TchMaterialHelper()

    # 添加文件日志
    logger.addHandler(_setup_file_logger())

    try:
        if args.stats:
            helper.show_stats()
            return

        if args.reset:
            helper.reset_pending()
            return

        if args.fix_paths:
            helper.fix_duplicate_paths()
            return

        if args.verify:
            helper.verify_downloads(fix=False)
            return

        if args.fix_incomplete:
            helper.verify_downloads(fix=True)
            return

        if args.collect or args.all:
            # 元数据采集不需要浏览器，直接请求 part_*.json
            crawler.collect_metadata(max_books=args.max_books)
            _save_categories_from_data(crawler.db)

        if args.download or args.all:
            crawler.download_pdfs(
                max_books=args.max_books,
                start_from=args.start_from,
            )

        helper.show_stats()

    except KeyboardInterrupt:
        logger.info("用户中断，正在保存进度...")
    except Exception as e:
        logger.error(f"运行出错: {e}", exc_info=True)
    finally:
        crawler.close()


def _save_categories_from_data(db: TchMaterialDB):
    """从已采集的教材数据中提取并保存分类"""
    for phase in db.get_distinct("phase"):
        if phase:
            db.upsert_category(name=phase, tag_code=f"phase_{phase}", level=1, sort_order=1)
    for subject in db.get_distinct("subject"):
        if subject:
            db.upsert_category(name=subject, tag_code=f"subject_{subject}", level=2, sort_order=2)
    for grade in db.get_distinct("grade"):
        if grade:
            db.upsert_category(name=grade, tag_code=f"grade_{grade}", level=3, sort_order=3)


if __name__ == "__main__":
    main()
