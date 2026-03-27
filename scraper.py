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
    if price_float is None:
        return "N/D"
    return f"{price_float:.2f}".replace(".", ",") + " EUR"

def send_telegram_alert(msg_text, link_url, vinyl_id, cover_url=None):
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    
    # Aggiornati i bottoni in vista dell'interattività Webhook
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Apri Link", "url": link_url},
                {"text": "Statistiche", "callback_data": f"stats_{vinyl_id}"}
            ]
        ]
    }
    
    if cover_url:
        endpoint = f"{base_url}/sendPhoto"
        payload = {
            "chat_id": CHAT_ID,
            "photo": cover_url,
            "caption": msg_text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup
        }
    else:
        endpoint = f"{base_url}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": msg_text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup
        }
        
    try:
        response = cloudscraper.create_scraper().post(endpoint, json=payload, timeout=10)
        if not response.ok:
            print(f"Errore API Telegram: {response.text}")
    except Exception as e:
        print(f"Errore connessione Telegram: {e}")

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
    for hid in ["attach-base-product-price", "twister-plus-price-data-price"]:
        inp = soup.find("input", id=hid)
        if inp and inp.get("value"):
            return parse_price(inp["value"])
            
    offscreen_spans = soup.find_all("span", class_="a-offscreen")
    for span in offscreen_spans:
        val = parse_price(span.text)
        if val and val > 0:
            return val
            
    swatches = soup.find("div", id="tmmSwatches")
    if swatches:
        selected = swatches.find("li", class_=re.compile("selected"))
        if selected:
            price_tag = selected.find("span", class_="a-color-price")
            if price_tag:
                return parse_price(price_tag.text)

    whole = soup.find("span", class_="a-price-whole")
    fraction = soup.find("span", class_="a-price-fraction")
    if whole and fraction:
        w_text = re.sub(r'[^\d]', '', whole.text)
        f_text = re.sub(r'[^\d]', '', fraction.text)
        return parse_price(w_text + "." + f_text)

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
    print(f"Controllo {site_name}: {url}")
    
    scraper = cloudscraper.create_scraper(browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    })
    
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.google.com/"
    }
    
    try:
        response = scraper.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(response.content, "html.parser")
        
        page_title = soup.title.string.strip() if soup.title and soup.title.string else "Nessun titolo trovato"
        print(f"[{site_name}] Status Code: {response.status_code} | Titolo pagina: {page_title}")
        
        image_url = extract_image(soup)
        price = None
        
        name_lower = site_name.lower()
        if "amazon" in name_lower:
            price = scrape_amazon(soup)
        elif "feltrinelli" in name_lower:
            price = scrape_feltrinelli(soup)
        else:
            price = scrape_other(soup, url)
            
        if price is None:
            print(f"Prezzo non trovato per {site_name}.")
            
        return price, image_url
    except Exception as e:
        print(f"Errore scraping {site_name} - {url}: {e}")
        return None, None

def process_vinyls():
    # Filtra solo i vinili attivi per lo scraping
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
            time.sleep(random.uniform(3, 6))
            
            # Recupero dati attuali
            new_price, fetched_image = get_current_data(source["url"], source["site_name"])
            
            # Aggiornamento cover se mancante
            if not cover_url and fetched_image and not cover_updated:
                supabase.table("vinyls").update({"cover_url": fetched_image}).eq("id", vinyl_id).execute()
                cover_url = fetched_image
                cover_updated = True
            
            # Gestione storico e aggiornamento DB se il prezzo è valido
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
                
                # Gestione All-Time High/Low
                if current_ath is None or current_ath == 0 or new_price < current_ath:
                    update_payload["ath_price"] = new_price
                
                # Aggiorna la tabella sources
                supabase.table("sources").update(update_payload).eq("id", source_id).execute()
                
                # Scrivi nello storico se il prezzo è cambiato o non c'era
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

        # Logica di notifica Telegram
        if old_lowest is None:
            msg = f"<b>Inizio Monitoraggio</b>\n{artist} - {title}\n\n"
            msg += "Prezzi iniziali:\n"
            for p in new_prices_data:
                msg += f"• {p['site_name']}: {format_eur(p['price'])}\n"
            
            msg += f"\n<b>Prezzo minimo attuale:</b>\n{lowest_data['site_name']} a {format_eur(new_lowest)}"
            send_telegram_alert(msg, lowest_data['url'], vinyl_id, cover_url)
            continue

        if new_lowest < old_lowest:
            drop_eur = old_lowest - new_lowest
            drop_pct = (drop_eur / old_lowest) * 100
            
            msg = f"<b>Calo di prezzo!</b>\n{artist} - {title}\n\n"
            msg += f"Il prezzo minimo è sceso a <b>{format_eur(new_lowest)}</b> su {lowest_data['site_name']}.\n"
            msg += f"Risparmio: {format_eur(drop_eur)} ({drop_pct:.1f}%).\n\n"
            msg += f"Precedente minimo: {format_eur(old_lowest)}"
            send_telegram_alert(msg, lowest_data['url'], vinyl_id, cover_url)

if __name__ == "__main__":
    process_vinyls()
