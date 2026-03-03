# StealthPay Production Deployment Checklist

## 🔐 Credentials Required

Для deploy мне нужны следующие данные:

### 1. Server Access
```
- Server IP address: ________________
- SSH user: ________________
- SSH key or password: ________________
- SSH port (default 22): ________________
- sudo password (if needed): ________________
```

### 2. Domain & SSL
```
- Domain name: ________________ (e.g., api.stealthpay.io)
- SSL certificate: ________________ (или я сгенерирую Let's Encrypt)
- SSL private key: ________________
```

### 3. Database
```
- PostgreSQL password: ________________ (минимум 16 символов)
- Redis password: ________________ (минимум 16 символов)
```

### 4. Monero Wallet RPC
```
- RPC Host: ________________ (IP или hostname вашего Monero узла)
- RPC Port: ________________ (обычно 18082)
- RPC Username: ________________
- RPC Password: ________________
```

**ИЛИ** если нужно запустить Monero узел:
```
- Wallet file location: ________________
- Wallet password: ________________
- Monero daemon host: ________________ (node.monero.net или ваш)
```

### 5. API Security
```
- Admin API Key: ________________ (64+ символов случайных)
- Secret Key: ________________ (64+ символов случайных)
- CORS Origins: ________________ (через запятую, например: https://app.stealthpay.io,https://admin.stealthpay.io)
```

### 6. Cloudflare (опционально)
```
- Cloudflare API Token: ________________ (для автоматического DNS)
- Zone ID: ________________
```

### 7. Monitoring (опционально)
```
- Sentry DSN: ________________ (для отслеживания ошибок)
- Slack webhook URL: ________________ (для алертов)
```

---

## 📝 Pre-Deployment Steps

### На вашем сервере должно быть установлено:
- [ ] Ubuntu 20.04+ / Debian 11+ / CentOS 8+
- [ ] Docker 20.10+
- [ ] Docker Compose 2.0+
- [ ] Git

### Порты которые должны быть открыты:
- [ ] 22 (SSH)
- [ ] 80 (HTTP)
- [ ] 443 (HTTPS)

---

## 🚀 Deployment Commands

После получения credentials я выполню:

```bash
# 1. Подключение к серверу
ssh user@server-ip

# 2. Клонирование репозитория
git clone https://github.com/stealthpay/stealthpay.git
cd stealthpay/deploy

# 3. Создание .env файла с вашими credentials
nano .env

# 4. Запуск deploy
make init
make ssl-generate  # или копирование ваших SSL сертификатов
make deploy

# 5. Проверка статуса
make status
make health
```

---

## ✅ Post-Deployment Verification

После deploy проверим:

```bash
# Health check
curl https://api.stealthpay.io/health

# API работает
curl https://api.stealthpay.io/v2/agents

# SSL сертификат валиден
curl -vI https://api.stealthpay.io 2>&1 | grep "SSL certificate"
```

---

## 🔧 Useful Commands After Deploy

```bash
# Просмотр логов
make logs
make logs-api
make logs-db

# Бэкап базы данных
make db-backup

# Рестарт сервисов
make restart
make restart-api

# Проверка статуса
make status
make health

# Обновление
make update
```

---

## 📊 Expected Output

После успешного deploy:

```
=== StealthPay Deployment ===
Environment: production
Time: 2026-03-03 10:00:00

[INFO] Checking prerequisites...
[INFO] Prerequisites check passed
[INFO] Setting up directories...
[INFO] Directories created
[INFO] Creating database backup...
[INFO] Building application...
[INFO] Application built
[INFO] Starting deployment...
[INFO] Stopping existing containers...
[INFO] Starting new containers...
[INFO] Waiting for services to be healthy...
[INFO] ✓ API is healthy
[INFO] Deployment complete!

Current status:
           Name                         Command               State                    Ports
---------------------------------------------------------------------------------------------------------
stealthpay-api             uvicorn stealthpay.api.mai ...   Up (healthy)   8000/tcp
stealthpay-nginx           /docker-entrypoint.sh ngin ...   Up             0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
stealthpay-postgres        docker-entrypoint.sh postgres    Up (healthy)   5432/tcp
stealthpay-redis           docker-entrypoint.sh redis ...   Up (healthy)   6379/tcp
stealthpay-webhook-worker  python -c import asyncio;  ...   Up             

[INFO] Cleanup complete
```

---

## 🆘 Emergency Contacts

Если что-то пойдёт не так:

1. **Rollback**: `make rollback`
2. **Stop all**: `make stop-all`
3. **Check logs**: `make logs`
4. **Database shell**: `make db-shell`

---

## 📋 Please Provide

Отправьте мне данные в любом формате (можно по одному):

1. **SSH доступ** (я подключусь и сделаю всё сам)
2. **Или** заполните CHECKLIST выше

**Важно:** Все credentials будут использованы только для deploy и не сохранятся.

**Ещё лучше:** Давайте сделаем через Telegram/Discord для безопасной передачи ключей.
