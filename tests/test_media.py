"""The `media` host object, against a stubbed OpenRouter and a stand-in
workspace."""

import base64
import json
import struct

import httpx
import pytest

from nontainer_studio import media as media_mod
from nontainer_studio.media import Media, media_enabled


class _Fs:
    def __init__(self):
        self.files = {}

    def makedirs(self, path, exist_ok=False):
        pass

    def write(self, path, data):
        self.files[path] = bytes(data)

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class _Ws:
    root = "/workspace"

    def __init__(self):
        self.files = type("Files", (), {})()
        self.files.fs = _Fs()


def _png(width, height):
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0" * 4


def _media(handler):
    """A bound Media over a handler that sees (url, body) and returns an
    httpx.Response."""
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append((str(request.url), body))
        return handler(str(request.url), body)

    m = Media(media_mod.make_client("key", transport=httpx.MockTransport(respond)))
    ws = _Ws()
    m._bind(ws)
    return m, ws, sent


def _image_ok(url, body):
    png = _png(1536, 1024)
    return httpx.Response(
        200,
        json={
            "data": [{"b64_json": base64.b64encode(png).decode()}],
            "usage": {"cost": 0, "cost_details": {"upstream_inference_cost": 0.006}},
        },
    )


def _speech_ok(url, body):
    return httpx.Response(
        200,
        content=b"\1\0" * 48_000,  # 2s at 24kHz mono 16-bit
        headers={"content-type": "audio/pcm;rate=24000;channels=1"},
    )


def _mp3(frames, *, info=True):
    """An MPEG-1 Layer III stream at 44.1kHz and 128kbps behind an ID3v2
    tag: ``frames`` frames of sound, after an encoder's Info frame."""
    header = (0x7FF << 21 | 3 << 19 | 1 << 17 | 1 << 16 | 9 << 12).to_bytes(4, "big")
    size = 144 * 128_000 // 44_100  # 417 bytes a frame
    frame = header + b"\0" * (size - 4)
    tag = b"ID3\x03\x00\x00" + bytes([0, 0, 0, 20]) + b"\0" * 20
    info = header + b"\0" * 32 + b"Info" + b"\0" * (size - 40) if info else b""
    return tag + info + frame * frames


def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _music_ok(url, body, *, text="<instrumental>", mp3=None):
    data = base64.b64encode(mp3 if mp3 is not None else _mp3(1000)).decode()
    half = len(data) // 2
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse(
            {"choices": [{"delta": {"role": "assistant", "content": text}}]},
            {"choices": [{"delta": {"audio": {"data": data[:half]}}}]},
            {"choices": [{"delta": {"audio": {"data": data[half:]}}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"cost": 0.04},
            },
        ),
    )


def test_an_image_is_written_and_described():
    m, ws, sent = _media(_image_ok)
    out = m.image("a robot", "app/img/robot.png", transparent=True, aspect="3:2")
    assert out == {
        "path": "/workspace/app/img/robot.png",
        "width": 1536,
        "height": 1024,
        "alpha": True,
        "cost": 0.006,
    }
    assert ws.files.fs.read("/workspace/app/img/robot.png").startswith(b"\x89PNG")
    url, body = sent[0]
    assert url == media_mod.IMAGES_URL
    assert body["model"] == media_mod.IMAGE_MODEL
    assert body["background"] == "transparent"
    assert body["aspect_ratio"] == "3:2"
    assert body["quality"] == "low"
    assert "input_references" not in body

    m.image("a robot", "/workspace/b.png")
    assert sent[1][1]["background"] == "opaque"


