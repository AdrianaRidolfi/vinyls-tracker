import os
import re
import time
import json
import random
import logging
import sys
import threading

import cloudscraper
from bs4 import BeautifulSoup
from supabase import create_client
from flask import Flask, request

# ---------------------------------------------------------------------------
# App & logging
# ---------------------------------------------------------------------------

app = Flask(__name__)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
if not logger.handlers:
    logger.addHandler(_handler)

# ---------------------------------------------------------------------------
# Environment / clients
# ---------------------------------------------------------------------------

SUPABASE_URL    = os.environ.get("SUPABASE_URL")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID         = os.environ.get("CHAT_ID")
SCRAPER_TOKEN   = os.environ.get("SCRAPER_TOKEN")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Single shared scraper instance — avoids recreating it on every request
_scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "desktop": True}
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
]

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def format_eur(price: float | None) -> str:
    if price is None:
        return "N/D"
    return f"{price:.2f}".replace(".", ",") + " €"


def parse_price(price_str) -> float | None:
    if not price_str:
        return None
    s = str(price_str).replace("€", "").replace("EUR", "").strip()
    if "." in s and "," in s:
        s = s.replace(".", "")
    match = re.search(r"\d+[.,]\d+|\d+", s)
    if match:
        try:
            return float(match.group().replace(",", "."))
        except ValueError:
            return None
    return None


def get_site_name_from_url(url: str) -> str:
    url_lower = url.lower()
    if "amazon"           in url_lower: return "Amazon"
    if "feltrinelli"      in url_lower: return "Feltrinelli"
    if "discotecalaziale" in url_lower: return "Discoteca Laziale"
    if "ibs"              in url_lower: return "IBS"
    return "Altro"

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _tg_post(endpoint: str, payload: dict, timeout: int = 10) -> bool:
    """Low-level POST to the Telegram Bot API. Returns True on success."""
    if not TELEGRAM_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}"
    try:
        resp = _scraper.post(url, json=payload, timeout=timeout)
        if not resp.ok:
            logger.error("Telegram API error [%s]: %s", endpoint, resp.text)
        return resp.ok
    except Exception as exc:
        logger.error("Errore connessione Telegram [%s]: %s", endpoint, exc)
        return False


def send_telegram_alert(
    text: str,
    vinyl_id,
    cover_url: str | None = None,
    keyboard: list | None = None,
) -> None:
    if not CHAT_ID:
        return
    payload = {"chat_id": CHAT_ID, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}

    if cover_url:
        payload["photo"]   = cover_url
        payload["caption"] = text
        _tg_post("sendPhoto", payload)
    else:
        payload["text"] = text
        _tg_post("sendMessage", payload)


def answer_callback(callback_query_id: str, text: str | None = None) -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    _tg_post("answerCallbackQuery", payload, timeout=5)


def edit_telegram_message(chat_id, message_id, new_text: str) -> None:
    payload = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "caption":    new_text,
        "parse_mode": "HTML",
    }
    ok = _tg_post("editMessageCaption", payload, timeout=5)
    if not ok:
        # Fallback: the original message might be plain text, not a photo caption
        payload["text"] = payload.pop("caption")
        _tg_post("editMessageText", payload, timeout=5)


