import os
import json
import asyncio
import sqlite3
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

DB_FILE = "videos.db"
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "edgedge0603@gmail.com")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            cover_url TEXT,
            tags TEXT,
            actress TEXT,
            video_url TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("PRAGMA table_info(videos)")
    columns = [col[1] for col in cursor.fetchall()]
    if "actress" not in columns:
        cursor.execute("ALTER TABLE videos ADD COLUMN actress TEXT")
    conn.commit()
    conn.close()

def save_video(title, cover_url, tags, actress, video_url):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR IGNORE INTO videos (title, cover_url, tags, actress, video_url)
            VALUES (?, ?, ?, ?, ?)
        """, (title, cover_url, tags, actress, video_url))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

def get_all_videos():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, cover_url, tags, actress, video_url, created_at FROM videos ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def sync_to_google_sheet(new_videos):
    """智慧同步：自動比對 Google 試算表與資料庫，補齊遺漏影片，並自動將該列列高設定為 120px 放大圖片"""
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")

    if not creds_json or not sheet_id:
        print("⚠️ 未設定 Google 試算表相關 Secrets，跳過試算表同步。")
        return

    try:
        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        client = gspread.authorize(creds)

        sheet = client.open_by_key(sheet_id).sheet1
        existing_rows = sheet.get_all_values()

        if not existing_rows:
            sheet.append_row(["標題", "女優", "標籤", "影片連結", "封面圖片", "抓取時間"])
            existing_urls = set()
            current_row_index = 2
        else:
            existing_urls = {row[3] for row in existing_rows[1:] if len(row) > 3}
            current_row_index = len(existing_rows) + 1

        all_videos = get_all_videos()
        added_count = 0

        for v in all_videos:
            vid_id, title, cover_url, tags, actress, video_url, created_at = v
            if video_url not in existing_urls:
                image_formula = f'=IMAGE("{cover_url}")' if cover_url else ""
                sheet.append_row([
                    title, actress or "未知", tags or "", video_url, image_formula, created_at
                ], value_input_option='USER_ENTERED')
                
                try:
                    sheet.format(f"E{current_row_index}", {"textFormat": {"fontSize": 10}})
                    sheet.update_row_height(current_row_index, 120)
                except Exception:
                    pass
                
                current_row_index += 1
                added_count += 1

        if added_count > 0:
            print(f"✅ 成功同步 {added_count} 筆影片至 Google 試算表，並已自動放大圖片儲存格！")
        else:
            print("✅ Google 試算表已包含所有資料庫影片，無需同步。")
    except Exception as e:
        print(f"❌ 同步 Google 試算表失敗 (略過以繼續執行): {e}")

def generate_index_html():
    videos = get_all_videos()
    all_actresses = set()
    all_tags_set = set()
    cards_list = []

    for v in reversed(videos):
        vid_id, title, cover_url, tags, actress, video_url, created_at = v
        actress_display = actress.strip() if actress and actress.strip() else "未知/多人"
        if actress:
            for act in actress.split():
                if act.strip():
                    all_actresses.add(act.strip())
        
        if tags:
            for t in tags.split(','):
                if t.strip():
                    all_tags_set.add(t.strip())

        date_str = created_at[:10] if created_at else ""
        cover_img = cover_url if cover_url else "https://via.placeholder.com/300x180?text=No+Cover"

        card_html = (
            f'<div class="card" data-title="{title.lower()}" data-actress="{actress_display.lower()}" data-tags="{(tags or "").lower()}" data-date="{date_str}">'
            f'<img src="{cover_img}" alt="封面" loading="lazy" referrerpolicy="no-referrer">'
            f'<div class="card-body">'
            f'<div class="title" title="{title}">{title}</div>'
            f'<div class="meta-row">'
            f'<span class="badge actress-badge">👩 {actress_display}</span>'
            f'<span class="date">📅 {date_str}</span>'
            f'</div>'
            f'<div class="tags" title="{tags or ""}">🏷️ {tags or "無標籤"}</div>'
            f'<div class="footer">'
