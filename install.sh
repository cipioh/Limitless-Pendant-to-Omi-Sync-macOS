#!/bin/bash

echo "Setting up Limitless to Omi Sync..."

# 1. Determine base directory (where this script is located)
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Base directory set to: $BASE_DIR"

# 2. Create required directory structure
echo "Creating data directories..."
mkdir -p "$BASE_DIR/limitless_data/downloads/wav_exports"
mkdir -p "$BASE_DIR/limitless_data/logs"
mkdir -p "$BASE_DIR/limitless_data/discarded_audio"
mkdir -p "$BASE_DIR/limitless_data/synced_to_omi"

# 3. Set up Python virtual environment
echo "Creating virtual environment..."
python3 -m venv "$BASE_DIR/.venv"

# 4. Install dependencies
echo "Installing Python dependencies..."
"$BASE_DIR/.venv/bin/pip" install --upgrade pip
"$BASE_DIR/.venv/bin/pip" install -r "$BASE_DIR/requirements.txt"

# 5. Set up .env file
if [ ! -f "$BASE_DIR/.env" ]; then
    echo "Creating .env file from template..."
    cp "$BASE_DIR/.env.example" "$BASE_DIR/.env"
    echo ""
    echo "IMPORTANT: Please open $BASE_DIR/.env and add your OMI_API_KEY."
else
    echo ".env file already exists."
fi

echo ""
echo "Setup complete! Once you add your API key, start the service with:"
echo "   $BASE_DIR/.venv/bin/python3 $BASE_DIR/scripts/pendant_sync.py"