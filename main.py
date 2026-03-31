import os
import sqlite3
import smtplib
import httpx
import asyncio
import webbrowser
import random
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from bs4 import BeautifulSoup

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
EMAIL_FROM    = os.getenv("EMAIL_FROM", "tu_correo@gmail.com")
EMAIL_TO      = os.getenv("EMAIL_TO", "tu_correo@gmail.com")
EMAIL_PASS    = os.getenv("EMAIL_PASS", "") 
DB_PATH       = "propiedades.db"
CHECK_MINUTES = 45 

# Lista de User-Agents para evitar bloqueos por parte de los portales
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

# ── BASE DE DATOS ─────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS propiedades (
            id TEXT PRIMARY KEY, portal TEXT, titulo TEXT, precio TEXT,
            direccion TEXT, url TEXT, visto_en TEXT
        )
    """)
    con.commit()
    con.close()

def guardar_propiedades(props):
    con = sqlite3.connect(DB_PATH)
    nuevas_para_email = []
    ahora = datetime.now().strftime("%d/%m %H:%M")
    for p in props:
        cursor = con.execute("SELECT id FROM propiedades WHERE id = ?", (p["id"],))
        existe = cursor.fetchone()
        
        con.execute("""
            INSERT OR REPLACE INTO propiedades (id, portal, titulo, precio, direccion, url, visto_en)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (p["id"], p["portal"], p["titulo"], p["precio"], p["direccion"], p["url"], ahora))
        
        if not existe:
            nuevas_para_email.append(p)
            
    con.commit()
    con.close()
    return nuevas_para_email

# ── SCRAPERS ──────────────────────────────────────────────────────────────────
async def buscar_propiedades():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando escaneo...")
    resultados = []
    
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
        "Upgrade-Insecure-Requests": "1"
    }
    
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=45.0) as client:
        # 1. ARGENPROP
        try:
            url_ap = "https://www.argenprop.com/casas-y-ph/venta/la-plata?precioMax=100000&moneda=dolares&aptoBanco=true"
            r = await client.get(url_ap)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                # Intenta capturar por clase genérica si la específica falla
                items = soup.select(".listing__item") or soup.select("[class*='listing__item']")
                print(f"Argenprop: Encontrados {len(items)} items brutos.")
                for item in items:
                    link_tag = item.select_one("a.card") or item.select_one("a[href*='detalle']")
                    if link_tag and link_tag.has_attr('href'):
                        link = "https://www.argenprop.com" + link_tag["href"]
                        precio = item.select_one(".card__price") or item.select_one("[class*='price']")
                        direccion = item.select_one(".card__address") or item.select_one("[class*='address']")
                        
                        resultados.append({
                            "id": f"ap_{hash(link)}", 
                            "portal": "Argenprop",
                            "titulo": "Propiedad en Venta", 
                            "precio": precio.get_text(strip=True) if precio else "Consultar",
                            "direccion": direccion.get_text(strip=True) if direccion else "La Plata",
                            "url": link
                        })
        except Exception as e: print(f"Error Argenprop: {e}")

        # 2. INMOBUSQUEDA
        try:
            url_in = "https://www.inmobusqueda.com.ar/casa-venta-la-plata-hasta-100000-dolares.html"
            r = await client.get(url_in)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                items = soup.select(".resultado-busqueda")
                print(f"Inmobusqueda: Encontrados {len(items)} items brutos.")
                for item in items:
                    link_tag = item.select_one("a")
                    if link_tag and link_tag.has_attr('href'):
                        link = link_tag["href"]
                        titulo = item.select_one(".titulo")
                        precio = item.select_one(".precio")
                        resultados.append({
                            "id": f"in_{hash(link)}", 
                            "portal": "Inmobusqueda",
                            "titulo": titulo.get_text(strip=True) if titulo else "Casa",
                            "precio": precio.get_text(strip=True) if precio else "Consultar",
                            "direccion": "La Plata", 
                            "url": link
                        })
        except Exception as e: print(f"Error Inmobusqueda: {e}")

    nuevas = guardar_propiedades(resultados)
    if nuevas and EMAIL_PASS and EMAIL_FROM:
        enviar_notificacion(nuevas)
    
    print(f"Proceso terminado. Total en DB: {len(resultados)}")

