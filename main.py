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

# ── Scrapers (Ejemplo Argenprop) ──────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

async def scrape_argenprop() -> list[dict]:
    url = "https://www.argenprop.com/casas-y-ph/venta/la-plata?precioMax=100000&moneda=dolares&aptoBanco=true"
    resultados = []
    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select(".listing__item"):
                link_tag = card.select_one("a")
                link = link_tag["href"] if link_tag else ""
                if not link: continue
                
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
    except Exception as e: 
        print(f"Error Argenprop: {e}")
    return resultados

# ── Notificaciones ────────────────────────────────────────────────────────────
def enviar_alerta(nuevas: list[dict]):
    if not EMAIL_FROM or not nuevas: return
    msg = MIMEMultipart()
    msg["Subject"] = f"🏠 {len(nuevas)} Nueva(s) propiedad(es) en La Plata"
    msg["From"], msg["To"] = EMAIL_FROM, EMAIL_TO
    
    items_html = "".join([f"<li><b>{p['portal']}</b>: {p['precio']} en {p['direccion']}<br><a href='{p['url']}'>Ver Propiedad</a></li>" for p in nuevas])
    html = f"<h2>Se encontraron nuevas oportunidades:</h2><ul>{items_html}</ul>"
    
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    except Exception as e: 
        print(f"Error enviando Email: {e}")

# ── Lógica de Segundo Plano ───────────────────────────────────────────────────
async def check_y_alertar():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando búsqueda...")
    props = await scrape_argenprop()
    # Aquí puedes agregar llamadas a otros scrapers (zonaprop, inmobusqueda, etc)
    nuevas = guardar_nuevas(props)
    print(f"Búsqueda finalizada. {len(props)} encontradas, {len(nuevas)} nuevas.")
    if nuevas:
        enviar_alerta(nuevas)

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Ejecuta una búsqueda inmediata al encender el servidor
    asyncio.create_task(check_y_alertar())
    # Programa las búsquedas automáticas
    scheduler.add_job(check_y_alertar, "interval", minutes=CHECK_MINUTES)
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

# ── RUTAS (Aquí está la solución a tu problema) ──────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """
    Esta ruta reemplaza el mensaje JSON por el contenido de tu index.html.
    Busca el archivo dentro de la carpeta /templates.
    """
    try:
        # Intentamos leer el archivo HTML del frontend
        path = os.path.join("templates", "index.html")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return """
        <html>
            <body style="font-family:sans-serif; text-align:center; padding:50px;">
                <h1>⚠️ Error: index.html no encontrado</h1>
                <p>Asegúrate de que el archivo <b>index.html</b> esté dentro de una carpeta llamada <b>templates</b> en tu repositorio.</p>
                <p><a href="/api/propiedades">Ver datos crudos (JSON)</a></p>
            </body>
        </html>
        """

@app.get("/healthman")
async def health():
    """Ruta para mantener la app despierta con cron-job.org"""
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.get("/api/propiedades")
async def list_props():
    """Devuelve las últimas 100 propiedades guardadas"""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM propiedades ORDER BY visto_en DESC LIMIT 100").fetchall()
    con.close()
    return [dict(r) for r in rows]

@app.post("/api/refresh")
async def manual_refresh():
    """Fuerza una búsqueda manual desde la interfaz"""
    asyncio.create_task(check_y_alertar())
    return {"message": "Búsqueda iniciada en segundo plano"}
