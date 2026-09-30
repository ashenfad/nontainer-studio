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

    m = Media("key", transport=httpx.MockTransport(respond))
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
