import os
import sqlite3
import smtplib
import httpx
import asyncio
import webbrowser
import random
import time
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
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
    if not props: return []
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

# ── SCRAPERS (ULTRA REFORZADOS) ───────────────────────────────────────────────
async def buscar_propiedades():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando escaneo profundo...")
    resultados = []
    
    # Headers dinámicos para engañar al firewall
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.8,en-US;q=0.5,en;q=0.3",
        "Referer": "https://www.google.com.ar/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=50.0) as client:
        # 1. ARGENPROP (Búsqueda agresiva)
        try:
            url_ap = "https://www.argenprop.com/casas-y-ph/venta/la-plata?precioMax=100000&moneda=dolares&aptoBanco=true"
            # Simular espera humana
            await asyncio.sleep(random.uniform(1.5, 3.5))
            r = await client.get(url_ap)
            
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                # Intenta múltiples selectores por si cambiaron las clases
                items = soup.find_all("div", class_="listing__item") or soup.find_all("div", class_=lambda x: x and 'card' in x)
                
                print(f"Argenprop: Se detectaron {len(items)} posibles anuncios.")
                
                for item in items:
                    # Buscamos el link que contenga /detalle/
                    link_tag = item.find("a", href=lambda x: x and '/detalle/' in x)
                    if link_tag:
                        url = "https://www.argenprop.com" + link_tag['href']
                        precio = item.select_one(".card__price") or item.find(class_=lambda x: x and 'price' in x)
                        direccion = item.select_one(".card__address") or item.find(class_=lambda x: x and 'address' in x)
                        
                        resultados.append({
                            "id": f"ap_{hash(url)}", 
                            "portal": "Argenprop",
                            "titulo": "Propiedad en Venta", 
                            "precio": precio.get_text(strip=True) if precio else "USD Consultar",
                            "direccion": direccion.get_text(strip=True) if direccion else "La Plata",
                            "url": url
                        })
            else:
                print(f"Argenprop Bloqueado: Status {r.status_code}")
        except Exception as e: print(f"Error Argenprop: {e}")

        # 2. INMOBUSQUEDA
        try:
            url_in = "https://www.inmobusqueda.com.ar/casa-venta-la-plata-hasta-100000-dolares.html"
            await asyncio.sleep(random.uniform(1, 2))
            r = await client.get(url_in)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                items = soup.select(".resultado-busqueda")
                print(f"Inmobusqueda: Se detectaron {len(items)} anuncios.")
                for item in items:
                    link_tag = item.find("a", href=True)
                    if link_tag:
                        url = link_tag["href"]
                        resultados.append({
                            "id": f"in_{hash(url)}", 
                            "portal": "Inmobusqueda",
                            "titulo": item.select_one(".titulo").get_text(strip=True) if item.select_one(".titulo") else "Casa",
                            "precio": item.select_one(".precio").get_text(strip=True) if item.select_one(".precio") else "Consultar",
                            "direccion": "La Plata", 
                            "url": url
                        })
        except Exception as e: print(f"Error Inmobusqueda: {e}")

    nuevas = guardar_propiedades(resultados)
    if nuevas and EMAIL_PASS and EMAIL_FROM:
        enviar_notificacion(nuevas)
    
    print(f"Escaneo finalizado. Total acumulado en lista: {len(resultados)}")

