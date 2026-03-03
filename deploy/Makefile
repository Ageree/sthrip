# StealthPay Deployment Makefile

.PHONY: help build deploy status logs rollback stop clean ssl-check

# Default environment
ENV ?= production
COMPOSE_FILE = docker-compose.yml

help: ## Show this help
	@echo "StealthPay Deployment Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

init: ## Initialize deployment (first time setup)
	@echo "Initializing StealthPay deployment..."
	@cp -n .env.example .env 2>/dev/null || echo ".env already exists"
	@mkdir -p nginx/ssl backups logs
	@echo "Please edit .env file with your configuration"
	@echo "Then run: make ssl-generate (for self-signed) or copy your certificates to nginx/ssl/"

ssl-generate: ## Generate self-signed SSL certificate (for testing)
	@echo "Generating self-signed SSL certificate..."
	@mkdir -p nginx/ssl
	@openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout nginx/ssl/privkey.pem \
		-out nginx/ssl/fullchain.pem \
		-subj "/C=US/ST=State/L=City/O=StealthPay/CN=localhost"
	@echo "Self-signed certificate generated"

ssl-check: ## Check SSL certificate expiration
	@openssl x509 -in nginx/ssl/fullchain.pem -noout -dates -subject

build: ## Build Docker images
	@echo "Building Docker images..."
	@docker-compose -f $(COMPOSE_FILE) build --no-cache

deploy: ## Deploy application
	@echo "Deploying StealthPay..."
	@./deploy.sh $(ENV) deploy

deploy-quick: ## Quick deploy (no backup)
	@echo "Quick deploying..."
	@docker-compose -f $(COMPOSE_FILE) up -d --build

status: ## Show service status
	@docker-compose -f $(COMPOSE_FILE) ps
	@echo ""
	@echo "Resource Usage:"
	@docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}"

logs: ## Show logs (follow mode)
	@docker-compose -f $(COMPOSE_FILE) logs -f --tail=100

logs-api: ## Show API logs only
	@docker-compose -f $(COMPOSE_FILE) logs -f --tail=100 api

logs-db: ## Show database logs only
	@docker-compose -f $(COMPOSE_FILE) logs -f --tail=100 postgres

db-shell: ## Open PostgreSQL shell
	@docker-compose -f $(COMPOSE_FILE) exec postgres psql -U stealthpay -d stealthpay

db-backup: ## Create database backup
	@mkdir -p backups
	@docker-compose -f $(COMPOSE_FILE) exec -T postgres pg_dump -U stealthpay stealthpay > backups/backup_$(shell date +%Y%m%d_%H%M%S).sql
	@echo "Backup created"

db-restore: ## Restore database from backup (make db-restore FILE=backups/backup_xxx.sql)
	@if [ -z "$(FILE)" ]; then echo "Usage: make db-restore FILE=backups/backup_xxx.sql"; exit 1; fi
	@docker-compose -f $(COMPOSE_FILE) exec -T postgres psql -U stealthpay stealthpay < $(FILE)
	@echo "Database restored from $(FILE)"

rollback: ## Rollback to previous version
	@echo "Rolling back..."
	@./deploy.sh $(ENV) rollback

stop: ## Stop all services
	@docker-compose -f $(COMPOSE_FILE) down --timeout 30

stop-all: ## Stop and remove all containers, networks, volumes
	@docker-compose -f $(COMPOSE_FILE) down -v --remove-orphans

restart: ## Restart all services
	@docker-compose -f $(COMPOSE_FILE) restart

restart-api: ## Restart API only
	@docker-compose -f $(COMPOSE_FILE) restart api

clean: ## Clean up unused Docker resources
	@docker system prune -f
	@docker volume prune -f

update: ## Update to latest version and deploy
	@git pull
	@make build
	@make deploy

health: ## Check service health
	@curl -s http://localhost:8000/health | python -m json.tool 2>/dev/null || curl -s http://localhost:8000/health

test: ## Run tests (if available)
	@docker-compose -f $(COMPOSE_FILE) exec api python -m pytest tests/ -v 2>/dev/null || echo "Tests not configured"

# Development helpers
dev-up: ## Start development environment
	@docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

dev-down: ## Stop development environment
	@docker-compose -f docker-compose.yml -f docker-compose.dev.yml down

# Monitoring
monitor: ## Show real-time monitoring
	@watch -n 1 'docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"'

# Security
security-scan: ## Run security scan on images
	@docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
		aquasec/trivy image stealthpay/api:latest

# Info
info: ## Show deployment info
	@echo "StealthPay Deployment Info"
	@echo "=========================="
	@echo "Environment: $(ENV)"
	@echo "Docker Compose: $(COMPOSE_FILE)"
	@echo ""
	@echo "Services:"
	@docker-compose -f $(COMPOSE_FILE) config --services
	@echo ""
	@echo "Volumes:"
	@docker-compose -f $(COMPOSE_FILE) config --volumes
