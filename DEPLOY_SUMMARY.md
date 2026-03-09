# Sthrip Production Deployment — Summary

## ✅ Стек выбран

| Компонент | Сервис | Цена | Зачем |
|-----------|--------|------|-------|
| **API + DB + Cache** | Railway | ~$25/мес | Managed PostgreSQL + Redis + автодеплой |
| **Monero Node** | Hetzner CX11 | ~$4/мес | VPS для Monero wallet RPC |
| **DNS + SSL** | Cloudflare | $0 | SSL сертификаты, DDoS защита |
| **Domain** | Namecheap/Cloudflare | ~$10/год | Красивый адрес |

**Итого:** ~$30/мес (~$360/год)

---

## 📦 Что я подготовил

### Файлы для Railway деплоя:
```
railway.toml              # Конфиг деплоя
railway.json              # Альтернативный конфиг
railway/
  └── Dockerfile.railway  # Docker image для Railway
```

### Файлы для Hetzner (Monero):
```
hetzner/
  └── setup-monero.sh     # Автоматическая установка Monero
```

### Документация:
```
DEPLOY_GUIDE.md           # Пошаговая инструкция
railway/
  └── CLOUDFLARE_SETUP.md # Настройка DNS + SSL
```

### Локальная разработка:
```
docker-compose.dev.yml    # PostgreSQL + Redis + API локально
```

---

## 🚀 Быстрый старт (копируй и вставляй)

### 1. Регистрация (5 мин)
```
1. https://railway.app → Continue with GitHub
2. https://console.hetzner.cloud → Регистрация
3. https://dash.cloudflare.com → Регистрация
4. Купить домен (например sthrip.io)
```

### 2. Hetzner VPS (5 мин)
```bash
# После создания VPS на Hetzner
ssh root@YOUR_HETZNER_IP

# Запустить установку Monero
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/sthrip/hetzner/setup-monero.sh | bash

# Сохранить выведенные credentials!
```

### 3. Railway Deploy (5 мин)
```
1. Railway Dashboard → New Project → Deploy from GitHub
2. Выбрать репозиторий с кодом
3. Добавить PostgreSQL: New → Database → PostgreSQL
4. Добавить Redis: New → Database → Redis
5. Добавить Variables (см. ниже)
6. Deploy!
```

### 4. Cloudflare DNS (5 мин)
```
1. Add Site → ввести домен
2. Сменить NS у регистратора
3. Добавить CNAME:
   - Name: api
   - Target: your-app.up.railway.app
   - Proxy: Enabled (оранжевое)
4. SSL/TLS → Full (strict)
```

---

## 🔐 Переменные окружения (Railway)

В Railway Dashboard → Variables:

```env
# Database (автоматически)
DATABASE_URL=${{Postgres.DATABASE_URL}}

# Redis (автоматически)
REDIS_URL=${{Redis.REDIS_URL}}

# Monero (из Hetzner)
MONERO_RPC_HOST=78.46.123.45        # IP с Hetzner
MONERO_RPC_PORT=18082
MONERO_RPC_USER=sthrip
MONERO_RPC_PASS=xxxxxxxxxxxxxxxxxxxx # Из setup-monero.sh

# Security
ADMIN_API_KEY=GENERATE_RANDOM_32CHAR
SECRET_KEY=GENERATE_RANDOM_32CHAR

# CORS
CORS_ORIGINS=https://yourdomain.com
```

---

## 📋 Что мне нужно от тебя

Для полного deploy мне нужны:

### Вариант A: Доступы (я сделаю всё сам)
```
1. GitHub repo с кодом (добавь меня collaborator)
2. Railway токен или доступ
3. Hetzner API токен
4. Cloudflare API токен
```

### Вариант B: Ты делаешь по инструкции
```
Я даю пошаговую инструкцию, ты:
1. Регистрируешься
2. Создаёшь сервисы
3. Вставляешь нужные значения
4. Я проверяю и помогаю если не работает
```

### Вариант C: Ты даёшь credentials (я настрою)
```
Ты даёшь мне:
1. Railway project URL
2. Hetzner server IP + root password
3. Cloudflare login
4. Я заходу и настраиваю
```

---

## 🎯 Следующие шаги

Выбери вариант и дай мне знать:

**A.** Я дам тебе доступы — настрой всё сам  
**B.** Дай пошаговую инструкцию — я сделаю по ней  
**C.** Вот мои credentials — настрой за меня  

---

## 🆘 Помощь

Если что-то пошло не так:

```bash
# Проверить API
curl https://api.yourdomain.com/health

# Проверить Monero
ssh root@HETZNER_IP "monero-status"

# Логи Railway
# Railway Dashboard → Deployments → View Logs

# Логи Hetzner
ssh root@HETZNER_IP "journalctl -u monerod -f"
```

---

**Готов к деплою! 🚀**

Какой вариант выбираешь? A, B или C?
