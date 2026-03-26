import os
import re
import requests
from bs4 import BeautifulSoup
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Errore invio Telegram: {e}")

def parse_price(price_str):
    if not price_str:
        return None
    
    # Cerca un pattern numerico con due decimali (es. 27,40 o 27.40)
    match = re.search(r'\d+[.,]\d{2}', price_str)
    if match:
        clean_str = match.group().replace(',', '.')
        try:
            return float(clean_str)
        except ValueError:
            return None
    return None

def scrape_amazon(soup):
    whole = soup.find("span", {"class": "a-price-whole"})
    fraction = soup.find("span", {"class": "a-price-fraction"})
    if whole and fraction:
        return parse_price(whole.text.strip() + "." + fraction.text.strip())
    return None

def scrape_feltrinelli(soup):
    price_tag = soup.find("span", {"class": "price"})
    if price_tag:
        return parse_price(price_tag.text)
    return None

def scrape_other(soup, url):
    # Controllo prioritario per Discoteca Laziale
    if "discotecalaziale" in url.lower():
        price_div = soup.find("div", {"class": "price"})
        if price_div:
            val = parse_price(price_div.text)
            if val is not None:
                return val

    # Fallback 1: Cerca qualsiasi elemento con classe che contiene 'price'
    price_elements = soup.find_all(class_=re.compile("price", re.I))
    for el in price_elements:
        val = parse_price(el.text)
        if val is not None:
            return val

    # Fallback 2: Ricerca testuale del simbolo Euro
    euro_pattern = re.compile(r'€\s*\d+[.,]\d{2}|\d+[.,]\d{2}\s*€')
    match = soup.find(string=euro_pattern)
    if match:
        found = euro_pattern.search(match).group()
        return parse_price(found)

    return None

def get_current_price(url, site_name):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.content, "html.parser")
        
        name_lower = site_name.lower()
        if "amazon" in name_lower:
            return scrape_amazon(soup)
        elif "feltrinelli" in name_lower:
            return scrape_feltrinelli(soup)
        else:
            return scrape_other(soup, url)
    except Exception as e:
        print(f"Errore scraping {url}: {e}")
        return None

def process_vinyls():
    response = supabase.table("vinyls").select("*, sources(*)").execute()
    vinyls = response.data

    for vinyl in vinyls:
        artist = vinyl["artist"]
        title = vinyl["title"]
        sources = vinyl.get("sources", [])
        
        if not sources:
            continue

        old_prices = [s["current_price"] for s in sources if s["current_price"] is not None]
        old_lowest = min(old_prices) if old_prices else None
        
        new_prices_data = []
        
        for source in sources:
            new_price = get_current_price(source["url"], source["site_name"])
            
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

        # Notifica prima esecuzione (prezzi a null nel DB)
        if old_lowest is None:
            msg = f"<b>Nuovo vinile in monitoraggio:</b> {artist} - {title}\n\n"
            msg += "Prezzi iniziali rilevati:\n"
            for p in new_prices_data:
                msg += f"- {p['site_name']}: {p['price']:.2f}€\n"
            
            msg += f"\n<b>Prezzo più basso:</b> {lowest_data['site_name']} a {new_lowest:.2f}€\n"
            msg += f"<a href='{lowest_data['url']}'>Link diretto</a>"
            send_telegram_message(msg)
            continue

        # Notifica ribasso
        if new_lowest < old_lowest:
            drop_eur = old_lowest - new_lowest
            drop_pct = (drop_eur / old_lowest) * 100
            
            msg = f"<b>Ribasso rilevato!</b>\n{artist} - {title}\n\n"
            msg += f"Il prezzo totale più basso è sceso a <b>{new_lowest:.2f}€</b> su {lowest_data['site_name']}.\n"
            msg += f"Calo di {drop_eur:.2f}€ ({drop_pct:.1f}%).\n\n"
            msg += f"Precedente minimo: {old_lowest:.2f}€\n"
            msg += f"<a href='{lowest_data['url']}'>Acquista ora</a>"
            send_telegram_message(msg)

if __name__ == "__main__":
    process_vinyls()
