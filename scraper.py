import os
import json
import asyncio
import sqlite3
import smtplib
import re
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
    cards_html = ""
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

        cards_html += f"""
        <div class="card" data-title="{title.lower()}" data-actress="{actress_display.lower()}" data-tags="{(tags or '').lower()}" data-date="{date_str}">
            <img src="{cover_img}" alt="封面" loading="lazy" referrerpolicy="no-referrer">
            <div class="card-body">
                <div class="title" title="{title}">{title}</div>
                <div class="meta-row">
                    <span class="badge actress-badge">👩 {actress_display}</span>
                    <span class="date">📅 {date_str}</span>
                </div>
                <div class="tags" title="{tags or ''}">🏷️ {tags or '無標籤'}</div>
                <div class="footer">
                    <a href="{video_url}" target="_blank" class="btn">前往觀看</a>
                </div>
            </div>
        </div>
        """

    actress_options = "".join([f'<option value="{act.lower()}">{act}</option>' for act in sorted(all_actresses)])
    
    checkboxes_html = "".join([
        f'<label class="checkbox-label"><input type="checkbox" name="keyword" value="{tag}"> {tag}</label>' 
        for tag in sorted(all_tags_set)
    ]) if all_tags_set else '<span style="color: #94a3b8; font-size: 0.85rem;">尚無標籤資料</span>'

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jable & MissAv 影片自動典藏庫 - 智慧篩選</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0b0f19; color: #f8fafc; padding: 24px 16px; }}
        .header {{ text-align: center; margin-bottom: 24px; }}
        .header h1 {{ font-size: 1.8rem; color: #38bdf8; margin-bottom: 6px; }}
        .header p {{ color: #94a3b8; font-size: 0.9rem; }}
        .filter-panel {{ max-width: 1400px; margin: 0 auto 28px auto; background: #1e293b; padding: 18px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); display: flex; flex-direction: column; gap: 16px; }}
        .filter-row {{ display: flex; flex-wrap: wrap; gap: 20px; align-items: center; justify-content: space-between; }}
        .filter-group {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: center; flex-grow: 1; }}
        .filter-item {{ display: flex; align-items: center; gap: 8px; }}
        .filter-item label {{ font-size: 0.85rem; color: #94a3b8; }}
        .filter-item select {{ background: #0f172a; border: 1px solid #334155; color: #f1f5f9; padding: 8px 12px; border-radius: 6px; font-size: 0.85rem; outline: none; }}
        .checkbox-container {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; background: #0f172a; padding: 10px 14px; border-radius: 6px; border: 1px solid #334155; max-height: 140px; overflow-y: auto; flex-grow: 1; }}
        .checkbox-label {{ font-size: 0.85rem; color: #cbd5e1; display: flex; align-items: center; gap: 4px; cursor: pointer; }}
        .checkbox-label input {{ cursor: pointer; }}
        .reset-btn {{ background: #475569; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }}
        .reset-btn:hover {{ background: #64748b; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; max-width: 1400px; margin: 0 auto; }}
        .card {{ background: #1e293b; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); display: flex; flex-direction: column; transition: transform 0.2s; }}
        .card:hover {{ transform: translateY(-4px); }}
        .card img {{ width: 100%; height: 180px; object-fit: cover; background-color: #334155; }}
        .card-body {{ padding: 14px; flex-grow: 1; display: flex; flex-direction: column; justify-content: space-between; }}
        .title {{ font-size: 0.95rem; font-weight: 600; line-height: 1.4; color: #f1f5f9; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-bottom: 8px; }}
        .meta-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 0.75rem; }}
        .actress-badge {{ background: #0284c7; color: white; padding: 2px 8px; border-radius: 4px; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .date {{ color: #64748b; }}
        .tags {{ font-size: 0.75rem; color: #94a3b8; background: #0f172a; padding: 4px 8px; border-radius: 4px; margin-bottom: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .footer {{ display: flex; justify-content: flex-end; margin-top: auto; }}
        .btn {{ background: #0284c7; color: #fff; text-decoration: none; padding: 6px 14px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; }}
        .btn:hover {{ background: #0369a1; }}
        .empty-msg {{ text-align: center; grid-column: 1 / -1; color: #94a3b8; padding: 40px; font-size: 1rem; display: none; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Jable & MissAv 影片自動典藏庫</h1>
        <p>目前庫存 <span id="total-count">{len(videos)}</span> 部影片 | 篩選顯示 <span id="visible-count">{len(videos)}</span> 部</p>
    </div>
    <div class="filter-panel">
        <div class="filter-row">
            <div class="filter-group">
                <div class="filter-item" style="width: 100%;">
                    <label style="min-width: 80px;">Tags 複選:</label>
                    <div class="checkbox-container">
                        {checkboxes_html}
                    </div>
                </div>
            </div>
            <button class="reset-btn" onclick="resetFilters()">重設條件</button>
        </div>
        <div class="filter-row">
            <div class="filter-group">
                <div class="filter-item">
                    <label for="actress-select">女優篩選:</label>
                    <select id="actress-select">
                        <option value="">全部女優</option>
                        {actress_options}
                    </select>
                </div>
                <div class="filter-item">
                    <label for="date-sort">新增時間排序:</label>
                    <select id="date-sort">
                        <option value="newest">最新優先</option>
                        <option value="oldest">較舊優先</option>
                    </select>
                </div>
            </div>
        </div>
    </div>
    <div class="grid" id="video-grid">
        {cards_html}
        <div class="empty-msg" id="empty-msg">查無符合條件的影片</div>
    </div>
    <script>
        const keywordCheckboxes = document.querySelectorAll('input[name="keyword"]');
        const actressSelect = document.getElementById('actress-select
