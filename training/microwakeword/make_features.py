"""Augment generated clips and write mmap spectrogram features.

Ports cells 5 and 7 of the microWakeWord basic_training_notebook. Run with the
training venv. Writes, under --root:
  generated_augmented_features/      wake word positives (from positives/)
  generated_augmented_confusables/   confusable negatives (from confusables/)
Each holds training/, validation/, testing/ with a wakeword_mmap.
"""

import argparse
import os
import sys


def build_features(root, input_dir, output_dir):
    from mmap_ninja.ragged import RaggedMmap
    from microwakeword.audio.augmentation import Augmentation
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration

    clips = Clips(
        input_directory=input_dir,
        file_pattern="*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=10,
        split_count=0.1,
    )
    augmenter = Augmentation(
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.1,
            "TanhDistortion": 0.1,
            "PitchShift": 0.1,
            "BandStopFilter": 0.1,
            "AddColorNoise": 0.1,
            "AddBackgroundNoise": 0.75,
            "Gain": 1.0,
            "RIR": 0.5,
        },
        impulse_paths=[os.path.join(root, "mit_rirs")],
        background_paths=[
            os.path.join(root, "fma_16k"),
            os.path.join(root, "audioset_16k"),
        ],
        background_min_snr_db=-5,
        background_max_snr_db=10,
        min_jitter_s=0.195,
        max_jitter_s=0.205,
    )

    os.makedirs(output_dir, exist_ok=True)
    for split in ["training", "validation", "testing"]:
        out_dir = os.path.join(output_dir, split)
        os.makedirs(out_dir, exist_ok=True)

        split_name = "train"
        repetition = 2

        spectrograms = SpectrogramGeneration(
            clips=clips,
            augmenter=augmenter,
            slide_frames=10,
            step_ms=10,
        )
        if split == "validation":
            split_name = "validation"
            repetition = 1
        elif split == "testing":
            split_name = "test"
            repetition = 1
            spectrograms = SpectrogramGeneration(
                clips=clips,
                augmenter=augmenter,
                slide_frames=1,
                step_ms=10,
            )

        mmap_dir = os.path.join(out_dir, "wakeword_mmap")
        if os.path.exists(mmap_dir):
            print(f"skip (exists): {mmap_dir}")
            continue
        # Build in a .part dir so an interrupted run is never mistaken for done.
        if os.path.lexists(mmap_dir + ".part"):
            sys.exit(
                f"leftover {mmap_dir}.part from an interrupted run; this script "
                "never deletes. Clear it first (train.sh does, via lib.sh)."
            )
        RaggedMmap.from_generator(
            out_dir=mmap_dir + ".part",
            sample_generator=spectrograms.spectrogram_generator(
                split=split_name, repeat=repetition
            ),
            batch_size=100,
            verbose=True,
        )
        os.rename(mmap_dir + ".part", mmap_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="MWW_ROOT")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    for in_name, out_name in [
        ("positives", "generated_augmented_features"),
        ("confusables", "generated_augmented_confusables"),
    ]:
        in_dir = os.path.join(root, in_name)
        if not os.path.isdir(in_dir):
            sys.exit(f"missing {in_dir}; run the positives/negatives steps first")
        print(f"== features: {in_dir} -> {out_name}", flush=True)
        build_features(root, in_dir, os.path.join(root, out_name))


if __name__ == "__main__":
    main()
