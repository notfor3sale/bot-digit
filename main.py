import os
import time
import asyncio
import logging
import yt_dlp
import qrcode
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# 1. LOGS Y ENTORNO SECRETO
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") 

MAX_DURATION = 300  # 5 minutos en segundos
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # Límite de la API de Telegram (50 MB)

# Inicializar FastAPI y Telegram App
app = FastAPI()
ptb_app = ApplicationBuilder().token(TOKEN).updater(None).build()

# 2. TECLADO Y MENÚS
def menu_principal():
    keyboard = [
        [
            InlineKeyboardButton("📹 Descargar Video", callback_data='btn_video'),
            InlineKeyboardButton("🎵 Extraer MP3", callback_data='btn_audio')
        ],
        [
            InlineKeyboardButton("🖼️ Generar Código QR", callback_data='btn_qr'),
            InlineKeyboardButton("🌐 Mi Web", url="https://frontdigit.net/")
        ],
        [InlineKeyboardButton("📊 Status del Sistema", callback_data='m_status')]
    ]
    return InlineKeyboardMarkup(keyboard)

# 3. UTILIDADES
def generar_qr(texto, nombre_archivo):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(texto)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(nombre_archivo)

def construir_barra(porcentaje):
    bloques_totales = 10
    bloques_llenos = int(round((porcentaje / 100) * bloques_totales))
    bloques_vacios = bloques_totales - bloques_llenos
    barra = "█" * bloques_llenos + "░" * bloques_vacios
    return f"[{barra}] {porcentaje}%"

