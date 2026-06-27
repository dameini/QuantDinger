#!/bin/sh
# QuantDinger Docker Entrypoint Script
# Checks and validates SECRET_KEY before starting the application

set -e

echo "============================================"
echo "  QuantDinger Backend - Starting..."
echo "============================================"

# Check if .env file exists
if [ ! -f /app/.env ]; then
    echo "[WARNING] .env file not found at /app/.env"
    echo "Creating .env from env.example..."
    if [ -f /app/env.example ]; then
        cp /app/env.example /app/.env
        echo "[INFO] Created .env from env.example"
        echo "[IMPORTANT] Please edit /app/.env and set a secure SECRET_KEY before restarting!"
    else
        echo "[ERROR] env.example not found. Cannot create .env automatically."
        exit 1
    fi
fi

# Check SECRET_KEY configuration
DEFAULT_SECRET="quantdinger-secret-key-change-me"
CURRENT_SECRET=$(grep -E "^SECRET_KEY=" /app/.env 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs || echo "")

if [ -z "$CURRENT_SECRET" ]; then
    NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    echo "SECRET_KEY=${NEW_SECRET}" >> /app/.env
    echo "[AUTO] Generated random SECRET_KEY (was missing)."
    CURRENT_SECRET="$NEW_SECRET"
fi

# Keep an explicit default SECRET_KEY stable. Existing deployments may have
# already encrypted exchange credentials with this exact value; silently
# rotating it at container boot breaks live credential decryption and can
# auto-stop running strategies. Only generate a key when SECRET_KEY is missing.
if [ "$CURRENT_SECRET" = "$DEFAULT_SECRET" ]; then
    echo "[WARNING] SECRET_KEY is still using the default value."
    echo "[TIP]  Keep it unchanged if you already encrypted live credentials with it."
    echo "[TIP]  If you want to rotate it, re-encrypt credentials first, then restart."
fi

echo "[OK] SECRET_KEY is configured"
SECRET_LEN=$(printf '%s' "$CURRENT_SECRET" | wc -c | tr -d ' ')
if [ "$SECRET_LEN" -lt 32 ]; then
    echo "[WARNING] SECRET_KEY is only ${SECRET_LEN} bytes; RFC 7518 recommends >= 32 for HS256."
    echo "          Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    echo "          After updating .env, restart the stack; users must sign in again."
fi
echo ""

# Start the application
exec "$@"