def enviar_notificacion(nuevas):
    msg = MIMEMultipart()
    msg["Subject"] = f"🏠 Alerta: {len(nuevas)} Propiedades Nuevas"
    msg["From"], msg["To"] = EMAIL_FROM, EMAIL_TO
    html = f"<h3>Se encontraron {len(nuevas)} nuevas opciones:</h3><ul>"
    for p in nuevas:
        html += f"<li><b>{p['portal']}</b>: {p['precio']} en {p['direccion']} - <a href='{p['url']}'>Ver más</a></li>"
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
    <title>InmoLaPlata Tracker</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .portal-Argenprop { border-left: 5px solid #f87171; }
        .portal-Inmobusqueda { border-left: 5px solid #4ade80; }
        @keyframes pulse-custom { 0%, 100% { opacity: 1; } 50% { opacity: .5; } }
        .animate-pulse-fast { animation: pulse-custom 1.5s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
    </style>
</head>
<body class="bg-slate-50 min-h-screen">
    <nav class="bg-slate-900 text-white p-4 shadow-2xl sticky top-0 z-50">
        <div class="container mx-auto flex justify-between items-center">
            <h1 class="font-black text-xl tracking-tighter italic">INMO<span class="text-blue-500 underline">LAPLATA</span></h1>
            <div class="flex items-center gap-4">
                <span id="contador" class="hidden sm:block text-[10px] font-mono bg-slate-800 px-3 py-1 rounded-full text-blue-400 border border-blue-900">0 PROPS</span>
                <button onclick="refresh()" id="btn" class="bg-blue-600 hover:bg-blue-500 px-6 py-2 rounded-full text-xs font-black uppercase tracking-widest transition-all active:scale-95 shadow-lg">Actualizar</button>
            </div>
        </div>
    </nav>

    <div class="container mx-auto p-6 max-w-6xl">
        <div id="lista" class="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
            <div class="col-span-full flex flex-col items-center justify-center py-32 text-slate-300">
                <div class="text-6xl mb-4">🔍</div>
                <p class="font-medium">Presiona el botón para iniciar la búsqueda</p>
                <p class="text-xs mt-2 uppercase tracking-widest opacity-50">El primer escaneo puede tardar 15 segundos</p>
            </div>
        </div>
    </div>

    <script>
        async function load() {
            try {
                const r = await fetch('/api/propiedades');
                const data = await r.json();
                const container = document.getElementById('lista');
                const contador = document.getElementById('contador');
                
                contador.innerText = `${data.length} RESULTADOS`;
                contador.classList.remove('hidden');

                if (data.length === 0) return;

                container.innerHTML = data.map(p => `
                    <div class="bg-white rounded-3xl shadow-sm hover:shadow-xl transition-all duration-500 overflow-hidden group border border-slate-100 portal-${p.portal}">
                        <div class="p-6">
                            <div class="flex justify-between items-start mb-4">
                                <span class="text-[9px] font-black bg-slate-100 px-2 py-1 rounded text-slate-500 uppercase tracking-tighter">${p.portal}</span>
                                <div class="text-right">
                                    <p class="text-2xl font-black text-slate-900 leading-none">${p.precio}</p>
                                    <p class="text-[9px] text-blue-500 font-bold uppercase mt-1">Dólares</p>
                                </div>
                            </div>
                            <h3 class="text-slate-800 font-bold text-sm mb-2 line-clamp-2 min-h-[40px]">${p.titulo}</h3>
                            <div class="flex items-center gap-2 text-slate-400 mb-6">
                                <span class="text-xs">📍</span>
                                <p class="text-[11px] font-medium truncate">${p.direccion}</p>
                            </div>
                            <div class="flex items-center justify-between pt-4 border-t border-slate-50">
                                <span class="text-[9px] text-slate-300 font-bold tracking-widest">${p.visto_en}</span>
                                <a href="${p.url}" target="_blank" class="bg-slate-900 text-white px-4 py-2 rounded-xl text-[10px] font-black hover:bg-blue-600 transition-colors uppercase">Detalles</a>
                            </div>
                        </div>
                    </div>`).join('');
            } catch(e) { console.error("Error UI:", e); }
        }

        async function refresh() {
            const btn = document.getElementById('btn');
            const list = document.getElementById('lista');
            btn.innerText = '🛰️ ESCANEANDO...';
            btn.disabled = true;
            btn.classList.add('animate-pulse-fast');
            
            try {
                await fetch('/api/refresh', {method: 'POST'});
                // Damos tiempo suficiente para que los scrapers terminen
                setTimeout(async () => { 
                    await load(); 
                    btn.innerText = 'Actualizar'; 
                    btn.disabled = false;
                    btn.classList.remove('animate-pulse-fast');
                }, 15000);
            } catch(e) {
                btn.innerText = 'REINTENTAR';
                btn.disabled = false;
            }
        }
        
        load();
        setInterval(load, 60000);
    </script>
</body>
</html>
"""

# ── SERVIDOR ──────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # No disparamos el escaneo inmediatamente para no bloquear el inicio en Render
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
