"""The ``media`` host object: images, speech and music through OpenRouter.

Handed to an agent session's Python, never to a published app (see
``Registry._python_config``). Each call writes its file into the
session's workspace and returns plain facts about it (path, size,
length, cost): bytes inside a dict or a list cannot cross back from a
dud guest, and a batch has to. The write goes through the workspace's
filesystem directly, the way the sandbox's own file writes do; the
file-tool API would wait on the workspace lock that the agent's running
call already holds.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

IMAGES_URL = "https://openrouter.ai/api/v1/images"
SPEECH_URL = "https://openrouter.ai/api/v1/audio/speech"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Real transparency (an alpha channel, not a painted checkerboard) is
#: an OpenAI image-model feature on OpenRouter; Gemini's image models and
#: gpt-image-2 take no transparent background. Measured: a low-quality
#: 1024x1024 in about 11s for $0.006.
IMAGE_MODEL = "openai/gpt-image-2.5-sunburst"
IMAGE_ASPECTS = ("1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9")
IMAGE_QUALITIES = ("low", "medium", "high", "xhigh", "max")
MAX_REFERENCES = 16

SPEECH_MODEL = "google/gemini-3.8-flash-tts"
DEFAULT_VOICE = "Kore"
#: Gemini's prebuilt voices, with the tone each is described by. The
#: provider answers an unknown name with a bare "Provider returned 400",
#: so names are checked here, where the error can list the choices.
VOICES = {
    "Achernar": "soft",
    "Achird": "friendly",
    "Algenib": "gravelly",
    "Algieba": "smooth",
    "Alnilam": "firm",
    "Aoede": "breezy",
    "Autonoe": "bright",
    "Callirrhoe": "easy-going",
    "Charon": "informative",
    "Despina": "smooth",
    "Enceladus": "breathy",
    "Erinome": "clear",
    "Fenrir": "excitable",
    "Gacrux": "mature",
    "Iapetus": "clear",
    "Kore": "firm",
    "Laomedeia": "upbeat",
    "Leda": "youthful",
    "Orus": "firm",
    "Puck": "upbeat",
    "Pulcherrima": "forward",
    "Rasalgethi": "informative",
    "Sadachbia": "lively",
    "Sadaltager": "knowledgeable",
    "Schedar": "even",
    "Sulafat": "warm",
    "Umbriel": "easy-going",
    "Vindemiatrix": "gentle",
    "Zephyr": "bright",
    "Zubenelgenubi": "casual",
}

#: Lyria 3 on OpenRouter: a clip is about 30s for $0.04; a song follows
#: the length its prompt asks for, roughly (45s asked, 57s made), for
#: $0.08. Both answer with a 44.1kHz stereo MP3, in about 10s and 25s.
MUSIC_MODELS = {
    "clip": "google/lyria-3-clip-preview",
    "song": "google/lyria-3-pro-preview",
}
#: Audio first: asked for ["text", "audio"], Lyria answers with its text
#: alone, and an empty stop.
MUSIC_MODALITIES = ["audio", "text"]

# Gemini speech arrives as raw 16-bit PCM; the response's content type
# names the rate and channels, and these are what it said when measured.
_PCM_RATE = 24_000
_PCM_CHANNELS = 1

IMAGE_TIMEOUT = 150.0
SPEECH_TIMEOUT = 120.0
MUSIC_TIMEOUT = 150.0
#: The most items one list call generates at once.
MAX_CONCURRENT = 8

_OFF = ("0", "false", "no", "off")


def make_client(
    api_key: str, *, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """An HTTP client for OpenRouter, to share across ``Media`` objects."""
    return httpx.Client(
        transport=transport,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/ashenfad/nontainer-studio",
            "X-Title": "nontainer-studio",
        },
    )


def media_enabled() -> bool:
    """Whether agent sessions get ``media``: an OpenRouter key is set
    and ``NONTAINER_STUDIO_MEDIA`` doesn't turn it off."""
    if not os.getenv("OPENROUTER_API_KEY"):
        return False
    return os.getenv("NONTAINER_STUDIO_MEDIA", "").strip().lower() not in _OFF


