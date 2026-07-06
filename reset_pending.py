"""将指定的教材重置为待下载状态"""
import sqlite3
import sys

if len(sys.argv) < 2:
    print("用法: python reset_pending.py <content_id前缀>")
    print("示例: python reset_pending.py 4573420f")
    sys.exit(1)

prefix = sys.argv[1]
conn = sqlite3.connect("tch_material.db")
cursor = conn.cursor()

cursor.execute(
    "UPDATE textbooks SET download_status='pending' WHERE content_id LIKE ?",
    (f"{prefix}%",)
)
print(f"已将 {cursor.rowcount} 本教材重置为 pending (content_id 前缀: {prefix})")
conn.commit()
conn.close()
