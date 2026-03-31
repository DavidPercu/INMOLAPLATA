import os
import sqlite3
import smtplib
import httpx
import asyncio
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from bs4 import BeautifulSoup

# ── Configuración ─────────────────────────────────────────────────────────────
EMAIL_FROM    = os.getenv("EMAIL_FROM", "")
EMAIL_TO      = os.getenv("EMAIL_TO", "")
EMAIL_PASS    = os.getenv("EMAIL_PASS", "")
DB_PATH       = "propiedades.db"
CHECK_MINUTES = 60

# ── Base de Datos ─────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS propiedades (
            id TEXT PRIMARY KEY, portal TEXT, titulo TEXT, precio TEXT,
            direccion TEXT, m2 TEXT, url TEXT, imagen TEXT, apto TEXT, visto_en TEXT
        )
    """)
    con.commit()
    con.close()

def guardar_nuevas(props: list[dict]) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    nuevas = []
    for p in props:
        try:
            con.execute("INSERT INTO propiedades VALUES (?,?,?,?,?,?,?,?,?,?)",
                (p["id"], p["portal"], p["titulo"], p["precio"], p["direccion"], 
                 p["m2"], p["url"], p["imagen"], p["apto"], p["visto_en"]))
            nuevas.append(p)
        except sqlite3.IntegrityError:
            pass
    con.commit()
    con.close()
    return nuevas

# ── Scrapers ──────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

async def scrape_argenprop() -> list[dict]:
    url = "https://www.argenprop.com/casas-y-ph/venta/la-plata?precioMax=100000&moneda=dolares&aptoBanco=true"
    resultados = []
    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select(".listing__item"):
                link = card.select_one("a")["href"] if card.select_one("a") else ""
                resultados.append({
                    "id": f"ap_{hash(link)}",
                    "portal": "Argenprop",
                    "titulo": card.select_one(".card__title").text.strip() if card.select_one(".card__title") else "Casa",
                    "precio": card.select_one(".card__price").text.strip() if card.select_one(".card__price") else "Consultar",
                    "direccion": card.select_one(".card__address").text.strip() if card.select_one(".card__address") else "La Plata",
                    "m2": "?",
                    "url": "https://www.argenprop.com" + link,
                    "imagen": "",
                    "apto": "Apto Crédito",
                    "visto_en": datetime.now().isoformat(),
                })
    except Exception as e: print(f"Error Argenprop: {e}")
    return resultados

# (Puedes replicar la estructura para Zonaprop e Inmobusqueda siguiendo esta lógica)

# ── Notificaciones ────────────────────────────────────────────────────────────
def enviar_alerta(nuevas: list[dict]):
    if not EMAIL_FROM or not nuevas: return
    msg = MIMEMultipart()
    msg["Subject"] = f"🏠 {len(nuevas)} Nueva(s) propiedad(es)"
    msg["From"], msg["To"] = EMAIL_FROM, EMAIL_TO
    html = "<ul>" + "".join([f"<li>{p['portal']}: {p['precio']} - <a href='{p['url']}'>Ver</a></li>" for p in nuevas]) + "</ul>"
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    except Exception as e: print(f"Error Email: {e}")

# ── Lógica de la App ──────────────────────────────────────────────────────────
async def check_y_alertar():
    print("Buscando nuevas propiedades...")
    props = await scrape_argenprop()
    nuevas = guardar_nuevas(props)
    if nuevas: enviar_alerta(nuevas)

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(check_y_alertar())
    scheduler.add_job(check_y_alertar, "interval", minutes=CHECK_MINUTES)
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/healthman")
async def health(): return {"status": "ok"} # Para cron-job.org

@app.get("/")
async def home(): return {"message": "Buscador de propiedades activo"}

@app.get("/api/propiedades")
async def list_props():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM propiedades ORDER BY visto_en DESC LIMIT 50").fetchall()
    con.close()
    return [dict(r) for r in rows]
@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html") as f:
        return f.read()
