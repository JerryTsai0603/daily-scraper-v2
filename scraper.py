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
    """智慧同步：自動比對 Google 試算表與資料庫，將遺漏的影片全部補齊（圖片使用 =IMAGE() 放大顯示）"""
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
                
                # 自動將寫入該筆資料的列高放大至 120 像素
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
    <title>Jable 影片典藏庫 - 智慧篩選</title>
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
        <h1>Jable 影片自動典藏庫</h1>
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
        const actressSelect = document.getElementById('actress-select');
        const dateSort = document.getElementById('date-sort');
        const visibleCountSpan = document.getElementById('visible-count');
        const emptyMsg = document.getElementById('empty-msg');
        const grid = document.getElementById('video-grid');
        const cards = Array.from(document.querySelectorAll('.card'));

        function applyFilter() {{
            const selectedKeywords = Array.from(keywordCheckboxes)
                .filter(cb => cb.checked)
                .map(cb => cb.value.toLowerCase());
            const actressVal = actressSelect.value.trim().toLowerCase();
            
            let visibleCount = 0;
            cards.forEach(card => {{
                const actress = card.getAttribute('data-actress') || '';
                const tags = card.getAttribute('data-tags') || '';
                
                const matchKeyword = selectedKeywords.length === 0 || selectedKeywords.some(kw => tags.includes(kw));
                const matchActress = !actressVal || actress.includes(actressVal);

                if (matchKeyword && matchActress) {{
                    card.style.display = 'flex';
                    visibleCount++;
                }} else {{
                    card.style.display = 'none';
                }}
            }});
            visibleCountSpan.textContent = visibleCount;
            emptyMsg.style.display = visibleCount === 0 ? 'block' : 'none';
        }}

        function applySort() {{
            const isOldest = dateSort.value === 'oldest';
            const sortedCards = cards.slice().sort((a, b) => {{
                const dateA = a.getAttribute('data-date') || '';
                const dateB = b.getAttribute('data-date') || '';
                return isOldest ? dateA.localeCompare(dateB) : dateB.localeCompare(dateA);
            }});
            sortedCards.forEach(card => grid.appendChild(card));
        }}

        function resetFilters() {{
            keywordCheckboxes.forEach(cb => cb.checked = false);
            actressSelect.value = '';
            dateSort.value = 'newest';
            applySort();
            applyFilter();
        }}

        keywordCheckboxes.forEach(cb => cb.addEventListener('change', applyFilter));
        actressSelect.addEventListener('change', applyFilter);
        dateSort.addEventListener('change', () => {{ applySort(); applyFilter(); }});
    </script>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✅ 已成功產出最新動態 index.html（共收錄 {len(videos)} 部影片）")

def send_email_report(new_videos):
    if not new_videos or not SENDER_EMAIL or not SENDER_PASSWORD:
        return
    subject = f"【每日影片爬蟲報告】今日新增 {len(new_videos)} 部符合條件的影片"
    items_html = "".join([
        f"""<div style="border:1px solid #e2e8f0; border-radius:8px; padding:12px; margin-bottom:12px;">
            <img src="{v['cover_url']}" style="width:160px; height:100px; object-fit:cover; border-radius:4px;" referrerpolicy="no-referrer">
            <h4 style="margin:6px 0;">{v['title']} (女優: {v['actress']})</h4>
            <a href="{v['video_url']}" target="_blank">前往觀看</a>
        </div>""" for v in new_videos
    ])
    html_content = f"<h2>今日更新通知</h2><p>本次新增 <strong>{len(new_videos)}</strong> 部：</p>{items_html}"

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

async def wait_until_cf_cleared(page, is_category_page=True, max_wait_sec=35):
    """精準雙語防護辨識：支援中英文 Cloudflare 盾牌，並確認目標內容載入完成"""
    for _ in range(max_wait_sec):
        title = await page.title()
        title_lower = title.lower()

        # 🎯 中英雙語盾牌關鍵字辨識
        is_cf = any(k in title_lower for k in [
            "just a moment", "請稍候", "请稍候", "checking your browser",
            "challenge", "cloudflare", "attention required"
        ])

        if is_cf:
            await page.wait_for_timeout(1000)
            continue

        # 盾牌已過，進一步確認頁面主體是否已經渲染
        if is_category_page:
            boxes = await page.query_selector_all('.video-img-box')
            if len(boxes) > 0:
                return True
        else:
            header = await page.query_selector('h4, h1, h5.tags')
            if header:
                return True

        await page.wait_for_timeout(1000)
    return False

