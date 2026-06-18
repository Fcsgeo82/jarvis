# 🤖 Jarvis — Desktop Voice Assistant Agent

Python assistant that listens to your microphone and reacts to **clap patterns**
and a **wake word**. Supports an offline STT pipeline, DeepSeek LLM with tool
calling, ElevenLabs TTS, and a system tray icon.

---

## Features

| Feature | Detail |
| --- | --- |
| 👏👏 **Double clap** | Boot sequence: Spotify → Claude (Chrome) → Binance (Chrome) → Antigravity IDE → Dynamic LLM welcome |
| 👏👏👏 **Triple clap** | Voice agent: record → Whisper STT → DeepSeek + tools → ElevenLabs speaks |
| 🎙️ **Wake word** | "Hey Jarvis" (offline, no API key) triggers the voice agent |
| 🌤️ **Weather tool** | Real-time weather via wttr.in — no API key required |
| 🔍 **Web search tool** | DuckDuckGo search via `ddgs` — no API key required |
| ⏰ **Reminder tool** | Spoken reminder after N seconds via threading + TTS |
| 💬 **Conversation memory** | Last 20 messages stored in `.cache/jarvis_memory.json` |
| 🟢 **Tray icon** | System tray icon: green (listening), yellow (processing), blue (speaking) |

---

## Requirements

- Python 3.10+
- Windows (multi-monitor Chrome/Antigravity placement uses Win32 APIs)
- A working microphone

---

## Setup

```bash
# 1 — Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2 — Install dependencies
pip install -r requirements.txt

# 3 — Configure environment variables
copy .env.example .env
# Then edit .env with your API keys
```

> **Note:** On the first run with wake word enabled, `openwakeword` downloads
> the `hey_jarvis` ONNX model (~5 MB) automatically.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values.

### Required for TTS

