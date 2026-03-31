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
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
EMAIL_FROM    = os.getenv("EMAIL_FROM", "tu_correo@gmail.com")
EMAIL_TO      = os.getenv("EMAIL_TO", "tu_correo@gmail.com")
EMAIL_PASS    = os.getenv("EMAIL_PASS", "")
DB_PATH       = "propiedades.db"
CHECK_MINUTES = 45

# ── PARÁMETROS DE BÚSQUEDA (MercadoLibre API oficial) ─────────────────────────
# La Plata bounding box: lat -34.95 a -34.85 / lon -58.05 a -57.88
# category MLA1466 = Casas (subcategoría correcta de Inmuebles MLA1459)
# La API pública de search no acepta filtro de moneda directo;
# filtramos por precio en USD manualmente después de recibir los resultados.
MELI_SEARCH_URL = "https://api.mercadolibre.com/sites/MLA/search"
PRECIO_MAX_USD = 100000

# ── BASE DE DATOS ─────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS propiedades (
            id TEXT PRIMARY KEY,
            portal TEXT,
            titulo TEXT,
            precio TEXT,
            direccion TEXT,
            url TEXT,
            thumbnail TEXT,
            metros TEXT,
            ambientes TEXT,
            apto_banco TEXT,
            visto_en TEXT
        )
    """)
    con.commit()
    con.close()

def guardar_propiedades(props):
    if not props:
        return []
    con = sqlite3.connect(DB_PATH)
    nuevas_para_email = []
    ahora = datetime.now().strftime("%d/%m %H:%M")
    for p in props:
        cursor = con.execute("SELECT id FROM propiedades WHERE id = ?", (p["id"],))
        existe = cursor.fetchone()
        con.execute("""
            INSERT OR REPLACE INTO propiedades
            (id, portal, titulo, precio, direccion, url, thumbnail, metros, ambientes, apto_banco, visto_en)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p["id"], p["portal"], p["titulo"], p["precio"],
            p["direccion"], p["url"], p.get("thumbnail", ""),
            p.get("metros", "—"), p.get("ambientes", "—"),
            p.get("apto_banco", "—"), ahora
        ))
        if not existe:
            nuevas_para_email.append(p)
    con.commit()
    con.close()
    return nuevas_para_email

# ── SCRAPER VÍA API OFICIAL DE MERCADOLIBRE ───────────────────────────────────
async def fetch_meli_page(client, offset: int) -> list:
    """
    Busca casas en venta en La Plata por bounding box geográfico.
    category MLA1466 = Casas | item_location filtra por coordenadas de La Plata.
    """
    params = {
        "category": "MLA1466",   # Casas (subcategoría correcta)
        # Bounding box La Plata: lat -34.95/-34.82, lon -58.05/-57.88
        "item_location": "lat:-34.95_-34.82,lon:-58.05_-57.88",
        "limit": 50,
        "offset": offset,
    }
    r = await client.get(MELI_SEARCH_URL, params=params)
    r.raise_for_status()
    data = r.json()
    print(f"  offset={offset} → HTTP {r.status_code}, resultados: {len(data.get('results', []))}, total: {data.get('paging', {}).get('total', '?')}")
    return data.get("results", []), data.get("paging", {}).get("total", 0)


