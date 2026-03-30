# 🏠 Buscador de Propiedades — La Plata

Scraper automático de Zonaprop, Argenprop, Inmobusqueda y BNA +Hogares.  
Filtra: **casas/PH · hasta USD 100.000 · apto banco/crédito · La Plata casco urbano**

---

## Cómo deployar (paso a paso)

### 1. Subir el código a GitHub

1. Andá a [github.com](https://github.com) → **New repository**
2. Nombre: `inmo-laplata` → Create repository
3. En tu computadora, abrí una terminal en esta carpeta y ejecutá:

```bash
git init
git add .
git commit -m "primer commit"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/inmo-laplata.git
git push -u origin main
```

*(reemplazá TU_USUARIO con tu usuario de GitHub)*

---

### 2. Crear cuenta en Render.com

1. Andá a [render.com](https://render.com) → Sign up con tu cuenta de GitHub
2. En el dashboard → **New +** → **Web Service**
3. Conectá tu repositorio `inmo-laplata`
4. Render detecta automáticamente el `render.yaml` con toda la configuración
5. Click en **Deploy Web Service**

---

### 3. Configurar las alertas por email (opcional pero recomendado)

Las alertas se envían desde Gmail. Necesitás crear una **contraseña de aplicación**:

1. Andá a tu cuenta Google → Seguridad → Verificación en 2 pasos (activala si no está)
2. Buscá "Contraseñas de aplicaciones" → creá una nueva para "Correo"
3. Copiá la clave de 16 caracteres que te da Google

En Render, andá a tu servicio → **Environment** y agregá estas variables:

| Variable     | Valor                              |
|--------------|------------------------------------|
| EMAIL_FROM   | tucorreo@gmail.com                 |
| EMAIL_TO     | tucorreo@gmail.com  (o cualquiera) |
| EMAIL_PASS   | la clave de aplicación de 16 chars |

---

### 4. Usar desde el celular

Una vez deployado, Render te da una URL del tipo:
```
https://inmo-laplata.onrender.com
```

Entrá a esa URL desde el celular. Podés agregarla a la pantalla de inicio:
- **iPhone**: Safari → compartir → "Agregar a pantalla de inicio"
- **Android**: Chrome → menú → "Agregar a pantalla de inicio"

---

## Cómo funciona

- Al iniciar, busca propiedades en Zonaprop y Argenprop
- Cada **60 minutos** vuelve a buscar automáticamente
- Si encuentra propiedades nuevas → te manda un email con los detalles
- El botón **Actualizar** en la app fuerza una búsqueda inmediata

## Importante — Plan gratuito de Render

El plan free "duerme" el servidor tras 15 minutos sin visitas.
Al entrar a la app puede tardar ~30 segundos en despertar la primera vez.
Para que no duerma, podés usar [cron-job.org](https://cron-job.org) gratis
para hacerle un ping cada 14 minutos a tu URL.

---

## Estructura del proyecto

```
inmo-laplata/
├── main.py              ← servidor FastAPI + scrapers + alertas
├── templates/
│   └── index.html       ← app mobile
├── requirements.txt
├── render.yaml          ← configuración de deploy
└── README.md
```
