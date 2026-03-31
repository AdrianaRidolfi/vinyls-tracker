import os
import re
import time
import json
import random
import logging
import sys
import cloudscraper
from bs4 import BeautifulSoup
from supabase import create_client
import functions_framework

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(message)s'))
if not logger.handlers:
    logger.addHandler(handler)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0"
]

def format_eur(price_float):
    if price_float is None:
        return "N/D"
    return f"{price_float:.2f}".replace(".", ",") + " €"

def send_telegram_alert(msg_text, vinyl_id, cover_url=None, keyboard=None):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    
    payload = {
        "chat_id": CHAT_ID,
        "parse_mode": "HTML"
    }
    
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
        
    if cover_url:
        endpoint = f"{base_url}/sendPhoto"
        payload["photo"] = cover_url
        payload["caption"] = msg_text
    else:
        endpoint = f"{base_url}/sendMessage"
        payload["text"] = msg_text
        
    try:
        response = cloudscraper.create_scraper().post(endpoint, json=payload, timeout=10)
        if not response.ok:
            logger.error(f"Errore API Telegram: {response.text}")
    except Exception as e:
        logger.error(f"Errore connessione Telegram: {e}")

def parse_price(price_str):
    if not price_str:
        return None
    s = str(price_str).replace('€', '').replace('EUR', '').strip()
    if '.' in s and ',' in s:
        s = s.replace('.', '')
    match = re.search(r'\d+[.,]\d+|\d+', s)
    if match:
        val = match.group().replace(',', '.')
        try:
            return float(val)
        except ValueError:
            return None
    return None

def extract_json_ld_price(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                for item in data:
                    if item.get('@type') in ['Product', 'Book', 'MusicAlbum'] and 'offers' in item:
                        offers = item['offers']
                        if isinstance(offers, list):
                            return float(offers[0].get('price'))
                        return float(offers.get('price'))
            elif isinstance(data, dict):
                if data.get('@type') in ['Product', 'Book', 'MusicAlbum'] and 'offers' in data:
                    offers = data['offers']
                    if isinstance(offers, list):
                        return float(offers[0].get('price'))
                    return float(offers.get('price'))
        except:
            continue
    return None

def extract_image(soup):
    meta_og = soup.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        return meta_og["content"]
    amz_img = soup.find("img", id="landingImage")
    if amz_img and amz_img.get("src"):
        return amz_img["src"]
    return None

def scrape_amazon(soup):
    # Cerca il prezzo solo nei contenitori principali del Buy Box. Nessun fallback.
    containers = [
        soup.find("div", id="corePriceDisplay_desktop_feature_div"),
        soup.find("div", id="corePrice_desktop"),
        soup.find("div", id="price_inside_buybox"),
        soup.find("div", id="apex_desktop"),
        soup.find("div", id="tmmSwatches")
    ]

    for container in containers:
        if not container:
            continue
        
        offscreen = container.find("span", class_="a-offscreen")
        if offscreen:
            val = parse_price(offscreen.text)
            if val: return val
            
        whole = container.find("span", class_="a-price-whole")
        fraction = container.find("span", class_="a-price-fraction")
        if whole and fraction:
            w_text = re.sub(r'[^\d]', '', whole.text)
            f_text = re.sub(r'[^\d]', '', fraction.text)
            val = parse_price(w_text + "." + f_text)
            if val: return val
            
        color_price = container.find("span", class_="a-color-price")
        if color_price:
            val = parse_price(color_price.text)
            if val: return val

    for hid in ["attach-base-product-price", "twister-plus-price-data-price"]:
        inp = soup.find("input", id=hid)
        if inp and inp.get("value"):
            base_val = parse_price(inp["value"])
            if base_val: return base_val

    return None

def scrape_feltrinelli(soup):
    json_price = extract_json_ld_price(soup)
    if json_price:
        return json_price

    for script in soup.find_all("script"):
        if script.string and "price" in script.string.lower():
            match = re.search(r'"price"\s*:\s*"?(\d+[.,]\d{2})"?', script.string)
            if match:
                return parse_price(match.group(1))

    price_tag = soup.find("span", {"class": "price"})
    if price_tag:
        return parse_price(price_tag.text)
        
    return None

def scrape_other(soup, url):
    if "discotecalaziale" in url.lower():
        price_div = soup.find("div", {"class": "price"})
        if price_div:
            val = parse_price(price_div.text)
            if val is not None:
                return val

    json_price = extract_json_ld_price(soup)
    if json_price:
        return json_price

    price_elements = soup.find_all(class_=re.compile("price", re.I))
    for el in price_elements:
        val = parse_price(el.text)
        if val is not None:
            return val

    euro_pattern = re.compile(r'€\s*\d+[.,]\d{2}|\d+[.,]\d{2}\s*€')
    match = soup.find(string=euro_pattern)
    if match:
        found = euro_pattern.search(match).group()
        return parse_price(found)

    return None

def get_current_data(url, site_name):
    logger.info(f"Controllo {site_name}: {url}")
    
    scraper = cloudscraper.create_scraper(browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    })
    
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.8,en-US;q=0.5,en;q=0.3",
        "Referer": "https://www.google.it/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    
    try:
        response = scraper.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(response.content, "html.parser")
        
        page_title = soup.title.string.strip() if soup.title and soup.title.string else "Nessun titolo trovato"
        logger.info(f"[{site_name}] Status: {response.status_code} | Titolo: {page_title}")
        
        image_url = extract_image(soup)
        price = None
        
        name_lower = site_name.lower()
        if "amazon" in name_lower:
            if "captcha" in page_title.lower() or page_title == "Amazon.it":
                logger.info("BLOCCATO DA CAPTCHA AMAZON")
                return None, None
            price = scrape_amazon(soup)
        elif "feltrinelli" in name_lower:
            price = scrape_feltrinelli(soup)
        else:
            price = scrape_other(soup, url)
            
        logger.info(f"Prezzo rilevato per {site_name}: {price}")
        return price, image_url
    except Exception as e:
        logger.error(f"Errore scraping {site_name}: {url}: {e}")
        return None, None

