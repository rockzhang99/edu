"""修改指定教材的下载状态"""
import argparse
import sqlite3
import sys

VALID_STATUSES = ("pending", "downloaded", "failed", "skipped")

parser = argparse.ArgumentParser(description="修改教材下载状态")
parser.add_argument("prefix", help="content_id 前缀，如 4573420f")
parser.add_argument(
    "--status", "-s",
    default="pending",
    choices=VALID_STATUSES,
    help=f"目标状态（默认: pending），可选: {', '.join(VALID_STATUSES)}"
)
args = parser.parse_args()

conn = sqlite3.connect("tch_material.db")
cursor = conn.cursor()

cursor.execute(
    f"UPDATE textbooks SET download_status=? WHERE content_id LIKE ?",
    (args.status, f"{args.prefix}%")
)
print(f"已将 {cursor.rowcount} 本教材状态改为 '{args.status}' (content_id 前缀: {args.prefix})")
conn.commit()
conn.close()
