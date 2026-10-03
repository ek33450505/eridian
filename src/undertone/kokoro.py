"""Kokoro-82M ONNX voice: phoneme string -> 16 kHz mono int16 PCM bytes.

This module never sees raw text. Phonemes come from `speaker.espeak_phonemes`,
which is the choke point that scrubs the wake word and `[[ ]]` before espeak-ng.

Local-first: the onnxruntime macOS wheel embeds Microsoft's 1DS telemetry
client (mobile.events.data.microsoft.com/OneCollector), so ORT_DISABLE_TELEMETRY
is FORCED to "1" here, before onnxruntime is ever imported. HF_HUB_OFFLINE is
setdefault'd to "1" like `stt.py`; `fetch_model` is the one place that unsets it.

Spike facts (measured): the repo's model_fp16.onnx produced NaN audio on 2/8
sentences on CPU, fp32 produced 0 NaN in 200; inference costs ~0.05 s plus
0.085 s per second of audio, so the first piece of a sentence is kept short;
every export starts with ~0.36-0.42 s of near-silence that must be trimmed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import TYPE_CHECKING, Any, Callable

os.environ["ORT_DISABLE_TELEMETRY"] = "1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np

MODEL_REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
MODEL_REVISION = "1939ad2a8e416c0acfeecc08a694d14ef25f2231"
MODEL_FILE = "onnx/model.onnx"
TOKENIZER_FILE = "tokenizer.json"
DEFAULT_VOICE = "am_michael"
VOICE_ENV = "UNDERTONE_KOKORO_VOICE"

FIRST_PIECE_TOKENS = 50
PIECE_TOKENS = 200
MAX_TOKENS = 510

MODEL_RATE = 24000
OUT_RATE = 16000
STYLE_DIM = 256
WARM_UP_PHONEMES = "ɹˈɛdi."

# SHA-256 of each file at the pinned revision, from the HF tree API's LFS oids
# (tokenizer confirmed via its git blob id), derived 2026-10-02. The revision pin
# alone is not an integrity control, so files from the HF cache are re-hashed.
MODEL_SHA256 = "8fbea51ea711f2af382e88c833d9e288c6dc82ce5e98421ea61c058ce21a34cb"
TOKENIZER_SHA256 = "77a02c8e164413299b4b4c403b14f8e0e1c1b727db4d46a09d6327b861060a34"
VOICE_SHA256 = {
    "af": "a4f11d9d055a12bfa0db2668a3e4f0ef8fd1f1ccca69494479718e44dbf9e41a",
    "af_alloy": "c4a6b876047fd7fb472edf4ebd63cfac7c3b958a7cae7c106e8f038ca6308c45",
    "af_aoede": "4a004c33430762e2461eedb2013fad808ef4ab3121f5300f554476caf58d8361",
    "af_bella": "f69d836209b78eb8c66e75e3cda491e26ea838a3674257e9d4e5703cbaf55c8b",
    "af_heart": "d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b",
    "af_jessica": "a240a5e3c15b43563d6e923bdca8ef5613a23471d9b77653694012435df23bd8",
    "af_kore": "9be5221b6a941c04b561959b8ff0b06e809444dcc4ab7e75a7b23606f691819e",
    "af_nicole": "cd2191ab31b914ed7b318416b0e4440fdf392ddad9106a060819aa600a64f59a",
    "af_nova": "18778272caa0d0eebaea251c35fd635f038434f9eee5e691d02a174bd328414f",
    "af_river": "00a2bcf82b1d86e8f19902ede58c65ccf6c0e43b44b7d74fad54e5d8933c9c30",
    "af_sarah": "4409fbc125afabacc615d94db5398d847006a737b0247d6892b7a9a0007a2f0a",
    "af_sky": "4435255c9744f3f31659e0d714ab7689bf65d9e77ec1cce060f083912614f0b9",
    "am_adam": "162b035ed91cfc48b6046982184c645f72edcdd1b82843347f605d7bf7b15716",
    "am_echo": "3968b92c3c4cd1c4416dbded36c13eaa388a90d5788d02a13e4d781f5f8cf3c3",
    "am_eric": "e8b5be17edd1e3636901ce7598baafe2dc8dd8ff707a0c23bf9e461add7e2832",
    "am_fenrir": "c27989f741f7ee34d273a39d8a595cc0837d35f5ced9a29b7cc162614616df43",
    "am_liam": "52403be32fd047c6a44517cb0bcd6b134f2a18baa73e70ef41651e0eab921ade",
    "am_michael": "1d1f21dd8da39c30705cd4c75d039d265e9bc4a2a93ed09bc9e1b1225eb95ba1",
    "am_onyx": "da5d135b424164916d75a68ffb4c2abce3d7d5ccc82dd1ee6cf447ce286145e6",
    "am_puck": "fcf73c989033e9233e0b98713eca600c8c74dcc1614b37009d5450ff4a2274a0",
    "am_santa": "61150cf726ab6c5ed7a99f90a304f91f5a72c00c592e89ec94e5df11c319227a",
    "bf_alice": "08afa6ba24da61ea5e8efa139e5aadc938d83f0a6da5a900adaf763ac1da5573",
    "bf_emma": "669fe0647f9dd04fcab92f1439a40eeb4c8b4ab1f82e4996fe3d918ce4a63b73",
    "bf_isabella": "3754352c4aaa46d17f27654ab7518d65b62ad6163a0f55a5f4330c2da2c4e94f",
    "bf_lily": "5e0ee32ebe64a467124976b14e69590746f1c4ce41a12b587a50c862edfea335",
    "bm_daniel": "6b3194bbceffb746733cbc22c8f593dd44e401a71d53895a2dca891bc595a1e8",
    "bm_fable": "f889083196807b4adb15e9204252165f503b8d33d3982e681c52443c49d798f1",
    "bm_george": "c4b235a4c1f2cd3b939fed08b899ce9385638b763f7b73a59616c4fc9bd6c9bc",
    "bm_lewis": "b8f671cef828c30e66fdf0b0756a76bba58f6bb3398cbbf27058642acbcedb97",
    "ef_dora": "f66ec66bd295acb18372e37008533a9a3228483ccd294e7538d5d9294ac9a532",
    "em_alex": "27809e9eafdcbcfff90a3016c697568676531de2a2c39cee29c96c7bd6b83e95",
    "em_santa": "ad43b774e1ca24d05c6161297d8aeb770ac3d29bb95daf516727af5f7d543683",
    "ff_siwis": "a35f5675ad08948e326ae75fd0ea16ba5d0042e4f76b5f3d1df77d0a48c54861",
    "hf_alpha": "040be6a4425411cc01fda5fd06693c76bfa78572632852bc8cda9c99232ffb56",
    "hf_beta": "cd83ae0bb9b2e4e4fb92b4973bd8d1822ca0036d3c498bf4fc89aa8e33917cc7",
    "hm_omega": "b02d9222d9ed00ce26b302173a862c2c93f96cc40b5c422b8d14910b9ff34137",
    "hm_psi": "644daf88ba8aeb7bd08950bbdcd4453bb280864e49dc4df93fabc6be32e03f37",
    "if_sara": "409b69248798fcdc2542330c76953d230710f19b057e59cb82fdc3c4cf71265c",
    "im_nicola": "bc578e510d52a96d6940d46f12e96d7b3df00905dbea075113226d100e6e1ab0",
    "jf_alpha": "56b479360aad9f367aeb8cef908f9201cf48b4555e488c5f4590c9dfcd978bb6",
    "jf_gongitsune": "0f1181f3772d27b7c12aaf4bcd71e31b186c4146e330d074a3dc64ee392af396",
    "jf_nezumi": "13cb71eebb0b48739d444558322aa35a8c9a489b80e1e631f14d2e6aea93026b",
    "jf_tebukuro": "29c6c0561b4288d59639677bebe7533c919743d5ea68d0d2ae992644beea6696",
    "jm_kumo": "09e959d239724c734d65661f06f14cdabcddfd476bfaaad905a937099ae9e64f",
    "pf_dora": "3da7b5b2d91847ebf5646f57631af6ececae3c29a89cd300f06edf9aa6cfe9ee",
    "pm_alex": "0175c753f59c54e7fd5a995bedef0c5ff2fb67e0043dd3dcb2ae74ec2acbeb2a",
    "pm_santa": "8b012db3185778afe2e45a62cbad69db73021774fe68dda634bcc748a982eede",
    "zf_xiaobei": "5dde6e1c9c4f12c8b327bc29c0cee361a23b52b952c04636858ba637ec66e640",
    "zf_xiaoni": "08892b62a39af0a615cd0581238db7e19e44c578e8fa0bfd0e586e93327d9cba",
    "zf_xiaoxiao": "03adb5d5e3ddd88b047954e974e651cb0a4b524c985057e5d872e962c7be1169",
    "zf_xiaoyi": "bc1555c5c486099196ac254bae5e0bb543c121952a3092f50b7d8724f1bc36b3",
    "zm_yunjian": "de48a00bdbf3649f07162269a2b6e0513604389bfac8a2e6c75cb34b323ad6fa",
    "zm_yunxi": "7243892fb4e560d47014090ddf010f8b8b790f3c6b029ff82b2ac06aa4e27c8b",
    "zm_yunxia": "6b2b8fc15b3df19a368daebe5c581c7fabf433ee5b8a17ffd6b3d723cff8936d",
    "zm_yunyang": "261e2c89470534dbbcb8fd98b8fdc495ec94063d9bb6c8277f7be43cccba3f42",
}

_VOICE_RE = re.compile(r"[a-z]{2}_[a-z]+")
_LONG_PAUSE = ".!?…"
_SHORT_PAUSE = ",;:—"
_LONG_PAUSE_S = 0.25
_SHORT_PAUSE_S = 0.1
_SILENCE_FRACTION = 0.01  # -40 dB relative to the clip's peak
_LEAD_S = 0.02
_TAIL_S = 0.05


class KokoroUnavailable(RuntimeError):
    """Raised when the Kokoro model or voice can't be loaded."""


