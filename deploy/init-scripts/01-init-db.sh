#!/bin/bash
set -e

echo "=== StealthPay Database Initialization ==="

# Create extensions
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Enable extensions
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS "pgcrypto";
    
    -- Create application schema
    CREATE SCHEMA IF NOT EXISTS stealthpay;
    
    -- Set search path
    ALTER DATABASE $POSTGRES_DB SET search_path TO stealthpay, public;
    
    echo "Extensions and schema created successfully"
EOSQL

# Run schema creation
if [ -f /docker-entrypoint-initdb.d/02-schema.sql ]; then
    echo "Applying schema..."
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/02-schema.sql
fi

echo "=== Database initialization complete ==="
