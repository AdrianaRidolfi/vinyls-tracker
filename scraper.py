import os
import time
import random
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    ContextTypes, 
    ConversationHandler, 
    MessageHandler, 
    filters,
    CallbackQueryHandler
)

load_dotenv()

# Configurazione variabili d'ambiente
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# Lista di ID separati da virgola, es: 123456,789012
ALLOWED_USERS = [int(i.strip()) for i in os.getenv("ALLOWED_USERS").split(",")]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Stati per la conversazione /add
ARTIST, TITLE, URL_AMZ, URL_FELT, URL_DISC = range(5)

def scrape_price(url, site_name):
    try:
        response = requests.get(url, headers=get_headers(), timeout=10)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.content, "html.parser")
        price = None

        if site_name == "amazon":
            # Cerca il prezzo intero e i decimali
            price_span = soup.find("span", {"class": "a-price-whole"})
            fraction_span = soup.find("span", {"class": "a-price-fraction"})
            if price_span:
                price_str = price_span.get_text().replace(",", "").replace(".", "")
                fraction_str = fraction_span.get_text() if fraction_span else "00"
                price = float(f"{price_str}.{fraction_str}")

        elif site_name == "feltrinelli":
            price_tag = soup.find("span", {"class": "advisor-price"})
            if price_tag:
                price = float(price_tag.get_text().replace("€", "").replace(",", ".").strip())

        elif site_name == "discoteca_laziale":
            price_tag = soup.find("span", {"class": "price"})
            if price_tag:
                price = float(price_tag.get_text().replace("€", "").replace(",", ".").strip())

        return price
    except Exception as e:
        print(f"Errore durante lo scraping di {url}: {e}")
        return None


# --- LOGICA BOT ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    await update.message.reply_text("Sistema attivo. Usa /add per monitorare un nuovo vinile.")

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    await update.message.reply_text("Inserisci l'Artista del vinile:")
    return ARTIST

async def add_artist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['artist'] = update.message.text
    await update.message.reply_text("Inserisci il Titolo dell'album:")
    return TITLE

async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['title'] = update.message.text
    await update.message.reply_text("Inserisci l'URL di Amazon.it (o scrivi 'no'):")
    return URL_AMZ

async def add_amz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['url_amz'] = update.message.text
    await update.message.reply_text("Inserisci l'URL di Feltrinelli.it (o scrivi 'no'):")
    return URL_FELT

async def add_felt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['url_felt'] = update.message.text
    await update.message.reply_text("Inserisci l'URL di Discoteca Laziale (o scrivi 'no'):")
    return URL_DISC

async def add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_disc = update.message.text
    u_data = context.user_data
    
    # Salvataggio su DB
    res = supabase.table("vinyls").insert({"artist": u_data['artist'], "title": u_data['title']}).execute()
    v_id = res.data[0]["id"]
    
    urls = [
        ("amazon", u_data['url_amz']),
        ("feltrinelli", u_data['url_felt']),
        ("discoteca_laziale", url_disc)
    ]
    
    for site, url in urls:
        if url.lower() != 'no':
            supabase.table("sources").insert({"vinyl_id": v_id, "site_name": site, "url": url}).execute()
            
    await update.message.reply_text("Vinile aggiunto al monitoraggio.")
    return ConversationHandler.END

async def deactivate_vinyl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    vinyl_id = query.data.split("_")[1]
    
    supabase.table("vinyls").update({"is_active": False}).eq("id", vinyl_id).execute()
    await query.edit_message_text(text=f"{query.message.text}\n\n[STATO: MONITORAGGIO DISATTIVATO]")

async def check_prices_job(context: ContextTypes.DEFAULT_TYPE):
    # Recupera solo i vinili attivi
    vinyls = supabase.table("vinyls").select("*, sources(*)").eq("is_active", True).execute()
    
    for vinyl in vinyls.data:
        best_current_price = float('inf')
        best_site = ""
        notifications = []
        
        for source in vinyl['sources']:
            new_price = scrape_price(source["url"], source["site_name"])
            if not new_price: continue
            
            # Calcolo statistiche
            last = source["last_price"] or new_price
            ath = source["ath_price"] or new_price
            
            if new_price < best_current_price:
                best_current_price = new_price
                best_site = source["site_name"]

            # Notifica se il prezzo cala
            if new_price < last:
                diff = last - new_price
                perc = (diff / last) * 100
                notifications.append({
                    "site": source["site_name"],
                    "new": new_price,
                    "old": last,
                    "ath": ath,
                    "diff": diff,
                    "perc": perc,
                    "url": source["url"]
                })

            # Aggiorna DB
            upd = {"current_price": new_price, "last_price": last, "updated_at": "now()"}
            if new_price > ath: upd["ath_price"] = new_price
            supabase.table("sources").update(upd).eq("id", source["id"]).execute()

        # Invia messaggi se ci sono ribassi
        for n in notifications:
            keyboard = [[InlineKeyboardButton("Ho comprato / Disattiva", callback_data=f"stop_{vinyl['id']}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            msg = (
                f"OFFERTA: {vinyl['artist']} - {vinyl['title']}\n"
                f"Store: {n['site']}\n"
                f"Prezzo: €{n['new']} (Risparmi €{n['diff']:.2f}, -{n['perc']:.1f}%)\n"
                f"Prezzo precedente: €{n['old']}\n"
                f"Massimo storico: €{n['ath']}\n"
                f"Miglior prezzo attuale totale: €{best_current_price} su {best_site}\n"
                f"Link: {n['url']}"
            )
            await context.bot.send_message(chat_id=ALLOWED_USERS[0], text=msg, reply_markup=reply_markup)

if __name__ == "__main__":
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ARTIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_artist)],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            URL_AMZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amz)],
            URL_FELT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_felt)],
            URL_DISC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_finish)],
        },
        fallbacks=[],
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(deactivate_vinyl, pattern="^stop_"))
    application.job_queue.run_repeating(check_prices_job, interval=3600, first=10)
    
    application.run_polling()
