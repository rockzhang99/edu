"""将指定教材的所有页面图片打包成高清PDF（原图无损）。
单个 PDF 不超过 100MB，超过则自动拆分为多个文件（P1, P2, ...）"""
import argparse
import sqlite3
import sys
from pathlib import Path

PDF_DIR = Path("textbook_pdfs")
DB_PATH = Path("tch_material.db")

# 单个 PDF 上限 100MB，img2pdf 无损嵌入 JPEG 时 PDF 略有额外开销，
# 故按 JPEG 原始字节总量 90MB 为分片阈值，保证最终 PDF < 100MB。
MAX_PART_BYTES = 90 * 1024 * 1024  # 90MB


def _split_images(images: list[Path]) -> list[list[Path]]:
    """将图片列表按累计大小拆成多个分片，每片 ≤ 90MB"""
    parts = []
    current_part = []
    current_size = 0

    for img in images:
        size = img.stat().st_size
        if current_part and current_size + size > MAX_PART_BYTES:
            parts.append(current_part)
            current_part = []
            current_size = 0
        current_part.append(img)
        current_size += size

    if current_part:
        parts.append(current_part)

    return parts


def pack_pdf(content_id_prefix: str, output_dir: str = ""):
    import img2pdf

    # 1. 从数据库获取书名
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT content_id, title, page_count FROM textbooks WHERE content_id LIKE ?",
        (f"{content_id_prefix}%",)
    ).fetchone()
    conn.close()

    if row is None:
        print(f"[错误] 未找到 content_id 前缀为 '{content_id_prefix}' 的教材")
        sys.exit(1)

    book_title = row["title"]
    total_pages = row["page_count"]
    print(f"教材: {book_title}")
    print(f"总页数(数据库): {total_pages}")

    # 2. 找到图片文件夹
    img_dir = None
    for d in PDF_DIR.iterdir():
        if d.is_dir() and content_id_prefix in d.name:
            img_dir = d
            break

    if img_dir is None:
        print(f"[错误] 未在 {PDF_DIR}/ 下找到包含 '{content_id_prefix}' 的文件夹")
        sys.exit(1)

    # 3. 收集并排序图片
    images = sorted(
        img_dir.glob("*.jpg"),
        key=lambda p: p.name  # page_0001.jpg 字典序即页码序
    )
    if not images:
        print(f"[错误] {img_dir} 下没有图片文件")
        sys.exit(1)

    total_raw_size = sum(img.stat().st_size for img in images)
    print(f"图片数量: {len(images)} 张")
    print(f"图片总大小: {total_raw_size / (1024 * 1024):.1f} MB")
    print(f"图片文件夹: {img_dir}")

    # 4. 输出目录
    if output_dir:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = PDF_DIR

    safe_title = book_title.replace("/", "_").replace("\\", "_").replace(":", "_")

    # 5. 拆片（需要时）
    parts = _split_images(images)
    single_file = len(parts) == 1
    if single_file:
        print(f"无需拆分，单文件即可")
    else:
        print(f"将拆分为 {len(parts)} 个文件（每片 ≤ 100MB）")

    # 6. 逐片生成 PDF
    for idx, part_images in enumerate(parts):
        from_page = part_images[0].stem.replace("page_", "").lstrip("0") or "1"
        to_page = part_images[-1].stem.replace("page_", "").lstrip("0") or "1"

        if single_file:
            pdf_name = f"{safe_title}_{content_id_prefix}.pdf"
        else:
            pn = idx + 1
            pdf_name = f"{safe_title}_{content_id_prefix}_P{pn}.pdf"

        pdf_path = out_dir / pdf_name

        print(f"\n[P{idx + 1}] 第 {from_page}-{to_page} 页 ({len(part_images)} 张) ...")
        with open(str(pdf_path), "wb") as f:
            f.write(img2pdf.convert([str(p) for p in part_images]))

        pdf_size_mb = pdf_path.stat().st_size / (1024 * 1024)
        print(f"  -> {pdf_name}  ({pdf_size_mb:.1f} MB)")

    print(f"\n完成! 共生成 {len(parts)} 个 PDF，输出目录: {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="将教材页面图片打包为高清PDF（原图无损）")
    parser.add_argument(
        "prefix",
        help="content_id 前缀，如 4573420f"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="",
        help="输出目录（默认为 textbook_pdfs/）"
    )
    args = parser.parse_args()

    pack_pdf(args.prefix, args.output_dir)


if __name__ == "__main__":
    main()