def delete_telegram_message(chat_id, message_id) -> None:
    _tg_post("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=5)


def send_telegram_message(chat_id, text: str, keyboard=None, parse_mode: str = "HTML") -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if keyboard:
        payload["reply_markup"] = keyboard
    _tg_post("sendMessage", payload, timeout=5)


def send_telegram_photo(chat_id, photo_url: str, caption: str, keyboard=None) -> None:
    payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = keyboard
    _tg_post("sendPhoto", payload, timeout=5)

# ---------------------------------------------------------------------------
# HTML parsing / scraping
# ---------------------------------------------------------------------------

def extract_json_ld_price(soup: BeautifulSoup) -> float | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Product", "Book", "MusicAlbum") and "offers" in item:
                    offers = item["offers"]
                    raw = offers[0].get("price") if isinstance(offers, list) else offers.get("price")
                    if raw is not None:
                        return float(raw)
        except Exception:
            continue
    return None


def extract_image(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", property="og:image")
    if meta and meta.get("content"):
        return meta["content"]
    amz = soup.find("img", id="landingImage")
    if amz and amz.get("src"):
        return amz["src"]
    return None


def scrape_amazon(soup: BeautifulSoup) -> float | None:
    container_ids = [
        "corePriceDisplay_desktop_feature_div",
        "corePrice_desktop",
        "price_inside_buybox",
        "apex_desktop",
        "tmmSwatches",
    ]
    for cid in container_ids:
        container = soup.find("div", id=cid)
        if not container:
            continue

        offscreen = container.find("span", class_="a-offscreen")
        if offscreen:
            val = parse_price(offscreen.text)
            if val:
                return val

        whole    = container.find("span", class_="a-price-whole")
        fraction = container.find("span", class_="a-price-fraction")
        if whole and fraction:
            w = re.sub(r"[^\d]", "", whole.text)
            f = re.sub(r"[^\d]", "", fraction.text)
            val = parse_price(f"{w}.{f}")
            if val:
                return val

        color_price = container.find("span", class_="a-color-price")
        if color_price:
            val = parse_price(color_price.text)
            if val:
                return val

    for hid in ("attach-base-product-price", "twister-plus-price-data-price"):
        inp = soup.find("input", id=hid)
        if inp and inp.get("value"):
            val = parse_price(inp["value"])
            if val:
                return val

    return None


def scrape_feltrinelli(soup: BeautifulSoup) -> float | None:
    val = extract_json_ld_price(soup)
    if val:
        return val

    for script in soup.find_all("script"):
        if script.string and "price" in script.string.lower():
            match = re.search(r'"price"\s*:\s*"?(\d+[.,]\d{2})"?', script.string)
            if match:
                return parse_price(match.group(1))

    tag = soup.find("span", class_="price")
    if tag:
        return parse_price(tag.text)

    return None


def scrape_other(soup: BeautifulSoup, url: str) -> float | None:
    if "discotecalaziale" in url.lower():
        div = soup.find("div", class_="price")
        if div:
            val = parse_price(div.text)
            if val is not None:
                return val

    val = extract_json_ld_price(soup)
    if val:
        return val

    for el in soup.find_all(class_=re.compile("price", re.I)):
        val = parse_price(el.text)
        if val is not None:
            return val

    euro_re = re.compile(r"€\s*\d+[.,]\d{2}|\d+[.,]\d{2}\s*€")
    match = soup.find(string=euro_re)
    if match:
        found = euro_re.search(match)
        if found:
            return parse_price(found.group())

    return None


def get_current_data(url: str, site_name: str) -> tuple[float | None, str | None]:
    logger.info("Controllo %s: %s", site_name, url)
    headers = {
        "User-Agent":               random.choice(USER_AGENTS),
        "Accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":          "it-IT,it;q=0.8,en-US;q=0.5,en;q=0.3",
        "Referer":                  "https://www.google.it/",
        "Connection":               "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        response = _scraper.get(url, headers=headers, timeout=20)
        soup     = BeautifulSoup(response.content, "html.parser")
        title    = soup.title.string.strip() if soup.title and soup.title.string else "(nessun titolo)"
        logger.info("[%s] HTTP %s | %s", site_name, response.status_code, title)

        image_url  = extract_image(soup)
        name_lower = site_name.lower()

        if "amazon" in name_lower:
            if "captcha" in title.lower() or title == "Amazon.it":
                logger.warning("[%s] Bloccato da CAPTCHA Amazon", site_name)
                return None, None
            price = scrape_amazon(soup)
        elif "feltrinelli" in name_lower:
            price = scrape_feltrinelli(soup)
        else:
            price = scrape_other(soup, url)

        logger.info("[%s] Prezzo rilevato: %s", site_name, format_eur(price))
        return price, image_url

    except Exception as exc:
        logger.error("Errore scraping %s (%s): %s", site_name, url, exc)
        return None, None

# ---------------------------------------------------------------------------
# Notification message builders
# ---------------------------------------------------------------------------

def _build_buy_keyboard(lowest: dict, others: list, vinyl_id) -> list:
    keyboard = [
        [{"text": f"🛒 COMPRA SU {lowest['site_name'].upper()} - {format_eur(lowest['price'])}", "url": lowest["url"]}]
    ]
    other_btns = [{"text": p["site_name"].upper(), "url": p["url"]} for p in others]
    for i in range(0, len(other_btns), 2):
        keyboard.append(other_btns[i : i + 2])
    return keyboard


def build_initial_monitoring_message(artist: str, title: str, new_prices: list, lowest: dict) -> tuple[str, list]:
    others = [p for p in new_prices if p["site_name"] != lowest["site_name"]]
    msg  = "🟢 <b>MONITORAGGIO AVVIATO</b>\n\n"
    msg += f"<b>{artist} - {title}</b>\n\n"
    msg += "<b>Prezzi rilevati:</b>\n"
    for p in new_prices:
        marker = "⭐ " if p["site_name"] == lowest["site_name"] else "   "
        msg += f"{marker}<b>{p['site_name']}</b>: {format_eur(p['price'])}\n"
    msg += f"\n💡 <b>Prezzo più basso:</b> {lowest['site_name']} a {format_eur(lowest['price'])}"

    keyboard = _build_buy_keyboard(lowest, others, None)
    keyboard.append([{"text": "➕ Aggiungi link", "callback_data": f"addlink_{lowest.get('vinyl_id', '')}"}])
    keyboard.append([{"text": "📊 Statistiche",   "callback_data": f"stats_{lowest.get('vinyl_id', '')}"}])
    return msg, keyboard


def build_price_drop_message(artist: str, title: str, new_prices: list, lowest: dict, old_lowest: float) -> tuple[str, list]:
    others   = [p for p in new_prices if p["site_name"] != lowest["site_name"]]
    drop_eur = old_lowest - lowest["price"]
    drop_pct = (drop_eur / old_lowest) * 100

    msg  = "🔥 <b>CALO DI PREZZO!</b>\n\n"
    msg += f"<b>{artist} - {title}</b>\n\n"
    msg += f"📉 Il prezzo minimo è sceso a <b>{format_eur(lowest['price'])}</b> su <b>{lowest['site_name']}</b>\n"
    msg += f"💰 <b>Risparmio:</b> {format_eur(drop_eur)} ({drop_pct:.1f}% in meno)\n"

    if others:
        msg += "\n<b>Confronto con gli altri siti:</b>\n"
        for p in others:
            diff_pct = ((p["price"] - lowest["price"]) / lowest["price"]) * 100
            msg += f"   <b>{p['site_name']}</b>: {format_eur(p['price'])} (+{diff_pct:.1f}%)\n"

    keyboard = _build_buy_keyboard(lowest, others, None)
    keyboard.append([{"text": "📊 Statistiche", "callback_data": f"stats_{lowest.get('vinyl_id', '')}"}])
    keyboard.append([
        {"text": "⏸ Sospendi", "callback_data": f"pause_{lowest.get('vinyl_id', '')}"},
        {"text": "🗑 Elimina",  "callback_data": f"delete_{lowest.get('vinyl_id', '')}"},
    ])
    return msg, keyboard

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def update_source_in_db(source: dict, new_price: float) -> None:
    current_db_price = source.get("current_price")
    current_ath      = source.get("ath_price")
    source_id        = source["id"]

    update_payload = {
        "last_price":   current_db_price,
        "current_price": new_price,
        "updated_at":   "now()",
    }
    if current_ath is None or current_ath == 0 or new_price < current_ath:
        update_payload["ath_price"] = new_price

    supabase.table("sources").update(update_payload).eq("id", source_id).execute()

    if current_db_price != new_price:
        supabase.table("price_history").insert({
            "source_id": source_id,
            "price":     new_price,
        }).execute()

# ---------------------------------------------------------------------------
# Core scraper logic
# ---------------------------------------------------------------------------

def process_vinyl(vinyl: dict) -> None:
    artist    = vinyl["artist"]
    title     = vinyl["title"]
    cover_url = vinyl.get("cover_url")
    sources   = vinyl.get("sources", [])
    vinyl_id  = vinyl["id"]

    if not sources:
        return

    old_prices = [s["current_price"] for s in sources if s.get("current_price") is not None]
    old_lowest = min(old_prices) if old_prices else None

    new_prices_data: list[dict] = []
    cover_updated = False

    for source in sources:
        time.sleep(random.uniform(5, 12))
        new_price, fetched_image = get_current_data(source["url"], source["site_name"])

        if not cover_url and fetched_image and not cover_updated:
            supabase.table("vinyls").update({"cover_url": fetched_image}).eq("id", vinyl_id).execute()
            cover_url     = fetched_image
            cover_updated = True

        if new_price is None:
            continue

        new_prices_data.append({
            "site_name": source["site_name"],
            "url":       source["url"],
            "price":     new_price,
            "vinyl_id":  vinyl_id,
        })
        update_source_in_db(source, new_price)

    if not new_prices_data:
        logger.info("[%s - %s] Nessun prezzo recuperato in questo ciclo.", artist, title)
        return

    new_lowest  = min(p["price"] for p in new_prices_data)
    lowest_data = next(p for p in new_prices_data if p["price"] == new_lowest)

    if old_lowest is None:
        msg, keyboard = build_initial_monitoring_message(artist, title, new_prices_data, lowest_data)
        send_telegram_alert(msg, vinyl_id, cover_url, keyboard)
        logger.info("[%s - %s] Notifica inizio monitoraggio inviata.", artist, title)
        return

    if new_lowest < old_lowest:
        msg, keyboard = build_price_drop_message(artist, title, new_prices_data, lowest_data, old_lowest)
        send_telegram_alert(msg, vinyl_id, cover_url, keyboard)
        logger.info("[%s - %s] Notifica calo prezzo inviata.", artist, title)


def run_scraper() -> None:
    logger.info("=== AVVIO SCRAPER ===")
    if not supabase:
        logger.error("Supabase non configurato. Scraper interrotto.")
        return

    response = supabase.table("vinyls").select("*, sources(*)").eq("is_active", True).execute()
    vinyls   = response.data or []
    logger.info("Vinili attivi da controllare: %d", len(vinyls))

    for vinyl in vinyls:
        try:
            process_vinyl(vinyl)
        except Exception as exc:
            logger.error("Errore imprevisto su vinile id=%s: %s", vinyl.get("id"), exc)

    logger.info("=== FINE SCRAPER ===")

# ---------------------------------------------------------------------------
# Gift-list helper (used by both /start and callback)
# ---------------------------------------------------------------------------

def send_regali_list(chat_id) -> None:
    try:
        res_friend = supabase.table("friends").select("name").eq("chat_id", chat_id).execute()

        if not res_friend.data:
            supabase.table("friends").insert({"chat_id": chat_id}).execute()
            nome = None
        else:
            nome = res_friend.data[0].get("name")

        if nome:
            text = (
                f"Bentornato/a, <b>{nome}</b>! 🎁\n\n"
                "Ecco la lista aggiornata dei vinili:"
            )
        else:
            text = (
                "Ciao! 👋 Benvenuto/a qui.\n\n"
                "Questa è la lista dei vinili che mi piacerebbe tanto avere. "
                "Se decidi di regalarmene uno, cliccaci sopra e prenotalo: in questo modo "
                "verrà nascosto agli altri, così non riceverò regali doppi. ✨💿✨\n\n"
                "I prezzi che vedi sono i più bassi trovati online dal mio bot (potrebbero "
                "esserci piccole variazioni nel momento in cui clicchi), ma sentiti "
                "liberissim* di comprarlo dove preferisci, anche usato o dal tuo negozio "
                "di fiducia. E ovviamente, se hai un'altra idea al di fuori di questa lista, "
                "mi renderai ugualmente felicissima! ❤️"
            )

        res_vinyls = supabase.table("vinyls").select(
            "id, artist, title, reserved_by, sources(current_price)"
        ).or_(f"is_active.eq.true,reserved_by.eq.{chat_id}").execute()

        vinyls = sorted(
            res_vinyls.data or [],
            key=lambda x: (
                min(
                    (s["current_price"] for s in x.get("sources", []) if s.get("current_price") is not None),
                    default=float("inf"),
                ),
                x.get("artist", "").lower(),
                x.get("title", "").lower(),
            ),
        )

        keyboard = []
        for v in vinyls:
            prices       = [s["current_price"] for s in v.get("sources", []) if s.get("current_price") is not None]
            lowest_price = min(prices) if prices else None
            price_str    = f" - {format_eur(lowest_price)}" if lowest_price else ""
            is_mine      = str(v.get("reserved_by")) == str(chat_id)
            check        = "✅ " if is_mine else ""
            btn_text     = f"{check}{v['artist']}, {v['title']}{price_str}"
            keyboard.append([{"text": btn_text, "callback_data": f"regalo_{v['id']}"}])

        if not keyboard:
            text += "\n\nAl momento non ci sono vinili in lista."

        send_telegram_message(
            chat_id,
            text,
            keyboard={"inline_keyboard": keyboard} if keyboard else None,
        )

    except Exception as exc:
        logger.error("Errore generazione lista regali: %s", exc)

# ---------------------------------------------------------------------------
# /get-all — full list for the owner (CHAT_ID only)
# ---------------------------------------------------------------------------

def send_get_all(chat_id) -> None:
    """Send a plain-text summary of ALL vinyls (active, reserved, inactive) to the owner."""
    try:
        res = supabase.table("vinyls").select(
            "id, artist, title, is_active, reserved_by, sources(site_name, current_price)"
        ).execute()

        vinyls = sorted(
            res.data or [],
            key=lambda x: (x.get("artist", "").lower(), x.get("title", "").lower()),
        )

        if not vinyls:
            send_telegram_message(chat_id, "Nessun vinile nel database.")
            return

        # Fetch friends to resolve reserved_by chat_id -> name
        res_friends = supabase.table("friends").select("chat_id, name").execute()
        friends_map = {str(f["chat_id"]): f.get("name") or str(f["chat_id"]) for f in (res_friends.data or [])}

        active, reserved, inactive = [], [], []
        for v in vinyls:
            prices  = [s["current_price"] for s in v.get("sources", []) if s.get("current_price") is not None]
            lowest  = min(prices) if prices else None
            label   = f"{v['artist']}, {v['title']} - {format_eur(lowest)}"

            if v.get("reserved_by"):
                who = friends_map.get(str(v["reserved_by"]), str(v["reserved_by"]))
                reserved.append(f"🎁 {label}  [prenotato da {who}]")
            elif v.get("is_active"):
                active.append(f"💿 {label}")
            else:
                inactive.append(f"⏸ {label}  [sospeso]")

        lines = ["<b>📋 LISTA COMPLETA VINILI</b>\n"]
        if active:
            lines.append(f"<b>Attivi ({len(active)})</b>")
            lines.extend(active)
        if reserved:
            lines.append(f"\n<b>Prenotati ({len(reserved)})</b>")
            lines.extend(reserved)
        if inactive:
            lines.append(f"\n<b>Sospesi ({len(inactive)})</b>")
            lines.extend(inactive)

        send_telegram_message(chat_id, "\n".join(lines))

    except Exception as exc:
        logger.error("Errore send_get_all: %s", exc)
        send_telegram_message(chat_id, "❌ Errore nel recupero della lista.")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/trigger", methods=["GET"])
def trigger():
    token = request.args.get("token")
    if token != SCRAPER_TOKEN:
        return "Non autorizzato", 401

    thread = threading.Thread(target=run_scraper, daemon=True)
    thread.start()
    return "Scraper avviato in background", 200


@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True)
    if not update:
        return "OK", 200

    # ---- Plain messages ----
    if "message" in update:
        _handle_message(update["message"])

    # ---- Button callbacks ----
    if "callback_query" in update:
        _handle_callback(update["callback_query"])

    return "OK", 200


def _handle_message(msg: dict) -> None:
    chat_id  = msg.get("chat", {}).get("id")
    text     = msg.get("text", "").strip()
    reply_to = msg.get("reply_to_message")

    # ---- Commands ----
    if text in ("/start regali", "/regali"):
        send_regali_list(chat_id)
        return

    if text == "/set-name":
        try:
            res = supabase.table("friends").select("name").eq("chat_id", chat_id).execute()
            existing_name = res.data[0].get("name") if res.data else None
        except Exception:
            existing_name = None

        if existing_name:
            prompt = (
                f"Il tuo nome attuale è <b>{existing_name}</b>.\n\n"
                "Vuoi cambiarlo? Rispondi a questo messaggio con il nuovo nome (max due parole).\n"
                "[SET_NAME]"
            )
        else:
            prompt = (
                "Come ti chiami? Rispondi a questo messaggio con il tuo nome (max due parole).\n"
                "[SET_NAME]"
            )
        send_telegram_message(chat_id, prompt, keyboard={"force_reply": True})
        return

    if text == "/get-all":
        if str(chat_id) != str(CHAT_ID):
            send_telegram_message(chat_id, "⛔ Comando riservato per Adriana.")
            return
        send_get_all(chat_id)
        return

    # ---- Reply-to flows ----
    if not reply_to or not reply_to.get("text"):
        return

    reply_text = reply_to.get("text", "")

    # Set-name reply
    if "[SET_NAME]" in reply_text:
        words = text.split()
        if not words or len(words) > 2 or not all(w.isalpha() for w in words):
            send_telegram_message(
                chat_id,
                "⚠️ Nome non valido. Usa al massimo due parole, solo lettere. Riprova con /set-name."
            )
            return
        name = " ".join(w.capitalize() for w in words)
        try:
            existing = supabase.table("friends").select("chat_id").eq("chat_id", chat_id).execute()
            if existing.data:
                supabase.table("friends").update({"name": name}).eq("chat_id", chat_id).execute()
            else:
                supabase.table("friends").insert({"chat_id": chat_id, "name": name}).execute()
            send_telegram_message(chat_id, f"✅ Nome salvato: <b>{name}</b>. Grazie!")
        except Exception as exc:
            logger.error("Errore salvataggio nome: %s", exc)
            send_telegram_message(chat_id, "❌ Errore nel salvare il nome. Riprova più tardi.")
        return

    # Add-link reply
    if "[ID_VINILE:" in reply_text:
        try:
            record_id = reply_text.split("[ID_VINILE:")[1].split("]")[0]
        except (IndexError, KeyError):
            return

        if not text.startswith("http"):
            send_telegram_message(chat_id, "⚠️ Inserisci un link valido che inizi con http o https.")
            return

        site_name = get_site_name_from_url(text)
        try:
            supabase.table("sources").insert({"vinyl_id": record_id, "site_name": site_name, "url": text}).execute()
            send_telegram_message(chat_id, f"✅ Link di <b>{site_name}</b> aggiunto con successo!")
        except Exception as exc:
            logger.error("Errore inserimento link DB: %s", exc)
            send_telegram_message(chat_id, "❌ Errore nel salvare il link. Riprova più tardi.")


def _handle_callback(cb: dict) -> None:
    cb_id     = cb["id"]
    data      = cb.get("data", "")
    msg       = cb.get("message", {})
    chat_id   = msg.get("chat", {}).get("id")
    msg_id    = msg.get("message_id")

    if not data or not supabase:
        answer_callback(cb_id, "Errore di sistema.")
        return

    parts = data.split("_", 1)
    if len(parts) != 2:
        answer_callback(cb_id, "Azione non riconosciuta.")
        return

    action, record_id = parts

    if action == "pause":
        supabase.table("vinyls").update({"is_active": False}).eq("id", record_id).execute()
        answer_callback(cb_id, "Monitoraggio sospeso.")
        edit_telegram_message(
            chat_id, msg_id,
            "⏸ <b>MONITORAGGIO SOSPESO</b>\n\nNon riceverai più notifiche per questo vinile. "
            "Puoi riattivarlo direttamente dal database quando vuoi."
        )

    elif action == "delete":
        supabase.table("vinyls").delete().eq("id", record_id).execute()
        answer_callback(cb_id, "Vinile eliminato.")
        edit_telegram_message(
            chat_id, msg_id,
            "🗑 <b>VINILE ELIMINATO</b>\n\nIl vinile e tutti i suoi link sono stati rimossi dal database."
        )

    elif action == "stats":
        answer_callback(cb_id, "Recupero statistiche…")
        try:
            res = supabase.table("vinyls").select(
                "artist, title, sources(site_name, current_price, ath_price)"
            ).eq("id", record_id).execute()

            if not res.data or not res.data[0].get("sources"):
                send_telegram_message(chat_id, "📊 Nessuna statistica disponibile al momento.")
                return

            v        = res.data[0]
            stats_msg = f"📊 <b>STATISTICHE</b>\n<b>{v['artist']} - {v['title']}</b>\n\n"
            for s in v["sources"]:
                stats_msg += (
                    f"<b>{s['site_name']}</b>\n"
                    f"   Prezzo attuale:    {format_eur(s.get('current_price'))}\n"
                    f"   Minimo storico:    {format_eur(s.get('ath_price'))}\n\n"
                )

            send_telegram_message(
                chat_id, stats_msg,
                keyboard={"inline_keyboard": [[{"text": "➕ Aggiungi link", "callback_data": f"addlink_{record_id}"}]]},
            )
        except Exception as exc:
            logger.error("Errore recupero statistiche: %s", exc)
            send_telegram_message(chat_id, "❌ Errore nel recupero delle statistiche. Riprova più tardi.")

    elif action == "addlink":
        answer_callback(cb_id, "In attesa del link…")
        send_telegram_message(
            chat_id,
            f"📎 Incolla il link da aggiungere <b>rispondendo a questo messaggio</b>.\n[ID_VINILE:{record_id}]",
            keyboard={"force_reply": True},
        )

    elif action == "listaregali":
        answer_callback(cb_id)
        delete_telegram_message(chat_id, msg_id)
        send_regali_list(chat_id)

    elif action in ("book", "unbook"):
        answer_callback(cb_id)

        if action == "book":
            check = supabase.table("vinyls").select("reserved_by").eq("id", record_id).execute()
            if check.data and check.data[0].get("reserved_by") is not None:
                msg_testo = "😅 Ops! Qualcuno è stato più veloce e l'ha già prenotato."
            else:
                supabase.table("vinyls").update(
                    {"is_active": False, "reserved_by": str(chat_id)}
                ).eq("id", record_id).execute()
                msg_testo = "🎉 Vinile prenotato con successo!\n\nGrazie mille, sei fantastico/a! ❤️"
        else:
            supabase.table("vinyls").update(
                {"is_active": True, "reserved_by": None}
            ).eq("id", record_id).execute()
            msg_testo = "↩️ Prenotazione annullata. Il vinile è tornato disponibile nella lista."

        delete_telegram_message(chat_id, msg_id)
        send_telegram_message(
            chat_id, msg_testo,
            keyboard={"inline_keyboard": [[{"text": "🔙 Torna alla lista", "callback_data": "listaregali_0"}]]},
        )

    elif action == "regalo":
        answer_callback(cb_id, "Recupero dettagli…")
        try:
            res = supabase.table("vinyls").select(
                "artist, title, cover_url, reserved_by, sources(site_name, current_price, url)"
            ).eq("id", record_id).execute()

            if not res.data:
                send_telegram_message(chat_id, "⚠️ Vinile non trovato.")
                return

            v           = res.data[0]
            is_mine     = str(v.get("reserved_by")) == str(chat_id)
            is_available = v.get("reserved_by") is None

            caption = f"<b>{v['artist']} - {v['title']}</b>\n\n"
            if is_mine:
                caption += "✅ <i>Hai prenotato questo vinile.</i>\n\n"
            elif not is_available:
                caption += "❌ <i>Questo vinile è già stato prenotato da qualcun altro.</i>\n\n"

            keyboard = []
            sources  = v.get("sources") or []

            if not sources:
                caption += "Nessun prezzo disponibile al momento.\n"
            else:
                for s in sources:
                    cp = s.get("current_price")
                    caption += f"<b>{s['site_name']}</b>: {format_eur(cp)}\n"
                    if cp is not None and s.get("url"):
                        keyboard.append([{"text": f"💸 Compra su {s['site_name']}", "url": s["url"]}])

            if is_mine:
                keyboard.append([{"text": "❌ Cancella prenotazione", "callback_data": f"unbook_{record_id}"}])
            elif is_available:
                keyboard.append([{"text": "🎁 Prenota questo vinile", "callback_data": f"book_{record_id}"}])

            keyboard.append([{"text": "🔙 Torna alla lista", "callback_data": "listaregali_0"}])
            reply_markup = {"inline_keyboard": keyboard}

            delete_telegram_message(chat_id, msg_id)

            if v.get("cover_url"):
                send_telegram_photo(chat_id, v["cover_url"], caption, reply_markup)
            else:
                send_telegram_message(chat_id, caption, reply_markup)

        except Exception as exc:
            logger.error("Errore dettaglio regalo: %s", exc)
            send_telegram_message(chat_id, "❌ Si è verificato un errore. Riprova tra poco.")

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