def enviar_notificacion(nuevas):
    msg = MIMEMultipart()
    msg["Subject"] = f"🏠 {len(nuevas)} Nuevas Casas en La Plata"
    msg["From"], msg["To"] = EMAIL_FROM, EMAIL_TO
    html = f"<h3>Ingresos nuevos:</h3><ul>"
    for p in nuevas:
        html += f"<li><b>{p['portal']}</b>: {p['precio']} - <a href='{p['url']}'>Ver publicación</a></li>"
    html += "</ul>"
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    except Exception as e: print(f"Error Email: {e}")

# ── INTERFAZ HTML ─────────────────────────────────────────────────────────────
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Buscador Inmobiliario LP</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .portal-Argenprop { border-left-color: #f87171; }
        .portal-Inmobusqueda { border-left-color: #4ade80; }
    </style>
</head>
<body class="bg-gray-50 min-h-screen">
    <nav class="bg-slate-800 text-white p-4 shadow-lg sticky top-0 z-50">
        <div class="container mx-auto flex justify-between items-center">
            <h1 class="font-bold text-lg tracking-tight">🏠 Inmo<span class="text-blue-400">LaPlata</span></h1>
            <div class="flex items-center gap-4">
                <span id="contador" class="text-xs bg-slate-700 px-2 py-1 rounded text-gray-300 tracking-wide">Cargando...</span>
                <button onclick="refresh()" id="btn" class="bg-blue-600 px-4 py-2 rounded-lg text-sm font-bold hover:bg-blue-500 transition shadow-md">Actualizar</button>
            </div>
        </div>
    </nav>
    <div class="container mx-auto p-4 max-w-5xl">
        <div id="lista" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div class="col-span-full text-center py-20 text-gray-400">Buscando propiedades disponibles...</div>
        </div>
    </div>
    <script>
        async function load() {
            try {
                const r = await fetch('/api/propiedades');
                const data = await r.json();
                const container = document.getElementById('lista');
                const contador = document.getElementById('contador');
                
                contador.innerText = `${data.length} Propiedades`;

                if (data.length === 0) {
                    container.innerHTML = '<div class="col-span-full text-center py-20 text-gray-500"><p class="mb-4">No se encontraron resultados aún.</p><p class="text-xs">Intenta presionar "Actualizar" y espera unos segundos.</p></div>';
                    return;
                }
                container.innerHTML = data.map(p => `
                    <div class="bg-white p-5 rounded-2xl shadow-sm border-l-4 portal-${p.portal} flex flex-col justify-between hover:shadow-md transition duration-300">
                        <div>
                            <div class="flex justify-between items-center mb-3">
                                <span class="text-[10px] font-bold text-gray-400 uppercase tracking-widest">${p.portal}</span>
                                <span class="text-blue-600 font-extrabold text-lg">${p.precio}</span>
                            </div>
                            <h3 class="text-gray-800 font-bold leading-tight mb-1 text-sm">${p.titulo}</h3>
                            <p class="text-[11px] text-gray-500 line-clamp-1">${p.direccion}</p>
                        </div>
                        <div class="mt-6 flex justify-between items-center pt-4 border-t border-gray-50">
                            <span class="text-[9px] text-gray-300 font-medium uppercase">Visto: ${p.visto_en}</span>
                            <a href="${p.url}" target="_blank" class="text-xs font-bold text-blue-500 hover:text-blue-700 underline decoration-2 underline-offset-4">VER FICHA →</a>
                        </div>
                    </div>`).join('');
            } catch(e) { console.error("Error cargando UI:", e); }
        }
        async function refresh() {
            const btn = document.getElementById('btn');
            btn.innerText = '⏳ Buscando...'; btn.disabled = true;
            try {
                await fetch('/api/refresh', {method: 'POST'});
                // Tiempo de espera para permitir que el scraper trabaje
                setTimeout(() => { 
                    load(); 
                    btn.innerText = 'Actualizar'; 
                    btn.disabled = false; 
                }, 10000);
            } catch(e) {
                btn.innerText = 'Error';
                btn.disabled = false;
            }
        }
        load();
        setInterval(load, 60000);
    </script>
</body>
</html>
"""

# ── SERVIDOR FASTAPI ──────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(buscar_propiedades())
    scheduler.add_job(buscar_propiedades, "interval", minutes=CHECK_MINUTES)
    scheduler.start()
    if not os.getenv("RENDER"):
        webbrowser.open("http://127.0.0.1:8000")
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_CONTENT

@app.get("/api/propiedades")
async def get_props():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    res = con.execute("SELECT * FROM propiedades ORDER BY visto_en DESC").fetchall()
    con.close()
    return [dict(r) for r in res]

@app.post("/api/refresh")
async def manual_search():
    asyncio.create_task(buscar_propiedades())
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