def test_references_are_read_from_the_workspace():
    m, ws, sent = _media(_image_ok)
    ws.files.fs.write("/workspace/app/img/robot.png", b"PNGBYTES")
    ws.files.fs.write("/workspace/style.jpg", b"JPGBYTES")
    m.image(
        "the robot, waving",
        "app/img/wave.png",
        references=["app/img/robot.png", "style.jpg"],
    )
    refs = sent[0][1]["input_references"]
    assert (
        refs[0]["image_url"]["url"]
        == "data:image/png;base64," + base64.b64encode(b"PNGBYTES").decode()
    )
    assert refs[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with pytest.raises(
        ValueError, match="no reference image at /workspace/missing.png"
    ):
        m.image("x", "a.png", references=["missing.png"])


def test_speech_is_wrapped_as_wav_and_timed():
    m, ws, sent = _media(_speech_ok)
    out = m.speech("[whispers] hello", "app/audio/hi.wav", voice="Charon")
    assert out == {"path": "/workspace/app/audio/hi.wav", "seconds": 2.0}
    wav = ws.files.fs.read("/workspace/app/audio/hi.wav")
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    channels, rate = struct.unpack("<HI", wav[22:28])
    assert (channels, rate) == (1, 24_000)
    assert len(wav) == 44 + 96_000
    url, body = sent[0]
    assert url == media_mod.SPEECH_URL
    assert body == {
        "model": media_mod.SPEECH_MODEL,
        "input": "[whispers] hello",
        "voice": "Charon",
        "response_format": "pcm",
    }


def test_music_is_written_timed_and_its_lyrics_read():
    lyrics = "[0.0:] Made a little change today\n[2.2:] Saved it in a special way\n"

    def handler(url, body):
        # the pro model marked an instrumental's sections, not its lines
        clip = body["model"].endswith("clip-preview")
        return _music_ok(url, body, text=lyrics if clip else "[[A0]]\n[[B1]]")

    m, ws, sent = _media(handler)
    out = m.music("indie pop about versioning, female vocals", "app/audio/song.mp3")
    assert out == {
        "path": "/workspace/app/audio/song.mp3",
        "seconds": 26.12,  # 1000 frames of 1152 samples at 44.1kHz
        "lyrics": [
            {"at": 0.0, "line": "Made a little change today"},
            {"at": 2.2, "line": "Saved it in a special way"},
        ],
        "cost": 0.04,
    }
    assert ws.files.fs.read("/workspace/app/audio/song.mp3") == _mp3(1000)
    url, body = sent[0]
    assert url == media_mod.CHAT_URL
    assert body == {
        "model": "google/lyria-3-clip-preview",
        "messages": [
            {"role": "user", "content": "indie pop about versioning, female vocals"}
        ],
        # audio first: the other order gets text and no music
        "modalities": ["audio", "text"],
        "stream": True,
    }

    out = m.music("an ambient bed, no vocals", "bed.mp3", length="song")
    assert out["lyrics"] == []
    assert sent[1][1]["model"] == "google/lyria-3-pro-preview"


def test_an_mp3s_length_is_read_from_its_frames():
    assert media_mod._mp3_seconds(_mp3(441)) == 11.52
    assert media_mod._mp3_seconds(_mp3(441, info=False)) == 11.52
    assert media_mod._mp3_seconds(_mp3(441) + b"TAG" + b"\0" * 125) == 11.52
    assert media_mod._mp3_seconds(b"not an mp3") is None


def test_music_that_does_not_come_is_an_error():
    """Lyria can stop with its text and no audio: a 200 that would
    otherwise write an empty file."""
    m, ws, _ = _media(lambda url, body: _music_ok(url, body, mp3=b""))
    with pytest.raises(RuntimeError, match="answered without music"):
        m.music("x", "a.mp3")

    def broken(url, body):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"error": {"message": "Provider returned error"}}),
        )

    m, ws, _ = _media(broken)
    with pytest.raises(RuntimeError, match="media.music: Provider returned error"):
        m.music("x", "a.mp3")
    m, ws, _ = _media(
        lambda url, body: httpx.Response(402, text="Insufficient credits")
    )
    with pytest.raises(RuntimeError, match="media.music: HTTP 402: Insufficient"):
        m.music("x", "a.mp3")
    assert ws.files.fs.files == {}


def test_a_list_is_made_together_and_written_in_order():
    """One failing item holds its error in its slot; the rest, which were
    paid for, are written."""

    def handler(url, body):
        if "fail" in body["input"]:
            return httpx.Response(500, text="upstream broke")
        return _speech_ok(url, body)

    m, ws, _ = _media(handler)
    out = m.speech(
        [
            {"text": "one", "path": "a/1.wav"},
            {"text": "fail", "path": "a/2.wav"},
            {"text": "three", "path": "a/3.wav", "voice": "Puck"},
            {"text": "four", "path": "a/4.wav", "voice": "Nobody"},
        ]
    )
    assert out[0] == {"path": "/workspace/a/1.wav", "seconds": 2.0}
    assert out[1]["path"] == "a/2.wav" and "HTTP 500" in out[1]["error"]
    assert out[2]["path"] == "/workspace/a/3.wav"
    assert "no voice 'Nobody'" in out[3]["error"]
    assert sorted(ws.files.fs.files) == ["/workspace/a/1.wav", "/workspace/a/3.wav"]
    assert m.image([]) == []


def test_mistakes_are_refused_before_anything_is_spent():
    m, ws, sent = _media(_image_ok)
    for call, match in [
        (lambda: m.image("x", "../escape.png"), "outside /workspace"),
        (lambda: m.image("x", "/etc/x.png"), "outside /workspace"),
        (lambda: m.image("x", "a.jpg"), r"name it \*\.png"),
        (lambda: m.image("x", None), "say where to write it"),
        (lambda: m.image("", "a.png"), "the prompt is empty"),
        (lambda: m.image("x", "a.png", aspect="5:4"), "aspect is one of"),
        (lambda: m.image("x", "a.png", quality="ultra"), "quality is one of"),
        (lambda: m.speech("hi", "a.mp3"), r"name it \*\.wav"),
        (lambda: m.speech("hi", "a.wav", voice="Nope"), "no voice 'Nope'"),
        (lambda: m.music("x", "a.wav"), r"name it \*\.mp3"),
        (lambda: m.music("x", "a.mp3", length="album"), "length is one of"),
        (lambda: m.music(" ", "a.mp3"), "the prompt is empty"),
    ]:
        with pytest.raises(ValueError, match=match):
            call()
    assert sent == []
    with pytest.raises(TypeError, match="a list call takes dicts"):
        m.speech(["just text"])


def test_a_failed_single_call_raises():
    m, _, _ = _media(lambda url, body: httpx.Response(401, text="No auth credentials"))
    with pytest.raises(RuntimeError, match="media.image: HTTP 401: No auth"):
        m.image("x", "a.png")


def test_media_needs_a_key_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NONTAINER_STUDIO_MEDIA", raising=False)
    assert not media_enabled()
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert media_enabled()
    monkeypatch.setenv("NONTAINER_STUDIO_MEDIA", "off")
    assert not media_enabled()
