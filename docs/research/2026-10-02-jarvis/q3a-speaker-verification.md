# Q3a: On-device speaker verification for undertone (2026-10-02)

## Recommendation
Use **WeSpeaker CAM++ (VoxCeleb, 7.18M params) or ResNet34-LM as ONNX, run on the onnxruntime undertone already ships**; fbank frontend (80-mel Kaldi fbank + mean-norm) computed in numpy or via sherpa-onnx's extractor.
- Export path: sherpa-onnx `speaker-recongition-models` release (sic) ships `wespeaker_en_voxceleb_CAM++.onnx` (~28 MB) and `wespeaker_en_voxceleb_resnet34.onnx` (~26 MB); WeSpeaker HF repos also carry ONNX. No PyTorch.
- Fallback: MLX port `mlx-community/wespeaker-voxceleb-resnet34-LM` (MIT, 25 MB npz, 6.6M params) if ONNX frontend is a pain.
- License caveat: weights are CC-BY-4.0 (inherit VoxCeleb's license). Permissive (attribution required, commercial OK) but NOT MIT/Apache/BSD strictly. Needs a NOTICE entry. The MLX port is labelled MIT, which is probably inaccurate for derived CC-BY weights; do not rely on that label.
- Strict-permissive alternative: SpeechBrain ECAPA (Apache-2.0, EER 0.80%) but no ONNX and needs PyTorch to export (export is a one-time offline step; shipping the .onnx is then your call, with the same VoxCeleb data caveat).
- Honest expectation: verification of 1-3 s text-independent commands is the weak spot. Treat it as a second factor in addition to wake word + random phrase, not a replacement.

## Comparison table
| Model | EER Vox1-O | Size | Code / weights license | ONNX | Maintenance |
|---|---|---|---|---|---|
| WeSpeaker CAM++ | 0.803 (0.659 w/ LM+AS-norm) | 7.18M | Apache-2.0 / CC-BY-4.0 | yes (sherpa 28 MB; WeSpeaker docs) | active |
| WeSpeaker ResNet34 | 0.867 (0.723 LM+ASnorm) | 6.63M, 4.55 GFLOPs | Apache-2.0 / CC-BY-4.0 | yes (26 MB; HF has 2 quantized) | active |
| WeSpeaker ERes2Net34 | 0.914 (0.744) | 7.88M | same | yes | active |
| WeSpeaker ECAPA512 | 1.069 (0.782) | 6.19M | same | yes | active |
| 3D-Speaker CAM++/ERes2Net(V2) | CAM++ 0.65, ERes2Net-base 0.84, ERes2NetV2 0.61 (full), 0.98 @3s, 1.48 @2s | 7.2M / 17.8M | Apache-2.0 / weights: model card terms not confirmed | yes (sherpa: `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` 28.2 MB) | last update seen Dec 2024 |
| NVIDIA TitaNet-L | 0.66 | ~23M | NeMo Apache-2.0 / CC-BY-4.0 | via sherpa-onnx (`nemo_en_titanet_small/large.onnx`) | NeMo active |
| SpeechBrain ECAPA | 0.80 | n/a (~20M, not verified) | Apache-2.0 / Apache-2.0 | none official; needs PyTorch | active |
| pyannote/embedding | 2.8 (no VAD/PLDA) | small | MIT, gated HF | no | superseded |
| Resemblyzer (GE2E) | not stated | ~1.4M (not verified) | Apache-2.0 | no (PyTorch) | stale, English-only |
| MLX ResNet34-LM | same as WeSpeaker, cosine diff <=2.4% vs PyTorch | 25 MB | labelled MIT | n/a (MLX) | community |

Latency: no per-utterance CPU numbers found. Models are ~7M params; sherpa-onnx targets mobile/RPi real-time, so <100 ms for 2-3 s on Apple Silicon is plausible but UNMEASURED (spike item 1).

## Enrollment design sketch
- Enroll 8-10 utterances (~30-60 s total speech) of varied content incl. "hey jarvis" and the confirm-phrase vocabulary; record in the real mic/room.
- Embed each, L2-normalise, store mean centroid (+ keep individual embeddings for a max-of-k or mean-of-top-k score). Store as a local file, mode 0600, never the raw audio unless user opts in.
- Score = cosine(utterance embedding, centroid). Re-enrol UX: `undertone enroll-voice`, with a `--verify` self-test.
- Optionally embed the wake-word segment + the phrase portion only (that is what carries the confirmation), skipping VAD-padded silence.

## Threshold / short utterances
- Benchmarks are on full-length Vox1 trials. Degradation on short audio is steep: a baseline went from 2.4% EER at 3 s to 18.4% at 1 s; ERes2NetV2 1.48% at 2 s, 0.98% at 3 s (arXiv/ISCA, ERes2NetV2). Real mic, room noise, and a 2-word phrase will be worse than any of these.
- Choice for confirmations: bias toward false-reject (owner just repeats), since false-accept grants a risky action. Pick threshold at e.g. FAR <= 0.1% on impostor + TTS set measured locally, accept the FRR that falls out (likely 5-20% on short audio; unmeasured). Fail-closed: no embedding, short audio (<~1.5 s speech), or low score -> no grant.
- Use score-normalisation only if you gather a cohort; skip for v1.

## Spoofing
- Speaker embeddings are NOT anti-spoofing. Replay and modern TTS/voice clones pass ASV at high rates. Kokoro's output is a different synthetic voice, so it would likely score low against the owner (that is a useful property for undertone's own-speech case, but must be measured, and does not hold against voice-cloned TTS of the owner).
- It helps self-wake: our own speaker output should fail owner match; a cheap extra guard on top of existing `wake_barge_in` logic.
- Anti-spoofing: AASIST (clovaai) code MIT, EER 0.83% / AASIST-L 0.99% (85k params) on ASVspoof2019 LA; weights/data licence (ASVspoof2019 / VCTK) not verified; PyTorch only, no ONNX found; poor generalisation out-of-domain is widely reported (not verified here). Defer; v1 = ASV + wake + random phrase.