async def safe_navigate(page, url, is_category=True):
    """安全導航：載入後靜止等待驗證通過，不重複重整以避免打斷 Cloudflare 計算"""
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        cleared = await wait_until_cf_cleared(page, is_category_page=is_category, max_wait_sec=35)
        if not cleared:
            curr_title = await page.title()
            print(f"    ⚠️ 驗證或內容載入超時 ({url})，當前標題: '{curr_title}'")
        return cleared
    except Exception as e:
        print(f"    ⚠️ 導航失敗 ({url}): {e}")
        return False

async def run_scraper():
    init_db()
    target_tags_paths = ["/tags/pantyhose/", "/tags/black-pantyhose/", "/tags/footjob/"]
    seen_urls = set()
    newly_added_videos = []

    async with Stealth().use_async(async_playwright()) as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-TW"
            )
            page = await context.new_page()

            # 🌐 預熱首頁建立 session 與通行 Cookie
            print("🌐 正在連線預熱...")
            await safe_navigate(page, "https://jable.tv/", is_category=False)
            await page.wait_for_timeout(2000)

            for page_num in range(1, 3):
                target_url = "https://jable.tv/categories/chinese-subtitle/" if page_num == 1 else f"https://jable.tv/categories/chinese-subtitle/{page_num}/"
                print(f"正在掃描第 {page_num}/2 頁: {target_url}")

                success = await safe_navigate(page, target_url, is_category=True)

                # 模擬真實滾動以加載包含 IPZZ-921 等動態加載項目
                for _ in range(4):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    await page.wait_for_timeout(1500)

                soup = BeautifulSoup(await page.content(), 'html.parser')
                video_boxes = soup.select('.video-img-box')
                print(f"  -> 第 {page_num} 頁總共抓到 {len(video_boxes)} 個 video-img-box")

                page_links = []
                for box in video_boxes:
                    link_elem = box.find('a', href=True)
                    if link_elem and '/videos/' in link_elem['href']:
                        full_url = link_elem['href'] if link_elem['href'].startswith('http') else f"https://jable.tv{link_elem['href']}"
                        if full_url not in seen_urls:
                            seen_urls.add(full_url)
                            page_links.append(full_url)

                print(f"第 {page_num} 頁共收集到 {len(page_links)} 個不重複影片連結，準備逐一進入頁面檢查...")

                for idx, video_url in enumerate(page_links, 1):
                    try:
                        # 進入影片詳細頁同樣確保避開盾牌
                        await safe_navigate(page, video_url, is_category=False)

                        detail_soup = BeautifulSoup(await page.content(), 'html.parser')
                        title_elem = detail_soup.select_one('h4') or detail_soup.select_one('h1') or detail_soup.find('title')
                        video_title = title_elem.text.strip().replace(" - Jable.tv", "").strip() if title_elem else "未知標題"

                        img_meta = detail_soup.select_one('meta[property="og:image"]')
                        cover_image_url = img_meta.get('content') if img_meta else ""

                        parts = video_title.split()
                        actress_name = parts[-1].strip() if parts else "未知"

                        tag_links = detail_soup.select('h5.tags.h6-md a[href]')
                        tags_list = []
                        has_target_tag = False
                        found_hrefs = []

                        for a_elem in tag_links:
                            href = a_elem.get('href', '')
                            t_text = a_elem.text.strip()
                            found_hrefs.append(href)
                            if t_text and "此作品曾在本站上傳" not in t_text:
                                tags_list.append(t_text)
                            
                            # 🎯 檢查超連結路徑是否包含指定的標籤
                            if any(path in href for path in target_tags_paths):
                                has_target_tag = True

                        tags_str = ", ".join(tags_list) if tags_list else "一般"

                        print(f"  [檢查] {video_title[:20]}... | 標籤數: {len(found_hrefs)} | 符合: {has_target_tag}")

                        if has_target_tag:
                            if save_video(video_title, cover_image_url, tags_str, actress_name, video_url):
                                newly_added_videos.append({
                                    "title": video_title,
                                    "cover_url": cover_image_url,
                                    "tags": tags_str,
                                    "actress": actress_name,
                                    "video_url": video_url
                                })
                                print(f"  🎯 [符合條件收錄] {video_title} (標籤: {tags_str})")
                    except Exception as e:
                        print(f"  ❌ 檢查失敗 {video_url}: {e}")
                        continue

            print(f"✅ 爬取結束！新收錄 {len(newly_added_videos)} 部影片")
            
            sync_to_google_sheet(newly_added_videos)
            generate_index_html()
            send_email_report(newly_added_videos)

        finally:
            if 'browser' in locals() and browser:
                await browser.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())
