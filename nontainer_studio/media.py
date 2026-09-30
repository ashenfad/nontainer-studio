"""The ``media`` host object: images and speech through OpenRouter.

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
import os
import posixpath
import struct
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

IMAGES_URL = "https://openrouter.ai/api/v1/images"
SPEECH_URL = "https://openrouter.ai/api/v1/audio/speech"

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

# Gemini speech arrives as raw 16-bit PCM; the response's content type
# names the rate and channels, and these are what it said when measured.
_PCM_RATE = 24_000
_PCM_CHANNELS = 1

IMAGE_TIMEOUT = 150.0
SPEECH_TIMEOUT = 120.0
#: The most items one list call generates at once.
MAX_CONCURRENT = 8

_OFF = ("0", "false", "no", "off")


def media_enabled() -> bool:
    """Whether agent sessions get ``media``: an OpenRouter key is set
    and ``NONTAINER_STUDIO_MEDIA`` doesn't turn it off."""
    if not os.getenv("OPENROUTER_API_KEY"):
        return False
    return os.getenv("NONTAINER_STUDIO_MEDIA", "").strip().lower() not in _OFF


class Media:
    """Generate images and speech into one session's workspace.

    Built before the workspace it writes to exists (a Python config is
    part of opening one), so the session binds it once open."""

    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/ashenfad/nontainer-studio",
                "X-Title": "nontainer-studio",
            },
        )
        self._ws: Any = None

    def _bind(self, ws: Any) -> None:
        self._ws = ws

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


def _cost(data: dict) -> float | None:
    """What the call cost upstream, in dollars. A bring-your-own-key
    call bills the provider, so OpenRouter's own figure reads 0 and the
    upstream cost is the real one."""
    usage = data.get("usage") or {}
    upstream = (usage.get("cost_details") or {}).get("upstream_inference_cost")
    return upstream if upstream else usage.get("cost")