class Media:
    """Generate images, speech and music into one session's workspace.

    Built before the workspace it writes to exists (a Python config is
    part of opening one), so the session binds it once open and unbinds
    it at close. The HTTP client is the caller's, so every session can
    share one connection pool rather than each holding its own."""

    def __init__(self, client: httpx.Client):
        self._client = client
        self._ws: Any = None

    def _bind(self, ws: Any) -> None:
        self._ws = ws

    def _unbind(self) -> None:
        self._ws = None

    # -- images ----------------------------------------------------------

    def image(
        self,
        prompt: str | list[dict],
        path: str | None = None,
        *,
        transparent: bool = False,
        aspect: str = "1:1",
        quality: str = "low",
        references: list[str] | None = None,
    ) -> dict | list[dict]:
        """Generate a PNG from ``prompt`` and write it to ``path``.

        ``transparent`` gives a real alpha channel. ``references`` are
        workspace images to work from: a character or style to keep, or
        an image to edit. Returns ``{"path", "width", "height", "alpha",
        "cost"}``, read from the file written.

        A list of dicts, each with this call's arguments by name, is
        generated concurrently and returns a list in the same order; one
        that fails holds ``{"path", "error"}`` in its slot."""
        if isinstance(prompt, (list, tuple)):
            return self._batch(self._image, prompt, "media.image")
        return self._single(
            self._image(
                prompt=prompt,
                path=path,
                transparent=transparent,
                aspect=aspect,
                quality=quality,
                references=references,
            )
        )

    def _image(
        self,
        prompt: Any = None,
        path: Any = None,
        transparent: bool = False,
        aspect: str = "1:1",
        quality: str = "low",
        references: Any = None,
    ) -> dict:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("media.image: the prompt is empty")
        dest = self._dest("media.image", path, ".png")
        if aspect not in IMAGE_ASPECTS:
            raise ValueError(
                f"media.image: aspect is one of {', '.join(IMAGE_ASPECTS)}"
            )
        if quality not in IMAGE_QUALITIES:
            raise ValueError(
                f"media.image: quality is one of {', '.join(IMAGE_QUALITIES)}"
            )
        body: dict[str, Any] = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "aspect_ratio": aspect,
            "quality": quality,
            "background": "transparent" if transparent else "opaque",
            "output_format": "png",
        }
        refs = [references] if isinstance(references, str) else list(references or [])
        if len(refs) > MAX_REFERENCES:
            raise ValueError(f"media.image: at most {MAX_REFERENCES} references")
        if refs:
            body["input_references"] = [self._reference(r) for r in refs]
        data = self._post_json("media.image", IMAGES_URL, body, IMAGE_TIMEOUT)
        try:
            png = base64.b64decode(data["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("media.image: the response held no image") from None
        width, height, alpha = _png_facts(png)
        return {
            "path": dest,
            "width": width,
            "height": height,
            "alpha": alpha,
            "cost": _cost(data),
            "_bytes": png,
        }

    def _reference(self, path: Any) -> dict:
        src = self._resolve("media.image", path)
        try:
            data = self._ws.files.fs.read(src)
        except FileNotFoundError:
            raise ValueError(f"media.image: no reference image at {src}") from None
        kind = {".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif"}.get(
            posixpath.splitext(src)[1].lower(), "png"
        )
        url = f"data:image/{kind};base64,{base64.b64encode(data).decode()}"
        return {"type": "image_url", "image_url": {"url": url}}

    # -- speech ----------------------------------------------------------

    def speech(
        self,
        text: str | list[dict],
        path: str | None = None,
        voice: str = DEFAULT_VOICE,
    ) -> dict | list[dict]:
        """Speak ``text`` in ``voice`` and write a WAV to ``path``.

        Bracketed direction in the text steers the delivery and is not
        spoken: ``"[whispers] It's here. [excited] It's really here!"``.
        Returns ``{"path", "seconds"}``.

        A list of dicts, each with this call's arguments by name, is
        spoken concurrently and returns a list in the same order; one
        that fails holds ``{"path", "error"}`` in its slot."""
        if isinstance(text, (list, tuple)):
            return self._batch(self._speech, text, "media.speech")
        return self._single(self._speech(text=text, path=path, voice=voice))

    def _speech(
        self, text: Any = None, path: Any = None, voice: Any = DEFAULT_VOICE
    ) -> dict:
        text = str(text or "").strip()
        if not text:
            raise ValueError("media.speech: the text is empty")
        dest = self._dest("media.speech", path, ".wav")
        if voice not in VOICES:
            raise ValueError(
                f"media.speech: no voice {voice!r}; the voices are " + ", ".join(VOICES)
            )
        r = self._post(
            "media.speech",
            SPEECH_URL,
            {
                "model": SPEECH_MODEL,
                "input": text,
                "voice": voice,
                "response_format": "pcm",
            },
            SPEECH_TIMEOUT,
        )
        pcm = r.content
        if not pcm:
            raise RuntimeError("media.speech: the response held no audio")
        rate, channels = _pcm_format(r.headers.get("content-type", ""))
        return {
            "path": dest,
            "seconds": round(len(pcm) / (2 * rate * channels), 2),
            "_bytes": _wav(pcm, rate, channels),
        }

    # -- music -----------------------------------------------------------

    def music(
        self,
        prompt: str | list[dict],
        path: str | None = None,
        length: str = "clip",
    ) -> dict | list[dict]:
        """Compose music from ``prompt`` and write an MP3 to ``path``.

        ``length="clip"`` is about 30 seconds; ``"song"`` follows the
        length the prompt asks for, roughly. The prompt names the genre,
        instruments, tempo and mood, and says "no vocals" for an
        instrumental. Returns ``{"path", "seconds", "lyrics", "cost"}``,
        where ``lyrics`` is ``[{"at", "line"}]``: when each sung line
        starts, in seconds, and empty for an instrumental.

        A list of dicts, each with this call's arguments by name, is
        composed concurrently and returns a list in the same order; one
        that fails holds ``{"path", "error"}`` in its slot."""
        if isinstance(prompt, (list, tuple)):
            return self._batch(self._music, prompt, "media.music")
        return self._single(self._music(prompt=prompt, path=path, length=length))

    def _music(
        self, prompt: Any = None, path: Any = None, length: Any = "clip"
    ) -> dict:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("media.music: the prompt is empty")
        dest = self._dest("media.music", path, ".mp3")
        if length not in MUSIC_MODELS:
            raise ValueError(f"media.music: length is one of {', '.join(MUSIC_MODELS)}")
        audio, text, usage = self._post_stream(
            "media.music",
            CHAT_URL,
            {
                "model": MUSIC_MODELS[length],
                "messages": [{"role": "user", "content": prompt}],
                "modalities": MUSIC_MODALITIES,
                "stream": True,
            },
            MUSIC_TIMEOUT,
        )
        if not audio:
            raise RuntimeError(
                "media.music: the model answered without music; reword the "
                "prompt and try again"
            )
        return {
            "path": dest,
            "seconds": _mp3_seconds(audio),
            "lyrics": _lyrics(text),
            "cost": _cost({"usage": usage}),
            "_bytes": audio,
        }

    # -- plumbing --------------------------------------------------------

    def _batch(self, one: Any, items: Any, label: str) -> list[dict]:
        """Generate concurrently, then write in order on this thread: the
        requests are what take the time, and the workspace takes its
        writes one at a time."""
        items = list(items)
        for item in items:
            if not isinstance(item, dict):
                raise TypeError(f"{label}: a list call takes dicts, got {item!r}")

        def attempt(item: dict) -> dict:
            try:
                return one(**item)
            except TypeError as e:
                return {"path": item.get("path"), "error": f"{label}: {e}"}
            except Exception as e:
                return {"path": item.get("path"), "error": str(e)}

        if not items:
            return []
        with ThreadPoolExecutor(min(MAX_CONCURRENT, len(items))) as pool:
            made = list(pool.map(attempt, items))
        return [self._write(m) for m in made]

    def _single(self, made: dict) -> dict:
        out = self._write(made)
        if "error" in out:
            raise RuntimeError(out["error"])
        return out

    def _write(self, made: dict) -> dict:
        """Put a generated file in the workspace; the facts without the
        bytes come back."""
        data = made.pop("_bytes", None)
        if data is None:
            return made
        path = made["path"]
        try:
            fs = self._ws.files.fs
            fs.makedirs(posixpath.dirname(path), exist_ok=True)
            fs.write(path, data)
        except Exception as e:
            return {"path": path, "error": f"writing the file failed: {e}"}
        return made

    def _resolve(self, label: str, path: Any) -> str:
        """A workspace path: relative ones are taken from the workspace
        root (not the agent's cwd, which the host does not share), and
        nothing may land outside it."""
        if self._ws is None:
            raise RuntimeError(f"{label}: not bound to a workspace")
        root = self._ws.root
        raw = str(path or "").strip()
        if not raw:
            raise ValueError(f"{label}: say where to write it (a path)")
        full = posixpath.normpath(raw if raw.startswith("/") else f"{root}/{raw}")
        if full != root and not full.startswith(root + "/"):
            raise ValueError(f"{label}: {raw!r} is outside {root}")
        return full

    def _dest(self, label: str, path: Any, ext: str) -> str:
        dest = self._resolve(label, path)
        if not dest.lower().endswith(ext):
            raise ValueError(
                f"{label}: the file is {ext[1:].upper()}, so name it *{ext}"
            )
        return dest

    def _post(self, label: str, url: str, body: dict, timeout: float) -> httpx.Response:
        try:
            r = self._client.post(url, json=body, timeout=timeout)
        except httpx.TimeoutException:
            raise RuntimeError(f"{label}: no answer within {timeout:.0f}s") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"{label}: network error: {e}") from None
        if r.status_code != 200:
            raise RuntimeError(f"{label}: HTTP {r.status_code}: {r.text[:300]}")
        return r

    def _post_stream(
        self, label: str, url: str, body: dict, timeout: float
    ) -> tuple[bytes, str, dict]:
        """A streamed chat completion, gathered: its audio, its text and
        its usage. Audio output is only offered streamed."""
        audio: list[str] = []
        text: list[str] = []
        usage: dict = {}
        try:
            with self._client.stream("POST", url, json=body, timeout=timeout) as r:
                if r.status_code != 200:
                    r.read()
                    raise RuntimeError(f"{label}: HTTP {r.status_code}: {r.text[:300]}")
                for line in r.iter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        event = json.loads(line[6:])
                    except ValueError:
                        continue
                    if event.get("error"):
                        err = event["error"]
                        msg = err.get("message") if isinstance(err, dict) else err
                        raise RuntimeError(f"{label}: {msg}")
                    usage = event.get("usage") or usage
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text.append(delta["content"])
                        data = (delta.get("audio") or {}).get("data")
                        if data:
                            audio.append(data)
        except httpx.TimeoutException:
            raise RuntimeError(f"{label}: no answer within {timeout:.0f}s") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"{label}: network error: {e}") from None
        try:
            data = base64.b64decode("".join(audio))
        except ValueError:
            raise RuntimeError(f"{label}: the audio could not be decoded") from None
        return data, "".join(text), usage

    def _post_json(self, label: str, url: str, body: dict, timeout: float) -> dict:
        r = self._post(label, url, body, timeout)
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"{label}: the response was not JSON") from None


