#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
IMAGE_NAME="eridian-oww:latest"

echo "=== Building Docker image: $IMAGE_NAME ==="
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

echo ""
echo "=== Running smoke tests inside container ==="

# Test 1: Import all required modules
echo "Test 1: Importing required modules..."
docker run --rm "$IMAGE_NAME" python3 << 'EOF'
import sys
try:
    import openwakeword
    print(f"✓ openwakeword imported")
    import torch
    print(f"✓ torch {torch.__version__} (CPU: {not torch.cuda.is_available()})")
    import torchaudio
    print(f"✓ torchaudio {torchaudio.__version__}")
    import speechbrain
    print(f"✓ speechbrain {speechbrain.__version__}")
    import audiomentations
    print(f"✓ audiomentations {audiomentations.__version__}")
    import torch_audiomentations
    print(f"✓ torch_audiomentations {torch_audiomentations.__version__}")
    import acoustics
    print(f"✓ acoustics {acoustics.__version__}")
    import datasets
    print(f"✓ datasets {datasets.__version__}")
    import piper_phonemize
    print(f"✓ piper_phonemize imported")
    import webrtcvad
    print(f"✓ webrtcvad imported")
    print("\nAll imports successful!")
except ImportError as e:
    print(f"✗ Import failed: {e}", file=sys.stderr)
    sys.exit(1)
EOF

echo ""
echo "Test 2: Generating sample clips..."
start_time=$(date +%s.%N)

docker run --rm "$IMAGE_NAME" python3 << 'EOF'
import sys
import os
sys.path.insert(0, '/opt/piper-sample-generator')
from generate_samples import generate_samples

output_dir = '/tmp/smoke_test'
os.makedirs(output_dir, exist_ok=True)

try:
    print(f"Generating samples to {output_dir}...")
    generate_samples(
        text=["hey eridian"],
        output_dir=output_dir,
        max_samples=10,
        batch_size=5,
        model='/opt/piper-sample-generator/models/en_US-libritts_r-medium.pt'
    )

    # Count .wav files
    wav_files = [f for f in os.listdir(output_dir) if f.endswith('.wav')]
    print(f"\nGenerated {len(wav_files)} .wav files")

    if len(wav_files) != 10:
        print(f"✗ Expected 10 .wav files, got {len(wav_files)}", file=sys.stderr)
        sys.exit(1)

    # List files
    for f in sorted(wav_files):
        size = os.path.getsize(os.path.join(output_dir, f))
        print(f"  {f} ({size} bytes)")

    print("✓ Sample generation successful!")
except Exception as e:
    print(f"✗ Sample generation failed: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
EOF

echo ""
echo "Test 3: Testing feature extraction with AudioFeatures..."

docker run --rm "$IMAGE_NAME" python3 << 'EOF'
import sys
import os
import numpy as np
import soundfile as sf

try:
    # Test 1: AudioFeatures can be instantiated
    print("  Testing AudioFeatures instantiation...")
    from openwakeword.utils import AudioFeatures
    af = AudioFeatures()
    print("  ✓ AudioFeatures() constructed successfully")

    # Test 2: Load generated clips and test feature extraction
    print("  Testing feature extraction on generated clips...")
    output_dir = '/tmp/smoke_test'
    wav_files = sorted([f for f in os.listdir(output_dir) if f.endswith('.wav')])

    if len(wav_files) == 0:
        print("✗ No .wav files found in output_dir", file=sys.stderr)
        sys.exit(1)

    # Load first clip as int16 array for feature extraction
    wav_path = os.path.join(output_dir, wav_files[0])
    audio_data, sr = sf.read(wav_path, dtype='int16')

    # Embed the clip (this tests that model files are in the correct location)
    embeddings = af.embed_clips(np.array([audio_data]))

    if embeddings is None or len(embeddings) == 0:
        print("✗ embed_clips() returned empty result", file=sys.stderr)
        sys.exit(1)

    print(f"  ✓ Embeddings shape: {embeddings.shape}")
    print("✓ Feature extraction successful!")

except Exception as e:
    print(f"✗ Feature extraction test failed: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
EOF

echo ""
echo "Test 4: Testing deep-phonemizer import..."

docker run --rm "$IMAGE_NAME" python3 << 'EOF'
try:
    print("  Testing deep-phonemizer import...")
    import dp.phonemizer
    print("✓ deep-phonemizer import successful!")
except ImportError as e:
    print(f"✗ deep-phonemizer import failed: {e}", file=__import__('sys').stderr)
    __import__('sys').exit(1)
EOF

end_time=$(date +%s.%N)
elapsed=$(echo "$end_time - $start_time" | bc)

echo ""
echo "=== Test Results ==="
echo "Status: PASS"
echo "Wall time (clip generation): ${elapsed} seconds"
echo "Image size:"
docker image ls "$IMAGE_NAME"