async def buscar_propiedades():
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] Consultando API de MercadoLibre...")
    resultados = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            items, total = await fetch_meli_page(client, 0)

            # Hasta 3 páginas (150 resultados)
            for offset in [50, 100]:
                if total > offset:
                    page_items, _ = await fetch_meli_page(client, offset)
                    items += page_items

            print(f"Total ítems obtenidos antes de filtrar: {len(items)}")

            for item in items:
                precio_val = item.get("price")
                moneda = item.get("currency_id", "")

                # Filtrar: sólo USD hasta PRECIO_MAX_USD
                if moneda != "USD":
                    continue
                if precio_val is None or precio_val > PRECIO_MAX_USD:
                    continue

                attrs = {a["id"]: a.get("value_name", "—") for a in item.get("attributes", [])}
                metros    = attrs.get("TOTAL_AREA") or attrs.get("COVERED_AREA") or "—"
                ambientes = attrs.get("ROOMS", "—")
                banco_raw = attrs.get("FINANCING", "") or ""
                apto_banco = "✓ Sí" if banco_raw.lower() not in ("", "no", "false") else "—"

                precio_str = f"USD {precio_val:,.0f}"

                loc = item.get("location") or {}
                direccion = (
                    loc.get("address_line")
                    or (loc.get("neighborhood") or {}).get("name")
                    or (loc.get("city") or {}).get("name")
                    or "La Plata"
                )

                resultados.append({
                    "id": str(item["id"]),
                    "portal": "MercadoLibre",
                    "titulo": item.get("title", "Casa en venta"),
                    "precio": precio_str,
                    "direccion": direccion,
                    "url": item.get("permalink", ""),
                    "thumbnail": item.get("thumbnail", ""),
                    "metros": metros,
                    "ambientes": ambientes,
                    "apto_banco": apto_banco,
                })

            print(f"Después de filtrar por USD ≤ {PRECIO_MAX_USD}: {len(resultados)} propiedades")

        except Exception as e:
            import traceback
            print(f"Error MercadoLibre API: {e}")
            traceback.print_exc()

    nuevas = guardar_propiedades(resultados)
    print(f"Escaneo finalizado. {len(resultados)} totales, {len(nuevas)} nuevas.")

    if nuevas and EMAIL_PASS and EMAIL_FROM != "tu_correo@gmail.com":
        enviar_notificacion(nuevas)

    return len(resultados)


# ── EMAIL ─────────────────────────────────────────────────────────────────────
def enviar_notificacion(nuevas):
    msg = MIMEMultipart()
    msg["Subject"] = f"🏠 {len(nuevas)} propiedades nuevas en La Plata"
    msg["From"], msg["To"] = EMAIL_FROM, EMAIL_TO
    html = f"<h3>Se encontraron {len(nuevas)} propiedades nuevas (hasta USD 100.000):</h3><ul>"
    for p in nuevas:
        html += (
            f"<li><b>{p['titulo']}</b><br>"
            f"💰 {p['precio']} | 📍 {p['direccion']}<br>"
            f"📐 {p['metros']} m² | 🛏 {p['ambientes']} amb. | 🏦 Banco: {p['apto_banco']}<br>"
            f"<a href='{p['url']}'>Ver publicación →</a></li><br>"
        )
    html += "</ul>"
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print("Email enviado OK")
    except Exception as e:
        print(f"Error Mail: {e}")