| Variable | Purpose |
| --- | --- |
| `ELEVENLABS_API_KEY` | API key from [elevenlabs.io](https://elevenlabs.io) |
| `ELEVENLABS_VOICE_ID` | Voice ID from the ElevenLabs app (My Voices) |

Without these, TTS is silently skipped (all other features still work).

### Required for Voice Agent & Dynamic Welcome

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Free API key from [platform.deepseek.com](https://platform.deepseek.com) |

Without this, the voice agent returns a static error message and the welcome
uses the fallback phrase.

### Optional

| Variable | Default | Purpose |
| --- | --- | --- |
| `ELEVENLABS_MODEL_ID` | `eleven_multilingual_v2` | TTS model ID |
| `ELEVENLABS_OUTPUT_FORMAT` | `pcm_24000` | Audio format |
| `ELEVENLABS_PCM_SAMPLE_RATE` | Auto from format | Override PCM sample rate |
| `JARVIS_WELCOME_CACHE_DIR` | `.cache/jarvis_welcome/` | Cache folder for welcome WAVs |
| `DEEPSEEK_MODEL` | `deepseek-chat` | LLM model (e.g. `deepseek-reasoner` for R1) |
| `WHISPER_MODEL` | `base` | STT model: `tiny` / `base` / `small` / `medium` / `large-v3` |
| `ANTIGRAVITY_EXE` | Auto-detected | Full path to `Antigravity IDE.exe` |
| `CLAUDE_CODE_URL` | `https://claude.ai/new` | URL opened in Chrome for Claude |
| `BINANCE_BTC_URL` | `https://www.binance.com/en/trade/BTC_USDT` | URL for Binance |
| `CHROME_NEW_WINDOW_WAIT_S` | `25` | Seconds to wait for Chrome window on Windows |
| `CHROME_WINDOW_WIDTH` / `CHROME_WINDOW_HEIGHT` | `1400` / `900` | Chrome windowed size |

Example `.env`:

```env
ELEVENLABS_API_KEY=sk-...
ELEVENLABS_VOICE_ID=abc123

DEEPSEEK_API_KEY=sk-...

# Optional overrides
# WHISPER_MODEL=small
# DEEPSEEK_MODEL=deepseek-reasoner
```

---

## Run

```bash
python jarvis.py
```

Allow microphone access if Windows prompts. Stop with **Ctrl+C** or via the
tray icon menu → **Sair**.

Expected startup output:

```text
Ouvindo — dupla: 0.05–0.35s | tripla: <1.00s | rate=44100 | block=40ms | spike=7.0 | cooldown=0.45s
Palma dupla  → boot (Spotify + Chrome + Antigravity IDE + Welcome DeepSeek)
Palma tripla → agente de voz DeepSeek (deepseek-chat) com ferramentas
Ferramentas: search_web | get_weather | set_reminder
Wake word 'hey_jarvis' ativo em thread separada.
Tray icon ativo na bandeja do sistema.
```

---

## Voice Agent — How It Works

```text
You clap three times (or say "Hey Jarvis")
         ↓
Jarvis records 5 seconds of audio
         ↓
faster-whisper transcribes offline (no API key)
         ↓
DeepSeek-V3 receives your text + available tools
         ↓
DeepSeek decides whether to call a tool:
  • get_weather("São Paulo")   → wttr.in → "22°C, Nublado"
  • search_web("Bitcoin hoje") → DuckDuckGo → live result
  • set_reminder(300, "Stand up") → fires in 5 min
         ↓
DeepSeek generates a natural language reply
         ↓
ElevenLabs speaks the response
```

---

## Clap Detection Tuning

Edit constants at the top of `jarvis.py`:

| Constant | Effect |
| --- | --- |
| `SPIKE_RATIO` | Increase to reduce false triggers; decrease if claps are missed |
| `COOLDOWN_S` | Minimum gap between two detected claps |
| `MIN_DOUBLE_GAP_S` / `MAX_DOUBLE_GAP_S` | Valid gap range for double-clap detection |
| `TRIPLE_CLAP_WINDOW_S` | Time window to detect 3 claps as a triple |
| `BLOCK_MS` | Larger = slightly less CPU, less precise timing |
| `MIN_RMS` | Minimum loudness floor (helps in very quiet rooms) |
| `SAMPLE_RATE` | Try `48000` if your device does not support `44100` |

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| **PortAudio / audio errors** | Update audio drivers or try `SAMPLE_RATE=48000` |
| **No reaction to claps** | Lower `SPIKE_RATIO` slightly or clap closer to the mic |
| **Too many false triggers** | Raise `SPIKE_RATIO` or `COOLDOWN_S` |
| **No welcome speech** | Set `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` in `.env` and restart |
| **Voice agent returns nothing** | Set `DEEPSEEK_API_KEY` in `.env` |
| **Wake word not working** | First run downloads the ONNX model — needs internet. Check `openwakeword` logs |
| **Tray icon missing** | Install `pystray` and `Pillow`: `pip install pystray Pillow` |
| **Search returns empty** | `ddgs` requires internet access. Check firewall/proxy |

---

## Project Structure

```text
jarvis/
├── jarvis.py            # Main script
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variables template
├── .env                 # Your local config (not committed)
├── CHANGELOG.md         # Version history
└── .cache/
    ├── jarvis_memory.json    # Conversation history
    └── jarvis_welcome/       # Cached TTS welcome audio
```

---

## Dependencies

```text
elevenlabs          — TTS via ElevenLabs API
openai              — DeepSeek API client (OpenAI-compatible)
faster-whisper      — Offline speech-to-text (Whisper)
sounddevice         — Microphone input / audio playback
numpy               — Audio signal processing
ddgs                — DuckDuckGo web search (no API key)
openwakeword        — Offline wake word detection
pystray             — System tray icon
Pillow              — Tray icon image rendering
python-dotenv       — .env file loader
```

---

## License

See [LICENSE](LICENSE).
