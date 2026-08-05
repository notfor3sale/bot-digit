import os
import time
import asyncio
import logging
import yt_dlp
import qrcode
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# 1. LOGS Y ENTORNO
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") 

MAX_DURATION = 300  # 5 minutos en segundos
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # Límite de Telegram Bot API (50 MB)

app = FastAPI()
ptb_app = ApplicationBuilder().token(TOKEN).updater(None).build()

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
    user_text = update.message.text
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

    # --- MODO MULTIMEDIA ---
    elif "http" in user_text:
        status_msg = await update.message.reply_text("🔍 **Analizando enlace...**\n`⏳ Buscando metadatos...`", parse_mode="Markdown")
        es_audio = context.user_data.get('modo_audio', False)
        
        # Opciones base optimizadas para evitar bloqueos de IP
        base_opts = {
            'quiet': True,
            'no_warnings': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'nocheckcertificate': True,
        }

        file_id = f"{user_id}_{int(time.time())}"
        path_template = f"downloads/{file_id}.%(ext)s"

        try:
            # 1. Validar metadatos sin descargar
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(user_text, download=False))
                duracion = info.get('duration', 0)
                titulo = info.get('title', 'Multimedia')
            
            if duracion > MAX_DURATION:
                minutos = duracion // 60
                segundos = duracion % 60
                await status_msg.edit_text(
                    f"⚠️ **Video demasiado largo**\n\nEl límite es de **5:00 minutos**.\nDuración: **{int(minutos)}:{int(segundos):02d}**.",
                    parse_mode="Markdown"
                )
                context.user_data['modo_audio'] = False
                return

            ultima_actualizacion = 0

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
                ydl_opts.update({'format': 'bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/best[height<=480]/best'})

            # 2. Descarga efectiva
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_descarga = await loop.run_in_executor(None, lambda: ydl.extract_info(user_text, download=True))
                
            # Resolver ruta exacta del archivo generado
            path_final = None
            for file in os.listdir("downloads"):
                if file.startswith(file_id):
                    path_final = os.path.join("downloads", file)
                    break

            if not path_final or not os.path.exists(path_final):
                raise FileNotFoundError("El archivo procesado no fue localizado en disco.")

            # Validar tamaño máximo para Telegram API (50 MB)
            if os.path.getsize(path_final) > MAX_FILE_SIZE_BYTES:
                await status_msg.edit_text("❌ **Error:** El archivo descargado excede el límite de 50 MB de Telegram.")
                os.remove(path_final)
                return

            await status_msg.edit_text("⚡ **Enviando archivo a Telegram...**\n`[██████████] 100% Completo`", parse_mode="Markdown")
            
            # 3. Envío del archivo
            with open(path_final, 'rb') as media_file:
                if es_audio:
                    await update.message.reply_audio(audio=media_file, caption=f"🎵 **{titulo}**", parse_mode="Markdown")
                else:
                    await update.message.reply_video(video=media_file, caption=f"📹 **{titulo}**", parse_mode="Markdown")
            
            if os.path.exists(path_final): 
                os.remove(path_final)
            await status_msg.delete()

        except Exception as e:
            logger.error(f"Error procesando enlace: {e}")
            await status_msg.edit_text("❌ **Error:** El enlace es privado, no compatible o YouTube ha bloqueado la petición temporalmente.", parse_mode="Markdown")
        
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

# 6. ENRUTAMIENTO FASTAPI
@app.on_event("startup")
async def startup_event():
    if not os.path.exists('downloads'): 
        os.makedirs('downloads')
    
    # Inicializar y arrancar completamente PTB
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
    return Response(content="<h1>DigitTools Bot Gateway Activo</h1>", media_type="text/html")

@app.on_event("shutdown")
async def shutdown_event():
    await ptb_app.stop()
    await ptb_app.shutdown()
