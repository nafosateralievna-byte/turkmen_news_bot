import os, sqlite3, logging, asyncio, re, requests
import xml.etree.ElementTree as ET
from datetime import datetime
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Sozlamalar ────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "8950489749:AAHKcHhXrnGP9UHjDRZq8fEH814z3dt-Ot0")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@THPforlazy")
ADMIN_IDS  = list(map(int, os.environ.get("ADMIN_IDS", "").split(","))) if os.environ.get("ADMIN_IDS") else []
SEND_HOUR  = int(os.environ.get("SEND_HOUR", "5"))   # 05 UTC = 08:00 Toshkent
SEND_MIN   = int(os.environ.get("SEND_MIN",  "0"))
DB_PATH    = "news.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
}

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

    # Boshlang'ich sayt — agar bo'sh bo'lsa
    con.execute("INSERT OR IGNORE INTO sites(name,url,added_at) VALUES(?,?,?)",
                ("Хроника Туркменистана", "https://www.hronikatm.com", datetime.utcnow().isoformat()))
    con.commit(); con.close()

def db_sites():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT id,name,url FROM sites ORDER BY id").fetchall()
    con.close()
    return [{"id":r[0],"name":r[1],"url":r[2]} for r in rows]

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
    return [{"site":r[1],"title":r[2],"description":r[3],"url":r[4]} for r in rows]

# ── Saytni aqlli tekshirish ───────────────────────────────────────────────────
def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()

