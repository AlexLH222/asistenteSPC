import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from typing import Dict
import firebase_admin
from firebase_admin import credentials, firestore
import re
import logging

# Configuración inicial
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración de Gemini AI
try:
    genai.configure(api_key=os.getenv("API_KEY"))
    modelo = genai.GenerativeModel("gemini-1.5-flash")
except Exception as e:
    logger.error(f"Error configurando Gemini AI: {e}")
    raise

# Inicializar Firebase
try:
    if not firebase_admin._apps:
        firebase_config = os.getenv("FIREBASE_CONFIG")
        if firebase_config:
            cred = credentials.Certificate(json.loads(firebase_config))
        elif os.path.exists("clave_firebase.json"):
            cred = credentials.Certificate("clave_firebase.json")
        else:
            raise ValueError("No se encontró configuración para Firebase")
        
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    logger.error(f"Error inicializando Firebase: {e}")
    raise

app = FastAPI(
    title="API Coprodelito",
    description="Asistente emocional para estudiantes",
    version="1.0",
    docs_url="/docs",
    redoc_url=None
)

# Configuración CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Variables globales (para producción considera usar Redis)
historial_conversacion = []
emociones_detectadas = set()
situaciones_emocionales = []
correo_alumno = None
documento_emocion_id = None
primer_mensaje = None

# Modelos Pydantic
class ChatRequest(BaseModel):
    message: str

class UserRequest(BaseModel):
    email: str
    password: str

# Endpoint raíz mejorado
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")

# Endpoints de autenticación
@app.post("/register", response_model=Dict)
async def register(user: UserRequest):
    correo = user.email.lower().strip()
    password = user.password.strip()

    if not re.match(r'^[a-z]+\.[a-z]+@spc\.edu\.pe$', correo):
        raise HTTPException(
            status_code=400,
            detail="El correo debe tener el formato nombre.apellido@spc.edu.pe"
        )

    if len(password) != 8:
        raise HTTPException(
            status_code=400,
            detail="La contraseña debe tener 8 caracteres."
        )

    try:
        usuarios_ref = db.collection("correosEstudiantes")
        coincidencias = usuarios_ref.where("correoEstudiante", "==", correo).limit(1).get()
        
        if coincidencias:
            raise HTTPException(
                status_code=400,
                detail="El correo ya está registrado."
            )

        await usuarios_ref.add({
            "correoEstudiante": correo,
            "pswEstudiante": password
        })
        return {"success": True, "user_id": correo}
    except Exception as e:
        logger.error(f"Error en registro: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error en el servidor: {str(e)}"
        )

@app.post("/login", response_model=Dict)
async def login(user: UserRequest):
    correo = user.email.lower().strip()
    password = user.password.strip()

    try:
        usuarios_ref = db.collection("correosEstudiantes")
        coincidencias = usuarios_ref.where("correoEstudiante", "==", correo) \
                                  .where("pswEstudiante", "==", password) \
                                  .limit(1).get()
        
        if not coincidencias:
            raise HTTPException(
                status_code=401,
                detail="Credenciales incorrectas"
            )
        return {"success": True, "user_id": correo}
    except Exception as e:
        logger.error(f"Error en login: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error en el servidor: {str(e)}"
        )

@app.post("/welcome", response_model=Dict)
async def mensaje_bienvenida(user: UserRequest):
    global historial_conversacion, primer_mensaje, emociones_detectadas, correo_alumno, documento_emocion_id, situaciones_emocionales
    
    try:
        nombre = user.email.split('@')[0].replace('.', ' ')
        nombre_cap = ' '.join([p.capitalize() for p in nombre.split()])
        mensaje = f"¡Hola {nombre_cap}! 👋 Soy Coprodelito, tu asistente emocional. ¿Cómo te sientes hoy?"
        
        # Reiniciar estado de conversación
        historial_conversacion = [{"role": "assistant", "parts": [mensaje]}]
        emociones_detectadas = set()
        situaciones_emocionales = []
        correo_alumno = user.email.lower().strip()
        documento_emocion_id = None
        primer_mensaje = None
        
        return {"response": mensaje}
    except Exception as e:
        logger.error(f"Error en bienvenida: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error al generar mensaje de bienvenida"
        )

