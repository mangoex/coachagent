from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from typing import Optional

from database.connection import Base, engine, get_db
from database.models import User, ConversationLog
from agent.redis_memory import redis_memory
from agent.gemini_agent import GeminiAgent
from routers import whatsapp, cron
from config.settings import settings

import logging

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize FastAPI App
app = FastAPI(
    title="Google AI Sales Coach Agent API",
    description="Automated Sales Coaching, CRM Synchronization, and Proposal Generator agent.",
    version="1.0.0"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup DB migration helper (auto-creates tables if they don't exist)
@app.on_event("startup")
def startup_event():
    logger.info("Initializing database tables...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to initialize database tables: {str(e)}")

# Include Routers
app.include_router(whatsapp.router)
app.include_router(cron.router)

# Pydantic schemas for user registration
class UserCreate(BaseModel):
    email: EmailStr
    name: str
    phone_number: str  # Format: "52155..." or "+1..."
    google_refresh_token: str
    spreadsheet_id: Optional[str] = None
    template_doc_id: Optional[str] = None

class ChatRequest(BaseModel):
    phone_number: str
    message: str

@app.post("/agent/chat")
def agent_chat(payload: ChatRequest, db: Session = Depends(get_db)):
    """
    Direct endpoint to chat with the agent in real-time.
    Used by the Web UI playground.
    """
    user = db.query(User).filter(User.phone_number == payload.phone_number).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail="No se encontró ningún vendedor registrado con ese número de teléfono."
        )
    
    try:
        refresh_token = user.get_refresh_token()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Error al descifrar los tokens de Google. Re-registra al usuario."
        )

    # Fetch chat history from Redis
    chat_history = redis_memory.get_history(payload.phone_number)
    
    # Initialize the Gemini Cognitive Agent
    agent = GeminiAgent(
        user_refresh_token=refresh_token,
        spreadsheet_id=user.spreadsheet_id,
        template_doc_id=user.template_doc_id
    )

    # Run agent loop
    reply, updated_history = agent.run(chat_history, payload.message)

    # Save to Redis history
    redis_memory.add_message(payload.phone_number, "user", payload.message)
    redis_memory.add_message(payload.phone_number, "agent", reply)

    # Log to PostgreSQL DB
    db.add(ConversationLog(phone_number=payload.phone_number, sender="user", message=payload.message))
    db.add(ConversationLog(phone_number=payload.phone_number, sender="agent", message=reply))
    db.commit()

    return {"reply": reply}

@app.get("/users")
def list_users(db: Session = Depends(get_db)):
    """
    Returns registered users list for the Web UI playground dropdown.
    """
    users = db.query(User).all()
    return [{"phone_number": u.phone_number, "name": u.name, "email": u.email} for u in users]

@app.post("/users", status_code=201)
def register_user(user_in: UserCreate, db: Session = Depends(get_db)):
    """
    Registers a new salesperson/tenant and encrypts their Google OAuth 2.0 refresh token.
    """
    existing_user = db.query(User).filter(
        (User.email == user_in.email) | (User.phone_number == user_in.phone_number)
    ).first()
    
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Ya existe un vendedor registrado con ese correo o teléfono."
        )

    db_user = User(
        email=user_in.email,
        name=user_in.name,
        phone_number=user_in.phone_number,
        spreadsheet_id=user_in.spreadsheet_id,
        template_doc_id=user_in.template_doc_id
    )
    # Encrypt the refresh token before storing
    db_user.set_refresh_token(user_in.google_refresh_token)
    
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    logger.info(f"Registered new user: {db_user.email}")
    return {
        "id": db_user.id,
        "name": db_user.name,
        "email": db_user.email,
        "phone_number": db_user.phone_number,
        "spreadsheet_id": db_user.spreadsheet_id,
        "template_doc_id": db_user.template_doc_id
    }

