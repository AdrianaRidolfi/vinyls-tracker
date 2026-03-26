# Vinyls Tracker

Un sistema serverless per il monitoraggio automatizzato dei prezzi dei vinili su diverse piattaforme e-commerce (Amazon, Feltrinelli, Discoteca Laziale).

## Architettura

Il progetto è costruito interamente su servizi cloud gratuiti e non richiede un server sempre attivo:

* **Database:** Supabase (PostgreSQL). Memorizza il catalogo dei vinili, i link alle piattaforme e lo storico dei prezzi.
* **Motore di Scraping:** GitHub Actions. Un workflow schedulato avvia uno script Python ogni ora per estrarre i prezzi.
* **Bypass Anti-Bot:** Libreria `cloudscraper` per superare i controlli Cloudflare e l'estrazione di metadati JSON-LD per i siti dinamici.
* **Notifiche:** Telegram Bot API. Invia alert con foto, prezzi comparati e link diretti quando si registra un calo di prezzo.
* **Frontend Sicuro:** GitHub Pages. Un'interfaccia statica per l'inserimento di nuovi vinili che delega la scrittura a GitHub Actions tramite `repository_dispatch` e un Personal Access Token.

## Struttura del Repository

* `index.html`: Interfaccia utente statica per l'aggiunta dei vinili.
* `scraper.py`: Core logic in Python per l'estrazione dati e l'invio delle notifiche Telegram.
* `.github/workflows/scraper.yml`: Configurazione del cronjob orario per l'esecuzione dello scraper.
* `.github/workflows/add_vinyl.yml`: Riceve il payload dal frontend e inserisce i dati in Supabase in modo sicuro.
* `requirements.txt`: Dipendenze Python necessarie.

## Variabili d'Ambiente (Secrets)

Il funzionamento richiede la configurazione dei seguenti Repository Secrets su GitHub:

* `SUPABASE_URL`: Endpoint del progetto Supabase.
* `SUPABASE_KEY`: Service Role Key di Supabase per le operazioni di scrittura.
* `TELEGRAM_TOKEN`: Token del bot generato tramite BotFather.
* `CHAT_ID`: ID utente Telegram per la ricezione delle notifiche.

## Utilizzo

Per aggiungere un nuovo vinile al monitoraggio:
1. Aprire la pagina esposta su GitHub Pages.
2. Inserire il proprio GitHub Personal Access Token (con permessi di lettura/scrittura sui contenuti del repo).
3. Compilare Titolo, Artista e i link delle piattaforme desiderate.
4. L'azione innescherà il workflow di inserimento e lo scraper prenderà in carico il vinile al ciclo successivo.

## TODO
- [ ] GET
  - [ ] chg policy sicurezza db
  - [ ] add blocco lista su index.html
  - [ ] add logica js per fetch e render
- [ ] DELETE WEB
  - [ ] add btn elimina su index.html
  - [ ] add logica js trigger dispatch
  - [ ] create workflow delete_vinyl.yml
- [ ] DELETE TELEGRAM
  - [ ] add id vinile al bottone py
  - [ ] create edge function supabase
  - [ ] set edge function come webhook telegram
