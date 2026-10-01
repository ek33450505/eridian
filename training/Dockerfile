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
RUN git clone --depth 1 --branch v0.6.0 \
    https://github.com/dscripka/openWakeWord.git /opt/openwakeword && \
    git clone --depth 1 --branch v2.0.0 \
    https://github.com/rhasspy/piper-sample-generator.git /opt/piper-sample-generator

# Install pinned PyTorch first (before any other pip install that references torch)
# This prevents pip from pulling the latest torch in transitive dependencies
RUN pip install --no-cache-dir \
        torch==2.1.2 \
        torchaudio==2.1.2 \
        "numpy<2"

# Download piper TTS model
RUN mkdir -p /opt/piper-sample-generator/models && \
    wget -q https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt \
    -O /opt/piper-sample-generator/models/en_US-libritts_r-medium.pt

# Download openWakeWord feature models to correct package path
RUN mkdir -p /opt/openwakeword/openwakeword/resources/models && \
    wget -q https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx \
    -O /opt/openwakeword/openwakeword/resources/models/embedding_model.onnx && \
    wget -q https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx \
    -O /opt/openwakeword/openwakeword/resources/models/melspectrogram.onnx

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
