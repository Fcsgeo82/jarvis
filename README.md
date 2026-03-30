# Desktop clap → simple log

Small Python script that listens to your default microphone and writes a log line when it hears a sharp loud sound (e.g. a clap). Use it as a starting point to hook in other actions later.

## Setup

```bash
cd clap-trigger
python -m pip install -r requirements.txt
```

## Run

```bash
python clap_listen.py
```

Allow the microphone if Windows prompts you. Stop with **Ctrl+C**.

## Tuning

Edit the constants at the top of `clap_listen.py`:

| Constant | Effect |
|----------|--------|
| `SPIKE_RATIO` | Increase if you get false triggers; decrease if claps are missed. |
| `COOLDOWN_S` | Minimum time between two logged claps. |
| `BLOCK_MS` | Larger = slightly less CPU, a bit less precise timing. |
| `MIN_RMS` | Floor on how loud a block must be (helps in very quiet rooms). |
| `SAMPLE_RATE` | Try `48000` if your device does not like `44100`. |

## Troubleshooting

- **PortAudio / audio errors:** Update audio drivers or try another `SAMPLE_RATE`.
- **No reaction to claps:** Lower `SPIKE_RATIO` slightly or speak/clap closer to the mic.
- **Spam logs:** Raise `SPIKE_RATIO` or `COOLDOWN_S`.