def _png_facts(png: bytes) -> tuple[int | None, int | None, bool | None]:
    """Width, height and whether it has an alpha channel, from a PNG's
    IHDR chunk. Colour types 4 and 6 carry alpha."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 26:
        return None, None, None
    width, height = struct.unpack(">II", png[16:24])
    return width, height, png[25] in (4, 6)


def _pcm_format(content_type: str) -> tuple[int, int]:
    """The rate and channel count an ``audio/pcm;rate=…;channels=…``
    content type names, defaulting to what was measured."""
    rate, channels = _PCM_RATE, _PCM_CHANNELS
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key == "rate" and value.isdigit():
            rate = int(value)
        elif key == "channels" and value.isdigit():
            channels = int(value)
    return rate, channels


def _wav(pcm: bytes, rate: int, channels: int) -> bytes:
    """16-bit PCM wrapped in a WAV header, which a browser plays."""
    block = channels * 2
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


# MPEG audio, by version bits: bitrates (kbps) for Layer III by index,
# sample rates by index, and samples in a frame.
_MP3_BITRATES = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}


def _mp3_seconds(mp3: bytes) -> float | None:
    """An MP3's length, from walking its Layer III frames: each holds
    1152 samples (576 below MPEG-1). A leading ID3v2 tag is skipped, and
    so is an encoder's Xing or Info frame, which holds no sound."""
    pos = 0
    if mp3[:3] == b"ID3" and len(mp3) >= 10:
        size = 0
        for b in mp3[6:10]:  # syncsafe: seven bits a byte
            size = (size << 7) | (b & 0x7F)
        pos = 10 + size
    samples = 0
    rate = None
    first = True
    while pos + 4 <= len(mp3):
        h = int.from_bytes(mp3[pos : pos + 4], "big")
        version = (h >> 19) & 3
        layer = (h >> 17) & 3
        bitrate_i = (h >> 12) & 0xF
        rate_i = (h >> 10) & 3
        if (h >> 21) != 0x7FF or version == 1 or layer != 1:
            if samples:
                break  # the sound ends here: a trailing tag or padding
            pos += 1  # not a frame yet: look for the first one
            continue
        if bitrate_i in (0, 15) or rate_i == 3:
            break
        kbps = _MP3_BITRATES[3 if version == 3 else 2][bitrate_i]
        rate = _MP3_RATES[version][rate_i]
        per_frame = 1152 if version == 3 else 576
        size = per_frame // 8 * kbps * 1000 // rate + ((h >> 9) & 1)
        frame = mp3[pos : pos + size]
        if not (first and (b"Xing" in frame[:64] or b"Info" in frame[:64])):
            samples += per_frame
        first = False
        pos += size
    if not samples or rate is None:
        return None
    return round(samples / rate, 2)


_LYRIC = re.compile(r"\[(\d+(?:\.\d+)?):\]\s*(.*\S)")


def _lyrics(text: str) -> list[dict]:
    """The sung lines in Lyria's text, ``[12.5:] a line`` each, as
    ``{"at", "line"}``. An instrumental's text holds no such lines (it
    says ``<instrumental>``, or marks sections like ``[[A0]]``)."""
    return [
        {"at": float(m.group(1)), "line": m.group(2)}
        for m in _LYRIC.finditer(text or "")
    ]


def _cost(data: dict) -> float | None:
    """What the call cost upstream, in dollars. A bring-your-own-key
    call bills the provider, so OpenRouter's own figure reads 0 and the
    upstream cost is the real one."""
    usage = data.get("usage") or {}
    upstream = (usage.get("cost_details") or {}).get("upstream_inference_cost")
    return upstream if upstream else usage.get("cost")