@functions_framework.http
def run_scraper(request):
    logger.info("*** AVVIO PROCESSO SCRAPER ***")
    if not supabase: return "Errore Configurazione Supabase", 500

    response = supabase.table("vinyls").select("*, sources(*)").eq("is_active", True).execute()
    vinyls = response.data

    for vinyl in vinyls:
        artist = vinyl["artist"]
        title = vinyl["title"]
        cover_url = vinyl.get("cover_url")
        sources = vinyl.get("sources", [])
        vinyl_id = vinyl["id"]
        
        if not sources:
            continue

        old_prices = [s["current_price"] for s in sources if s["current_price"] is not None]
        old_lowest = min(old_prices) if old_prices else None
        
        new_prices_data = []
        cover_updated = False
        
        for source in sources:
            time.sleep(random.uniform(5, 12))
            
            new_price, fetched_image = get_current_data(source["url"], source["site_name"])
            
            if not cover_url and fetched_image and not cover_updated:
                supabase.table("vinyls").update({"cover_url": fetched_image}).eq("id", vinyl_id).execute()
                cover_url = fetched_image
                cover_updated = True
            
            if new_price is not None:
                new_prices_data.append({
                    "site_name": source["site_name"],
                    "url": source["url"],
                    "price": new_price
                })
                
                source_id = source["id"]
                current_db_price = source.get("current_price")
                current_ath = source.get("ath_price")
                
                update_payload = {
                    "last_price": current_db_price,
                    "current_price": new_price,
                    "updated_at": "now()"
                }
                
                if current_ath is None or current_ath == 0 or new_price < current_ath:
                    update_payload["ath_price"] = new_price
                
                supabase.table("sources").update(update_payload).eq("id", source_id).execute()
                
                if current_db_price != new_price:
                    supabase.table("price_history").insert({
                        "source_id": source_id,
                        "price": new_price
                    }).execute()

        if not new_prices_data:
            continue

        valid_new_prices = [p["price"] for p in new_prices_data]
        new_lowest = min(valid_new_prices)
        lowest_data = next(p for p in new_prices_data if p["price"] == new_lowest)

        # Gestione notifica Inizio Monitoraggio
        if old_lowest is None:
            msg = f"🟢 <b>INIZIO MONITORAGGIO</b> 🟢\n\n{artist} - {title}\n\n"
            msg += "<b>Prezzi iniziali</b>:\n"
            for p in new_prices_data:
                msg += f"su <b>{p['site_name']}</b>: {format_eur(p['price'])}\n"
            
            msg += f"\n<b>Prezzo minimo attuale:</b>\n{lowest_data['site_name']} a {format_eur(new_lowest)}"
            
            keyboard = []
            keyboard.append([{"text": f"COMPRA SU {lowest_data['site_name'].upper()}", "url": lowest_data['url']}])
            
            other_btns = [{"text": p['site_name'].upper(), "url": p['url']} for p in new_prices_data if p['site_name'] != lowest_data['site_name']]
            for i in range(0, len(other_btns), 2):
                keyboard.append(other_btns[i:i+2])
                
            keyboard.append([{"text": "AGGIUNGI LINK", "callback_data": f"addlink_{vinyl_id}"}])
            keyboard.append([{"text": "STATISTICHE", "callback_data": f"stats_{vinyl_id}"}])
            
            send_telegram_alert(msg, vinyl_id, cover_url, keyboard)
            logger.info(f"Notifica Inizio Monitoraggio inviata per {title}")
            continue

        # Gestione notifica Calo di Prezzo
        if new_lowest < old_lowest:
            drop_eur = old_lowest - new_lowest
            drop_pct = (drop_eur / old_lowest) * 100
            
            msg = f"🔥 <b>CALO DI PREZZO!</b> 🔥\n\n{artist} - {title}\n\n"
            msg += f"Il prezzo minimo è sceso a <b>{format_eur(new_lowest)}</b> su <b>{lowest_data['site_name']}</b>\n"
            msg += f"<b>Risparmio</b>: {format_eur(drop_eur)} ({drop_pct:.1f}%)\n\n"
            
            for p in new_prices_data:
                if p['site_name'] != lowest_data['site_name']:
                    diff_pct = ((p['price'] - new_lowest) / new_lowest) * 100
                    msg += f"prezzo su <b>{p['site_name']}</b>: {format_eur(p['price'])} (+{diff_pct:.1f}%)\n"
                    
            keyboard = []
            keyboard.append([{"text": f"COMPRA SU {lowest_data['site_name'].upper()}", "url": lowest_data['url']}])
            
            other_btns = [{"text": p['site_name'].upper(), "url": p['url']} for p in new_prices_data if p['site_name'] != lowest_data['site_name']]
            for i in range(0, len(other_btns), 2):
                keyboard.append(other_btns[i:i+2])
                
            keyboard.append([{"text": "STATISTICHE", "callback_data": f"stats_{vinyl_id}"}])
            keyboard.append([
                {"text": "SOSPENDI", "callback_data": f"pause_{vinyl_id}"},
                {"text": "ELIMINA", "callback_data": f"delete_{vinyl_id}"}
            ])

            send_telegram_alert(msg, vinyl_id, cover_url, keyboard)
            logger.info(f"Notifica Calo Prezzo inviata per {title}")

    logger.info("*** FINE PROCESSO ***")
    return "OK", 200


