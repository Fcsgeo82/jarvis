#!/usr/bin/env python3
"""
Jarvis — Desktop clap listener + voice assistant agent.

Gestos de palmas:
  Palma dupla  → Boot sequence: Spotify + Claude (Chrome) + Binance (Chrome)
                 + Antigravity IDE + Saudação ElevenLabs dinâmica via DeepSeek
  Palma tripla → Ativa modo escuta: grava ~5s, transcreve com Whisper,
                 envia ao DeepSeek (com ferramentas), responde em voz via ElevenLabs.

Wake word (opcional):
  "hey jarvis" → mesmo comportamento da palma tripla (requer openwakeword).

Tray icon (opcional):
  Ícone na bandeja do sistema mostrando status em tempo real.

Run:
  python -m pip install -r requirements.txt
  python jarvis.py

Variáveis de ambiente (.env):
  ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID  — TTS
  DEEPSEEK_API_KEY                          — LLM e welcome dinâmico
  ANTIGRAVITY_EXE                           — sobrescreve path detectado
  CLAUDE_CODE_URL, BINANCE_BTC_URL          — URLs do Chrome
  WHISPER_MODEL                             — tiny/base/small/medium (padrão: base)
  DEEPSEEK_MODEL                            — padrão: deepseek-chat
  OPENWEATHER_API_KEY                       — opcional (fallback wttr.in é gratuito)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import wave
import webbrowser
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd

# ---------------------------------------------------------------------------
# Tuning knobs — clap detection
# ---------------------------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

SPIKE_RATIO = 7.0
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.35
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.012
QUIET_GATE_MULT = 2.2

# Janela de tempo para detectar palma tripla
TRIPLE_CLAP_WINDOW_S = 1.0

# ---------------------------------------------------------------------------
# URLs e apps
# ---------------------------------------------------------------------------
SONG_URI = "https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz?si=2900c75c2e2d4b82"

FOCUS_EXISTING_ANTIGRAVITY_ON_DOUBLE_CLAP = True
OPEN_NEW_ANTIGRAVITY_ON_DOUBLE_CLAP = False
ANTIGRAVITY_OPEN_FULLSCREEN = True
ANTIGRAVITY_EXE = (
    r"C:\Users\02626810\AppData\Local\Programs\Antigravity IDE\Antigravity IDE.exe"
)

OPEN_CLAUDE_CODE_IN_CHROME = True
OPEN_BINANCE_BTC_IN_CHROME = True
OPEN_CHROME_FULLSCREEN = True
CHROME_SEPARATE_SITE_PROFILES = False
CLAUDE_CHROME_MONITOR = 1
BINANCE_CHROME_MONITOR = 3

# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------
JARVIS_WELCOME_ENABLED = True
# Frase estática usada como fallback se DeepSeek não estiver disponível
JARVIS_WELCOME_FALLBACK_PHRASE = (
    "Welcome home sir. All systems are ready and waiting your command."
)
JARVIS_AFTER_SONG_DELAY_S = 1.0
JARVIS_WELCOME_CACHE_ENABLED = True

# ---------------------------------------------------------------------------
# DeepSeek LLM
# ---------------------------------------------------------------------------
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"            # DeepSeek-V3; "deepseek-reasoner" para R1

DEEPSEEK_SYSTEM_PROMPT = (
    "Você é Jarvis, o assistente pessoal de IA do usuário. "
    "Responda de forma concisa e direta, em português. "
    "Você pode pesquisar na web, verificar o clima e definir lembretes. "
    "Use as ferramentas quando necessário. Máximo de 3 frases na resposta final."
)

# Welcome dinâmico gerado pelo LLM (substitui frase estática)
DYNAMIC_WELCOME_ENABLED = True
DYNAMIC_WELCOME_SYSTEM = (
    "Você é Jarvis, assistente de IA. Gere uma saudação personalizada, "
    "motivadora e profissional em português. Máximo 2 frases. "
    "Inclua a hora do dia e um conselho ou observação útil."
)

# ---------------------------------------------------------------------------
# Agente de voz
# ---------------------------------------------------------------------------
VOICE_LISTEN_DURATION_S = 5
VOICE_LANGUAGE = "pt"

# ---------------------------------------------------------------------------
# Wake word (openwakeword — opcional)
# ---------------------------------------------------------------------------
WAKE_WORD_ENABLED = True
WAKE_WORD_MODEL = "hey_jarvis"          # modelo do openwakeword
WAKE_WORD_THRESHOLD = 0.5
WAKE_WORD_CHUNK_MS = 80                 # tamanho do bloco para wake word (ms)

# ---------------------------------------------------------------------------
# Tray icon (pystray — opcional)
# ---------------------------------------------------------------------------
TRAY_ICON_ENABLED = True

# ---------------------------------------------------------------------------
# Memória de conversa
# ---------------------------------------------------------------------------
MEMORY_FILE = Path(__file__).resolve().parent / ".cache" / "jarvis_memory.json"
MAX_HISTORY_MESSAGES = 20

# ---------------------------------------------------------------------------
# Ferramentas (Function Calling)
# ---------------------------------------------------------------------------
SEARCH_MAX_RESULTS = 3

JARVIS_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Pesquisa informações atuais na web via DuckDuckGo e retorna resumo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Termo de busca",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Retorna condições climáticas atuais de uma cidade.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "Nome da cidade (ex: 'São Paulo', 'Rio de Janeiro')",
                    }
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Define um lembrete que será falado em voz após N segundos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Tempo de espera em segundos (ex: 300 = 5 minutos)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Mensagem do lembrete",
                    },
                },
                "required": ["seconds", "message"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent / ".env")

# Garante que stdout/stderr suportam UTF-8 no Windows (evita UnicodeEncodeError com emojis)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # Python < 3.7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Configuração Inteligente de Dispositivos de Áudio
# ---------------------------------------------------------------------------
def setup_audio_devices() -> None:
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as e:
        log.warning("Não foi possível interrogar dispositivos de áudio: %s", e)
        return

    # 1. Tentar ler do .env
    input_env = os.environ.get("JARVIS_INPUT_DEVICE")
    output_env = os.environ.get("JARVIS_OUTPUT_DEVICE")
    
    input_id = int(input_env) if (input_env and input_env.strip().isdigit()) else None
    output_id = int(output_env) if (output_env and output_env.strip().isdigit()) else None

    # Se não configurado pelo .env, resolvemos automaticamente
    default_input, default_output = sd.default.device
    
    # Função para verificar se a API host é problemática (ex: WDM-KS que não aceita blocking reads)
    def is_problematic_api(dev: dict) -> bool:
        api_idx = dev.get("hostapi", -1)
        if 0 <= api_idx < len(hostapis):
            api_name = hostapis[api_idx].get("name", "").upper()
            return "WDM-KS" in api_name or "WASAPI" in api_name  # WASAPI às vezes também falha com blocos pequenos
        return False

    # 2. Corrigir Input se for inválido (-1) ou se apontar para WDM-KS/WASAPI por padrão
    if input_id is None:
        needs_fallback = (default_input == -1)
        if default_input >= 0 and default_input < len(devices):
            needs_fallback = is_problematic_api(devices[default_input])
            
        if needs_fallback:
            # Primeiro tenta MME ou DirectSound
            for idx, dev in enumerate(devices):
                if dev.get("max_input_channels", 0) > 0 and not is_problematic_api(dev):
                    input_id = idx
                    break
            # Se não achou nenhum fora de WDM-KS/WASAPI, pega qualquer um disponível > 0
            if input_id is None:
                for idx, dev in enumerate(devices):
                    if dev.get("max_input_channels", 0) > 0:
                        input_id = idx
                        break
        else:
            input_id = default_input

    # 3. Corrigir Output se for inválido (-1) ou apontar para API incompatível
    if output_id is None:
        needs_fallback = (default_output == -1)
        if default_output >= 0 and default_output < len(devices):
            needs_fallback = is_problematic_api(devices[default_output])

        if needs_fallback:
            # Primeiro tenta MME ou DirectSound
            for idx, dev in enumerate(devices):
                if dev.get("max_output_channels", 0) > 0 and not is_problematic_api(dev):
                    output_id = idx
                    break
            # Se não achou, pega qualquer um
            if output_id is None:
                for idx, dev in enumerate(devices):
                    if dev.get("max_output_channels", 0) > 0:
                        output_id = idx
                        break
        else:
            output_id = default_output

    if input_id is not None or output_id is not None:
        final_input = input_id if input_id is not None else default_input
        final_output = output_id if output_id is not None else default_output
        sd.default.device = (final_input, final_output)
        
        input_name = devices[final_input]["name"] if (final_input >= 0 and final_input < len(devices)) else "Nenhum"
        output_name = devices[final_output]["name"] if (final_output >= 0 and final_output < len(devices)) else "Nenhum"
        
        log.info(
            "🎤 Áudio configurado: Entrada ID %s (%s) | Saída ID %s (%s)",
            final_input,
            input_name,
            final_output,
            output_name
        )

setup_audio_devices()



# ---------------------------------------------------------------------------
# Estado global do tray icon
# ---------------------------------------------------------------------------
_tray_icon_ref: object = None  # pystray.Icon
_tray_lock = threading.Lock()


def _set_tray_status(status: str, tooltip: str | None = None) -> None:
    """Atualiza ícone do tray: 'listening', 'processing', 'speaking', 'error'."""
    global _tray_icon_ref
    with _tray_lock:
        icon = _tray_icon_ref
    if icon is None:
        return
    try:
        icon.icon = _make_tray_image(status)
        if tooltip:
            icon.title = tooltip
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


# ---------------------------------------------------------------------------
# Memória de conversa
# ---------------------------------------------------------------------------

def load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"history": []}


def save_memory(memory: dict) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_to_history(memory: dict, role: str, text: str) -> None:
    memory["history"].append({"role": role, "content": text})
    memory["history"] = memory["history"][-MAX_HISTORY_MESSAGES:]


# ---------------------------------------------------------------------------
# Ferramentas (executadas pelo agente)
# ---------------------------------------------------------------------------

def tool_search_web(query: str) -> str:
    """Pesquisa na web via DuckDuckGo (sem API key). Usa ddgs (novo nome) com fallback."""
    # Tenta o pacote novo (ddgs) e depois o legado (duckduckgo_search)
    DDGS = None
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore[assignment]
        except ImportError:
            return "ddgs não instalado. Execute: pip install ddgs"
    try:
        with DDGS() as client:
            results = list(client.text(query, max_results=SEARCH_MAX_RESULTS))
        if not results:
            return f"Nenhum resultado encontrado para: {query}"
        lines = [f"• {r['title']}: {r['body'][:250]}" for r in results]
        return "\n".join(lines)
    except Exception as e:
        log.warning("search_web falhou: %s", e)
        return f"Erro ao pesquisar: {e}"


def tool_get_weather(city: str) -> str:
    """Retorna clima atual via wttr.in (gratuito, sem API key)."""
    try:
        encoded = urllib.parse.quote(city)
        url = f"https://wttr.in/{encoded}?format=3&lang=pt"
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as e:
        log.warning("get_weather falhou: %s", e)
        return f"Não consegui obter o clima de {city}: {e}"


def tool_set_reminder(seconds: int, message: str) -> str:
    """Define lembrete em voz após `seconds` segundos."""
    seconds = max(1, int(seconds))

    def _fire():
        time.sleep(seconds)
        log.info("⏰ Lembrete disparado: %r", message)
        say_text_elevenlabs(f"Lembrete: {message}")

    threading.Thread(target=_fire, daemon=True).start()
    mins = seconds // 60
    secs = seconds % 60
    if mins:
        eta = f"{mins} minuto{'s' if mins > 1 else ''}" + (f" e {secs}s" if secs else "")
    else:
        eta = f"{secs} segundo{'s' if secs > 1 else ''}"
    return f"Lembrete definido: '{message}' em {eta}."


def execute_tool(name: str, args: dict) -> str:
    """Despachante de ferramentas — chamado pelo loop de function calling."""
    log.info("🔧 Ferramenta: %s(%s)", name, args)
    if name == "search_web":
        return tool_search_web(args.get("query", ""))
    if name == "get_weather":
        return tool_get_weather(args.get("city", ""))
    if name == "set_reminder":
        return tool_set_reminder(args.get("seconds", 60), args.get("message", ""))
    return f"Ferramenta desconhecida: {name}"


# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _welcome_cache_dir() -> Path:
    base = Path(__file__).resolve().parent
    override = (os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "jarvis_welcome"


def _welcome_cache_path(text: str, voice_id: str, model_id: str, fmt: str) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{fmt}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _welcome_cache_dir() / f"{digest}.wav"


def _play_pcm_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return False
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error):
        return False
    if not raw:
        return False
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        sd.play(pcm, rate)
        sd.wait()
    except Exception:
        return False
    return True


def _save_pcm_wav(path: Path, raw: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(raw)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def say_text_elevenlabs(text: str, *, use_cache: bool = False) -> None:
    """Fala texto via ElevenLabs TTS."""
    text = text.strip()
    if not text:
        return
    vid, model_id, fmt, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("ELEVENLABS_VOICE_ID não definido.")
        return

    cache_path = _welcome_cache_path(text, vid, model_id, fmt)
    if use_cache and JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        if _play_pcm_wav(cache_path):
            return

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("ELEVENLABS_API_KEY não definido.")
        return
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("elevenlabs não instalado.")
        return
    try:
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid, text=text, model_id=model_id, output_format=fmt
        )
        raw = b"".join(chunks)
    except Exception as e:
        log.warning("ElevenLabs TTS falhou: %s", e)
        return
    if not raw:
        return
    if use_cache and JARVIS_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav(cache_path, raw, pcm_rate)
        except OSError:
            pass
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        _set_tray_status("speaking", "Jarvis — Falando…")
        sd.play(pcm, pcm_rate)
        sd.wait()
    except Exception as e:
        log.warning("Playback falhou: %s", e)
    finally:
        _set_tray_status("listening", "Jarvis — Ouvindo")


# ---------------------------------------------------------------------------
# Welcome dinâmico via DeepSeek
# ---------------------------------------------------------------------------

def generate_dynamic_welcome() -> str:
    """Gera saudação contextual via DeepSeek. Retorna frase estática em caso de erro."""
    if not DYNAMIC_WELCOME_ENABLED:
        return JARVIS_WELCOME_FALLBACK_PHRASE

    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        return JARVIS_WELCOME_FALLBACK_PHRASE

    now = datetime.now()
    hora = now.strftime("%H:%M")
    dia = now.strftime("%A, %d de %B de %Y")

    prompt = (
        f"Hora atual: {hora}. Data: {dia}. "
        "Gere uma saudação curta de boas-vindas para o usuário. "
        "Mencione a hora do dia e inclua uma frase motivadora ou dica prática."
    )
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
        )
        resp = client.chat.completions.create(
            model=(os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_MODEL),
            messages=[
                {"role": "system", "content": DYNAMIC_WELCOME_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=120,
            temperature=0.9,
        )
        phrase = resp.choices[0].message.content.strip()
        log.info("Welcome dinâmico: %r", phrase)
        return phrase
    except Exception as e:
        log.warning("Welcome dinâmico falhou, usando frase padrão: %s", e)
        return JARVIS_WELCOME_FALLBACK_PHRASE


def say_jarvis_welcome() -> None:
    if not JARVIS_WELCOME_ENABLED:
        return
    phrase = generate_dynamic_welcome()
    # Welcome dinâmico não usa cache (frase muda a cada sessão)
    say_text_elevenlabs(phrase, use_cache=False)


# ---------------------------------------------------------------------------
# DeepSeek — agente com function calling
# ---------------------------------------------------------------------------

def _ask_deepseek_with_tools(user_text: str, memory: dict) -> str:
    """Envia mensagem ao DeepSeek com suporte a ferramentas e retorna resposta final."""
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        return "DEEPSEEK_API_KEY não configurado no .env."

    try:
        from openai import OpenAI
    except ImportError:
        return "openai não instalado. Execute: pip install openai"

    model = (os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_MODEL)
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    messages: list[dict] = [{"role": "system", "content": DEEPSEEK_SYSTEM_PROMPT}]
    messages.extend(memory.get("history", []))
    messages.append({"role": "user", "content": user_text})

    try:
        # Primeira chamada — com ferramentas disponíveis
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=JARVIS_TOOL_DEFINITIONS,
            tool_choice="auto",
            max_tokens=500,
            temperature=0.7,
        )
        assistant_msg = resp.choices[0].message

        # Se o modelo quer usar ferramentas, executa e chama novamente
        if assistant_msg.tool_calls:
            # Adiciona a mensagem do assistente (com tool_calls) ao histórico
            messages.append(assistant_msg)

            for tc in assistant_msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                result = execute_tool(tc.function.name, args)
                log.info("🔧 Resultado de %s: %s", tc.function.name, result[:120])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Segunda chamada — resposta final baseada nos resultados das ferramentas
            resp2 = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=300,
                temperature=0.7,
            )
            reply = resp2.choices[0].message.content.strip()
        else:
            reply = (assistant_msg.content or "").strip()

        log.info("🤖 DeepSeek: %r", reply)
        return reply

    except Exception as e:
        log.warning("DeepSeek falhou: %s", e)
        return "Desculpe, ocorreu um erro ao consultar o assistente."


# ---------------------------------------------------------------------------
# STT — transcrição offline com faster-whisper
# ---------------------------------------------------------------------------

def _record_audio(duration_s: float) -> np.ndarray:
    log.info("🎙️  Ouvindo por %.1fs…", duration_s)
    _set_tray_status("processing", "Jarvis — Ouvindo comando…")
    audio = sd.rec(
        int(SAMPLE_RATE * duration_s),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    return audio.flatten()


def _transcribe_audio(audio: np.ndarray) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper não instalado.")
        return ""
    model_size = (os.environ.get("WHISPER_MODEL") or "base").strip()
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, info = model.transcribe(audio, language=VOICE_LANGUAGE, beam_size=5)
        text = " ".join(seg.text for seg in segments).strip()
        log.info("📝 Transcrição [%s]: %r", info.language, text)
        return text
    except Exception as e:
        log.warning("Transcrição falhou: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Agente de voz — sequência completa
# ---------------------------------------------------------------------------

def run_voice_agent() -> None:
    """Gravar → STT → DeepSeek (com ferramentas) → TTS."""
    log.info("🔴 Modo conversa ativo! Fale sua mensagem…")
    _set_tray_status("processing", "Jarvis — Gravando…")

    audio = _record_audio(VOICE_LISTEN_DURATION_S)
    user_text = _transcribe_audio(audio)

    if not user_text:
        _set_tray_status("listening", "Jarvis — Ouvindo")
        say_text_elevenlabs("Desculpe, não entendi. Pode repetir?")
        return

    _set_tray_status("processing", f"Jarvis — Pensando: {user_text[:40]}…")
    memory = load_memory()
    add_to_history(memory, "user", user_text)

    reply = _ask_deepseek_with_tools(user_text, memory)
    add_to_history(memory, "assistant", reply)
    save_memory(memory)

    say_text_elevenlabs(reply)


# ---------------------------------------------------------------------------
# Tray icon (pystray)
# ---------------------------------------------------------------------------

def _make_tray_image(status: str = "listening") -> "Image":
    """Cria imagem 64x64 para o tray icon com cor de status."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None  # type: ignore

    colors = {
        "listening": (0, 200, 100),    # verde
        "processing": (220, 160, 0),   # amarelo
        "speaking": (0, 150, 220),     # azul
        "error": (200, 50, 50),        # vermelho
    }
    bg = (20, 20, 30)
    fg = colors.get(status, colors["listening"])

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Fundo escuro arredondado
    draw.ellipse([2, 2, 62, 62], fill=bg)
    # Círculo de status
    draw.ellipse([12, 12, 52, 52], fill=fg)
    # Letra J no centro
    draw.text((22, 16), "J", fill=(255, 255, 255))
    return img


