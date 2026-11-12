import os
import json
import asyncio
import sqlite3
import smtplib
import re
import random
import functools
import html
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# 即時輸出終端機日誌至 GitHub Actions
print = functools.partial(print, flush=True)

# 定義台灣時區 (UTC+8)
TAIWAN_TZ = timezone(timedelta(hours=8))

def get_taiwan_now():
    return datetime.now(TAIWAN_TZ)

def get_taiwan_now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return get_taiwan_now().strftime(fmt)

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
            release_date TEXT,
            created_at TEXT
        )
    """)
    cursor.execute("PRAGMA table_info(videos)")
    columns = [col[1] for col in cursor.fetchall()]
    if "actress" not in columns:
        cursor.execute("ALTER TABLE videos ADD COLUMN actress TEXT")
    if "release_date" not in columns:
        cursor.execute("ALTER TABLE videos ADD COLUMN release_date TEXT")
    
    # 清理非影片的分類或無效路徑
    cursor.execute("""
        DELETE FROM videos 
        WHERE video_url LIKE '%/genres/%'
           OR video_url LIKE '%/tags/%'
           OR video_url LIKE '%/actresses/%'
           OR video_url = 'https://missav.ws/chinese-subtitle'
           OR video_url = 'https://missav.ws/dm278/chinese-subtitle'
           OR title = '未知標題'
           OR title = ''
    """)
    conn.commit()
    conn.close()

def save_video(title, cover_url, tags, actress, video_url, release_date="", custom_created_at=None):
    """
    儲存影片至資料庫：
    - 若為「符合條件的新影片」：寫入最新台灣時間，回傳 True（自動排在最前面）
    - 若為「已收錄過的舊影片」：僅補齊缺漏欄位並保留原收錄時間，回傳 False（不影響新片置頂排序）
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    clean_title = re.sub(r'\s+', ' ', title).strip()
    tw_now = custom_created_at or get_taiwan_now_str()
    try:
        cursor.execute("SELECT id FROM videos WHERE video_url = ?", (video_url,))
        existing = cursor.fetchone()
        
        if existing is None:
            cursor.execute("""
                INSERT INTO videos (title, cover_url, tags, actress, video_url, release_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (clean_title, cover_url, tags, actress, video_url, release_date, tw_now))
            conn.commit()
            return True
        else:
            cursor.execute("""
                UPDATE videos SET
                    title = ?,
                    cover_url = CASE WHEN ? != '' THEN ? ELSE cover_url END,
                    tags = CASE WHEN ? != '' AND ? != '一般' THEN ? ELSE tags END,
                    actress = CASE WHEN ? != '' AND ? != '未知' THEN ? ELSE actress END,
                    release_date = CASE WHEN ? != '' THEN ? ELSE release_date END
                WHERE video_url = ?
            """, (
                clean_title,
                cover_url, cover_url,
                tags, tags, tags,
                actress, actress, actress,
                release_date, release_date,
                video_url
            ))
            conn.commit()
            return False
    finally:
        conn.close()

def get_all_videos():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 依最新收錄時間由新到舊排序，確保新發現的影片永遠在最前面
    cursor.execute("""
        SELECT id, title, cover_url, tags, actress, video_url, COALESCE(release_date, ''), created_at 
        FROM videos 
        ORDER BY created_at DESC, id DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

def sync_to_google_sheet(new_videos):
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
            sheet.append_row(["標題", "女優", "標籤", "發布日期(台灣時間)", "影片連結", "封面圖片", "爬取時間(台灣時間)"])
            existing_urls = set()
            current_row_index = 2
        else:
            existing_urls = {col for row in existing_rows[1:] for col in row if str(col).startswith('http')}
            current_row_index = len(existing_rows) + 1

        all_videos = get_all_videos()
        added_count = 0

        for v in all_videos:
            vid_id, title, cover_url, tags, actress, video_url, release_date, created_at = v
            if video_url not in existing_urls:
                image_formula = f'=IMAGE("{cover_url}")' if cover_url else ""
                sheet.append_row([
                    title, actress or "未知", tags or "", release_date or "未知", video_url, image_formula, created_at
                ], value_input_option='USER_ENTERED')
                
                try:
                    sheet.format(f"F{current_row_index}", {"textFormat": {"fontSize": 10}})
                    sheet.update_row_height(current_row_index, 120)
                except Exception:
                    pass
                
                current_row_index += 1
                added_count += 1

        if added_count > 0:
            print(f"✅ 成功同步 {added_count} 筆影片至 Google 試算表！")
        else:
            print("✅ Google 試算表已包含所有資料庫影片，無需同步。")
    except Exception as e:
        print(f"❌ 同步 Google 試算表失敗: {e}")

def generate_index_html():
    videos = get_all_videos()
    all_actresses = set()
    all_tags_set = set()
    cards_html = ""
    now_tw = get_taiwan_now()
    current_build_time = now_tw.strftime("%Y-%m-%d %H:%M:%S")
    
    for v in videos:
        vid_id, title, cover_url, tags, actress, video_url, release_date, created_at = v
        
        actress_display = actress.strip() if actress and actress.strip() else "未知/多人"
        if actress:
            for act in actress.split(','):
                cleaned_act = act.strip()
                if cleaned_act and cleaned_act != "未知":
                    all_actresses.add(cleaned_act)
        
        if tags:
            for t in tags.split(','):
                cleaned_tag = t.strip()
                if cleaned_tag and cleaned_tag != "一般":
                    all_tags_set.add(cleaned_tag)

        crawled_full = created_at if created_at else ""
        release_date_str = release_date.strip() if release_date and release_date.strip() else "未知"
        cover_img = cover_url if cover_url else "https://via.placeholder.com/300x180?text=No+Cover"

        # 判斷是否為 24 小時內新收錄的影片，顯示醒目標籤
        is_recent_new = False
        if crawled_full:
            try:
                crawled_dt = datetime.strptime(crawled_full[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIWAN_TZ)
                if (now_tw - crawled_dt).total_seconds() <= 86400:
                    is_recent_new = True
            except Exception:
                pass

        new_badge_html = '<span class="new-badge">🆕 新收錄</span>' if is_recent_new else ''

        safe_title_attr = html.escape(title.lower(), quote=True)
        safe_title_text = html.escape(title)
        safe_actress_attr = html.escape(actress_display.lower(), quote=True)
        safe_actress_text = html.escape(actress_display)
        safe_tags_attr = html.escape((tags or '').lower(), quote=True)
        safe_tags_text = html.escape(tags or '無標籤')

        cards_html += f"""
        <div class="card{' card-new' if is_recent_new else ''}" data-title="{safe_title_attr}" data-actress="{safe_actress_attr}" data-tags="{safe_tags_attr}" data-release="{release_date_str}" data-crawled="{crawled_full}">
            <div class="img-wrapper">
                {new_badge_html}
                <img src="{cover_img}" alt="封面" loading="lazy" referrerpolicy="no-referrer">
            </div>
            <div class="card-body">
                <div class="title" title="{safe_title_attr}">{safe_title_text}</div>
                <div class="meta-row">
                    <span class="badge actress-badge">👩 {safe_actress_text}</span>
                    <span class="date" title="影片上架/發行日期 (台灣時間)">📅 {release_date_str}</span>
                </div>
                <div class="crawled-time">🕒 收錄時間 (台灣): {crawled_full}</div>
                <div class="tags" title="{safe_tags_attr}">🏷️ {safe_tags_text}</div>
                <div class="footer">
                    <a href="{video_url}" target="_blank" class="btn">前往觀看</a>
                </div>
            </div>
        </div>
        """

    actress_options = "".join([f'<option value="{html.escape(act.lower(), quote=True)}">{html.escape(act)}</option>' for act in sorted(all_actresses)])
    
    checkboxes_html = "".join([
        f'<label class="checkbox-label"><input type="checkbox" name="keyword" value="{html.escape(tag, quote=True)}"> {html.escape(tag)}</label>' 
        for tag in sorted(all_tags_set)
    ]) if all_tags_set else '<span style="color: #94a3b8; font-size: 0.85rem;">尚無標籤資料</span>'

    html_template = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Jable & MissAv 影片自動典藏庫 - 台灣時間 (UTC+8)</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0b0f19; color: #f8fafc; padding: 24px 16px; }
        .header { text-align: center; margin-bottom: 24px; }
        .header h1 { font-size: 1.8rem; color: #38bdf8; margin-bottom: 6px; }
        .header p { color: #94a3b8; font-size: 0.9rem; }
        .build-time { font-size: 0.85rem; color: #38bdf8; margin-top: 6px; font-weight: 500; }
        .filter-panel { max-width: 1400px; margin: 0 auto 28px auto; background: #1e293b; padding: 18px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); display: flex; flex-direction: column; gap: 16px; }
        .filter-row { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; justify-content: space-between; }
        .filter-group { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; flex-grow: 1; }
        .filter-item { display: flex; align-items: center; gap: 8px; }
        .filter-item label { font-size: 0.85rem; color: #94a3b8; white-space: nowrap; }
        .filter-item select, .search-box { background: #0f172a; border: 1px solid #334155; color: #f1f5f9; padding: 8px 12px; border-radius: 6px; font-size: 0.85rem; outline: none; }
        .search-box { width: 280px; transition: border-color 0.2s; }
        .search-box:focus { border-color: #38bdf8; }
        .checkbox-container { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; background: #0f172a; padding: 10px 14px; border-radius: 6px; border: 1px solid #334155; max-height: 140px; overflow-y: auto; flex-grow: 1; }
        .checkbox-label { font-size: 0.85rem; color: #cbd5e1; display: flex; align-items: center; gap: 4px; cursor: pointer; }
        .checkbox-label input { cursor: pointer; }
        .reset-btn { background: #475569; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }
        .reset-btn:hover { background: #64748b; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; max-width: 1400px; margin: 0 auto; }
        .card { background: #1e293b; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); display: flex; flex-direction: column; transition: transform 0.2s; border: 1px solid transparent; }
        .card-new { border-color: #38bdf8; box-shadow: 0 0 12px rgba(56, 189, 248, 0.25); }
        .card:hover { transform: translateY(-4px); }
        .img-wrapper { position: relative; width: 100%; height: 180px; background-color: #334155; }
        .img-wrapper img { width: 100%; height: 100%; object-fit: cover; }
        .new-badge { position: absolute; top: 8px; left: 8px; background: #ef4444; color: #fff; font-size: 0.72rem; font-weight: 700; padding: 3px 8px; border-radius: 4px; box-shadow: 0 2px 4px rgba(0,0,0,0.4); z-index: 2; }
        .card-body { padding: 14px; flex-grow: 1; display: flex; flex-direction: column; justify-content: space-between; }
        .title { font-size: 0.95rem; font-weight: 600; line-height: 1.4; color: #f1f5f9; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-bottom: 8px; }
        .meta-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; font-size: 0.75rem; }
        .crawled-time { font-size: 0.72rem; color: #64748b; margin-bottom: 8px; }
        .actress-badge { background: #0284c7; color: white; padding: 2px 8px; border-radius: 4px; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .date { color: #94a3b8; }
        .tags { font-size: 0.75rem; color: #94a3b8; background: #0f172a; padding: 4px 8px; border-radius: 4px; margin-bottom: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .footer { display: flex; justify-content: flex-end; margin-top: auto; }
        .btn { background: #0284c7; color: #fff; text-decoration: none; padding: 6px 14px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; }
        .btn:hover { background: #0369a1; }
        .empty-msg { text-align: center; grid-column: 1 / -1; color: #94a3b8; padding: 40px; font-size: 1rem; display: none; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Jable & MissAv 影片自動典藏庫</h1>
        <p>目前庫存 <span id="total-count">{total_count}</span> 部影片 | 篩選顯示 <span id="visible-count">{visible_count}</span> 部</p>
        <div class="build-time">🕒 最後更新時間 (台灣時間): {build_time}</div>
    </div>
    <div class="filter-panel">
        <div class="filter-row">
            <div class="filter-group">
                <div class="filter-item">
                    <label for="search-input">🔍 番號/片名搜尋:</label>
                    <input type="text" id="search-input" class="search-box" placeholder="輸入番號或片名 (例: IPZZ-963)">
                </div>
            </div>
            <button class="reset-btn" onclick="resetFilters()">重設條件</button>
        </div>
        <div class="filter-row">
            <div class="filter-group">
                <div class="filter-item" style="width: 100%;">
                    <label style="min-width: 80px;">Tags 複選:</label>
                    <div class="checkbox-container">
                        {checkboxes_html}
                    </div>
                </div>
            </div>
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
                    <label for="sort-select">排序方式:</label>
                    <select id="sort-select">
                        <option value="crawled-desc" selected>新收錄優先：最新 → 最舊 (預設)</option>
                        <option value="crawled-asc">收錄時間：最舊 → 最新</option>
                        <option value="release-desc">影片上架：最新 → 最舊</option>
                        <option value="release-asc">影片上架：最舊 → 最新</option>
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
        const searchInput = document.getElementById('search-input');
        const keywordCheckboxes = document.querySelectorAll('input[name="keyword"]');
        const actressSelect = document.getElementById('actress-select');
        const sortSelect = document.getElementById('sort-select');
        const visibleCountSpan = document.getElementById('visible-count');
        const emptyMsg = document.getElementById('empty-msg');
        const grid = document.getElementById('video-grid');
        const cards = Array.from(document.querySelectorAll('.card'));

        function applyFilter() {
            const query = searchInput.value.trim().toLowerCase();
            const selectedKeywords = Array.from(keywordCheckboxes)
                .filter(cb => cb.checked)
                .map(cb => cb.value.toLowerCase());
            const actressVal = actressSelect.value.trim().toLowerCase();
            
            let visibleCount = 0;
            cards.forEach(card => {
                const title = card.getAttribute('data-title') || '';
                const actress = card.getAttribute('data-actress') || '';
                const tags = card.getAttribute('data-tags') || '';
                
                const matchSearch = !query || title.includes(query) || actress.includes(query);
                const matchKeyword = selectedKeywords.length === 0 || selectedKeywords.some(kw => tags.includes(kw));
                const matchActress = !actressVal || actress.includes(actressVal);

                if (matchSearch && matchKeyword && matchActress) {
                    card.style.display = 'flex';
                    visibleCount++;
                } else {
                    card.style.display = 'none';
                }
            });
            visibleCountSpan.textContent = visibleCount;
            emptyMsg.style.display = visibleCount === 0 ? 'block' : 'none';
        }

        function applySort() {
            const sortMode = sortSelect.value;
            const sortedCards = cards.slice().sort((a, b) => {
                const crawledA = a.getAttribute('data-crawled') || '';
                const crawledB = b.getAttribute('data-crawled') || '';
                const releaseA = a.getAttribute('data-release') || '';
                const releaseB = b.getAttribute('data-release') || '';

                if (sortMode === 'crawled-desc') {
                    return crawledB.localeCompare(crawledA);
                } else if (sortMode === 'crawled-asc') {
                    return crawledA.localeCompare(crawledB);
                } else if (sortMode === 'release-desc') {
                    if (releaseA === '未知' && releaseB !== '未知') return 1;
                    if (releaseB === '未知' && releaseA !== '未知') return -1;
                    return releaseB.localeCompare(releaseA) || crawledB.localeCompare(crawledA);
                } else if (sortMode === 'release-asc') {
                    if (releaseA === '未知' && releaseB !== '未知') return 1;
                    if (releaseB === '未知' && releaseA !== '未知') return -1;
                    return releaseA.localeCompare(releaseB) || crawledA.localeCompare(crawledB);
                }
                return 0;
            });
            sortedCards.forEach(card => grid.appendChild(card));
        }

        function resetFilters() {
            searchInput.value = '';
            keywordCheckboxes.forEach(cb => cb.checked = false);
            actressSelect.value = '';
            sortSelect.value = 'crawled-desc';
            applySort();
            applyFilter();
        }

        searchInput.addEventListener('input', applyFilter);
        keywordCheckboxes.forEach(cb => cb.addEventListener('change', applyFilter));
        actressSelect.addEventListener('change', applyFilter);
        sortSelect.addEventListener('change', () => { applySort(); applyFilter(); });

        applySort();
        applyFilter();
    </script>
</body>
</html>
"""
    html_content = html_template.replace("{total_count}", str(len(videos))) \
                                .replace("{visible_count}", str(len(videos))) \
                                .replace("{build_time}", current_build_time) \
                                .replace("{checkboxes_html}", checkboxes_html) \
                                .replace("{actress_options}", actress_options) \
                                .replace("{cards_html}", cards_html)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✅ 已成功產出最新動態 index.html（共收錄 {len(videos)} 部影片，台灣時間: {current_build_time}）")

def send_email_report(new_videos):
    if not new_videos or not SENDER_EMAIL or not SENDER_PASSWORD:
        return
    subject = f"【每日影片爬蟲報告】今日新發現 {len(new_videos)} 部符合條件的影片"
    items_html = "".join([
        f"""<div style="border:1px solid #e2e8f0; border-radius:8px; padding:12px; margin-bottom:12px;">
            <img src="{v['cover_url']}" style="width:160px; height:100px; object-fit:cover; border-radius:4px;" referrerpolicy="no-referrer">
            <h4 style="margin:6px 0;">{v['title']} (女優: {v['actress']})</h4>
            <p style="margin:4px 0; color:#64748b; font-size:12px;">上架日期: {v.get('release_date', '未知')}</p>
            <a href="{v['video_url']}" target="_blank">前往觀看</a>
        </div>""" for v in new_videos
    ])
    html_content = f"<h2>今日更新通知 (台灣時間: {get_taiwan_now_str()})</h2><p>本次新收錄 <strong>{len(new_videos)}</strong> 部：</p>{items_html}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        print("✅ 郵件寄送成功！")
    except Exception as e:
        print(f"❌ 郵件寄送失敗: {e}")

async def is_cf_challenge(page):
    try:
        title = (await page.title()).lower()
        if any(k in title for k in [
            "just a moment", "請稍候", "请稍候", "checking your browser",
            "challenge", "attention required", "verify you are human"
        ]):
            return True
        
        cf_cnt = await page.locator(
            '#challenge-running, #challenge-form, #cf-turnstile, iframe[src*="challenges.cloudflare.com"], #cf-challenge-running'
        ).count()
        if cf_cnt > 0:
            return True
            
        content = (await page.content()).lower()
        if "cf-browser-verification" in content or "checking your browser before accessing" in content:
            return True
            
        return False
    except Exception:
        return False

async def wait_until_cf_cleared(page, max_wait_sec=45):
    for _ in range(max_wait_sec):
        cf_active = await is_cf_challenge(page)
        if not cf_active:
            if await page.locator('.video-img-box, a[href*="/videos/"], h1, h4, .video-info').count() > 0:
                return True
            title = await page.title()
            if title.strip() and title.strip().lower() != "jable.tv":
                return True

        try:
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    checkbox = frame.locator('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage')
                    if await checkbox.count() > 0:
                        await checkbox.first.click(timeout=2000)
                        await page.wait_for_timeout(2000)
                        break
        except Exception:
            pass

        await page.wait_for_timeout(1000)

    return not await is_cf_challenge(page)

async def safe_navigate(page, url, is_category=True, max_retries=2):
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            cleared = await wait_until_cf_cleared(page, max_wait_sec=40)
            
            if is_category:
                try:
                    await page.wait_for_selector('.video-img-box, a[href*="/videos/"], div.col-6', timeout=15000)
                except Exception:
                    pass

            await page.wait_for_timeout(2000)

            if is_category:
                has_boxes = await page.locator('.video-img-box, a[href*="/videos/"]').count()
                if has_boxes == 0 and attempt < max_retries:
                    print(f"    ⚠️ 第 {attempt} 次導航未抓到影片元素，重新整理重試中...")
                    await page.wait_for_timeout(3000)
                    continue

            if not cleared:
                title = await page.title()
                print(f"    ⚠️ Cloudflare 驗證超時 ({url})，當前標題: '{title}'")
                if attempt < max_retries:
                    await page.wait_for_timeout(3000)
                    continue
                return False

            return True
        except Exception as e:
            print(f"    ⚠️ 導航失敗 ({url}, 第 {attempt} 次): {e}")
            if attempt < max_retries:
                await page.wait_for_timeout(3000)
                continue
            return False
    return False

def extract_field_after_label(soup, label):
    pattern = re.compile(rf'{label}\s*[:：]')
    for node in soup.find_all(string=pattern):
        container = node.find_parent(['div', 'p', 'li', 'tr', 'section'])
        if not container:
            container = node.parent
        if container and container.name in ['span', 'b', 'strong', 'label', 'a']:
            container = container.parent

        if container:
            a_texts = [
                a.get_text(strip=True) for a in container.find_all('a') 
                if a.get_text(strip=True) and not re.search(pattern, a.get_text(strip=True))
            ]
            if a_texts:
                return ", ".join(list(dict.fromkeys(a_texts)))
            
            text = container.get_text(" ", strip=True)
            m = re.search(rf'{label}\s*[:：]\s*(.+)', text)
            if m:
                val = m.group(1).strip()
                val = re.split(r'\s+(?:類型|女優|番號|標籤|發行日期|發布日期|導演|片商)[:：]', val)[0]
                return val.strip()
    return ""

def extract_jable_release_date(detail_soup, card_sub_title=""):
    """精準擷取 Jable 影片上架/發布日期（轉換為台灣日期格式 YYYY-MM-DD）"""
    for container in detail_soup.select('.info-header, .video-info, .header-tags, div.header-left'):
        for s in container.find_all(['span', 'p', 'div']):
            text = s.get_text(strip=True)
            m = re.search(r'\b(20\d{2}[-/]\d{2}[-/]\d{2})\b', text)
            if m:
                return m.group(1).replace('/', '-')
            
            tw_today = get_taiwan_now().date()
            m_days = re.search(r'(\d+)\s*天前', text)
            if m_days:
                return str(tw_today - timedelta(days=int(m_days.group(1))))
            m_months = re.search(r'(\d+)\s*個月前', text)
            if m_months:
                return str(tw_today - timedelta(days=int(m_months.group(1)) * 30))
            m_years = re.search(r'(\d+)\s*年前', text)
            if m_years:
                return str(tw_today - timedelta(days=int(m_years.group(1)) * 365))

    if card_sub_title:
        m = re.search(r'\b(20\d{2}[-/]\d{2}[-/]\d{2})\b', card_sub_title)
        if m:
            return m.group(1).replace('/', '-')
        tw_today = get_taiwan_now().date()
        m_days = re.search(r'(\d+)\s*天前', card_sub_title)
        if m_days:
            return str(tw_today - timedelta(days=int(m_days.group(1))))

    return ""

def extract_missav_release_date(soup):
    """精準擷取 MissAv 影片發行日期"""
    for label in ["發行日期", "發布日期", "上映日期"]:
        val = extract_field_after_label(soup, label)
        if val:
            m = re.search(r'\b(20\d{2}[-/]\d{2}[-/]\d{2})\b', val)
            if m:
                return m.group(1).replace('/', '-')
    
    for prop in ["og:release_date", "video:release_date"]:
        meta = soup.select_one(f'meta[property="{prop}"], meta[name="{prop}"]')
        if meta and meta.get("content"):
            m = re.search(r'\b(20\d{2}[-/]\d{2}[-/]\d{2})\b', meta["content"])
            if m:
                return m.group(1).replace('/', '-')
    return ""

async def run_scraper():
    init_db()
    jable_target_paths = ["/tags/pantyhose/", "/tags/black-pantyhose/", "/tags/footjob/"]
    target_genre_keywords = ["ol", "絲襪", "丝袜", "腳交", "脚交", "足交", "戀足", "恋足", "pantyhose", "footjob"]

    seen_urls = set()
    newly_added_videos = []
    run_base_time = get_taiwan_now()

    async with Stealth().use_async(async_playwright()) as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars",
                    "--disable-dev-shm-usage",
                    "--lang=zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-TW",
                timezone_id="Asia/Taipei"
            )
            page = await context.new_page()

            # ==========================================
            # 0️⃣ Jable 首頁預熱
            # ==========================================
            print(f"🌐 [{get_taiwan_now_str()}] 正在連線 Jable.tv 首頁預熱與建立連線憑證...")
            await safe_navigate(page, "https://jable.tv/", is_category=False, max_retries=2)
            await page.wait_for_timeout(3000)

            # ==========================================
            # 1️⃣ 爬取 Jable.tv (前 2 頁)
            # ==========================================
            print(f"🌐 [{get_taiwan_now_str()}] 開始掃描 Jable.tv 清單頁...")
            for page_num in range(1, 3):
                target_url = "https://jable.tv/categories/chinese-subtitle/" if page_num == 1 else f"https://jable.tv/categories/chinese-subtitle/{page_num}/"
                print(f"正在掃描 Jable 第 {page_num}/2 頁: {target_url}")

                nav_ok = await safe_navigate(page, target_url, is_category=True, max_retries=2)
                if not nav_ok:
                    print(f"  ❌ 無法進入 Jable 第 {page_num} 頁，跳過該頁")
                    continue

                for _ in range(4):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    await page.wait_for_timeout(1000)

                soup = BeautifulSoup(await page.content(), 'html.parser')
                video_boxes = soup.select('.video-img-box')
                if not video_boxes:
                    video_boxes = soup.select('div.col-6.col-sm-4.col-lg-3, div[class*="video-img-box"]')

                print(f"  -> Jable 第 {page_num} 頁抓到 {len(video_boxes)} 個影片方塊")

                for box in video_boxes:
                    link_elem = box.find('a', href=True)
                    if link_elem and '/videos/' in link_elem['href']:
                        full_url = link_elem['href'] if link_elem['href'].startswith('http') else f"https://jable.tv{link_elem['href']}"
                        
                        sub_title_span = box.select_one('.sub-title, .inactive-color')
                        card_sub_title = sub_title_span.get_text(strip=True) if sub_title_span else ""

                        if full_url not in seen_urls:
                            seen_urls.add(full_url)
                            try:
                                await page.wait_for_timeout(random.randint(1200, 2500))
                                await safe_navigate(page, full_url, is_category=False, max_retries=2)
                                detail_soup = BeautifulSoup(await page.content(), 'html.parser')
                                
                                title_elem = detail_soup.select_one('h4') or detail_soup.select_one('h1') or detail_soup.find('title')
                                video_title = title_elem.text.strip().replace(" - Jable.tv", "").strip() if title_elem else "未知標題"

                                img_meta = detail_soup.select_one('meta[property="og:image"]')
                                cover_image_url = img_meta.get('content') if img_meta else ""

                                parts = video_title.split()
                                actress_name = parts[-1].strip() if parts else "未知"
                                release_date = extract_jable_release_date(detail_soup, card_sub_title)

                                tag_links = detail_soup.select('h5.tags.h6-md a[href]')
                                tags_list = []
                                has_target_tag = False

                                for a_elem in tag_links:
                                    href = a_elem.get('href', '')
                                    t_text = a_elem.text.strip()
                                    if t_text and "此作品曾在本站上傳" not in t_text:
                                        tags_list.append(t_text)
                                    if any(path in href for path in jable_target_paths):
                                        has_target_tag = True

                                tags_str = ", ".join(tags_list) if tags_list else "一般"

                                if has_target_tag:
                                    # 依發現順序給予遞減 1 秒的時間戳，確保最先出現的新片排在第一位
                                    item_ts = (run_base_time - timedelta(seconds=len(newly_added_videos))).strftime("%Y-%m-%d %H:%M:%S")
                                    if save_video(video_title, cover_image_url, tags_str, actress_name, full_url, release_date, custom_created_at=item_ts):
                                        newly_added_videos.append({
                                            "title": video_title, "cover_url": cover_image_url,
                                            "tags": tags_str, "actress": actress_name, "video_url": full_url,
                                            "release_date": release_date
                                        })
                                        print(f"  🆕 [Jable 新收錄置頂] {video_title} (上架: {release_date or '未知'}) (標籤: {tags_str})")
                                    else:
                                        print(f"  📌 [Jable 已存在] {video_title}")
                            except Exception:
                                continue

            # ==========================================
            # 2️⃣ 爬取 MissAv (第 1 頁與第 2 頁)
            # ==========================================
            print(f"🌐 [{get_taiwan_now_str()}] 開始掃描 MissAv (https://missav.ws/dm278/chinese-subtitle)...")
            for page_num in range(1, 3):
                missav_url = "https://missav.ws/dm278/chinese-subtitle" if page_num == 1 else f"https://missav.ws/dm278/chinese-subtitle?page={page_num}"
                print(f"正在掃描 MissAv 第 {page_num}/2 頁: {missav_url}")

                nav_ok = await safe_navigate(page, missav_url, is_category=True, max_retries=2)
                if not nav_ok:
                    print(f"  ❌ 無法進入 MissAv 第 {page_num} 頁，跳過該頁")
                    continue

                for _ in range(4):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    await page.wait_for_timeout(1500)

                soup = BeautifulSoup(await page.content(), 'html.parser')
                
                candidate_urls = []
                for a in soup.find_all('a', href=True):
                    href = a.get('href', '').strip()
                    
                    if any(p in href for p in ['/genres/', '/tags/', '/actresses/', '/actress/', '/makers/', '/series/', 'login', 'search', 'page=']):
                        continue
                    
                    if re.search(r'[a-zA-Z0-9]+-\d+', href):
                        full_url = href if href.startswith('http') else f"https://missav.ws{href if href.startswith('/') else '/' + href}"
                        full_url = full_url.split('?')[0].rstrip('/')
                        if full_url not in seen_urls:
                            seen_urls.add(full_url)
                            candidate_urls.append(full_url)

                print(f"  -> MissAv 第 {page_num} 頁精準辨識出 {len(candidate_urls)} 部待檢查影片")

                for video_url in candidate_urls:
                    try:
                        await page.wait_for_timeout(random.randint(1000, 2000))
                        await safe_navigate(page, video_url, is_category=False, max_retries=2)
                        detail_soup = BeautifulSoup(await page.content(), 'html.parser')
                        
                        if detail_soup.find('header'):
                            detail_soup.find('header').decompose()
                        if detail_soup.find('footer'):
                            detail_soup.find('footer').decompose()

                        # 1. 標題
                        title_elem = detail_soup.select_one('h1') or detail_soup.find('title')
                        video_title = title_elem.text.strip().replace(" - MissAV", "").replace(" - MissAV.com", "").strip() if title_elem else "未知標題"

                        # 2. 封面圖片
                        img_meta = detail_soup.select_one('meta[property="og:image"]') or detail_soup.select_one('meta[name="twitter:image"]')
                        cover_image_url = img_meta.get('content') if img_meta else ""
                        if not cover_image_url:
                            v_tag = detail_soup.find('video')
                            if v_tag and v_tag.get('poster'):
                                cover_image_url = v_tag['poster']

                        # 3. 女優名稱
                        actress_name = extract_field_after_label(detail_soup, "女優")
                        if not actress_name:
                            actress_elems = detail_soup.select('a[href*="/actresses/"], a[href*="/actress/"]')
                            actresses = [a.text.strip() for a in actress_elems if a.text.strip()]
                            actress_name = ", ".join(list(dict.fromkeys(actresses))) if actresses else "未知"

                        # 4. 發布日期
                        release_date = extract_missav_release_date(detail_soup)

                        # 5. 類型標籤
                        tags_str = extract_field_after_label(detail_soup, "類型")
                        if not tags_str:
                            genre_elems = detail_soup.select('a[href*="/genres/"]')
                            genres_list = [g.text.strip() for g in genre_elems if g.text.strip()]
                            tags_str = ", ".join(list(dict.fromkeys(genres_list))) if genres_list else "一般"

                        tags_lower = tags_str.lower()
                        has_genre_match = any(kw in tags_lower for kw in target_genre_keywords)

                        if not has_genre_match:
                            for g in detail_soup.select('a[href*="/genres/"]'):
                                g_href = g.get('href', '').lower()
                                if any(slug in g_href for slug in ["/ol", "/pantyhose", "/black-pantyhose", "/footjob", "/feet"]):
                                    has_genre_match = True
                                    break

                        if has_genre_match:
                            item_ts = (run_base_time - timedelta(seconds=len(newly_added_videos))).strftime("%Y-%m-%d %H:%M:%S")
                            if save_video(video_title, cover_image_url, tags_str, actress_name, video_url, release_date, custom_created_at=item_ts):
                                newly_added_videos.append({
                                    "title": video_title, "cover_url": cover_image_url,
                                    "tags": tags_str, "actress": actress_name, "video_url": video_url,
                                    "release_date": release_date
                                })
                                print(f"  🆕 [MissAv 新收錄置頂] {video_title} (女優: {actress_name}) (發布: {release_date or '未知'}) (類型: {tags_str})")
                            else:
                                print(f"  📌 [MissAv 已存在] {video_title}")
                        else:
                            print(f"  ⚪ [MissAv 未符合] {video_title} (類型: {tags_str})")

                    except Exception as e:
                        print(f"    ⚠️ 解析 MissAv 內頁失敗 ({video_url}): {e}")
                        continue

            print(f"✅ 雙平台爬取結束！本次新收錄並置頂 {len(newly_added_videos)} 部影片")
            
            sync_to_google_sheet(newly_added_videos)
            generate_index_html()
            send_email_report(newly_added_videos)

        finally:
            if 'browser' in locals() and browser:
                await browser.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())
