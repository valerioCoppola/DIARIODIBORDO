"""Parti della pipeline senza database: scelta del modello, testo, segmenti, decodifica audio."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.config import Settings
from app.pipeline.chunking import chunk_text
from app.pipeline.text import best_supporting_sentence, sentences, slugify
from app.pipeline.transcribe import FasterWhisperTranscriber, Segment, assemble, choose


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize("cuda,vram,expected", [
    (1, 24000, ("large-v3", "cuda", "float16", True)),
    (1, 12000, ("large-v3", "cuda", "float16", False)),
    (1, 8000, ("large-v3", "cuda", "int8_float16", False)),
    (1, 4000, ("large-v3-turbo", "cuda", "int8_float16", False)),
    (0, None, ("large-v3-turbo", "cpu", "int8", False)),
])
def test_model_choice(cuda, vram, expected):
    c = choose(_settings(), cuda, vram)
    assert (c.model, c.device, c.compute_type, c.keep_loaded) == expected


def test_model_choice_respects_explicit_settings():
    c = choose(_settings(whisper_model="medium", whisper_device="cpu", whisper_compute_type="int8"), 1, 24000)
    assert (c.model, c.device, c.compute_type) == ("medium", "cpu", "int8")


def test_assemble_keeps_positions_and_paragraphs():
    text, segs = assemble([Segment(0, 2, " Ciao a tutti."), Segment(2.1, 4, " Seconda frase."),
                           Segment(9, 11, " Dopo una pausa lunga."), Segment(11, 12, "  ")])
    assert text == "Ciao a tutti. Seconda frase.\n\nDopo una pausa lunga."
    assert [text[s["char_start"]:s["char_end"]] for s in segs] == ["Ciao a tutti.", "Seconda frase.",
                                                                     "Dopo una pausa lunga."]


class _Info:
    language, language_probability, duration, duration_after_vad = "it", 0.98, 3.0, 2.5


class _FakeModel:
    def __init__(self, fail_with: Exception | None = None):
        self.fail_with = fail_with

    def transcribe(self, path, **kw):
        if self.fail_with:
            raise self.fail_with
        assert kw["vad_filter"] is True and kw["language"] == "it" and kw["condition_on_previous_text"] is False
        return iter([Segment(0, 1.5, " Prova di trascrizione.")]), _Info()


@pytest.mark.parametrize("errore", [
    "Library libcublas.so.12 is not found or cannot be loaded",          # NVIDIA senza librerie CUDA
    "HIP failed with error hipErrorOutOfMemory",                         # AMD senza memoria video
    "libamdhip64.so.7: cannot open shared object file",                  # AMD senza librerie ROCm
])
def test_gpu_failure_falls_back_to_cpu(tmp_path, errore):
    created = []

    def factory(name, device, compute_type):
        created.append((name, device, compute_type))
        if device == "cuda":
            return _FakeModel(RuntimeError(errore))
        return _FakeModel()

    f = tmp_path / "a.webm"
    f.write_bytes(b"x")
    t = FasterWhisperTranscriber(_settings(), model_factory=factory, cuda_devices=1, vram_mb=8000)
    r = t.transcribe(f)
    assert created == [("large-v3", "cuda", "int8_float16"), ("large-v3-turbo", "cpu", "int8")]
    assert r.text == "Prova di trascrizione." and r.device == "cpu" and r.audio_seconds == 3.0
    assert t.describe()["dispositivo"] == "cpu"               # resta sulla CPU per i vocali successivi


def test_sentences_and_support():
    text = "Primo punto qui.  Devo chiamare Anna domani!\n\nUltima cosa"
    assert [s.text(text) for s in sentences(text)] == ["Primo punto qui.", "Devo chiamare Anna domani!", "Ultima cosa"]
    sp = best_supporting_sentence("chiamare Anna", text)
    assert sp and sp.text(text) == "Devo chiamare Anna domani!"
    assert best_supporting_sentence("prenotare il volo per Tokyo", text) is None


def test_slugify():
    assert slugify("Città  e Perché!") == "citta-e-perche"
    assert slugify("###") == ""


def test_chunks_cover_text_with_exact_offsets():
    text = " ".join(f"Frase numero {i} con un po' di contenuto per riempire lo spazio." for i in range(80))
    chunks = chunk_text(text)
    assert len(chunks) > 3
    assert all(text[c.char_start:c.char_end] == c.content and len(c.content) <= 1600 for c in chunks)
    assert chunks[0].char_start == 0 and chunks[-1].char_end == len(text)
    for a, b in zip(chunks, chunks[1:]):
        assert b.char_start <= a.char_end                       # sovrapposti o contigui: nessun buco


def test_chunks_of_text_without_punctuation():
    text = "parola " * 1000
    chunks = chunk_text(text.strip())
    assert all(len(c.content) <= 1600 for c in chunks) and chunks[-1].char_end == len(text.strip())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="serve ffmpeg")
@pytest.mark.parametrize("ext,codec", [("webm", "libopus"), ("m4a", "aac")])
def test_decode_phone_formats(tmp_path, ext, codec):
    """I formati di Chrome/Android (WebM/Opus) e di Safari/iPhone (MP4/AAC) si decodificano."""
    from faster_whisper import decode_audio

    f = tmp_path / f"voce.{ext}"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-ac", "1", "-c:a", codec, "-b:a", "48k", str(f)], check=True)
    audio = decode_audio(str(f), sampling_rate=16000)
    assert abs(len(audio) / 16000 - 3.0) < 0.2


@pytest.mark.parametrize("vram,primo", [(24000, "mistral-small3.2:24b"), (16304, "ministral-3:14b"),
                                        (8188, "ministral-3:8b"), (None, "ministral-3:3b")])
def test_analysis_candidates_prefer_mistral(vram, primo):
    from app.cli import candidati_analisi

    assert candidati_analisi(vram)[0] == primo