def _build_tray_menu() -> object:
    try:
        import pystray
    except ImportError:
        return None
    return pystray.Menu(
        pystray.MenuItem("Jarvis AI — Ativo", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Ouvindo palmas e wake word", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Sair",
            lambda icon, item: (icon.stop(), os.kill(os.getpid(), 2)),
        ),
    )


def _start_tray_icon() -> None:
    """Inicia o tray icon em thread daemon."""
    global _tray_icon_ref
    if not TRAY_ICON_ENABLED:
        return
    try:
        import pystray
        from PIL import Image  # noqa: F401
    except ImportError:
        log.warning("pystray/Pillow não instalados — tray icon desabilitado.")
        return

    img = _make_tray_image("listening")
    if img is None:
        return

    icon = pystray.Icon(
        "jarvis",
        img,
        "Jarvis — Ouvindo",
        _build_tray_menu(),
    )
    with _tray_lock:
        _tray_icon_ref = icon

    def _run():
        try:
            icon.run()
        except Exception as e:
            log.warning("Tray icon encerrou: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    log.info("🟢 Tray icon iniciado.")


# ---------------------------------------------------------------------------
# Wake word (openwakeword)
# ---------------------------------------------------------------------------

def _wake_word_loop() -> None:
    """Thread de detecção de wake word ('hey jarvis') via openwakeword."""
    if not WAKE_WORD_ENABLED:
        return
    try:
        from openwakeword.model import Model as OWWModel
    except ImportError:
        log.warning(
            "openwakeword não instalado — wake word desabilitado. "
            "Execute: pip install openwakeword"
        )
        return

    # Carrega o modelo (baixa automaticamente na primeira execução)
    try:
        oww = OWWModel(wakeword_models=[WAKE_WORD_MODEL], inference_framework="onnx")
        log.info("🔊 Wake word '%s' ativo.", WAKE_WORD_MODEL)
    except Exception as e:
        log.warning("Wake word: falha ao carregar modelo '%s': %s", WAKE_WORD_MODEL, e)
        return

    chunk_samples = int(SAMPLE_RATE * WAKE_WORD_CHUNK_MS / 1000)
    
    state = {
        "last_triggered": 0.0
    }
    WAKE_COOLDOWN_S = 3.0

    def wake_callback(indata, frames, time_info, status):
        if status:
            log.debug("Wake status: %s", status)
        pcm = indata.flatten()
        prediction = oww.predict(pcm)
        score = max(prediction.get(WAKE_WORD_MODEL, {0: 0.0}).values(), default=0.0)
        now = time.monotonic()
        if score >= WAKE_WORD_THRESHOLD and (now - state["last_triggered"]) > WAKE_COOLDOWN_S:
            state["last_triggered"] = now
            log.info("🎙️  Wake word detectado (score=%.2f) → agente de voz", score)
            threading.Thread(target=run_voice_agent, daemon=True).start()

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=chunk_samples,
            callback=wake_callback
        ):
            while True:
                time.sleep(1.0)
    except Exception as e:
        log.warning("Wake word loop encerrou: %s", e)




