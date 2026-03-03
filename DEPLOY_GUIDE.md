# StealthPay Deployment Guide
## Railway + Hetzner + Cloudflare Stack

**Время:** ~30 минут  
**Стоимость:** ~$30/мес  
**Сложность:** Нужно нажимать кнопки в 3 сервисах

---

## 📋 Чеклист перед стартом

- [ ] Аккаунт GitHub (для Railway)
- [ ] Кредитная карта или PayPal (для оплаты)
- [ ] ~30 минут времени

---

## Шаг 1: Регистрация на сервисах (10 мин)

### 1.1 Railway (API хостинг)
🔗 https://railway.app
1. Нажми "Start a New Project"
2. Выбери "Deploy from GitHub repo"
3. Дай доступ к GitHub
4. **Остановись здесь** — дальше я скажу

### 1.2 Hetzner (Monero VPS) 
🔗 https://console.hetzner.cloud
1. Зарегистрируйся
2. Добавь способ оплаты (карта/PayPal)
3. Подтверди email
4. **Остановись здесь**

### 1.3 Cloudflare (DNS + SSL)
🔗 https://dash.cloudflare.com
1. Зарегистрируйся
2. **Остановись здесь** — домен добавим позже

### 1.4 Namecheap (домен) — опционально
🔗 https://www.namecheap.com
1. Купи домен (~$10/год)
   - Рекомендую: `.io`, `.app`, `.dev`
   - Примеры: `stealthpay.io`, `agentpay.app`
2. Или купи в Cloudflare (удобнее)

---

## Шаг 2: Создать VPS на Hetzner (5 мин)

1. В Hetzner Cloud нажми "Add Server"
2. **Location:** Germany (Nuremberg) или Finland
3. **Image:** Ubuntu 22.04
4. **Type:** CX11 (1 vCPU, 2GB RAM) — €3.79/мес
5. **Networking:** IPv4 enabled
6. **SSH Key:** 
   - Если нет ключа, сгенерируй: `ssh-keygen -t ed25519`
   - Скопируй содержимое `~/.ssh/id_ed25519.pub`
7. **Name:** monero-node
8. Нажми "Create & Buy"

**Сохрани IP адрес сервера!** (будет типа `78.46.123.45`)

---

## Шаг 3: Настроить Monero на Hetzner (5 мин)

### 3.1 Подключись к серверу
```bash
ssh root@YOUR_HETZNER_IP
```

### 3.2 Запусти setup скрипт
```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_REPO/main/hetzner/setup-monero.sh | bash
```

**Или вручную:**
```bash
# Скопируй файл setup-monero.sh на сервер
scp hetnzer/setup-monero.sh root@YOUR_HETZNER_IP:/tmp/
ssh root@YOUR_HETZNER_IP
bash /tmp/setup-monero.sh
```

### 3.3 Сохрани credentials
Скрипт выведет:
```
RPC Username: stealthpay
RPC Password: xxxxxxxxxxxxxxxxxxxxxxx
Wallet Password: yyyyyyyyyyyyyyyyyyyyy
```

**Сохрани в надёжное место!**

### 3.4 Создать кошелёк
```bash
sudo /opt/monero/create-wallet.sh stealthpay
```

Сохрани seed phrase (12 или 24 слова)!

### 3.5 Проверить статус
```bash
monero-status
```

Жди синхронизации блокчейна (~24-48 часов для полной синхронизации).

---

## Шаг 4: Настроить Railway (10 мин)

### 4.1 Подготовить код
Сделай fork репозитория или создай новый:

```bash
# Если у тебя уже есть код
git init
git add .
git commit -m "Initial commit"
git push github
```

### 4.2 Создать проект в Railway
1. В Railway нажми "New Project"
2. "Deploy from GitHub repo"
3. Выбери свой репозиторий
4. Railway автоматически найдёт `railway.toml`

### 4.3 Добавить PostgreSQL
1. Нажми "New" → "Database" → "Add PostgreSQL"
2. Railway создаст БД автоматически
3. **Сохрани DATABASE_URL** (Variables → RAW Editor)

### 4.4 Добавить Redis
1. "New" → "Database" → "Add Redis"
2. Railway создаст Redis
3. **Сохрани REDIS_URL**

### 4.5 Настроить переменные окружения
В Railway перейди в "Variables" и добавь:

