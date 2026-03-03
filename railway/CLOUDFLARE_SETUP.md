# Cloudflare DNS Setup for StealthPay

## Что делаем
Настраиваем домен + SSL + защиту от DDoS для твоего API.

## Шаг 1: Добавить домен в Cloudflare

1. Открой https://dash.cloudflare.com
2. Нажми "Add a Site"
3. Введи свой домен (например: `stealthpay.io` или `api.yourdomain.com`)
4. Выбери Free Plan ($0)

## Шаг 2: Сменить NS-записи у регистратора

Cloudflare даст тебе 2 nameservers типа:
```
alex.ns.cloudflare.com
lara.ns.cloudflare.com
```

**Если домен на Namecheap:**
1. Залогинься на namecheap.com
2. Domain List → Manage → Nameservers
3. Выбери "Custom DNS"
4. Вставь 2 NS от Cloudflare
5. Save

**Если домен куплен в Cloudflare:**
- Ничего делать не нужно, NS уже правильные

Жди 5-30 минут пока обновятся DNS.

## Шаг 3: Настроить DNS записи

В Cloudflare перейди в раздел **DNS** → **Records**

Удали все записи если есть, и добавь:

### Для Railway (рекомендуется):

```
Type: CNAME
Name: api
Target: your-railway-app.up.railway.app
Proxy status: Proxied (оранжевое облако)
TTL: Auto
```

Или для корневого домена:
```
Type: CNAME
Name: @
Target: your-railway-app.up.railway.app
Proxy status: Proxied
```

### Для поддомена Hetzner (Monero RPC):
```
Type: A
Name: monero
IPv4 address: YOUR_HETZNER_IP
Proxy status: DNS only (серое облако!)
TTL: Auto
```

**Важно:** Для Monero RPC облако должно быть СЕРЫМ (DNS only), иначе Cloudflare заблокирует RPC соединения.

## Шаг 4: SSL/TLS Настройка

Перейди в раздел **SSL/TLS**:

1. **Overview** → Выбери **Full (strict)**
2. **Edge Certificates**:
   - Always Use HTTPS: ON
   - Automatic HTTPS Rewrites: ON
3. **Origin Certificates** (опционально):
   - Можно создать Origin CA certificate для Railway

## Шаг 5: Security настройки

Перейди в раздел **Security** → **WAF**:

1. **Security Level:** High
2. **Bot Fight Mode:** ON

Правила для API:
```
Field: URI Path
Operator: contains
Value: /v2/
Action: Managed Challenge
```

## Шаг 6: CORS для Railway (если нужно)

Перейди в **Rules** → **Transform Rules**:

```
Field: URI Path
Operator: starts with
Value: /
```

**Modify Request Header:**
- X-Forwarded-Host: your-domain.com

## Проверка

После настройки:

```bash
# Проверь что домен работает
nslookup api.yourdomain.com

# Проверь SSL
curl -I https://api.yourdomain.com/health

# Должно показать HTTP/2 200
```

## Готово! 🎉

Теперь:
- API доступно по HTTPS: `https://api.yourdomain.com`
- SSL автоматически обновляется
- Защита от DDoS включена
- CDN работает

**Следующий шаг:** Верниься к Railway инструкции и добавь свой домен в Railway dashboard.