def answer_callback(callback_query_id, text=None):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text: payload["text"] = text
    try:
        cloudscraper.create_scraper().post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Errore answerCallbackQuery: {e}")

def edit_telegram_message(chat_id, message_id, new_text):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageCaption"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": new_text,
        "parse_mode": "HTML"
    }
    try:
        resp = cloudscraper.create_scraper().post(url, json=payload, timeout=5)
        if not resp.ok:
            url_text = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
            payload["text"] = payload.pop("caption")
            cloudscraper.create_scraper().post(url_text, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Errore edit_telegram_message: {e}")

def get_site_name_from_url(url):
    url_lower = url.lower()
    if "amazon" in url_lower: return "Amazon"
    if "feltrinelli" in url_lower: return "Feltrinelli"
    if "discotecalaziale" in url_lower: return "Discoteca Laziale"
    if "ibs" in url_lower: return "IBS"
    return "Altro"

@functions_framework.http
def telegram_webhook(request):
    if request.method != "POST":
        return "Only POST allowed", 405

    update = request.get_json()
    if not update:
        return "OK", 200

    # Gestione ricezione link testuali
    if "message" in update:
        msg = update["message"]
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        reply_to = msg.get("reply_to_message")

        # Verifica che il messaggio sia una risposta a una nostra richiesta
        if reply_to and reply_to.get("text") and "[ID_VINILE:" in reply_to["text"]:
            try:
                record_id = reply_to["text"].split("[ID_VINILE:")[1].split("]")[0]
            except Exception:
                return "OK", 200

            url_send = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            
            if "http" not in text:
                payload = {"chat_id": chat_id, "text": "Errore: Inserisci un link valido che inizi con http/https."}
                cloudscraper.create_scraper().post(url_send, json=payload, timeout=5)
                return "OK", 200

            site_name = get_site_name_from_url(text)

            try:
                # Inserimento nuovo link nel database
                supabase.table("sources").insert({
                    "vinyl_id": record_id,
                    "site_name": site_name,
                    "url": text
                }).execute()
                
                payload = {"chat_id": chat_id, "text": f"Link di {site_name} aggiunto con successo al database!"}
            except Exception as e:
                logger.error(f"Errore inserimento link DB: {e}")
                payload = {"chat_id": chat_id, "text": "Errore di salvataggio nel database."}
                
            cloudscraper.create_scraper().post(url_send, json=payload, timeout=5)
            return "OK", 200

    # Gestione click sui bottoni
    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        data = cb.get("data", "")
        msg = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        msg_id = msg.get("message_id")
        
        if not data or not supabase:
            answer_callback(cb_id, "Errore di sistema.")
            return "OK", 200

        action, record_id = data.split("_", 1)
        
        if action == "pause":
            supabase.table("vinyls").update({"is_active": False}).eq("id", record_id).execute()
            answer_callback(cb_id, "Monitoraggio sospeso")
            if chat_id and msg_id:
                edit_telegram_message(chat_id, msg_id, "<b>MONITORAGGIO SOSPESO</b>\nRicevuto! Non ti invierò più notifiche per questo vinile.")

        elif action == "delete":
            supabase.table("vinyls").delete().eq("id", record_id).execute()
            answer_callback(cb_id, "Vinile eliminato dal DB")
            if chat_id and msg_id:
                edit_telegram_message(chat_id, msg_id, "<b>VINILE ELIMINATO</b>\nIl vinile e tutti i suoi link sono stati rimossi dal database.")
    
        elif action == "stats":
            answer_callback(cb_id, "Elaborazione in corso...")
            try:
                res = supabase.table("vinyls").select("artist, title, sources(site_name, current_price, ath_price)").eq("id", record_id).execute()
                if res.data and res.data[0].get("sources"):
                    v = res.data[0]
                    stats_msg = f"<b>📊 STATISTICHE 📊 \n {v['artist']} - {v['title']}</b>\n\n"
                    
                    for s in v["sources"]:
                        cp = s.get("current_price")
                        ath = s.get("ath_price")
                        stats_msg += f"<b>{s['site_name']}</b>\n"
                        stats_msg += f"Attuale: {format_eur(cp)}\n"
                        stats_msg += f"Minimo storico: {format_eur(ath)}\n\n"
                    
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                    
                    # Creazione del bottone Aggiungi Link
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "AGGIUNGI LINK", "callback_data": f"addlink_{record_id}"}]
                        ]
                    }
                    
                    payload = {
                        "chat_id": chat_id, 
                        "text": stats_msg, 
                        "parse_mode": "HTML",
                        "reply_markup": keyboard
                    }
                    cloudscraper.create_scraper().post(url, json=payload, timeout=5)
            except Exception as e:
                logger.error(f"Errore statistiche: {e}")
                
        elif action == "addlink":
            answer_callback(cb_id, "In attesa del link...")
            url_send = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": f"Incolla il link da aggiungere rispondendo a questo messaggio.\n[ID_VINILE:{record_id}]",
                "reply_markup": {"force_reply": True}
            }
            cloudscraper.create_scraper().post(url_send, json=payload, timeout=5)

    return "OK", 200