# ---------------------------------------------------------------------------
# Spotify / browser
# ---------------------------------------------------------------------------

def play_song(uri: str) -> None:
    u = uri.strip()
    if not u:
        return
    try:
        if sys.platform == "win32":
            os.startfile(u)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------

def _chrome_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    return shutil.which("google-chrome") or shutil.which("chrome")


def _win32_sorted_monitor_rects() -> list[tuple[int, int, int, int]]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG), ("top", wintypes.LONG),
            ("right", wintypes.LONG), ("bottom", wintypes.LONG),
        ]

    collected: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                        ctypes.POINTER(RECT), wintypes.LPARAM)
    def _cb(_hm, _hdc, lprc, _lp):
        r = lprc.contents
        collected.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
    collected.sort(key=lambda t: (t[0], t[1]))
    return collected


def _chrome_monitor_bounds(idx1: int) -> tuple[int, int, int, int]:
    rects = _win32_sorted_monitor_rects()
    if not rects:
        return (0, 0, 1920, 1080)
    i = max(0, min(idx1 - 1, len(rects) - 1))
    return rects[i]


def _chrome_monitor_top_left(idx1: int) -> tuple[int, int]:
    l, t, _, _ = _chrome_monitor_bounds(idx1)
    return (l, t)


def _chrome_monitor_pixel_size(idx1: int) -> tuple[int, int]:
    l, t, r, b = _chrome_monitor_bounds(idx1)
    return (max(320, r - l), max(240, b - t))


