FROM python:3.10-slim-bookworm

# Install minimal system dependencies
# Note: build-essential and python3-dev needed for webrtcvad compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    libsndfile1 \
    ffmpeg \
    ca-certificates \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone repositories (but don't install yet)
# Pinned to commits (trust-on-first-use from 2026-11-09); the tags are asserted
# to still resolve to them.
RUN git clone --depth 1 --branch v0.6.0 \
    https://github.com/dscripka/openWakeWord.git /opt/openwakeword && \
    test "$(git -C /opt/openwakeword rev-parse HEAD)" = "c8ef6912c5feccf1037b852d9bc6c7ed644135ba" && \
    git clone --depth 1 --branch v2.0.0 \
    https://github.com/rhasspy/piper-sample-generator.git /opt/piper-sample-generator && \
    test "$(git -C /opt/piper-sample-generator rev-parse HEAD)" = "195e3bd967d54589c2137c9de2b22ad526ba6b6f"

# Install pinned PyTorch first (before any other pip install that references torch)
# This prevents pip from pulling the latest torch in transitive dependencies
RUN pip install --no-cache-dir \
        torch==2.1.2 \
        torchaudio==2.1.2 \
        "numpy<2"

# Download piper TTS model
RUN mkdir -p /opt/piper-sample-generator/models && \
    wget -q https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt \
    -O /opt/piper-sample-generator/models/en_US-libritts_r-medium.pt && \
    echo "e95ee53770bf598c354a6e6dbfc95ccb259aeeb501d35a86be8a767429ab0ff6  /opt/piper-sample-generator/models/en_US-libritts_r-medium.pt" | sha256sum -c -

# Download openWakeWord feature models to correct package path
RUN mkdir -p /opt/openwakeword/openwakeword/resources/models && \
    wget -q https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx \
    -O /opt/openwakeword/openwakeword/resources/models/embedding_model.onnx && \
    wget -q https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx \
    -O /opt/openwakeword/openwakeword/resources/models/melspectrogram.onnx && \
    echo "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f  /opt/openwakeword/openwakeword/resources/models/embedding_model.onnx" | sha256sum -c - && \
    echo "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f  /opt/openwakeword/openwakeword/resources/models/melspectrogram.onnx" | sha256sum -c -

# Install piper-sample-generator dependencies (torch already satisfied)
RUN pip install --no-cache-dir \
        audiomentations==0.33.0 \
        piper-phonemize==1.1.0 \
        webrtcvad==2.0.10

# Install training-specific dependencies from openWakeWord's 'full' extras (minus TensorFlow)
RUN pip install --no-cache-dir \
        mutagen==1.47.0 \
        torchinfo==1.8.0 \
        torchmetrics==1.2.0 \
        speechbrain==0.5.14 \
        torch-audiomentations==0.11.0 \
        acoustics==0.2.6 \
        pronouncing==0.2.0 \
        datasets==2.14.6 \
        deep-phonemizer==0.0.19 \
        pyyaml \
        scipy \
        pyarrow==14.0.2 \
        fsspec==2023.10.0

# Install openWakeWord in editable mode for development
WORKDIR /opt/openwakeword
RUN pip install --no-cache-dir -e .

# Set working directory for training
WORKDIR /work

# Default command
CMD ["/bin/bash"]
