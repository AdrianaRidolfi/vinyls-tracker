import os
import re
import time
import json
import random
import cloudscraper
from bs4 import BeautifulSoup
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def format_eur(price_float):
    if price_float is None: return "N/D"
    return f"{price_float:.2f}".replace(".", ",") + " EUR"

def send_telegram_alert(msg_text, link_url, vinyl_id, cover_url=None):
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    reply_markup = {
        "inline_keyboard": [[
            {"text": "Apri Link", "url": link_url},
            {"text": "Statistiche", "callback_data": f"stats_{vinyl_id}"}
        ]]
    }
    endpoint = f"{base_url}/sendPhoto" if cover_url else f"{base_url}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
        ("photo" if cover_url else "text"): (cover_url if cover_url else msg_text)
    }
    if cover_url: payload["caption"] = msg_text
    try:
        cloudscraper.create_scraper().post(endpoint, json=payload, timeout=10)
    except Exception as e:
        print(f"Errore Telegram: {e}")

def parse_price(price_str):
    if not price_str: return None
    s = str(price_str).replace('€', '').replace('EUR', '').strip()
    if '.' in s and ',' in s: s = s.replace('.', '')
    match = re.search(r'\d+[.,]\d+|\d+', s)
    if match:
        val = match.group().replace(',', '.')
        try: return float(val)
        except: return None
    return None

def scrape_amazon(soup):
    # Prova prima dai dati strutturati (meno influenzati dal layout)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if 'offers' in item:
                    offers = item['offers']
                    if isinstance(offers, list): return float(offers[0].get('price'))
                    return float(offers.get('price'))
        except: continue

    # Selettori visivi nel BuyBox o nell'area centrale
    selectors = [
        "#corePrice_desktop .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#buyNewSection .a-price .a-offscreen",
        "#price_inside_buybox",
        "#newBuyBoxPrice"
    ]
    for sel in selectors:
        elem = soup.select_one(sel)
        if elem:
            val = parse_price(elem.text)
            if val: return val
    return None

def get_current_data(url, site_name):
    print(f"Controllo {site_name}: {url}")
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome','platform': 'windows','desktop': True})
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "it-IT,it;q=0.9",
        "Referer": "https://www.google.com/"
    }

    try:
        response = scraper.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(response.content, "html.parser")
        title = soup.title.string.strip() if soup.title else ""
        
        if "amazon" in site_name.lower() and ("captcha" in title.lower() or title == "Amazon.it"):
            print("BLOCCATO DA CAPTCHA")
            return None, None
            
        price = None
        if "amazon" in site_name.lower():
            price = scrape_amazon(soup)
        elif "feltrinelli" in site_name.lower():
            price_meta = soup.find("meta", property="product:price:amount")
            if price_meta: price = float(price_meta["content"])
        else:
            price_elements = soup.find_all(class_=re.compile("price", re.I))
            for el in price_elements:
                val = parse_price(el.text)
                if val: price = val; break

        print(f"Prezzo rilevato: {price}")
        img = None
        og_img = soup.find("meta", property="og:image")
        if og_img: img = og_img["content"]
        
        return price, img
    except Exception as e:
        print(f"Errore {site_name}: {e}")
        return None, None

def process_vinyls():
    res = supabase.table("vinyls").select("*, sources(*)").eq("is_active", True).execute()
    for vinyl in res.data:
        sources = vinyl.get("sources", [])
        if not sources: continue

        # Trova il vecchio minimo assoluto tra tutte le sorgenti attive
        valid_old_prices = [s["current_price"] for s in sources if s["current_price"] is not None]
        old_min_total = min(valid_old_prices) if valid_old_prices else None
        
        current_run_min = None
        best_source_run = None

        for source in sources:
            time.sleep(random.uniform(5, 10))
            new_p, new_img = get_current_data(source["url"], source["site_name"])
            
            if new_p is not None:
                if not vinyl.get("cover_url") and new_img:
                    supabase.table("vinyls").update({"cover_url": new_img}).eq("id", vinyl["id"]).execute()
                
                source_id = source["id"]
                old_p_source = source["current_price"]
                
                # Update source data
                update_data = {"current_price": new_p, "last_price": old_p_source, "updated_at": "now()"}
                if source["ath_price"] is None or new_p < source["ath_price"]:
                    update_data["ath_price"] = new_p
                
                supabase.table("sources").update(update_data).eq("id", source_id).execute()
                
                # Salva nello storico solo se il prezzo e cambiato davvero rispetto all ultimo salvato
                if old_p_source != new_p:
                    supabase.table("price_history").insert({"source_id": source_id, "price": new_p}).execute()
                
                if current_run_min is None or new_p < current_run_min:
                    current_run_min = new_p
                    best_source_run = source

        # Alert se il nuovo minimo assoluto e inferiore al vecchio minimo assoluto
        if current_run_min and old_min_total and current_run_min < old_min_total:
            diff = old_min_total - current_run_min
            perc = (diff / old_min_total) * 100
            msg = f"<b>🔥 CALO PREZZO!</b>\n{vinyl['artist']} - {vinyl['title']}\n\n"
            msg += f"Nuovo minimo: <b>{format_eur(current_run_min)}</b> su {best_source_run['site_name']}\n"
            msg += f"Risparmio: {format_eur(diff)} (-{perc:.1f}%)\n"
            msg += f"Precedente: {format_eur(old_min_total)}"
            send_telegram_alert(msg, best_source_run["url"], vinyl["id"], vinyl.get("cover_url"))

if __name__ == "__main__":
    process_vinyls()