# 4. LÓGICA PRINCIPAL DEL BOT
async def handle_everything(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    user_id = update.effective_user.id
    loop = asyncio.get_running_loop()
    
    # --- MODO QR ---
    if context.user_data.get('modo_qr', False):
        status_msg = await update.message.reply_text("✨ **Generando tu código QR...**", parse_mode="Markdown")
        path = f"downloads/qr_{user_id}_{int(time.time())}.png"
        try:
            await loop.run_in_executor(None, generar_qr, user_text, path)
            with open(path, 'rb') as qr_file:
                await update.message.reply_photo(
                    photo=qr_file,
                    caption=f"✅ **QR Generado con éxito**\nContenido: `{user_text}`",
                    parse_mode="Markdown"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Error al crear el QR: {e}")
        finally:
            if os.path.exists(path): 
                os.remove(path)
            context.user_data['modo_qr'] = False
            await status_msg.delete()

    # --- MODO MULTIMEDIA (Soporte Universal) ---
    elif "http://" in user_text.lower() or "https://" in user_text.lower():
        status_msg = await update.message.reply_text("🔍 **Analizando enlace...**\n`⏳ Buscando metadatos...`", parse_mode="Markdown")
        es_audio = context.user_data.get('modo_audio', False)
        
        # Opciones base universales para cualquier sitio web
        base_opts = {
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'geo_bypass': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
            }
        }

        # Aplicar el bypass de clientes SOLO si es un enlace de YouTube
        url_lower = user_text.lower()
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            base_opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['android', 'ios', 'web_creator', 'mweb']
                }
            }

        file_id = f"{user_id}_{int(time.time())}"
        path_template = f"downloads/{file_id}.%(ext)s"

        try:
            # 1. Extracción de Metadatos
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(user_text, download=False))
                
                # Manejo de listas de reproducción (toma el primer elemento)
                if 'entries' in info and info['entries']:
                    info = info['entries'][0]

                duracion = info.get('duration', 0)
                titulo = info.get('title', 'Video')
            
            # Validar límite de duración de 5 minutos (si la plataforma la expone)
            if duracion and duracion > MAX_DURATION:
                minutos = duracion // 60
                segundos = duracion % 60
                await status_msg.edit_text(
                    f"⚠️ **Video demasiado largo**\n\nEl límite es de **5:00 minutos**.\nDuración del enlace: **{int(minutos)}:{int(segundos):02d}**.",
                    parse_mode="Markdown"
                )
                context.user_data['modo_audio'] = False
                return

            ultima_actualizacion = 0

            # Hook para la barra de progreso en vivo
            def progreso_hook(d):
                nonlocal ultima_actualizacion
                if d['status'] == 'downloading':
                    ahora = time.time()
                    if ahora - ultima_actualizacion > 2.0:
                        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                        descargado = d.get('downloaded_bytes', 0)
                        
                        if total > 0:
                            porcentaje = int((descargado / total) * 100)
                            barra = construir_barra(porcentaje)
                        else:
                            barra = "[░░░░░░░░░░] Descargando..."

                        texto = f"📥 **Descargando:** `{titulo[:30]}...`\n\n`{barra}`"
                        asyncio.run_coroutine_threadsafe(
                            status_msg.edit_text(texto, parse_mode="Markdown"), loop
                        )
                        ultima_actualizacion = ahora

            ydl_opts = {
                **base_opts,
                'outtmpl': path_template,
                'progress_hooks': [progreso_hook],
            }

            # Selección de formatos inteligente
            if es_audio:
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                })
            else:
                ydl_opts.update({
                    'format': 'best[ext=mp4]/bestvideo+bestaudio/best',
                })

            # 2. Descargar archivo en disco
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await loop.run_in_executor(None, lambda: ydl.extract_info(user_text, download=True))
                
            # Buscar el archivo descargado dinámicamente según la extensión asignada por yt-dlp
            path_final = None
            for file in os.listdir("downloads"):
                if file.startswith(file_id):
                    path_final = os.path.join("downloads", file)
                    break

            if not path_final or not os.path.exists(path_final):
                raise FileNotFoundError("El archivo descargado no se pudo encontrar en el almacenamiento del servidor.")

            # Validar límite de peso de Telegram (50 MB)
            if os.path.getsize(path_final) > MAX_FILE_SIZE_BYTES:
                await status_msg.edit_text("❌ **Error:** El archivo descargado supera los 50 MB (límite de subida de Telegram Bot API).")
                os.remove(path_final)
                return

            await status_msg.edit_text("⚡ **Enviando archivo a Telegram...**\n`[██████████] 100% Completo`", parse_mode="Markdown")
            
            # 3. Envío al usuario
            with open(path_final, 'rb') as media_file:
                if es_audio:
                    await update.message.reply_audio(audio=media_file, caption=f"🎵 **{titulo}**\n\n_MP3 extraído con éxito._", parse_mode="Markdown")
                else:
                    await update.message.reply_video(video=media_file, caption=f"📹 **{titulo}**\n\n_Video procesado con éxito._", parse_mode="Markdown")
            
            if os.path.exists(path_final): 
                os.remove(path_final)
            await status_msg.delete()

        except Exception as e:
            logger.error(f"Error procesando enlace: {e}")
            detalles_error = str(e).split('\n')[0][:150]
            await status_msg.edit_text(
                f"❌ **Error al procesar:**\n`{detalles_error}`\n\nVerifica que el enlace sea público y válido.", 
                parse_mode="Markdown"
            )
        
        context.user_data['modo_audio'] = False
    else:
        await update.message.reply_text("👋 Selecciona una opción del menú para empezar.", reply_markup=menu_principal())

# 5. MANEJADOR DE BOTONES
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()

    try:
        if query.data == 'btn_qr':
            context.user_data['modo_qr'] = True
            await query.edit_message_text("🖼️ **Modo QR Activo.**\nEnvíame el enlace o texto que quieres convertir en código QR.", parse_mode="Markdown")
        elif query.data == 'btn_audio':
            context.user_data['modo_audio'] = True
            await query.edit_message_text("🎵 **Modo MP3 Activo (Máx. 5 min).**\nEnvíame el enlace del video para extraer el audio.", parse_mode="Markdown")
        elif query.data == 'btn_video':
            await query.edit_message_text("📹 **Modo Video Activo (Máx. 5 min).**\nEnvíame el enlace para descargar el video.", parse_mode="Markdown")
        elif query.data == 'm_status':
            hora = time.strftime('%H:%M:%S')
            await query.edit_message_text(
                f"📊 **Estado del Servidor:** ONLINE ✅\n🕒 **Hora local:** {hora}\n🚀 **Todo funcionando.**", 
                reply_markup=menu_principal(),
                parse_mode="Markdown"
            )
    except Exception as e:
        if "Message is not modified" not in str(e): 
            logger.error(f"Error en botones: {e}")