# ── INTERFAZ HTML ─────────────────────────────────────────────────────────────
HTML_CONTENT = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InmoLaPlata</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital@0;1&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0f0f0f;
            --surface: #181818;
            --border: #2a2a2a;
            --accent: #c8ff57;
            --accent2: #57c8ff;
            --text: #f0f0f0;
            --muted: #666;
            --danger: #ff5757;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: 'Syne', sans-serif;
            min-height: 100vh;
        }

        /* NAV */
        nav {
            position: sticky; top: 0; z-index: 100;
            background: rgba(15,15,15,0.9);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--border);
            padding: 14px 32px;
            display: flex; align-items: center; justify-content: space-between;
        }
        .logo {
            font-size: 1.1rem; font-weight: 800; letter-spacing: -0.5px;
        }
        .logo span { color: var(--accent); }
        .nav-right { display: flex; align-items: center; gap: 16px; }
        #status-badge {
            font-family: 'DM Mono', monospace;
            font-size: 10px; letter-spacing: 0.05em;
            color: var(--muted);
            border: 1px solid var(--border);
            padding: 5px 12px; border-radius: 999px;
            transition: all 0.3s;
        }
        #status-badge.active { color: var(--accent); border-color: var(--accent); }
        #btn-refresh {
            font-family: 'Syne', sans-serif;
            font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
            background: var(--accent); color: #0f0f0f;
            border: none; padding: 9px 20px; border-radius: 6px;
            cursor: pointer; transition: all 0.15s;
        }
        #btn-refresh:hover { background: #d9ff6a; }
        #btn-refresh:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; }

        /* HERO STATS */
        .hero {
            padding: 40px 32px 24px;
            border-bottom: 1px solid var(--border);
            display: flex; align-items: flex-end; justify-content: space-between;
            flex-wrap: wrap; gap: 16px;
        }
        .hero-title { font-size: clamp(2rem, 5vw, 3.5rem); font-weight: 800; line-height: 1; }
        .hero-title em { color: var(--accent); font-style: normal; }
        .hero-sub {
            font-family: 'DM Mono', monospace;
            font-size: 11px; color: var(--muted); margin-top: 8px; letter-spacing: 0.05em;
        }
        .stat-chips { display: flex; gap: 8px; flex-wrap: wrap; }
        .chip {
            font-family: 'DM Mono', monospace; font-size: 10px;
            border: 1px solid var(--border); padding: 6px 14px; border-radius: 999px;
            color: var(--muted);
        }
        .chip b { color: var(--text); }

        /* GRID */
        .grid-container { padding: 28px 32px; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 1px;
            background: var(--border);
            border: 1px solid var(--border);
        }
        .card {
            background: var(--surface);
            padding: 24px;
            transition: background 0.2s;
            display: flex; flex-direction: column;
        }
        .card:hover { background: #1f1f1f; }

        .card-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
        .portal-tag {
            font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.12em;
            text-transform: uppercase; color: var(--muted);
            border: 1px solid var(--border); padding: 3px 8px; border-radius: 4px;
        }
        .precio {
            text-align: right;
            font-family: 'DM Mono', monospace;
            font-size: 1.15rem; font-weight: 400; color: var(--accent);
            line-height: 1;
        }

        .titulo {
            font-size: 0.85rem; font-weight: 700; line-height: 1.4;
            margin-bottom: 10px; color: var(--text);
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
        }
        .direccion {
            font-family: 'DM Mono', monospace; font-size: 10px; color: var(--muted);
            margin-bottom: 16px; letter-spacing: 0.03em;
        }

        .attrs {
            display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px;
        }
        .attr-tag {
            font-family: 'DM Mono', monospace; font-size: 9px;
            background: rgba(255,255,255,0.05); padding: 4px 8px; border-radius: 4px;
            color: var(--muted);
        }
        .attr-tag.banco { color: var(--accent2); }

        .card-footer {
            margin-top: auto;
            display: flex; justify-content: space-between; align-items: center;
            padding-top: 16px; border-top: 1px solid var(--border);
        }
        .visto {
            font-family: 'DM Mono', monospace; font-size: 9px; color: var(--border);
        }
        .btn-ver {
            font-family: 'Syne', sans-serif; font-size: 10px; font-weight: 700;
            letter-spacing: 0.08em; text-transform: uppercase; text-decoration: none;
            background: transparent; color: var(--text);
            border: 1px solid var(--border); padding: 7px 16px; border-radius: 4px;
            transition: all 0.15s;
        }
        .btn-ver:hover { border-color: var(--accent); color: var(--accent); }

        /* EMPTY STATE */
        .empty {
            grid-column: 1 / -1;
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; padding: 80px 32px;
            background: var(--surface);
        }
        .empty-icon {
            font-size: 3rem; margin-bottom: 16px;
            animation: pulse 2s infinite;
        }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
        .empty-title {
            font-family: 'DM Mono', monospace; font-size: 11px; letter-spacing: 0.1em;
            text-transform: uppercase; color: var(--muted); margin-bottom: 6px;
        }
        .empty-sub { font-size: 12px; color: var(--border); }

        /* LOADING OVERLAY */
        #loading {
            display: none; position: fixed; inset: 0; z-index: 200;
            background: rgba(15,15,15,0.85); backdrop-filter: blur(4px);
            flex-direction: column; align-items: center; justify-content: center; gap: 20px;
        }
        #loading.show { display: flex; }
        .spinner {
            width: 40px; height: 40px;
            border: 2px solid var(--border);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text {
            font-family: 'DM Mono', monospace; font-size: 12px; color: var(--muted);
            letter-spacing: 0.08em;
        }
        .loading-sub {
            font-family: 'DM Mono', monospace; font-size: 10px; color: var(--border);
        }

        @media (max-width: 640px) {
            nav { padding: 12px 16px; }
            .hero { padding: 24px 16px 20px; }
            .grid-container { padding: 16px; }
        }
    </style>
</head>
<body>
    <div id="loading">
        <div class="spinner"></div>
        <div class="loading-text">Consultando API de MercadoLibre...</div>
        <div class="loading-sub">Esto puede tardar unos segundos</div>
    </div>

    <nav>
        <div class="logo">Inmo<span>LaPlata</span></div>
        <div class="nav-right">
            <span id="status-badge">Sin datos aún</span>
            <button id="btn-refresh" onclick="refresh()">Buscar ahora</button>
        </div>
    </nav>

    <div class="hero">
        <div>
            <div class="hero-title">Casas hasta<br><em>USD 100.000</em></div>
            <div class="hero-sub">La Plata · Buenos Aires · Argentina · Apto banco incluido</div>
        </div>
        <div class="stat-chips">
            <div class="chip" id="chip-total"><b>0</b> propiedades</div>
            <div class="chip" id="chip-portal"><b>MercadoLibre</b> API oficial</div>
            <div class="chip" id="chip-update">Actualiza cada <b>45min</b></div>
        </div>
    </div>

    <div class="grid-container">
        <div class="grid" id="grid">
            <div class="empty">
                <div class="empty-icon">🔍</div>
                <div class="empty-title">Listo para buscar</div>
                <div class="empty-sub">Presioná "Buscar ahora" para consultar la API</div>
            </div>
        </div>
    </div>

    <script>
        async function load() {
            const r = await fetch('/api/propiedades');
            const data = await r.json();
            const grid = document.getElementById('grid');
            const badge = document.getElementById('status-badge');
            const chipTotal = document.getElementById('chip-total');

            chipTotal.innerHTML = `<b>${data.length}</b> propiedades`;

            if (data.length === 0) {
                grid.innerHTML = `<div class="empty">
                    <div class="empty-icon">🔍</div>
                    <div class="empty-title">Sin resultados aún</div>
                    <div class="empty-sub">Presioná "Buscar ahora" para iniciar</div>
                </div>`;
                badge.textContent = 'Sin datos aún';
                badge.className = '';
                return;
            }

            badge.textContent = `${data.length} propiedades`;
            badge.className = 'active';

            grid.innerHTML = data.map(p => `
                <div class="card">
                    <div class="card-top">
                        <span class="portal-tag">${p.portal}</span>
                        <div class="precio">${p.precio}</div>
                    </div>
                    <div class="titulo">${p.titulo}</div>
                    <div class="direccion">📍 ${p.direccion}</div>
                    <div class="attrs">
                        ${p.metros && p.metros !== '—' ? `<span class="attr-tag">📐 ${p.metros} m²</span>` : ''}
                        ${p.ambientes && p.ambientes !== '—' ? `<span class="attr-tag">🛏 ${p.ambientes} amb.</span>` : ''}
                        ${p.apto_banco === '✓ Sí' ? `<span class="attr-tag banco">🏦 Apto banco</span>` : ''}
                    </div>
                    <div class="card-footer">
                        <span class="visto">Visto: ${p.visto_en}</span>
                        <a href="${p.url}" target="_blank" class="btn-ver">Ver ficha →</a>
                    </div>
                </div>
            `).join('');
        }

        async function refresh() {
            const btn = document.getElementById('btn-refresh');
            const loading = document.getElementById('loading');
            btn.disabled = true;
            loading.classList.add('show');

            try {
                await fetch('/api/refresh', { method: 'POST' });
                // Esperar a que termine el escaneo (máx 20s)
                await new Promise(res => setTimeout(res, 8000));
                await load();
            } finally {
                loading.classList.remove('show');
                btn.disabled = false;
            }
        }

        load();
    </script>
</body>
</html>"""

# ── SERVIDOR ──────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(buscar_propiedades, "interval", minutes=CHECK_MINUTES)
    scheduler.start()
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