@app.get("/", response_class=HTMLResponse)
def read_root():
    """
    Returns a beautiful, premium dark-mode SPA Dashboard & Playground for the Sales Coach.
    """
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Google AI Sales Coach Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=Space+Grotesk:wght@400;600&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-gradient: linear-gradient(135deg, #0f0c20 0%, #15102a 50%, #060214 100%);
                --card-bg: rgba(25, 20, 50, 0.45);
                --card-border: rgba(255, 255, 255, 0.08);
                --glow-color: rgba(124, 77, 255, 0.3);
                --primary: #7c4dff;
                --primary-glow: #b388ff;
                --accent: #00e5ff;
                --text-main: #f5f5fa;
                --text-muted: #9a90bd;
            }
            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }
            body {
                font-family: 'Outfit', sans-serif;
                background: var(--bg-gradient);
                color: var(--text-main);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                overflow-x: hidden;
            }
            header {
                padding: 24px 5%;
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--card-border);
                backdrop-filter: blur(12px);
                position: sticky;
                top: 0;
                z-index: 100;
            }
            .logo-container {
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .logo-icon {
                width: 38px;
                height: 38px;
                background: linear-gradient(135deg, var(--primary), var(--accent));
                border-radius: 10px;
                box-shadow: 0 0 20px var(--glow-color);
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: 800;
                font-size: 20px;
                color: #fff;
            }
            .logo-text {
                font-family: 'Space Grotesk', sans-serif;
                font-size: 22px;
                font-weight: 600;
                letter-spacing: 0.5px;
                background: linear-gradient(to right, #ffffff, var(--text-muted));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .status-badge {
                background: rgba(0, 229, 255, 0.15);
                border: 1px solid var(--accent);
                color: var(--accent);
                padding: 6px 14px;
                border-radius: 30px;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.5px;
                box-shadow: 0 0 10px rgba(0, 229, 255, 0.1);
            }
            main {
                flex: 1;
                max-width: 1200px;
                width: 90%;
                margin: 40px auto;
                display: grid;
                grid-template-columns: 1fr 1.3fr;
                gap: 40px;
            }
            @media (max-width: 968px) {
                main {
                    grid-template-columns: 1fr;
                }
            }
            .panel {
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                border-radius: 24px;
                padding: 30px;
                backdrop-filter: blur(20px);
                box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
                display: flex;
                flex-direction: column;
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }
            .panel:hover {
                box-shadow: 0 12px 40px 0 rgba(124, 77, 255, 0.15);
                border-color: rgba(124, 77, 255, 0.2);
            }
            h2 {
                font-family: 'Space Grotesk', sans-serif;
                font-size: 22px;
                margin-bottom: 24px;
                display: flex;
                align-items: center;
                gap: 10px;
                color: #fff;
            }
            .form-group {
                margin-bottom: 20px;
            }
            label {
                display: block;
                font-size: 14px;
                font-weight: 600;
                color: var(--text-muted);
                margin-bottom: 8px;
            }
            input, select {
                width: 100%;
                background: rgba(10, 5, 25, 0.5);
                border: 1px solid var(--card-border);
                border-radius: 12px;
                padding: 14px 16px;
                color: var(--text-main);
                font-size: 15px;
                outline: none;
                transition: border-color 0.3s ease, box-shadow 0.3s ease;
            }
            input:focus, select:focus {
                border-color: var(--primary);
                box-shadow: 0 0 15px rgba(124, 77, 255, 0.2);
            }
            .btn {
                background: linear-gradient(135deg, var(--primary) 0%, #6200ea 100%);
                border: none;
                border-radius: 12px;
                padding: 16px;
                color: #fff;
                font-size: 15px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s ease;
                box-shadow: 0 4px 15px rgba(124, 77, 255, 0.3);
                margin-top: 10px;
            }
            .btn:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(124, 77, 255, 0.5);
                filter: brightness(1.1);
            }
            .btn:active {
                transform: translateY(0);
            }
            /* Chat Box Container */
            .chat-container {
                height: 550px;
                display: flex;
                flex-direction: column;
                background: rgba(10, 5, 25, 0.35);
                border-radius: 20px;
                border: 1px solid var(--card-border);
                overflow: hidden;
            }
            .chat-header {
                padding: 16px 20px;
                background: rgba(25, 20, 50, 0.6);
                border-bottom: 1px solid var(--card-border);
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .avatar {
                width: 40px;
                height: 40px;
                background: linear-gradient(135deg, var(--primary), var(--accent));
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                color: #fff;
                box-shadow: 0 0 10px var(--glow-color);
            }
            .chat-info h3 {
                font-size: 15px;
                color: #fff;
            }
            .chat-info p {
                font-size: 12px;
                color: var(--text-muted);
            }
            .chat-messages {
                flex: 1;
                padding: 20px;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 16px;
            }
            .message {
                max-width: 80%;
                padding: 14px 18px;
                border-radius: 18px;
                font-size: 14.5px;
                line-height: 1.5;
            }
            .message.user {
                align-self: flex-end;
                background: var(--primary);
                color: #fff;
                border-bottom-right-radius: 4px;
                box-shadow: 0 4px 12px rgba(124, 77, 255, 0.25);
            }
            .message.agent {
                align-self: flex-start;
                background: rgba(255, 255, 255, 0.08);
                color: var(--text-main);
                border-bottom-left-radius: 4px;
                border: 1px solid rgba(255, 255, 255, 0.04);
            }
            .message.system {
                align-self: center;
                background: rgba(0, 229, 255, 0.1);
                color: var(--accent);
                border-radius: 10px;
                font-size: 12px;
                padding: 6px 12px;
            }
            .chat-input-area {
                padding: 16px;
                background: rgba(25, 20, 50, 0.4);
                border-top: 1px solid var(--card-border);
                display: flex;
                gap: 12px;
            }
            .chat-input-area input {
                flex: 1;
            }
            .btn-send {
                background: var(--accent);
                color: #000;
                border: none;
                border-radius: 12px;
                width: 50px;
                height: 50px;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                box-shadow: 0 4px 15px rgba(0, 229, 255, 0.3);
                transition: all 0.3s ease;
                font-weight: bold;
                font-size: 18px;
            }
            .btn-send:hover {
                transform: scale(1.05);
                box-shadow: 0 6px 20px rgba(0, 229, 255, 0.5);
            }
            .alert {
                padding: 12px 16px;
                border-radius: 10px;
                font-size: 14px;
                margin-top: 15px;
                display: none;
            }
            .alert.success {
                background: rgba(76, 175, 80, 0.15);
                color: #81c784;
                border: 1px solid #4caf50;
            }
            .alert.error {
                background: rgba(244, 67, 54, 0.15);
                color: #e57373;
                border: 1px solid #f44336;
            }
        </style>
    </head>
    <body>
        <header>
            <div class="logo-container">
                <div class="logo-icon">AI</div>
                <div class="logo-text">Google AI Sales Coach</div>
            </div>
            <div class="status-badge">Sistema Activo</div>
        </header>

        <main>
            <!-- Form Panel -->
            <div class="panel">
                <h2>Registrar Vendedor (Multitenant)</h2>
                <form id="registerForm">
                    <div class="form-group">
                        <label for="name">Nombre Completo</label>
                        <input type="text" id="name" required placeholder="Ej. Juan Pérez">
                    </div>
                    <div class="form-group">
                        <label for="email">Correo Electrónico</label>
                        <input type="email" id="email" required placeholder="Ej. juan@empresa.com">
                    </div>
                    <div class="form-group">
                        <label for="phone">Número de Teléfono (WhatsApp)</label>
                        <input type="text" id="phone" required placeholder="Ej. 5215555555555">
                    </div>
                    <div class="form-group">
                        <label for="token">Google OAuth 2.0 Refresh Token</label>
                        <input type="password" id="token" required placeholder="Pega el refresh token encriptado">
                    </div>
                    <div class="form-group">
                        <label for="sheet">ID del Google Sheet CRM (Opcional)</label>
                        <input type="text" id="sheet" placeholder="Spreadsheet ID de Google Drive">
                    </div>
                    <div class="form-group">
                        <label for="doc">ID de Plantilla Google Docs (Opcional)</label>
                        <input type="text" id="doc" placeholder="Document ID de la cotización">
                    </div>
                    <button type="submit" class="btn">Registrar y Guardar Seguro</button>
                </form>
                <div id="formAlert" class="alert"></div>
            </div>

            <!-- Chat Playground Panel -->
            <div class="panel">
                <h2>Playground de Chat Cognitivo</h2>
                <div class="form-group">
                    <label for="selectUser">Vendedor Seleccionado</label>
                    <select id="selectUser" onchange="resetChat()">
                        <option value="">-- Selecciona un Vendedor --</option>
                    </select>
                </div>
                
                <div class="chat-container">
                    <div class="chat-header">
                        <div class="avatar">🤖</div>
                        <div class="chat-info">
                            <h3 id="chatTitle">Sales Coach</h3>
                            <p id="chatSub">Selecciona un vendedor arriba para comenzar</p>
                        </div>
                    </div>
                    <div class="chat-messages" id="chatMessages">
                        <div class="message system">Chat Playground. Envía prompts al Agente Cognitivo.</div>
                    </div>
                    <div class="chat-input-area">
                        <input type="text" id="chatInput" disabled placeholder="Escribe un mensaje..." onkeypress="handleKeyPress(event)">
                        <button class="btn-send" id="btnSend" disabled onclick="sendMessage()">➔</button>
                    </div>
                </div>
            </div>
        </main>

        <script>
            // Fetch users list on load to populate dropdown
            async function loadUsers() {
                try {
                    const response = await fetch('/users');
                    const users = await response.json();
                    const select = document.getElementById('selectUser');
                    
                    // Clear all but first option
                    select.innerHTML = '<option value="">-- Selecciona un Vendedor --</option>';
                    
                    users.forEach(user => {
                        const opt = document.createElement('option');
                        opt.value = user.phone_number;
                        opt.textContent = `${user.name} (${user.phone_number})`;
                        select.appendChild(opt);
                    });
                } catch (err) {
                    console.error("Error loading users:", err);
                }
            }

            // Reset chat messages when user changes
            function resetChat() {
                const select = document.getElementById('selectUser');
                const phone = select.value;
                const input = document.getElementById('chatInput');
                const btn = document.getElementById('btnSend');
                const messages = document.getElementById('chatMessages');
                const sub = document.getElementById('chatSub');

                messages.innerHTML = '<div class="message system">Chat Playground. Envía prompts al Agente Cognitivo.</div>';

                if (phone) {
                    input.disabled = false;
                    btn.disabled = false;
                    sub.textContent = `Vendedor Activo: ${phone}`;
                    appendMessage("agent", "¡Hola! Soy tu Google AI Sales Coach. ¿En qué te puedo ayudar hoy? Puedo ver tu agenda, consultar clientes del CRM o generarte una cotización.");
                } else {
                    input.disabled = true;
                    btn.disabled = true;
                    sub.textContent = "Selecciona un vendedor arriba para comenzar";
                }
            }

            // Handle user registration
            document.getElementById('registerForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const alertDiv = document.getElementById('formAlert');
                alertDiv.style.display = 'none';

                const payload = {
                    email: document.getElementById('email').value,
                    name: document.getElementById('name').value,
                    phone_number: document.getElementById('phone').value,
                    google_refresh_token: document.getElementById('token').value,
                    spreadsheet_id: document.getElementById('sheet').value || null,
                    template_doc_id: document.getElementById('doc').value || null
                };

                try {
                    const response = await fetch('/users', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });

                    if (response.ok) {
                        alertDiv.className = 'alert success';
                        alertDiv.textContent = '¡Vendedor registrado exitosamente!';
                        alertDiv.style.display = 'block';
                        document.getElementById('registerForm').reset();
                        loadUsers(); // Refresh dropdown
                    } else {
                        const errData = await response.json();
                        alertDiv.className = 'alert error';
                        alertDiv.textContent = `Error: ${errData.detail || 'No se pudo guardar.'}`;
                        alertDiv.style.display = 'block';
                    }
                } catch (err) {
                    alertDiv.className = 'alert error';
                    alertDiv.textContent = 'Error de conexión con el servidor.';
                    alertDiv.style.display = 'block';
                }
            });

            // Send message to agent
            async function sendMessage() {
                const input = document.getElementById('chatInput');
                const text = input.value.trim();
                const phone = document.getElementById('selectUser').value;
                if (!text || !phone) return;

                input.value = '';
                appendMessage("user", text);

                // Append loading dots
                const loadingId = appendMessage("agent", "Pensando...");

                try {
                    const response = await fetch('/agent/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ phone_number: phone, message: text })
                    });

                    const data = await response.json();
                    document.getElementById(loadingId).remove(); // Remove loading

                    if (response.ok) {
                        appendMessage("agent", data.reply);
                    } else {
                        appendMessage("agent", `⚠️ Error: ${data.detail || 'No se pudo procesar la respuesta.'}`);
                    }
                } catch (err) {
                    document.getElementById(loadingId).remove();
                    appendMessage("agent", "⚠️ Error: No se pudo conectar con el servidor.");
                }
            }

            // Append a message bubble to chat window
            function appendMessage(sender, text) {
                const messages = document.getElementById('chatMessages');
                const msg = document.createElement('div');
                const id = 'msg_' + Math.random().toString(36).substr(2, 9);
                msg.id = id;
                msg.className = `message ${sender}`;
                msg.innerHTML = text.replace(/\\n/g, '<br>');
                messages.appendChild(msg);
                messages.scrollTop = messages.scrollHeight;
                return id;
            }

            function handleKeyPress(e) {
                if (e.key === 'Enter') {
                    sendMessage();
                }
            }

            // Initial load
            loadUsers();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
