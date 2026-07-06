#!/usr/bin/env python3
"""
重命名 textbook_covers 下图片文件和 textbook_pdfs 下文件夹，
去掉中文名，只保留 content_id 前8位 hash。

用法:
  # 先拿 1 本做测试（dry-run，不改任何东西）
  python rename_paths.py --dry-run --limit 1

  # 干跑全部，查看会做什么
  python rename_paths.py --dry-run

  # 正式执行
  python rename_paths.py
"""

import argparse
import re
import sqlite3
import shutil
import sys
from pathlib import Path

DB_PATH = Path("tch_material.db")
COVER_DIR = Path("textbook_covers")
PDF_DIR = Path("textbook_pdfs")

# 匹配文件名/目录名末尾的 _8位hex[.扩展名]
HASH_SUFFIX_RE = re.compile(r"_([a-f0-9]{8})(\.\w+)?$")


def extract_hash(name: str) -> tuple[str | None, str]:
    """从文件名/目录名提取末尾 hash。
    返回 (hash, 新名称) 或 (None, 原名称)
    """
    m = HASH_SUFFIX_RE.search(name)
    if m:
        hash_part = m.group(1)
        ext = m.group(2) or ""
        return hash_part, f"{hash_part}{ext}"
    return None, name


def run(dry_run: bool = True, limit: int = 0):
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    # 统计
    cover_count = 0
    pdf_count = 0
    skipped_cover = 0
    skipped_pdf = 0
    errors = []

    # ── 1. 重命名 textbook_covers 下的文件 ──
    if COVER_DIR.is_dir():
        jpgs = sorted(COVER_DIR.glob("*.jpg"))
        if limit > 0:
            jpgs = jpgs[:limit]

        print(f"\n{'='*60}")
        print(f"处理 textbook_covers/ ({len(jpgs)} 个文件)")
        print(f"{'='*60}")

        for old_path in jpgs:
            old_name = old_path.name
            h, new_name = extract_hash(old_name)

            if h is None or new_name == old_name:
                skipped_cover += 1
                if old_name != new_name:
                    print(f"  [跳过] 无法提取hash: {old_name}")
                continue

            new_path = COVER_DIR / new_name

            # 冲突检测
            if new_path.exists():
                print(f"  [冲突] {old_name} -> {new_name} (目标已存在)")
                skipped_cover += 1
                continue

            if dry_run:
                print(f"  [DRY-RUN] {old_name} -> {new_name}")
            else:
                try:
                    old_path.rename(new_path)
                    # 更新数据库中引用此封面的记录
                    old_abs = str(old_path.absolute())
                    new_abs = str(new_path.absolute())
                    result = db.execute(
                        "UPDATE textbooks SET cover_path = ? WHERE cover_path = ?",
                        (new_abs, old_abs)
                    )
                    affected = result.rowcount
                    db.commit()
                    print(f"  [OK] {old_name} -> {new_name} (DB更新 {affected} 条)")
                except Exception as e:
                    print(f"  [错误] {old_name}: {e}")
                    errors.append(f"cover: {old_name}")

            cover_count += 1

    # ── 2. 重命名 textbook_pdfs 下的文件夹 ──
    if PDF_DIR.is_dir():
        dirs = sorted(d for d in PDF_DIR.iterdir() if d.is_dir())
        if limit > 0:
            dirs = dirs[:limit]

        print(f"\n{'='*60}")
        print(f"处理 textbook_pdfs/ ({len(dirs)} 个文件夹)")
        print(f"{'='*60}")

        for old_path in dirs:
            old_name = old_path.name
            h, new_name = extract_hash(old_name)

            if h is None or new_name == old_name:
                skipped_pdf += 1
                continue

            new_path = PDF_DIR / new_name

            if new_path.exists():
                print(f"  [冲突] {old_name} -> {new_name} (目标已存在)")
                skipped_pdf += 1
                continue

            if dry_run:
                print(f"  [DRY-RUN] {old_name}/ -> {new_name}/")
            else:
                try:
                    old_path.rename(new_path)
                    # 更新数据库中引用此目录的记录
                    old_abs = str(old_path.absolute())
                    new_abs = str(new_path.absolute())
                    result = db.execute(
                        "UPDATE textbooks SET pdf_path = ? WHERE pdf_path = ?",
                        (new_abs, old_abs)
                    )
                    affected = result.rowcount
                    db.commit()
                    print(f"  [OK] {old_name}/ -> {new_name}/ (DB更新 {affected} 条)")
                except Exception as e:
                    print(f"  [错误] {old_name}: {e}")
                    errors.append(f"pdf: {old_name}")

            pdf_count += 1

    # ── 汇总 ──
    print(f"\n{'='*60}")
    if dry_run:
        print(f"[DRY-RUN 模式] 以上仅为预览，未做任何实际修改")
    print(f"textbook_covers: 处理 {cover_count}, 跳过 {skipped_cover}")
    print(f"textbook_pdfs : 处理 {pdf_count}, 跳过 {skipped_pdf}")
    if errors:
        print(f"错误数: {len(errors)}")
        for e in errors:
            print(f"  - {e}")
    print(f"{'='*60}")

    if not dry_run:
        # 验证结果
        db.execute(
            "UPDATE textbooks SET cover_path = replace(cover_path, 'textbook_covers\\', 'textbook_covers\\') "
            "WHERE cover_path like '%/_%'"
        )
        remaining = db.execute(
            "SELECT COUNT(*) FROM textbooks WHERE cover_path IS NOT NULL AND "
            "cover_path LIKE ?", (f"%{COVER_DIR}%_%.jpg",)
        ).fetchone()[0]
        if remaining > 0:
            print(f"\n注意: 还有 {remaining} 条 cover_path 未更新（可能是 dry-run 以外的文件）")

    db.close()


def main():
    parser = argparse.ArgumentParser(description="批量重命名文件/文件夹，去掉中文只保留 hash")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际修改")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量（用于测试）")
    parser.add_argument("--covers-only", action="store_true", help="只处理 textbook_covers")
    parser.add_argument("--pdfs-only", action="store_true", help="只处理 textbook_pdfs")
    args = parser.parse_args()

    run(dry_run=args.dry_run, limit=args.limit)

    # 暂未实现只处理单类
    if args.covers_only or args.pdfs_only:
        print("提示: --covers-only/--pdfs-only 暂未实现，直接处理全部")


if __name__ == "__main__":
    main()
