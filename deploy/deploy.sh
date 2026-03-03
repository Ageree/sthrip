#!/bin/bash
set -e

# StealthPay Production Deploy Script
# Usage: ./deploy.sh [environment]

ENVIRONMENT=${1:-production}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="stealthpay"

echo "=== StealthPay Deployment ==="
echo "Environment: $ENVIRONMENT"
echo "Time: $(date)"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Check Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi
    
    # Check Docker Compose
    if ! command -v docker-compose &> /dev/null; then
        log_error "Docker Compose is not installed"
        exit 1
    fi
    
    # Check .env file
    if [ ! -f "$SCRIPT_DIR/.env" ]; then
        log_error ".env file not found in $SCRIPT_DIR"
        log_info "Please create .env file from .env.example"
        exit 1
    fi
    
    log_info "Prerequisites check passed"
}

# Create necessary directories
setup_directories() {
    log_info "Setting up directories..."
    
    mkdir -p "$SCRIPT_DIR/nginx/ssl"
    mkdir -p "$SCRIPT_DIR/backups"
    mkdir -p "$SCRIPT_DIR/logs"
    
    log_info "Directories created"
}

# Backup database
backup_database() {
    log_info "Creating database backup..."
    
    BACKUP_FILE="$SCRIPT_DIR/backups/backup_$(date +%Y%m%d_%H%M%S).sql"
    
    if docker-compose -f "$SCRIPT_DIR/docker-compose.yml" ps | grep -q postgres; then
        docker-compose -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres pg_dump \
            -U "${POSTGRES_USER:-stealthpay}" \
            "${POSTGRES_DB:-stealthpay}" > "$BACKUP_FILE" || {
            log_warn "Backup failed, continuing..."
            return
        }
        log_info "Backup saved to: $BACKUP_FILE"
    else
        log_warn "PostgreSQL not running, skipping backup"
    fi
}

# Pull latest images
pull_images() {
    log_info "Pulling latest images..."
    
    cd "$SCRIPT_DIR"
    docker-compose pull
    
    log_info "Images pulled"
}

# Build application
build_app() {
    log_info "Building application..."
    
    cd "$SCRIPT_DIR/.."
    docker build -f deploy/Dockerfile -t stealthpay/api:latest .
    
    log_info "Application built"
}

# Deploy
deploy() {
    log_info "Starting deployment..."
    
    cd "$SCRIPT_DIR"
    
    # Stop existing containers gracefully
    log_info "Stopping existing containers..."
    docker-compose down --timeout 30
    
    # Start new containers
    log_info "Starting new containers..."
    docker-compose up -d
    
    # Wait for services to be healthy
    log_info "Waiting for services to be healthy..."
    sleep 10
    
    # Check health
    check_health
    
    log_info "Deployment complete!"
}

# Health check
check_health() {
    log_info "Checking service health..."
    
    MAX_RETRIES=30
    RETRY=0
    
    while [ $RETRY -lt $MAX_RETRIES ]; do
        if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
            log_info "✓ API is healthy"
            return 0
        fi
        
        RETRY=$((RETRY + 1))
        log_warn "Health check failed, retrying ($RETRY/$MAX_RETRIES)..."
        sleep 2
    done
    
    log_error "Health check failed after $MAX_RETRIES attempts"
    log_info "Check logs with: docker-compose logs -f api"
    return 1
}

# Clean up old backups and logs
cleanup() {
    log_info "Cleaning up old files..."
    
    # Keep only last 7 backups
    find "$SCRIPT_DIR/backups" -name "backup_*.sql" -type f -mtime +7 -delete 2>/dev/null || true
    
    # Clean old Docker images
    docker image prune -f --filter "until=168h" || true
    
    log_info "Cleanup complete"
}

# Rollback
rollback() {
    log_warn "Rolling back deployment..."
    
    cd "$SCRIPT_DIR"
    
    # Get last backup
    LATEST_BACKUP=$(ls -t "$SCRIPT_DIR/backups"/backup_*.sql 2>/dev/null | head -n1)
    
    if [ -n "$LATEST_BACKUP" ]; then
        log_info "Restoring from backup: $LATEST_BACKUP"
        docker-compose exec -T postgres psql -U "${POSTGRES_USER:-stealthpay}" "${POSTGRES_DB:-stealthpay}" < "$LATEST_BACKUP"
    fi
    
    # Restart previous version
    docker-compose restart
    
    log_info "Rollback complete"
}

# Show status
show_status() {
    log_info "Current status:"
    cd "$SCRIPT_DIR"
    docker-compose ps
    echo ""
    log_info "Resource usage:"
    docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}" 2>/dev/null || true
}

# Main
main() {
    case "${2:-deploy}" in
        deploy)
            check_prerequisites
            setup_directories
            backup_database
            build_app
            deploy
            cleanup
            show_status
            ;;
        status)
            show_status
            ;;
        rollback)
            rollback
            ;;
        logs)
            cd "$SCRIPT_DIR" && docker-compose logs -f
            ;;
        stop)
            cd "$SCRIPT_DIR" && docker-compose down
            log_info "Services stopped"
            ;;
        *)
            echo "Usage: $0 [environment] [command]"
            echo ""
            echo "Environments:"
            echo "  production    Deploy to production (default)"
            echo "  staging       Deploy to staging"
            echo ""
            echo "Commands:"
            echo "  deploy        Deploy application (default)"
            echo "  status        Show service status"
            echo "  rollback      Rollback to previous version"
            echo "  logs          Show logs"
            echo "  stop          Stop all services"
            exit 1
            ;;
    esac
}

# Load environment variables
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# Run main function
main "$@"