## Integration fit
- Where: in voice.py turn handling, at the point `_turn_via` is set to "wake" and the utterance is finalised (voice.py:335-370). Embed the buffered utterance audio (16 kHz mono, already present) and attach e.g. `"speaker": {"match": bool, "score": float}` next to `via` in the utterance envelope.
- Downstream gate: grant confirm only if `via == "wake"` AND `speaker.match` (missing field = deny). Keep ort session resident (load once, like Kokoro) to avoid cold-start.
- Run embedding off the audio thread; ~7M-param model, onnxruntime already a dependency (pyproject.toml:14). New dep only for fbank (numpy implementation ok, or `sherpa-onnx` wheel; py3.14 wheel availability NOT verified).

## First spike (go/no-go)
Data: owner 50+ commands (real mic, 1-3 s, wake + phrase), 10+ other voices (family/friends or public clips cut to same lengths), Kokoro (all voices) and macOS `say` renditions of the same phrases, plus owner replayed through the speaker.
Measure for CAM++ and ResNet34 (ONNX):
1. Per-utterance latency on the target Mac (p50/p95) for 1, 2, 3 s inputs.
2. Genuine vs impostor cosine distributions by duration bucket (1/2/3 s); EER and FRR @ FAR=0.1%/1%.
3. TTS (Kokoro/say) and replay scores vs owner centroid: max score vs threshold.
4. Enrollment-size sweep (3, 5, 10 utterances).
Go: FAR<=0.1% at FRR<=15% on 2 s owner audio, TTS/replay max below threshold, p95 <100 ms. No-go: fall back to wake+phrase only, or require longer phrase (~3 s).

## Sources
- https://github.com/wenet-e2e/wespeaker/blob/master/examples/voxceleb/v2/README.md (EER table, verified)
- https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md (CC-BY-4.0 follows dataset quote, verified)
- https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM (cc-by-4.0, ONNX, verified)
- https://huggingface.co/mlx-community/wespeaker-voxceleb-resnet34-LM (MLX port, MIT label, verified)
- https://github.com/modelscope/3D-Speaker (Apache-2.0, verified)
- https://www.isca-archive.org/interspeech_2024/chen24l_interspeech.pdf (short-duration EER, via search snippet)
- https://huggingface.co/nvidia/speakerverification_en_titanet_large (CC-BY-4.0, 0.66, verified)
- https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb (Apache-2.0, 0.80, verified)
- https://github.com/resemble-ai/Resemblyzer (Apache-2.0, verified)
- https://huggingface.co/pyannote/embedding (MIT gated, 2.8%, verified)
- https://github.com/clovaai/aasist (MIT, via search snippet)
- https://k2-fsa.github.io/sherpa/onnx/speaker-identification/ (sherpa model files; sizes via search snippet)
- Repo: src/undertone/voice.py:16-46,335-370; pyproject.toml:14

## Not verified
- Any CPU latency on Apple Silicon; sherpa-onnx wheel on Python 3.14; exact ONNX input spec (fbank params) per model.
- sherpa release asset list (page failed to load); sizes come from search snippets.
- Weights license of 3D-Speaker checkpoints; AASIST weights/data license; any ONNX AASIST.
- Whether CC-BY-4.0 satisfies "permissive" for you; VoxCeleb audio provenance (YouTube) legal risk.
- Resemblyzer/ECAPA param counts and Resemblyzer EER; short-utterance EER for CAM++/ResNet34 specifically (only ERes2NetV2 and a baseline found).
- Whether Kokoro/replay audio is rejected (hypothesis only).
