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
    return f"{price_float:.2f}".replace(".", ",") + " EUR"

def send_telegram_alert(msg_text, link_url, cover_url=None):
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    
    reply_markup = {
        "inline_keyboard": [[{"text": "Vedi Offerta", "url": link_url}]]
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
    match = re.search(r'\d+[.,]\d{2}', str(price_str))
    if match:
        clean_str = match.group().replace(',', '.')
        try:
            return float(clean_str)
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
    whole = soup.find("span", {"class": "a-price-whole"})
    fraction = soup.find("span", {"class": "a-price-fraction"})
    if whole and fraction:
        return parse_price(whole.text.strip() + "." + fraction.text.strip())
    
    offscreen = soup.find("span", {"class": "a-offscreen"})
    if offscreen:
        return parse_price(offscreen.text)
        
    for pid in ["priceblock_ourprice", "priceblock_dealprice"]:
        ptag = soup.find("span", id=pid)
        if ptag:
            return parse_price(ptag.text)
            
    # Ricerca bruta per Amazon
    price_tag = soup.find("span", class_="a-color-price")
    if price_tag:
        return parse_price(price_tag.text)

    return None

def scrape_feltrinelli(soup):
    json_price = extract_json_ld_price(soup)
    if json_price:
        return json_price

    # Ricerca bruta nelle variabili Javascript di Feltrinelli
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
        
        # Stampa il titolo della pagina per capire se c'è un blocco CAPTCHA
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
    response = supabase.table("vinyls").select("*, sources(*)").execute()
    vinyls = response.data

    for vinyl in vinyls:
        artist = vinyl["artist"]
        title = vinyl["title"]
        cover_url = vinyl.get("cover_url")
        sources = vinyl.get("sources", [])
        
        if not sources:
            continue

        old_prices = [s["current_price"] for s in sources if s["current_price"] is not None]
        old_lowest = min(old_prices) if old_prices else None
        
        new_prices_data = []
        cover_updated = False
        
        for source in sources:
            time.sleep(random.uniform(4, 8))
            new_price, fetched_image = get_current_data(source["url"], source["site_name"])
            
            if not cover_url and fetched_image and not cover_updated:
                supabase.table("vinyls").update({"cover_url": fetched_image}).eq("id", vinyl["id"]).execute()
                cover_url = fetched_image
                cover_updated = True
            
            if new_price is not None:
                new_prices_data.append({
                    "site_name": source["site_name"],
                    "url": source["url"],
                    "price": new_price
                })
                
                supabase.table("sources").update({
                    "last_price": source["current_price"],
                    "current_price": new_price,
                    "updated_at": "now()"
                }).eq("id", source["id"]).execute()

        if not new_prices_data:
            continue

        valid_new_prices = [p["price"] for p in new_prices_data]
        new_lowest = min(valid_new_prices)
        lowest_data = next(p for p in new_prices_data if p["price"] == new_lowest)

        if old_lowest is None:
            msg = f"<b>Inizio Monitoraggio</b>\n{artist} - {title}\n\n"
            msg += "Prezzi iniziali:\n"
            for p in new_prices_data:
                msg += f"• {p['site_name']}: {format_eur(p['price'])}\n"
            
            msg += f"\n<b>Prezzo minimo attuale:</b>\n{lowest_data['site_name']} a {format_eur(new_lowest)}"
            send_telegram_alert(msg, lowest_data['url'], cover_url)
            continue

        if new_lowest < old_lowest:
            drop_eur = old_lowest - new_lowest
            drop_pct = (drop_eur / old_lowest) * 100
            
            msg = f"<b>Calo di prezzo!</b>\n{artist} - {title}\n\n"
            msg += f"Il prezzo minimo è sceso a <b>{format_eur(new_lowest)}</b> su {lowest_data['site_name']}.\n"
            msg += f"Risparmio: {format_eur(drop_eur)} ({drop_pct:.1f}%).\n\n"
            msg += f"Precedente minimo: {format_eur(old_lowest)}"
            send_telegram_alert(msg, lowest_data['url'], cover_url)

if __name__ == "__main__":
    process_vinyls()