def fetch_model() -> None:
    """Download the pinned model, tokenizer and voices once (used by `undertone models fetch`)."""
    previous = os.environ.pop("HF_HUB_OFFLINE", None)
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            MODEL_REPO,
            revision=MODEL_REVISION,
            allow_patterns=[MODEL_FILE, TOKENIZER_FILE, "voices/*.bin"],
        )
    finally:
        if previous is not None:
            os.environ["HF_HUB_OFFLINE"] = previous


def _check_voice_name(voice: str) -> str:
    # The name becomes part of a file path, so it is validated, never sanitized.
    if not _VOICE_RE.fullmatch(voice):
        raise KokoroUnavailable(
            f"undertone: invalid Kokoro voice name {voice!r} (expected e.g. 'am_michael')"
        )
    return voice


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def model_paths(voice: str) -> tuple[str, str, str]:
    """Return (model, tokenizer, voice file) from the local HF cache only, hash-verified."""
    _check_voice_name(voice)
    voice_sha = VOICE_SHA256.get(voice)
    if voice_sha is None:
        raise KokoroUnavailable(f"undertone: unknown Kokoro voice {voice!r}")
    from huggingface_hub import hf_hub_download

    wanted = (
        (MODEL_FILE, MODEL_SHA256),
        (TOKENIZER_FILE, TOKENIZER_SHA256),
        (f"voices/{voice}.bin", voice_sha),
    )
    paths: list[str] = []
    for name, sha in wanted:
        try:
            path = hf_hub_download(
                MODEL_REPO, name, revision=MODEL_REVISION, local_files_only=True
            )
            ok = _sha256_file(path) == sha
        except Exception as exc:  # noqa: BLE001 - any cache miss means "not fetched"
            raise KokoroUnavailable(
                f"undertone: Kokoro model/voice {voice!r} is not cached locally -- "
                "run `undertone models fetch` first"
            ) from exc
        if not ok:
            raise KokoroUnavailable(
                f"undertone: Kokoro file {name!r} failed its integrity check -- "
                "re-run `undertone models fetch`"
            )
        paths.append(path)
    return paths[0], paths[1], paths[2]