# Registrar manejadores
ptb_app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🌟 Bienvenido a DigitTools Bot", reply_markup=menu_principal())))
ptb_app.add_handler(CallbackQueryHandler(callback_handler))
ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_everything))

# 6. ENRUTAMIENTO FASTAPI (WEBHOOK ENTRYPOINTS)
@app.on_event("startup")
async def startup_event():
    if not os.path.exists('downloads'): 
        os.makedirs('downloads')
    
    await ptb_app.initialize()
    await ptb_app.start()
    
    if WEBHOOK_URL:
        target = f"{WEBHOOK_URL}/telegram-webhook"
        logger.info(f"Seteando Webhook de Telegram en: {target}")
        await ptb_app.bot.set_webhook(url=target)
    else:
        logger.warning("No se detectó RENDER_EXTERNAL_URL.")

@app.post("/telegram-webhook")
async def recibir_updates(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=200)

@app.get("/")
async def root():
    return Response(content="""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>DigitTools Bot Gateway</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f4f7f6; margin: 0; padding: 0; display: flex; justify-content: center; align-items: center; height: 100vh; }
                .api-container { background-color: #ffffff; max-width: 450px; width: 90%; border-radius: 12px; box-shadow: 0 4px 15px rgba(0, 0, 0, 0.05); padding: 35px; text-align: center; box-sizing: border-box; border-top: 5px solid #321fdb; }
                .icon-container { width: 60px; height: 60px; background-color: #e1e8ff; border-radius: 50%; display: flex; justify-content: center; align-items: center; margin: 0 auto 20px auto; }
                .icon-container svg { width: 30px; height: 30px; fill: #321fdb; }
                h1 { color: #333333; font-size: 22px; font-weight: 600; margin: 0 0 10px 0; }
                p { color: #666666; font-size: 14px; line-height: 1.5; margin: 0 0 25px 0; }
                .status-badge { display: inline-flex; align-items: center; gap: 8px; background-color: #e6f7ed; color: #1e7e34; padding: 8px 18px; border-radius: 20px; font-size: 13px; font-weight: 600; border: 1px solid #c3e6cb; }
                .status-dot { width: 8px; height: 8px; background-color: #28a745; border-radius: 50%; animation: pulse 2s infinite; }
                .footer-text { margin-top: 25px; font-size: 11px; color: #999999; text-transform: uppercase; letter-spacing: 0.5px; }
                .dev-credits { margin-top: 8px; font-size: 12px; color: #777777; }
                .dev-credits a { color: #321fdb; text-decoration: none; font-weight: 500; }
                .dev-credits a:hover { text-decoration: underline; }
                @keyframes pulse {
                    0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(40, 167, 69, 0.7); }
                    70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(40, 167, 69, 0); }
                    100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(40, 167, 69, 0); }
                }
            </style>
        </head>
        <body>
        <div class="api-container">
            <div class="icon-container">
                <svg viewBox="0 0 24 24"><path d="M20 13c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v4c0 1.1.9 2 2 2h16zm-11-5c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm3 0c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm8 11c1.1 0 2-.9 2-2v-4c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v4c0 1.1.9 2 2 2h16zm-11-5c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm3 0c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1z"/></svg>
            </div>
            <h1>DigitTools Bot</h1>
            <p>Servidor webhook optimizado y enlazado de manera segura con la API central de Telegram.</p>
            <div class="status-badge">
                <div class="status-dot"></div>
                <span>Servicios Activos</span>
            </div>
            <div class="footer-text">DigitTools © 2026</div>
            <div class="dev-credits">Desarrollado por <a href="https://frontdigit.net" target="_blank">frontdigit.net</a></div>
        </div>
        </body>
        </html>
    """, media_type="text/html")

@app.on_event("shutdown")
async def shutdown_event():
    await ptb_app.stop()
    await ptb_app.shutdown()
