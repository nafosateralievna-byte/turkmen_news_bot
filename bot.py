import os, sqlite3, logging, asyncio, re, requests, json
import xml.etree.ElementTree as ET
from datetime import datetime
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import telegram
from telegram import Update, Bot
from telegram.ext import Updater, CommandHandler, CallbackContext

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Sozlamalar ────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "8950489749:AAHKcHhXrnGP9UHjDRZq8fEH814z3dt-Ot0")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@THPforlazy")
ADMIN_IDS  = list(map(int, os.environ.get("ADMIN_IDS", "").split(","))) if os.environ.get("ADMIN_IDS") else []
SEND_HOUR  = int(os.environ.get("SEND_HOUR", "5"))
SEND_MIN   = int(os.environ.get("SEND_MIN",  "0"))
DB_PATH    = "news.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
}

# ── AI Tarjima ────────────────────────────────────────────────────────────────
def translate_to_uzbek(title, description):
    text = f"Sarlavha: {title}"
    if description:
        text += f"\nTavsif: {description}"
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": (
                    "Quyidagi yangilik sarlavhasi va tavsifini o'zbek tiliga tarjima qil. "
                    "Faqat JSON formatda qaytargin, boshqa hech narsa yozma:\n"
                    '{"title": "...", "description": "..."}\n\n' + text
                )}]
            },
            timeout=15
        )
        data = resp.json()
        raw = re.sub(r"```json|```", "", data["content"][0]["text"]).strip()
        result = json.loads(raw)
        return result.get("title", title), result.get("description", description)
    except Exception as e:
        log.warning(f"Tarjima xatosi: {e}")
        return title, description