def _default_session(model_path: str) -> Any:
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    try:
        return onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001 - ORT raises InvalidProtobuf etc., not OSError
        raise KokoroUnavailable(f"undertone: could not load the Kokoro model: {exc}") from exc


def _default_resample(pcm24: bytes) -> bytes:
    from livekit import rtc

    resampler = rtc.AudioResampler(
        MODEL_RATE, OUT_RATE, num_channels=1, quality=rtc.AudioResamplerQuality.HIGH
    )
    frames = resampler.push(rtc.AudioFrame(pcm24, MODEL_RATE, 1, len(pcm24) // 2))
    frames += resampler.flush()
    return b"".join(bytes(f.data) for f in frames)


class KokoroVoice:
    """Resident Kokoro voice. Inject `paths`/`session_factory`/`resample` in tests."""

    def __init__(
        self,
        voice: str | None = None,
        *,
        paths: tuple[str, str, str] | None = None,
        session_factory: Callable[[str], Any] | None = None,
        resample: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.voice = _check_voice_name(voice or os.environ.get(VOICE_ENV) or DEFAULT_VOICE)
        model_path, tokenizer_path, voice_path = paths or model_paths(self.voice)

        try:
            with open(tokenizer_path, encoding="utf-8") as fh:
                vocab = json.load(fh)["model"]["vocab"]
            styles = np.fromfile(voice_path, dtype=np.float32)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise KokoroUnavailable(f"undertone: could not load Kokoro assets: {exc}") from exc
        if not (
            isinstance(vocab, dict)
            and vocab.get("$") == 0
            and all(
                isinstance(k, str) and len(k) == 1 and type(v) is int for k, v in vocab.items()
            )
        ):
            raise KokoroUnavailable("undertone: Kokoro tokenizer vocab is malformed")
        if styles.size == 0 or styles.size % STYLE_DIM:
            raise KokoroUnavailable(
                f"undertone: Kokoro voice {self.voice!r} has an unexpected shape"
            )
        self._vocab: dict[str, int] = vocab
        self._styles = styles.reshape(-1, STYLE_DIM)

        self._session = (session_factory or _default_session)(model_path)
        self._resample = resample or _default_resample

    def _ntok(self, text: str) -> int:
        return sum(1 for c in text if c in self._vocab)

    def _hard_cut(self, word: str) -> list[str]:
        chunks: list[str] = []
        cur: list[str] = []
        count = 0
        for ch in word:
            if ch in self._vocab:
                if count == MAX_TOKENS:
                    chunks.append("".join(cur))
                    cur, count = [], 0
                count += 1
            cur.append(ch)
        if cur:
            chunks.append("".join(cur))
        return chunks

    def pieces(self, phonemes: str) -> list[str]:
        """Split into render pieces: short first piece, clause-aligned, <= MAX_TOKENS each."""
        clauses: list[list[str]] = []
        clause: list[str] = []
        for word in phonemes.split():
            clause.append(word)
            if word[-1] in _LONG_PAUSE + _SHORT_PAUSE:
                clauses.append(clause)
                clause = []
        if clause:
            clauses.append(clause)

        out: list[str] = []
        cur = ""

        def limit() -> int:
            return FIRST_PIECE_TOKENS if not out else PIECE_TOKENS

        def add(unit: str) -> None:
            nonlocal cur
            cand = f"{cur} {unit}" if cur else unit
            if cur and self._ntok(cand) > limit():
                out.append(cur)
                cur = unit
            else:
                cur = cand

        for words in clauses:
            text = " ".join(words)
            fresh_limit = PIECE_TOKENS if (out or cur) else FIRST_PIECE_TOKENS
            if self._ntok(text) <= fresh_limit:
                add(text)
                continue
            for word in words:
                for chunk in self._hard_cut(word) if self._ntok(word) > MAX_TOKENS else [word]:
                    add(chunk)
        if cur:
            out.append(cur)
        return out

    def render(self, phonemes: str) -> bytes | None:
        """Render one piece to 16 kHz int16 PCM, or None if there is nothing safe to play."""
        ids = [self._vocab[c] for c in phonemes if c in self._vocab]
        if not ids:
            return None
        style = self._styles[min(len(ids), len(self._styles)) - 1][None, :]
        feeds = {
            "input_ids": np.array([[0, *ids, 0]], dtype=np.int64),
            "style": style.astype(np.float32),
            "speed": np.array([1.0], dtype=np.float32),
        }
        wave = np.asarray(self._session.run(None, feeds)[0], dtype=np.float32).reshape(-1)
        # NaN/inf must never reach the speaker.
        if wave.size == 0 or not np.all(np.isfinite(wave)):
            return None
        peak = float(np.max(np.abs(wave)))
        if peak <= 0.0:
            return None
        loud = np.flatnonzero(np.abs(wave) > peak * _SILENCE_FRACTION)
        start = max(int(loud[0]) - int(_LEAD_S * MODEL_RATE), 0)
        end = min(int(loud[-1]) + 1 + int(_TAIL_S * MODEL_RATE), wave.size)
        wave = wave[start:end]

        last = phonemes.rstrip()[-1:]
        pause_s = (
            _LONG_PAUSE_S if last in _LONG_PAUSE else _SHORT_PAUSE_S if last in _SHORT_PAUSE else 0.0
        )
        wave = np.concatenate([wave, np.zeros(int(pause_s * MODEL_RATE), dtype=np.float32)])

        pcm24 = (np.clip(wave, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        pcm = self._resample(pcm24)
        return pcm[: len(pcm) // 2 * 2]

    def warm_up(self) -> None:
        """Run one throwaway inference; the first after load takes ~0.7 s."""
        try:
            out = self.render(WARM_UP_PHONEMES)
        except Exception as exc:  # noqa: BLE001 - startup must fall back, never crash
            raise KokoroUnavailable(f"undertone: Kokoro warm-up failed: {exc}") from exc
        if out is None:
            raise KokoroUnavailable("undertone: Kokoro warm-up failed: no audio produced")