def try_rss(base_url):
    """RSS feed yo'llarini sinab ko'radi"""
    candidates = [
        base_url.rstrip("/") + "/feed/",
        base_url.rstrip("/") + "/rss/",
        base_url.rstrip("/") + "/rss.xml",
        base_url.rstrip("/") + "/feed.xml",
        base_url.rstrip("/") + "/atom.xml",
        base_url.rstrip("/") + "/?feed=rss2",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200 and ("<rss" in r.text[:500] or "<feed" in r.text[:500]):
                log.info(f"RSS topildi: {url}")
                return url, r.content
        except:
            continue
    return None, None

def parse_rss_content(content, site_name):
    items = []
    try:
        root = ET.fromstring(content)
        channel = root.find("channel")
        entries = channel.findall("item") if channel else root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in entries[:30]:
            title = strip_html(item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "")
            link  = (item.findtext("link") or "").strip()
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                link = (link_el.get("href","") if link_el is not None else "").strip()
            desc  = strip_html(item.findtext("description") or item.findtext("{http://www.w3.org/2005/Atom}summary") or "")[:300]
            if title and link:
                items.append({"title":title, "url":link, "description":desc})
    except Exception as e:
        log.warning(f"RSS parse xatosi: {e}")
    return items

def scrape_html(base_url, site_name):
    """RSS yo'q bo'lsa HTML scraping"""
    items = []
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Barcha ichki linkları topamiz
        domain = re.search(r"https?://[^/]+", base_url).group()
        seen_hrefs = set()

        # Yangilik-ga o'xshash linklar: /2024/, /news/, /article/ va h.k.
        news_pattern = re.compile(r"/(20\d\d|news|article|post|yangilik|novost|haber)/", re.I)

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                href = domain + "/" + href.lstrip("/")

            if href in seen_hrefs or not href.startswith(domain):
                continue
            seen_hrefs.add(href)

            # Sarlavha: link matni yoki yaqin h1/h2/h3
            text = a.get_text(strip=True)
            if len(text) < 15:  # juda qisqa — sarlavha emas
                parent = a.find_parent(["article","div","li"])
                if parent:
                    h = parent.find(["h1","h2","h3","h4"])
                    text = h.get_text(strip=True) if h else text

            if len(text) < 15:
                continue

            # Yangilikka o'xshaydimi?
            is_news = (news_pattern.search(href) or
                       any(kw in href.lower() for kw in ["news","article","post","yangilik","novost","haber","read"]))

            if is_news:
                # Tavsif qidirish
                desc = ""
                parent = a.find_parent(["article","div","li","section"])
                if parent:
                    p = parent.find("p")
                    if p:
                        desc = p.get_text(strip=True)[:300]

                items.append({"title": text[:200], "url": href, "description": desc})

        log.info(f"[{site_name}] HTML scraping: {len(items)} ta potensial yangilik")
    except Exception as e:
        log.warning(f"[{site_name}] HTML scraping xatosi: {e}")
    return items[:40]

def fetch_site_news(site):
    """Har bir sayt uchun: avval RSS, bo'lmasa HTML"""
    rss_url, rss_content = try_rss(site["url"])
    if rss_content:
        return parse_rss_content(rss_content, site["name"])
    else:
        log.info(f"[{site['name']}] RSS topilmadi, HTML scraping...")
        return scrape_html(site["url"], site["name"])

# ── Asosiy funksiyalar ────────────────────────────────────────────────────────
async def scrape_all():
    log.info("Tekshirish boshlandi...")
    total = 0
    for site in db_sites():
        try:
            items = fetch_site_news(site)
            count = 0
            for item in items:
                if not is_seen(item["url"]):
                    mark_seen(item["url"], site["name"])
                    save_pending(site["name"], item["title"], item["description"], item["url"])
                    count += 1
            log.info(f"[{site['name']}] {count} ta yangi yangilik")
            total += count
        except Exception as e:
            log.error(f"[{site['name']}] Xato: {e}")
    log.info(f"Jami {total} ta yangilik saqlandi")

async def send_news():
    news = pop_pending()
    if not news:
        log.info("Yuborish uchun yangilik yo'q"); return

    log.info(f"{len(news)} ta yangilik yuborilmoqda...")
    bot = Bot(token=BOT_TOKEN)
    header = f"📰 <b>Bugungi yangiliklar</b> — {datetime.now().strftime('%d.%m.%Y')}\n\n"
    current = header

    for i, n in enumerate(news, 1):
        desc = f"\n<i>{n['description'][:200]}</i>" if n["description"] else ""
        block = f"<b>{i}.</b> {n['title']}{desc}\n🔗 {n['url']}\n\n"
        if len(current) + len(block) > 4000:
            await bot.send_message(chat_id=CHANNEL_ID, text=current,
                                   parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            await asyncio.sleep(2)
            current = block
        else:
            current += block

    if current.strip() and current != header:
        await bot.send_message(chat_id=CHANNEL_ID, text=current,
                               parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    log.info("Yuborildi ✅")

# ── Admin tekshirish ──────────────────────────────────────────────────────────
def is_admin(update: Update):
    if not ADMIN_IDS:
        return True  # ADMIN_IDS sozlanmagan bo'lsa — hamma ishlatishi mumkin
    return update.effective_user.id in ADMIN_IDS

# ── Telegram komandalar ───────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>News Monitor Bot</b>\n\n"
        "📋 <b>Komandalar:</b>\n"
        "/list — saytlar ro'yxati\n"
        "/add URL — yangi sayt qo'shish\n"
        "/remove ID — saytni o'chirish\n"
        "/check — hozir tekshirib ko'rish\n"
        "/send — hozir kanalga yuborish\n"
        "/status — bot holati",
        parse_mode=ParseMode.HTML
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sites = db_sites()
    if not sites:
        await update.message.reply_text("📭 Saytlar ro'yxati bo'sh.\n/add URL bilan qo'shing")
        return
    text = "📋 <b>Kuzatiladigan saytlar:</b>\n\n"
    for s in sites:
        text += f"<b>{s['id']}.</b> {s['name']}\n🔗 {s['url']}\n\n"
    text += "O'chirish: /remove ID"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    if not ctx.args:
        await update.message.reply_text("❗ URL kiriting:\n/add https://sayt.com"); return

    url = ctx.args[0].strip()
    if not url.startswith("http"):
        await update.message.reply_text("❗ To'g'ri URL kiriting (https:// bilan boshlang)"); return

    msg = await update.message.reply_text(f"🔍 <b>{url}</b> tekshirilmoqda...", parse_mode=ParseMode.HTML)

    # Sayt nomini avtomatik topish
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "lxml")
        name = (soup.find("title") or soup.find("h1"))
        name = name.get_text(strip=True)[:50] if name else url
    except:
        name = url

    # RSS bor-yo'qligini tekshirish
    rss_url, rss_content = try_rss(url)
    if rss_content:
        info = f"✅ RSS topildi: {rss_url}"
    else:
        info = "⚠️ RSS topilmadi — HTML scraping ishlatiladi"

    ok = db_add_site(name, url)
    if ok:
        await msg.edit_text(
            f"✅ <b>Qo'shildi!</b>\n\n"
            f"📌 Nom: {name}\n🔗 URL: {url}\n{info}",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg.edit_text("⚠️ Bu sayt allaqachon ro'yxatda bor")

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("❗ ID kiriting:\n/remove 2"); return
    ok = db_remove_site(int(ctx.args[0]))
    await update.message.reply_text("✅ O'chirildi" if ok else "❌ Topilmadi")

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    msg = await update.message.reply_text("🔍 Tekshirilmoqda...")
    await scrape_all()
    con = sqlite3.connect(DB_PATH)
    count = con.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    con.close()
    await msg.edit_text(f"✅ Tekshirildi!\n📦 Kutayotgan yangiliklar: <b>{count} ta</b>", parse_mode=ParseMode.HTML)

async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sizda ruxsat yo'q"); return
    msg = await update.message.reply_text("📤 Yuborilmoqda...")
    await send_news()
    await msg.edit_text("✅ Kanalga yuborildi!")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = sqlite3.connect(DB_PATH)
    sites_count   = con.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    seen_count    = con.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    pending_count = con.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    con.close()
    await update.message.reply_text(
        f"📊 <b>Bot holati:</b>\n\n"
        f"🌐 Saytlar: <b>{sites_count} ta</b>\n"
        f"👁 Ko'rilgan yangiliklar: <b>{seen_count} ta</b>\n"
        f"📦 Kutayotgan: <b>{pending_count} ta</b>\n"
        f"⏰ Yuborish vaqti: <b>{SEND_HOUR+3:02d}:00 Toshkent</b>",
        parse_mode=ParseMode.HTML
    )

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    db_init()
    log.info("Bot ishga tushdi 🚀")

    # Scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(scrape_all, "interval", hours=1, id="scrape")
    scheduler.add_job(send_news,  "cron", hour=SEND_HOUR, minute=SEND_MIN, id="send")
    scheduler.start()

    # Telegram application
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("send",   cmd_send))
    app.add_handler(CommandHandler("status", cmd_status))

    # Start xabari
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=f"✅ <b>News Monitor Bot ishga tushdi!</b>\n\n"
                 f"🌐 Saytlar: {len(db_sites())} ta\n"
                 f"📨 Har kuni {SEND_HOUR+3:02d}:00 Toshkent vaqtida yuboriladi",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        log.warning(f"Start xabari: {e}")

    await scrape_all()

    log.info("Bot polling boshlandi...")
    await app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())