# ── Database ──────────────────────────────────────────────────────────────────
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, url TEXT UNIQUE, added_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS seen (
        url TEXT PRIMARY KEY, site TEXT, added_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT, title TEXT, description TEXT, url TEXT UNIQUE, found_at TEXT)""")
    con.execute("INSERT OR IGNORE INTO sites(name,url,added_at) VALUES(?,?,?)",
                ("Хроника Туркменистана", "https://www.hronikatm.com",
                 datetime.utcnow().isoformat()))
    con.commit(); con.close()

def db_sites():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT id,name,url FROM sites ORDER BY id").fetchall()
    con.close()
    return [{"id": r[0], "name": r[1], "url": r[2]} for r in rows]

def db_add_site(name, url):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("INSERT INTO sites(name,url,added_at) VALUES(?,?,?)",
                    (name, url.rstrip("/"), datetime.utcnow().isoformat()))
        con.commit(); return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()

def db_remove_site(site_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("DELETE FROM sites WHERE id=?", (site_id,))
    con.commit(); con.close()
    return cur.rowcount > 0

def is_seen(url):
    con = sqlite3.connect(DB_PATH)
    r = con.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone()
    con.close(); return r is not None

def mark_seen(url, site):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR IGNORE INTO seen(url,site,added_at) VALUES(?,?,?)",
                (url, site, datetime.utcnow().isoformat()))
    con.commit(); con.close()

def save_pending(site, title, desc, url):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR IGNORE INTO pending(site,title,description,url,found_at) VALUES(?,?,?,?,?)",
                (site, title, desc, url, datetime.utcnow().isoformat()))
    con.commit(); con.close()

def pop_pending():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT id,site,title,description,url FROM pending ORDER BY id").fetchall()
    if rows:
        ids = [r[0] for r in rows]
        con.execute(f"DELETE FROM pending WHERE id IN ({','.join('?'*len(ids))})", ids)
        con.commit()
    con.close()
    return [{"site": r[1], "title": r[2], "description": r[3], "url": r[4]} for r in rows]

# ── RSS + HTML ────────────────────────────────────────────────────────────────
def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()

def try_rss(base_url):
    for path in ["/feed/", "/rss/", "/rss.xml", "/feed.xml", "/atom.xml", "/?feed=rss2"]:
        url = base_url.rstrip("/") + path
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200 and ("<rss" in r.text[:500] or "<feed" in r.text[:500]):
                return url, r.content
        except:
            continue
    return None, None

def parse_rss_content(content):
    items = []
    try:
        root = ET.fromstring(content)
        channel = root.find("channel")
        entries = channel.findall("item") if channel else root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in entries[:30]:
            title = strip_html(item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "")
            link  = (item.findtext("link") or "").strip()
            if not link:
                el = item.find("{http://www.w3.org/2005/Atom}link")
                link = (el.get("href", "") if el is not None else "").strip()
            desc = strip_html(item.findtext("description") or item.findtext("{http://www.w3.org/2005/Atom}summary") or "")[:300]
            if title and link:
                items.append({"title": title, "url": link, "description": desc})
    except Exception as e:
        log.warning(f"RSS parse xatosi: {e}")
    return items

def scrape_html(base_url, site_name):
    items = []
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        domain = re.search(r"https?://[^/]+", base_url).group()
        seen_hrefs = set()
        news_pattern = re.compile(r"/(20\d\d|news|article|post|yangilik|novost|haber)/", re.I)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                href = domain + "/" + href.lstrip("/")
            if href in seen_hrefs or not href.startswith(domain):
                continue
            seen_hrefs.add(href)
            text = a.get_text(strip=True)
            if len(text) < 15:
                parent = a.find_parent(["article", "div", "li"])
                if parent:
                    h = parent.find(["h1", "h2", "h3", "h4"])
                    text = h.get_text(strip=True) if h else text
            if len(text) < 15:
                continue
            is_news = (news_pattern.search(href) or
                       any(kw in href.lower() for kw in ["news","article","post","yangilik","novost"]))
            if is_news:
                desc = ""
                parent = a.find_parent(["article", "div", "li", "section"])
                if parent:
                    p = parent.find("p")
                    if p:
                        desc = p.get_text(strip=True)[:300]
                items.append({"title": text[:200], "url": href, "description": desc})
    except Exception as e:
        log.warning(f"[{site_name}] HTML scraping xatosi: {e}")
    return items[:40]

def fetch_site_news(site):
    _, rss_content = try_rss(site["url"])
    if rss_content:
        return parse_rss_content(rss_content)
    log.info(f"[{site['name']}] RSS topilmadi, HTML scraping...")
    return scrape_html(site["url"], site["name"])

# ── Asosiy funksiyalar ────────────────────────────────────────────────────────
def scrape_all():
    log.info("Tekshirish boshlandi...")
    total = 0
    for site in db_sites():
        try:
            items = fetch_site_news(site)
            count = 0
            for item in items:
                if not is_seen(item["url"]):
                    mark_seen(item["url"], site["name"])
                    uz_title, uz_desc = translate_to_uzbek(item["title"], item["description"])
                    save_pending(site["name"], uz_title, uz_desc, item["url"])
                    count += 1
            log.info(f"[{site['name']}] {count} ta yangi yangilik")
            total += count
        except Exception as e:
            log.error(f"[{site['name']}] Xato: {e}")
    log.info(f"Jami {total} ta yangilik saqlandi")

def send_news():
    news = pop_pending()
    if not news:
        log.info("Yuborish uchun yangilik yo'q"); return
    bot = Bot(token=BOT_TOKEN)
    header = f"📰 <b>Bugungi yangiliklar</b> — {datetime.now().strftime('%d.%m.%Y')}\n\n"
    current = header
    for i, n in enumerate(news, 1):
        desc = f"\n<i>{n['description'][:200]}</i>" if n["description"] else ""
        block = f"<b>{i}.</b> {n['title']}{desc}\n🔗 {n['url']}\n\n"
        if len(current) + len(block) > 4000:
            bot.send_message(chat_id=CHANNEL_ID, text=current,
                             parse_mode=telegram.ParseMode.HTML,
                             disable_web_page_preview=True)
            import time; time.sleep(2)
            current = block
        else:
            current += block
    if current.strip() and current != header:
        bot.send_message(chat_id=CHANNEL_ID, text=current,
                         parse_mode=telegram.ParseMode.HTML,
                         disable_web_page_preview=True)
    log.info("Yuborildi ✅")

# ── Admin tekshirish ──────────────────────────────────────────────────────────
def is_admin(update):
    if not ADMIN_IDS:
        return True
    return update.effective_user.id in ADMIN_IDS

# ── Komandalar ────────────────────────────────────────────────────────────────
def cmd_start(update: Update, ctx: CallbackContext):
    update.message.reply_text(
        "👋 <b>News Monitor Bot</b>\n\n"
        "📋 <b>Komandalar:</b>\n"
        "/list — saytlar ro'yxati\n"
        "/add URL — yangi sayt qo'shish\n"
        "/remove ID — saytni o'chirish\n"
        "/check — hozir tekshirib ko'rish\n"
        "/send — hozir kanalga yuborish\n"
        "/status — bot holati",
        parse_mode=telegram.ParseMode.HTML
    )

def cmd_list(update: Update, ctx: CallbackContext):
    sites = db_sites()
    if not sites:
        update.message.reply_text("📭 Saytlar ro'yxati bo'sh.\n/add URL bilan qo'shing")
        return
    text = "📋 <b>Kuzatiladigan saytlar:</b>\n\n"
    for s in sites:
        text += f"<b>{s['id']}.</b> {s['name']}\n🔗 {s['url']}\n\n"
    text += "O'chirish: /remove ID"
    update.message.reply_text(text, parse_mode=telegram.ParseMode.HTML)

def cmd_add(update: Update, ctx: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    if not ctx.args:
        update.message.reply_text("❗ URL kiriting:\n/add https://sayt.com"); return
    url = ctx.args[0].strip()
    if not url.startswith("http"):
        update.message.reply_text("❗ To'g'ri URL kiriting (https:// bilan boshlang)"); return
    msg = update.message.reply_text(f"🔍 Tekshirilmoqda...")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "lxml")
        title_el = soup.find("title") or soup.find("h1")
        name = title_el.get_text(strip=True)[:50] if title_el else url
    except:
        name = url
    _, rss = try_rss(url)
    info = "✅ RSS topildi" if rss else "⚠️ RSS topilmadi — HTML scraping ishlatiladi"
    ok = db_add_site(name, url)
    if ok:
        msg.edit_text(f"✅ <b>Qo'shildi!</b>\n\n📌 {name}\n🔗 {url}\n{info}",
                      parse_mode=telegram.ParseMode.HTML)
    else:
        msg.edit_text("⚠️ Bu sayt allaqachon ro'yxatda bor")

def cmd_remove(update: Update, ctx: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    if not ctx.args or not ctx.args[0].isdigit():
        update.message.reply_text("❗ ID kiriting:\n/remove 2"); return
    ok = db_remove_site(int(ctx.args[0]))
    update.message.reply_text("✅ O'chirildi" if ok else "❌ Topilmadi")

def cmd_check(update: Update, ctx: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    msg = update.message.reply_text("🔍 Tekshirilmoqda...")
    scrape_all()
    con = sqlite3.connect(DB_PATH)
    count = con.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    con.close()
    msg.edit_text(f"✅ Tekshirildi!\n📦 Kutayotgan: <b>{count} ta</b>",
                  parse_mode=telegram.ParseMode.HTML)

def cmd_send(update: Update, ctx: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    update.message.reply_text("📤 Yuborilmoqda...")
    send_news()
    update.message.reply_text("✅ Kanalga yuborildi!")

def cmd_status(update: Update, ctx: CallbackContext):
    con = sqlite3.connect(DB_PATH)
    sc = con.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    vc = con.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    pc = con.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    con.close()
    update.message.reply_text(
        f"📊 <b>Bot holati:</b>\n\n"
        f"🌐 Saytlar: <b>{sc} ta</b>\n"
        f"👁 Ko'rilgan: <b>{vc} ta</b>\n"
        f"📦 Kutayotgan: <b>{pc} ta</b>\n"
        f"⏰ Yuborish: <b>{SEND_HOUR+3:02d}:00 Toshkent</b>",
        parse_mode=telegram.ParseMode.HTML
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    db_init()
    log.info("Bot ishga tushdi 🚀")

    # Scheduler
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(scrape_all, "interval", hours=1)
    scheduler.add_job(send_news,  "cron", hour=SEND_HOUR, minute=SEND_MIN)
    scheduler.start()

    # Start xabari
    try:
        Bot(token=BOT_TOKEN).send_message(
            chat_id=CHANNEL_ID,
            text=f"✅ <b>News Monitor Bot ishga tushdi!</b>\n\n"
                 f"🌐 Saytlar: {len(db_sites())} ta\n"
                 f"📨 Har kuni {SEND_HOUR+3:02d}:00 Toshkent vaqtida yuboriladi",
            parse_mode=telegram.ParseMode.HTML
        )
    except Exception as e:
        log.warning(f"Start xabari xatosi: {e}")

    scrape_all()

    # Polling
    updater = Updater(token=BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start",  cmd_start))
    dp.add_handler(CommandHandler("list",   cmd_list))
    dp.add_handler(CommandHandler("add",    cmd_add))
    dp.add_handler(CommandHandler("remove", cmd_remove))
    dp.add_handler(CommandHandler("check",  cmd_check))
    dp.add_handler(CommandHandler("send",   cmd_send))
    dp.add_handler(CommandHandler("status", cmd_status))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