# Funciones auxiliares
def es_agradecimiento(texto: str) -> bool:
    palabras_clave = ["gracias", "muchas gracias", "agradecido", "agradecida"]
    return any(palabra in texto.lower() for palabra in palabras_clave)

def necesita_recomendaciones(texto: str) -> bool:
    palabras_clave = ["consejos", "tips", "recomendación", "qué hago", "no sé", "ayúdame"]
    return any(p in texto.lower() for p in palabras_clave)

def es_cambio_tema() -> bool:
    if len(historial_conversacion) < 2:
        return True
        
    ultimos_mensajes = [m['parts'][0].lower() for m in historial_conversacion[-3:] if m['role'] == 'user']
    conectores = ["y", "además", "también", "pero", "aunque", "luego"]
    return not any(conector in ' '.join(ultimos_mensajes) for conector in conectores)

async def generar_respuesta_emocional(mensaje_usuario: str) -> str:
    global primer_mensaje, documento_emocion_id

    try:
        if es_agradecimiento(mensaje_usuario):
            return "¡De nada! 😊 Aquí estaré cuando me necesites."

        historial_conversacion.append({"role": "user", "parts": [mensaje_usuario]})

        if primer_mensaje is None:
            primer_mensaje = mensaje_usuario

        contexto = "\n".join([f"{m['role']}: {m['parts'][0]}" for m in historial_conversacion[-5:]])
        
        prompt = f"""
Eres Coprodelito, un asistente emocional para jóvenes. Contexto previo:
{contexto}

Nuevo mensaje: "{mensaje_usuario}"

Responde de forma empática y natural, identificando emociones cuando sea nuevo tema.
"""
        respuesta = await modelo.generate_content_async(prompt)
        texto = respuesta.text.strip()

        # Procesamiento de emociones
        emocion_detectada = ""
        if es_cambio_tema() and not texto.lower().startswith("emoción detectada"):
            emocion_respuesta = await modelo.generate_content_async(
                f"Identifica la emoción principal en: '{mensaje_usuario}'. Responde solo con una palabra."
            )
            emocion_detectada = emocion_respuesta.text.strip()
            texto = f"Emoción detectada: {emocion_detectada} 😊\n{texto}"

        if necesita_recomendaciones(mensaje_usuario) and "🔹" not in texto:
            lineas = [line.strip() for line in texto.split('\n') if line.strip()]
            texto = "\n".join([f"🔹 {l}" for l in lineas[:3]])

        await guardar_emocion_firestore(emocion_detectada, mensaje_usuario)
        
        historial_conversacion.append({"role": "assistant", "parts": [texto]})
        return texto

    except Exception as e:
        logger.error(f"Error generando respuesta emocional: {e}")
        return "¡Vaya! Algo no ha ido bien. ¿Podrías intentarlo de nuevo?"

async def guardar_emocion_firestore(emocion: str, mensaje: str):
    if not emocion or not correo_alumno:
        return

    try:
        emocion = emocion.strip().capitalize()
        if emocion.lower() not in {e.lower() for e in emociones_detectadas}:
            emociones_detectadas.add(emocion)
            situaciones_emocionales.append(mensaje)

            data = {
                "alumno": correo_alumno,
                "emociones": list(emociones_detectadas),
                "situacion": situaciones_emocionales,
                "fechaHora": firestore.SERVER_TIMESTAMP
            }

            if documento_emocion_id:
                await db.collection("emocionesDetectadas").document(documento_emocion_id).update(data)
            else:
                doc_ref = await db.collection("emocionesDetectadas").add(data)
                documento_emocion_id = doc_ref.id
    except Exception as e:
        logger.error(f"Error guardando emoción en Firestore: {e}")

@app.post("/chat", response_model=Dict)
async def chat_endpoint(chat: ChatRequest):
    try:
        respuesta = await generar_respuesta_emocional(chat.message)
        return {"response": respuesta}
    except Exception as e:
        logger.error(f"Error en endpoint /chat: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error al procesar el mensaje"
        )

# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        timeout_keep_alive=30
    )