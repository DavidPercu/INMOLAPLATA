import os
import json
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
from fastapi.staticfiles import StaticFiles

# ── Config ────────────────────────────────────────────────────────────────────
EMAIL_FROM    = os.getenv("EMAIL_FROM", "")
EMAIL_TO      = os.getenv("EMAIL_TO", "")
EMAIL_PASS    = os.getenv("EMAIL_PASS", "")
DB_PATH       = "propiedades.db"
PRECIO_MAX    = 100000  # USD
CHECK_MINUTES = 60      # cada cuánto busca propiedades nuevas

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS propiedades (
            id        TEXT PRIMARY KEY,
            portal    TEXT,
            titulo    TEXT,
            precio    TEXT,
            direccion TEXT,
            m2        TEXT,
            url       TEXT,
            imagen    TEXT,
            apto      TEXT,
            visto_en  TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS favoritos (
            clave       TEXT,
            prop_id     TEXT,
            nota        TEXT DEFAULT \'\',
            guardado_en TEXT,
            PRIMARY KEY (clave, prop_id)
        )
    """)
    con.commit()
    con.close()

# ── Favoritos DB ──────────────────────────────────────────────────────────────
def get_favoritos(clave: str) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT p.*, f.nota, f.guardado_en
        FROM favoritos f
        LEFT JOIN propiedades p ON p.id = f.prop_id
        WHERE f.clave = ?
        ORDER BY f.guardado_en DESC
    """, (clave,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def toggle_favorito(clave: str, prop_id: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    exists = con.execute(
        "SELECT 1 FROM favoritos WHERE clave=? AND prop_id=?", (clave, prop_id)
    ).fetchone()
    if exists:
        con.execute("DELETE FROM favoritos WHERE clave=? AND prop_id=?", (clave, prop_id))
        saved = False
    else:
        con.execute(
            "INSERT INTO favoritos VALUES (?,?,?,?)",
            (clave, prop_id, "", datetime.now().isoformat())
        )
        saved = True
    con.commit()
    con.close()
    return saved

def actualizar_nota(clave: str, prop_id: str, nota: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE favoritos SET nota=? WHERE clave=? AND prop_id=?",
        (nota, clave, prop_id)
    )
    con.commit()
    con.close()

def guardar_nuevas(props: list[dict]) -> list[dict]:
    """Inserta las que no existen, devuelve sólo las nuevas."""
    con = sqlite3.connect(DB_PATH)
    nuevas = []
    for p in props:
        try:
            con.execute(
                "INSERT INTO propiedades VALUES (?,?,?,?,?,?,?,?,?,?)",
                (p["id"], p["portal"], p["titulo"], p["precio"],
                 p["direccion"], p["m2"], p["url"], p["imagen"],
                 p["apto"], p["visto_en"])
            )
            nuevas.append(p)
        except sqlite3.IntegrityError:
            pass
    con.commit()
    con.close()
    return nuevas

def todas_las_propiedades() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM propiedades ORDER BY visto_en DESC LIMIT 200"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

# ── Scrapers ──────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-AR,es;q=0.9",
    "Referer": "https://www.zonaprop.com.ar/",
}

async def scrape_zonaprop() -> list[dict]:
    """
    Usa el endpoint de listado de Zonaprop (JSON interno).
    Filtra: casas/PH, venta, La Plata, hasta USD 100.000, apto crédito.
    """
    url = (
        "https://www.zonaprop.com.ar/rplis-api/postings"
        "?ambientes=-1&aptoCreditoHipotecario=true"
        "&precio-desde=0&precio-hasta=100000&moneda=USD"
        "&operacion=venta&tipo-de-propiedad=casa,ph"
        "&zona=la-plata&orden=relevancia&pagina=1"
    )
    resultados = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            r = await client.get(url)
            if r.status_code != 200:
                print(f"[zonaprop] status {r.status_code}")
                return []
            data = r.json()
            items = data.get("listPostings", [])
            for item in items:
                prop = item.get("postingMiniData", item)
                precio_data = prop.get("price", {})
                precio = f"USD {precio_data.get('amount', '?'):,}" if precio_data.get("amount") else "Consultar"
                resultados.append({
                    "id": f"zp_{prop.get('postingId', prop.get('id', ''))}",
                    "portal": "Zonaprop",
                    "titulo": prop.get("title", "Sin título"),
                    "precio": precio,
                    "direccion": prop.get("address", prop.get("location", {}).get("address", "La Plata")),
                    "m2": str(prop.get("totalArea", prop.get("roofedArea", "?"))),
                    "url": "https://www.zonaprop.com.ar" + prop.get("url", ""),
                    "imagen": (prop.get("photos") or [{}])[0].get("url720", ""),
                    "apto": "Apto crédito",
                    "visto_en": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[zonaprop] error: {e}")
    return resultados


async def scrape_argenprop() -> list[dict]:
    """
    Usa el endpoint JSON de Argenprop.
    """
    url = (
        "https://www.argenprop.com/api/v2/listings"
        "?tipo=casas-y-ph&operacion=venta"
        "&zona=la-plata&precioMax=100000&moneda=dolares"
        "&aptoBanco=true&pagina=1&orden=relevancia"
    )
    resultados = []
    headers = {**HEADERS, "Referer": "https://www.argenprop.com/"}
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(url)
            if r.status_code != 200:
                print(f"[argenprop] status {r.status_code}")
                return []
            data = r.json()
            items = data.get("listingData", data.get("items", []))
            for item in items:
                precio = item.get("price", {})
                precio_str = f"USD {precio.get('amount', '?'):,}" if precio.get("amount") else "Consultar"
                resultados.append({
                    "id": f"ap_{item.get('id', '')}",
                    "portal": "Argenprop",
                    "titulo": item.get("title", "Sin título"),
                    "precio": precio_str,
                    "direccion": item.get("address", "La Plata"),
                    "m2": str(item.get("totalArea", item.get("roofedArea", "?"))),
                    "url": "https://www.argenprop.com" + item.get("url", ""),
                    "imagen": item.get("mainImage", {}).get("url", ""),
                    "apto": "Apto banco",
                    "visto_en": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[argenprop] error: {e}")
    return resultados


async def scrape_argenprop_html() -> list[dict]:
    """
    Fallback: parsea el HTML de Argenprop si el JSON falla.
    """
    from bs4 import BeautifulSoup
    url = (
        "https://www.argenprop.com/casas-y-ph/venta/la-plata"
        "?precioMax=100000&moneda=dolares&aptoBanco=true"
    )
    resultados = []
    headers = {**HEADERS, "Referer": "https://www.argenprop.com/"}
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".listing__items .card")
            for card in cards:
                link_tag = card.select_one("a[href]")
                href = link_tag["href"] if link_tag else ""
                titulo = card.select_one(".card__title")
                precio_tag = card.select_one(".card__price")
                dir_tag = card.select_one(".card__address")
                img_tag = card.select_one("img[src]")
                prop_id = href.replace("/", "_").strip("_") or str(hash(href))
                resultados.append({
                    "id": f"ap_{prop_id}",
                    "portal": "Argenprop",
                    "titulo": titulo.get_text(strip=True) if titulo else "Sin título",
                    "precio": precio_tag.get_text(strip=True) if precio_tag else "Consultar",
                    "direccion": dir_tag.get_text(strip=True) if dir_tag else "La Plata",
                    "m2": "?",
                    "url": "https://www.argenprop.com" + href,
                    "imagen": img_tag["src"] if img_tag else "",
                    "apto": "Apto banco",
                    "visto_en": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[argenprop-html] error: {e}")
    return resultados


async def scrape_inmobusqueda() -> list[dict]:
    """
    Parsea el HTML de Inmobusqueda para La Plata, casas, hasta USD 100.000, apto crédito.
    """
    from bs4 import BeautifulSoup
    url = (
        "https://www.inmobusqueda.com.ar/venta-de-casas-en-la-plata.html"
        "?precio_hasta=100000&moneda=2&apto_credito=1"
    )
    resultados = []
    headers = {**HEADERS, "Referer": "https://www.inmobusqueda.com.ar/"}
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".aviso") or soup.select(".prop-item") or soup.select("article")
            for card in cards:
                link_tag = card.select_one("a[href]")
                href = link_tag["href"] if link_tag else ""
                if href and not href.startswith("http"):
                    href = "https://www.inmobusqueda.com.ar" + href
                titulo_tag = card.select_one(".aviso-titulo, .prop-title, h2, h3")
                precio_tag = card.select_one(".aviso-precio, .prop-price, .precio")
                dir_tag    = card.select_one(".aviso-dir, .prop-address, .direccion")
                img_tag    = card.select_one("img[src]")
                m2_tag     = card.select_one(".sup, .m2, .superficie")
                prop_id = href.split("/")[-1].replace(".html", "") or str(abs(hash(href)))
                resultados.append({
                    "id": f"im_{prop_id}",
                    "portal": "Inmobusqueda",
                    "titulo": titulo_tag.get_text(strip=True) if titulo_tag else "Sin título",
                    "precio": precio_tag.get_text(strip=True) if precio_tag else "Consultar",
                    "direccion": dir_tag.get_text(strip=True) if dir_tag else "La Plata",
                    "m2": m2_tag.get_text(strip=True) if m2_tag else "?",
                    "url": href,
                    "imagen": img_tag["src"] if img_tag else "",
                    "apto": "Apto crédito",
                    "visto_en": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"[inmobusqueda] error: {e}")
    return resultados


async def scrape_bnamas() -> list[dict]:
    """
    Scraper para mashogaresconbna.com.ar (BNA +Hogares).
    El sitio es una SPA que carga propiedades vía API interna REST.
    Buscamos casas en La Plata hasta USD 100.000.
    """
    BASE = "https://mashogaresconbna.com.ar"
    resultados = []
    headers = {
        **HEADERS,
        "Referer": BASE + "/",
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE,
    }

    # Candidatos de endpoints que suelen usar las SPAs inmobiliarias del BNA
    endpoints = [
        f"{BASE}/api/propiedades?provincia=Buenos+Aires&localidad=La+Plata&tipo=casa&precio_hasta=100000&moneda=USD&pagina=1",
        f"{BASE}/api/listings?city=la-plata&type=casa&maxPrice=100000&currency=USD&page=1",
        f"{BASE}/api/properties?location=la-plata&propertyType=casa&maxPrice=100000&page=1",
        f"{BASE}/api/viviendas?localidad=la-plata&tipo=casa&precioHasta=100000&moneda=usd",
    ]

    try:
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            for url in endpoints:
                try:
                    r = await client.get(url)
                    if r.status_code == 200 and "application/json" in r.headers.get("content-type", ""):
                        data = r.json()
                        # Intentar distintas claves que podría tener la respuesta
                        items = (
                            data.get("propiedades")
                            or data.get("viviendas")
                            or data.get("listings")
                            or data.get("properties")
                            or data.get("items")
                            or data.get("data")
                            or []
                        )
                        if not isinstance(items, list):
                            items = []
                        for item in items:
                            precio_raw = item.get("precio") or item.get("price") or item.get("monto") or {}
                            if isinstance(precio_raw, dict):
                                monto = precio_raw.get("monto") or precio_raw.get("amount") or "?"
                                precio_str = f"USD {int(monto):,}" if str(monto).isdigit() else f"USD {monto}"
                            else:
                                precio_str = f"USD {int(precio_raw):,}" if str(precio_raw).isdigit() else str(precio_raw)

                            href = item.get("url") or item.get("link") or f"/propiedad/{item.get('id', '')}"
                            if href and not href.startswith("http"):
                                href = BASE + href

                            prop_id = str(item.get("id") or abs(hash(href)))
                            resultados.append({
                                "id": f"bna_{prop_id}",
                                "portal": "BNA +Hogares",
                                "titulo": item.get("titulo") or item.get("title") or item.get("descripcion") or "Casa en venta",
                                "precio": precio_str,
                                "direccion": item.get("direccion") or item.get("address") or item.get("ubicacion") or "La Plata",
                                "m2": str(item.get("superficie") or item.get("totalArea") or item.get("m2") or "?"),
                                "url": href,
                                "imagen": item.get("imagen") or item.get("foto") or item.get("mainImage") or item.get("image") or "",
                                "apto": "Apto crédito BNA",
                                "visto_en": datetime.now().isoformat(),
                            })
                        if resultados:
                            print(f"[bnamas] OK con endpoint: {url} — {len(resultados)} props")
                            return resultados
                except Exception as e:
                    print(f"[bnamas] endpoint falló {url}: {e}")
                    continue

            # Si ningún endpoint JSON funcionó, generamos una entrada directa al listado
            # para que el usuario pueda visitarlo manualmente desde la app
            print("[bnamas] no se encontró API, usando enlace directo al listado")
            resultados.append({
                "id": "bna_listado_laplata",
                "portal": "BNA +Hogares",
                "titulo": "Ver todas las propiedades en La Plata — BNA +Hogares",
                "precio": "Hasta USD 100.000",
                "direccion": "La Plata, Buenos Aires",
                "m2": "?",
                "url": f"{BASE}/list",
                "imagen": f"{BASE}/Logo_+Hogares_NaranjaBlanco.svg",
                "apto": "Apto crédito BNA",
                "visto_en": datetime.now().isoformat(),
            })

    except Exception as e:
        print(f"[bnamas] error general: {e}")

    return resultados


async def buscar_todas() -> list[dict]:
    zp, ap, im, bna = await asyncio.gather(
        scrape_zonaprop(),
        scrape_argenprop(),
        scrape_inmobusqueda(),
        scrape_bnamas(),
    )
    if not ap:
        ap = await scrape_argenprop_html()
    return zp + ap + im + bna

# ── Email ─────────────────────────────────────────────────────────────────────
def enviar_alerta(nuevas: list[dict]):
    if not EMAIL_FROM or not nuevas:
        return
    cuerpo_html = "<h2>🏠 Nuevas propiedades en La Plata</h2><ul>"
    for p in nuevas:
        cuerpo_html += (
            f"<li><b>{p['portal']}</b> — {p['titulo']}<br>"
            f"{p['precio']} · {p['direccion']}<br>"
            f"<a href='{p['url']}'>Ver publicación</a></li><br>"
        )
    cuerpo_html += "</ul>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏠 {len(nuevas)} nueva(s) propiedad(es) en La Plata"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(cuerpo_html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"[email] alerta enviada: {len(nuevas)} props nuevas")
    except Exception as e:
        print(f"[email] error: {e}")

# ── Tarea periódica ───────────────────────────────────────────────────────────
async def check_y_alertar():
    print(f"[check] {datetime.now().strftime('%H:%M')} — buscando propiedades...")
    props = await buscar_todas()
    nuevas = guardar_nuevas(props)
    print(f"[check] {len(props)} encontradas, {len(nuevas)} nuevas")
    if nuevas:
        enviar_alerta(nuevas)

# ── App ───────────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await check_y_alertar()
    scheduler.add_job(check_y_alertar, "interval", minutes=CHECK_MINUTES)
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

from fastapi import Request

@app.get("/api/propiedades")
async def api_propiedades():
    return JSONResponse(todas_las_propiedades())

@app.post("/api/refresh")
async def api_refresh():
    props = await buscar_todas()
    nuevas = guardar_nuevas(props)
    return {"total": len(props), "nuevas": len(nuevas)}

@app.get("/api/favoritos/{clave}")
async def api_get_favoritos(clave: str):
    return JSONResponse(get_favoritos(clave))

@app.post("/api/favoritos/{clave}/{prop_id}")
async def api_toggle_favorito(clave: str, prop_id: str):
    saved = toggle_favorito(clave, prop_id)
    return {"guardado": saved}

@app.put("/api/favoritos/{clave}/{prop_id}/nota")
async def api_nota(clave: str, prop_id: str, request: Request):
    body = await request.json()
    actualizar_nota(clave, prop_id, body.get("nota", ""))
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html") as f:
        return f.read()