```env
# Database (автоматически создано Railway)
DATABASE_URL=${{Postgres.DATABASE_URL}}

# Redis (автоматически создано Railway)
REDIS_URL=${{Redis.REDIS_URL}}

# Monero (из Hetzner)
MONERO_RPC_HOST=YOUR_HETZNER_IP
MONERO_RPC_PORT=18082
MONERO_RPC_USER=stealthpay
MONERO_RPC_PASS=your_rpc_password_from_step_3

# Security (сгенерируй случайные)
ADMIN_API_KEY=$(openssl rand -hex 32)
SECRET_KEY=$(openssl rand -hex 32)

# CORS
cors_origins=https://yourdomain.com,https://app.yourdomain.com
```

### 4.6 Задеплоить
1. Railway автоматически задеплоит при пуше
2. Или нажми "Deploy" вручную
3. Дождись зелёного статуса (Healthy)

**Сохрани Railway Domain** (типа `stealthpay-api.up.railway.app`)

---

## Шаг 5: Настроить Cloudflare + Домен (10 мин)

### 5.1 Добавить домен в Cloudflare
1. Cloudflare Dashboard → "Add Site"
2. Введи свой домен
3. Выбери Free Plan
4. Скопируй NS-записи (2 штуки)

### 5.2 Сменить NS у регистратора
**Namecheap:**
1. Domain List → Manage → Nameservers
2. Custom DNS
3. Вставь 2 NS от Cloudflare
4. Save

Жди 5-30 минут пока обновятся DNS.

### 5.3 Настроить DNS записи
В Cloudflare → DNS → Records:

**Для API (Railway):**
```
Type: CNAME
Name: api
Target: your-railway-app.up.railway.app
Proxy status: Proxied (оранжевое облако)
```

**Для Monero (Hetzner) — ТОЛЬКО если нужен доступ к RPC снаружи:**
```
Type: A
Name: monero
IPv4: YOUR_HETZNER_IP
Proxy status: DNS only (СЕРОЕ облако!)
```

### 5.4 SSL/TLS
Cloudflare → SSL/TLS → Overview:
- Выбери **Full (strict)**

Cloudflare → SSL/TLS → Edge Certificates:
- Always Use HTTPS: ON
- Automatic HTTPS Rewrites: ON

### 5.5 Добавить домен в Railway
1. Railway → Settings → Domains
2. "Generate Domain" или "Custom Domain"
3. Введи: `api.yourdomain.com`
4. Railway даст CNAME target
5. Обнови запись в Cloudflare если нужно

---

## Шаг 6: Проверка (2 мин)

### 6.1 Проверить API
```bash
curl https://api.yourdomain.com/health
```

Должно вернуть:
```json
{
  "status": "healthy",
  "version": "2.0.0"
}
```

### 6.2 Проверить регистрацию
```bash
curl -X POST https://api.yourdomain.com/v2/agents/register \
  -H "Content-Type: application/json" \
  -d '{"agent_name":"test-agent"}'
```

Должно вернуть API ключ.

### 6.3 Проверить Monero
```bash
# С Hetzner сервера
monero-status

# Должно показать:
# Daemon Status: active
# Wallet RPC Status: active
# Blockchain Height: xxxxxxx
```

---

## 🎉 Готово!

### Что у тебя есть:
- **API:** https://api.yourdomain.com
- **Health Check:** https://api.yourdomain.com/health
- **Docs:** https://api.yourdomain.com/docs
- **Monero RPC:** ваш_hetzner_ip:18082 (только с Railway!)

### Стоимость:
- Railway: ~$25/мес (зависит от нагрузки)
- Hetzner: €3.79 (~$4)/мес
- Домен: ~$1/мес
- **Итого: ~$30/мес**

---

## 🚨 Troubleshooting

### Railway: "Build failed"
```bash
# Проверь логи в Railway Dashboard → Deployments → View Logs
# Обычно проблема с requirements.txt
```

### Hetzner: "Connection refused"
```bash
# На сервере выполни:
sudo ufw allow 18082
sudo systemctl restart monero-wallet-rpc
```

### Cloudflare: "Error 525 SSL Handshake"
```
Cloudflare → SSL/TLS → Overview
Поменяй с "Full (strict)" на "Full"
```

### Railway: "Database connection failed"
```
Проверь что DATABASE_URL правильный
В Railway: Variables → должно быть ${{Postgres.DATABASE_URL}}
```

---

## 📝 Дальше

- [ ] Создать первого агента через API
- [ ] Настроить webhook URL
- [ ] Подключить TypeScript SDK
- [ ] Настроить мониторинг (опционально)

**Поздравляю! StealthPay теперь работает! 🚀**
