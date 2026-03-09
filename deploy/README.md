# Sthrip Production Deployment

Production-ready Docker deployment для Sthrip.

## 📁 Структура

```
deploy/
├── docker-compose.yml       # Production Docker Compose
├── Dockerfile              # Application container
├── .env.example            # Environment template
├── deploy.sh               # Deploy script
├── Makefile                # Convenience commands
├── CHECKLIST.md            # Pre-deploy checklist
├── nginx/
│   └── nginx.conf          # Reverse proxy config
└── init-scripts/
    └── 01-init-db.sh       # Database initialization
```

## 🚀 Быстрый старт

### 1. Подготовка сервера

Требования:
- Ubuntu 20.04+ / Debian 11+ / CentOS 8+
- Docker 20.10+
- Docker Compose 2.0+
- 2 CPU cores, 4GB RAM minimum
- 50GB disk space

### 2. Настройка окружения

```bash
cd sthrip/deploy
cp .env.example .env
nano .env  # Заполните ваши данные
```

### 3. Deploy

```bash
# Первый запуск
make init
make ssl-generate  # или скопируйте ваши сертификаты
make deploy

# Или через скрипт
./deploy.sh production deploy
```

## 🔧 Команды

```bash
make help         # Показать все команды
make deploy       # Deploy приложения
make status       # Статус сервисов
make logs         # Просмотр логов
make db-backup    # Бэкап базы
make rollback     # Откат изменений
make clean        # Очистка ресурсов
```

## 🌐 Сервисы

После deploy будут доступны:

| Сервис | URL | Описание |
|--------|-----|----------|
| API | https://api.sthrip.io | Основное API |
| Health | https://api.sthrip.io/health | Health check |
| PostgreSQL | localhost:5432 | База данных |
| Redis | localhost:6379 | Кэш и rate limiting |

## 🔐 Безопасность

- Все пароли хранятся в `.env`
- SSL/TLS для всех соединений
- Rate limiting на уровне Nginx и API
- Non-root пользователь в контейнерах
- Закрытые порты (только 80/443 открыты наружу)

## 📊 Мониторинг

```bash
# Проверка здоровья
make health

# Ресурсы
make monitor

# Логи
make logs-api
make logs-db
```

## 🆘 Troubleshooting

### Проблема: API не стартует
```bash
make logs-api
make restart-api
```

### Проблема: База данных
```bash
make db-shell
# Внутри psql:
\dt  # список таблиц
```

### Полный сброс
```bash
make stop-all
make deploy
```

## 📋 Что нужно для deploy

См. [CHECKLIST.md](CHECKLIST.md) для полного списка credentials.

Кратко:
1. SSH доступ к серверу
2. Domain name
3. PostgreSQL password
4. Redis password
5. Monero RPC credentials
6. SSL certificates (или Let's Encrypt)

## 🔄 Обновление

```bash
# Обновить код и redeploy
git pull
make update

# Или вручную
git pull
make build
make deploy
```

## 💾 Бэкапы

Автоматические бэкапы при каждом deploy. Ручной бэкап:

```bash
make db-backup
# Файл: backups/backup_YYYYMMDD_HHMMSS.sql
```

Восстановление:

```bash
make db-restore FILE=backups/backup_xxx.sql
```

## 📞 Поддержка

При проблемах с deploy:
1. Проверьте логи: `make logs`
2. Проверьте статус: `make status`
3. Попробуйте rollback: `make rollback`