def _chrome_window_size() -> tuple[int, int]:
    w = (os.environ.get("CHROME_WINDOW_WIDTH") or "1400").strip()
    h = (os.environ.get("CHROME_WINDOW_HEIGHT") or "900").strip()
    try:
        return (max(400, int(w)), max(300, int(h)))
    except ValueError:
        return (1400, 900)


def _chrome_site_user_data_dir(site_key: str) -> str:
    p = Path(tempfile.gettempdir()) / "clap-trigger-chrome" / site_key
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _chrome_top_level_browser_hwnds_win32() -> set[int]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "chrome.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        if (r.right - r.left) < 80 or (r.bottom - r.top) < 80:
            return True
        found.add(int(hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _wait_new_chrome_hwnd_win32(before: set[int], timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.12)
        new = _chrome_top_level_browser_hwnds_win32() - before
        if not new:
            continue
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        best, best_area = None, 0
        for h in new:
            r = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)):
                a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if a > best_area:
                    best_area, best = a, h
        if best is not None:
            return best
    return None


def _chrome_snap_to_monitor_win32(
    hwnd: int, monitor: int, *, fullscreen: bool, windowed_size: tuple | None
) -> None:
    import ctypes
    from ctypes import wintypes

    ml, mt, mr, mb = _chrome_monitor_bounds(monitor)
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if fullscreen:
        x, y, w, h = ml, mt, mr - ml, mb - mt
    else:
        ww, wh = windowed_size or _chrome_window_size()
        w, h = ww, wh
        x = ml + max(0, (mr - ml - w) // 2)
        y = mt + max(0, (mb - mt - h) // 2)
    user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040 | 0x0020)
    if fullscreen:
        user32.ShowWindow(hwnd, 3)  # SW_SHOWMAXIMIZED
        VK_F11 = 0x7A
        fg = user32.GetForegroundWindow()
        t1 = user32.GetWindowThreadProcessId(hwnd, None)
        t2 = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if t1 and t2:
            user32.AttachThreadInput(t2, t1, True)
        user32.SetForegroundWindow(hwnd)
        if t1 and t2:
            user32.AttachThreadInput(t2, t1, False)
        user32.keybd_event(VK_F11, 0, 0, 0)
        user32.keybd_event(VK_F11, 0, 0x0002, 0)


def _open_url_in_chrome(
    url: str, *, new_window=True, label="URL",
    window_position=None, window_size=None, fullscreen=False,
    win32_post_fullscreen_monitor=None, user_data_dir=None,
) -> None:
    u = url.strip()
    if not u:
        return
    chrome = _chrome_executable()
    try:
        if chrome:
            args = [chrome]
            if user_data_dir:
                args += [f"--user-data-dir={user_data_dir}", "--no-first-run"]
            if new_window:
                args.append("--new-window")
            if window_position:
                args.append(f"--window-position={window_position[0]},{window_position[1]}")
            if window_size:
                args.append(f"--window-size={window_size[0]},{window_size[1]}")
            if fullscreen and not (sys.platform == "win32" and win32_post_fullscreen_monitor):
                args.append("--start-fullscreen")
            args.append(u)
            kw: dict = {
                "args": args, "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            before = _chrome_top_level_browser_hwnds_win32() if win32_post_fullscreen_monitor else None
            subprocess.Popen(**kw)
            if sys.platform == "win32" and win32_post_fullscreen_monitor:
                timeout = max(3.0, float((os.environ.get("CHROME_NEW_WINDOW_WAIT_S") or "25")))
                hwnd = _wait_new_chrome_hwnd_win32(before, timeout)
                if hwnd:
                    _chrome_snap_to_monitor_win32(
                        hwnd, win32_post_fullscreen_monitor,
                        fullscreen=fullscreen,
                        windowed_size=window_size if not fullscreen else None,
                    )
                else:
                    log.warning("Chrome: timeout esperando nova janela (%s).", label)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open %s: %s", label, e)


def open_claude_in_chrome() -> None:
    if not OPEN_CLAUDE_CODE_IN_CHROME:
        return
    url = (os.environ.get("CLAUDE_CODE_URL") or "https://claude.ai/new").strip()
    fs = OPEN_CHROME_FULLSCREEN
    if sys.platform == "win32":
        pos = _chrome_monitor_top_left(CLAUDE_CHROME_MONITOR)
        size = _chrome_monitor_pixel_size(CLAUDE_CHROME_MONITOR) if fs else _chrome_window_size()
        ud = _chrome_site_user_data_dir("claude") if CHROME_SEPARATE_SITE_PROFILES else None
        _open_url_in_chrome(
            url, new_window=True, label="Claude", window_position=pos, window_size=size,
            fullscreen=fs, win32_post_fullscreen_monitor=CLAUDE_CHROME_MONITOR, user_data_dir=ud,
        )
    else:
        _open_url_in_chrome(url, new_window=True, label="Claude",
                            window_size=None if fs else _chrome_window_size(), fullscreen=fs)


def open_binance_btc_in_chrome() -> None:
    if not OPEN_BINANCE_BTC_IN_CHROME:
        return
    url = (os.environ.get("BINANCE_BTC_URL") or "https://www.binance.com/en/trade/BTC_USDT").strip()
    fs = OPEN_CHROME_FULLSCREEN
    if sys.platform == "win32":
        pos = _chrome_monitor_top_left(BINANCE_CHROME_MONITOR)
        size = _chrome_monitor_pixel_size(BINANCE_CHROME_MONITOR) if fs else _chrome_window_size()
        ud = _chrome_site_user_data_dir("binance") if CHROME_SEPARATE_SITE_PROFILES else None
        _open_url_in_chrome(
            url, new_window=True, label="Binance BTC", window_position=pos, window_size=size,
            fullscreen=fs, win32_post_fullscreen_monitor=BINANCE_CHROME_MONITOR, user_data_dir=ud,
        )
    else:
        _open_url_in_chrome(url, new_window=True, label="Binance BTC",
                            window_size=None if fs else _chrome_window_size(), fullscreen=fs)


# ---------------------------------------------------------------------------
# Antigravity IDE
# ---------------------------------------------------------------------------

def _antigravity_executable() -> str | None:
    fixed = (os.environ.get("ANTIGRAVITY_EXE") or ANTIGRAVITY_EXE).strip()
    if fixed and os.path.isfile(fixed):
        return fixed
    return shutil.which("antigravity") or shutil.which("antigravity-ide")


def _antigravity_largest_hwnd_win32() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PQLI = 0x1000
    candidates: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        if user32.GetWindow(hwnd, 4):
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x00000080:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        hproc = kernel32.OpenProcess(PQLI, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe).lower() != "antigravity ide.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return True
        candidates.append((w * h, int(hwnd)))
        return True

    user32.EnumWindows(_enum, 0)
    return max(candidates, key=lambda t: t[0])[1] if candidates else None


def _antigravity_foreground_win32(hwnd: int) -> None:
    import ctypes
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    fg = user32.GetForegroundWindow()
    t1 = user32.GetWindowThreadProcessId(hwnd, None)
    t2 = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if t1 and t2:
        user32.AttachThreadInput(t2, t1, True)
    user32.SetForegroundWindow(hwnd)
    if t1 and t2:
        user32.AttachThreadInput(t2, t1, False)


def open_antigravity_window() -> None:
    if not FOCUS_EXISTING_ANTIGRAVITY_ON_DOUBLE_CLAP and not OPEN_NEW_ANTIGRAVITY_ON_DOUBLE_CLAP:
        return
    exe = _antigravity_executable()
    if not exe:
        log.warning("Antigravity IDE não encontrado. Verifique ANTIGRAVITY_EXE no .env.")
        return
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if FOCUS_EXISTING_ANTIGRAVITY_ON_DOUBLE_CLAP:
            hwnd = _antigravity_largest_hwnd_win32() if sys.platform == "win32" else None
            if hwnd:
                _antigravity_foreground_win32(hwnd)
            else:
                log.info("Nenhuma janela do Antigravity IDE encontrada; abrindo…")
                subprocess.Popen([exe], **kw)
        if OPEN_NEW_ANTIGRAVITY_ON_DOUBLE_CLAP:
            subprocess.Popen([exe, "-n"], **kw)
    except OSError as e:
        log.warning("Could not start Antigravity IDE: %s", e)
        return
    if sys.platform == "win32" and ANTIGRAVITY_OPEN_FULLSCREEN:
        time.sleep(0.6)
        hwnd = _antigravity_largest_hwnd_win32()
        if hwnd:
            import ctypes
            user32 = ctypes.windll.user32
            _antigravity_foreground_win32(hwnd)
            user32.keybd_event(0x7A, 0, 0, 0)       # F11 down
            user32.keybd_event(0x7A, 0, 0x0002, 0)  # F11 up
        else:
            log.warning("Antigravity fullscreen: nenhuma janela para enviar F11.")


# ---------------------------------------------------------------------------
# Boot sequence (palma dupla)
# ---------------------------------------------------------------------------

def run_double_clap_actions() -> None:
    _set_tray_status("processing", "Jarvis — Iniciando ambiente…")
    play_song(SONG_URI)
    open_claude_in_chrome()
    open_binance_btc_in_chrome()
    if JARVIS_WELCOME_ENABLED:
        delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
        if delay:
            time.sleep(delay)
        threading.Thread(target=say_jarvis_welcome, daemon=True).start()
    open_antigravity_window()
    _set_tray_status("listening", "Jarvis — Ouvindo")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    # Inicia serviços em background
    _start_tray_icon()
    if WAKE_WORD_ENABLED:
        threading.Thread(target=_wake_word_loop, daemon=True, name="wake-word").start()

    blocksize = block_samples()
    noise_floor = 1e-4
    last_clap_event = 0.0
    clap_times: list[float] = []
    spike_armed = True
    welcome_done = False

    log.info(
        "👏 Ouvindo — dupla: %.2f–%.2fs | tripla: <%.2fs | "
        "rate=%d | block=%dms | spike=%.1f | cooldown=%.2fs | Ctrl+C para parar.",
        MIN_DOUBLE_GAP_S, MAX_DOUBLE_GAP_S, TRIPLE_CLAP_WINDOW_S,
        SAMPLE_RATE, BLOCK_MS, SPIKE_RATIO, COOLDOWN_S,
    )
    log.info("🎵 Palma dupla  → boot (Spotify + Chrome + Antigravity IDE + Welcome DeepSeek)")
    log.info("🤖 Palma tripla → agente de voz DeepSeek (%s) com ferramentas", DEEPSEEK_MODEL)
    log.info("🌤️  Ferramentas: search_web | get_weather | set_reminder")
    if WAKE_WORD_ENABLED:
        log.info("🔊 Wake word '%s' ativo em thread separada.", WAKE_WORD_MODEL)
    state = {
        "noise_floor": noise_floor,
        "last_clap_event": last_clap_event,
        "clap_times": clap_times,
        "spike_armed": spike_armed,
        "welcome_done": welcome_done
    }

    def clap_callback(indata, frames, time_info, status):
        if status:
            log.warning("Audio status: %s", status)
        level = rms_mono(indata)
        now = time.monotonic()

        if level < state["noise_floor"] * QUIET_GATE_MULT:
            state["noise_floor"] = (
                NOISE_FLOOR_ALPHA * state["noise_floor"]
                + (1.0 - NOISE_FLOOR_ALPHA) * level
            )
            state["noise_floor"] = max(state["noise_floor"], 1e-7)

        threshold = max(state["noise_floor"] * SPIKE_RATIO, MIN_RMS)
        retrigger = threshold * RETRIGGER_RATIO

        if level < retrigger:
            state["spike_armed"] = True

        if state["spike_armed"] and level >= threshold and (now - state["last_clap_event"]) >= COOLDOWN_S:
            state["spike_armed"] = False
            state["clap_times"].append(now)
            state["clap_times"] = [t for t in state["clap_times"] if now - t <= TRIPLE_CLAP_WINDOW_S]

            # ── Detecção de palma TRIPLA ──
            if len(state["clap_times"]) >= 3:
                span = state["clap_times"][-1] - state["clap_times"][-3]
                if span <= TRIPLE_CLAP_WINDOW_S:
                    state["last_clap_event"] = now
                    state["clap_times"].clear()
                    log.info("🤚🤚🤚 Palma tripla (span=%.3fs) → agente de voz", span)
                    threading.Thread(target=run_voice_agent, daemon=True).start()
                    return

            # ── Detecção de palma DUPLA ──
            if len(state["clap_times"]) >= 2:
                gap = state["clap_times"][-1] - state["clap_times"][-2]
                if MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S:
                    state["last_clap_event"] = now
                    if not state["welcome_done"]:
                        state["welcome_done"] = True
                        log.info("👏👏 Palma dupla (gap=%.3fs, rms=%.5f) → boot sequence", gap, level)
                        threading.Thread(target=run_double_clap_actions, daemon=True).start()
                    else:
                        log.info("👏👏 Palma dupla (gap=%.3fs) — boot já executado nesta sessão.", gap)

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", blocksize=blocksize,
            callback=clap_callback
        ):
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        return 1

    return 0




if __name__ == "__main__":
    sys.exit(main())
