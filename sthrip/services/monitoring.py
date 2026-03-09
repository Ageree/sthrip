"""
Health monitoring and alerting for Sthrip
"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import threading

logger = logging.getLogger("sthrip.monitoring")

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class HealthCheck:
    """Health check configuration"""
    name: str
    check_fn: Callable[[], Dict]
    interval_seconds: int = 60
    timeout_seconds: int = 10
    last_check: Optional[datetime] = None
    last_result: Optional[Dict] = None
    failures: int = 0
    max_failures: int = 3


@dataclass
class Alert:
    """Alert record"""
    id: str
    severity: AlertSeverity
    title: str
    message: str
    source: str
    timestamp: datetime
    acknowledged: bool = False
    resolved: bool = False


class HealthMonitor:
    """
    Health monitoring service
    
    Monitors:
    - Database connectivity
    - Redis connectivity
    - Monero wallet RPC
    - API response times
    - Disk space
    - Memory usage
    """
    
    def __init__(self):
        self.checks: Dict[str, HealthCheck] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._alerts: List[Alert] = []
        self._alert_handlers: List[Callable] = []
    
    def register_check(self, check: HealthCheck):
        """Register a health check"""
        self.checks[check.name] = check
    
    def register_alert_handler(self, handler: Callable[[Alert], None]):
        """Register alert handler"""
        self._alert_handlers.append(handler)
    
    def _run_check(self, check: HealthCheck) -> Dict:
        """Run single health check"""
        try:
            result = check.check_fn()
            result["timestamp"] = datetime.now(timezone.utc).isoformat()
            result["check_name"] = check.name
            
            # Reset failures on success
            if result.get("healthy", False):
                check.failures = 0
            else:
                check.failures += 1
            
            check.last_result = result
            check.last_check = datetime.now(timezone.utc)
            
            # Generate alert if max failures reached
            if check.failures >= check.max_failures:
                self._create_alert(
                    severity=AlertSeverity.CRITICAL if check.failures >= 5 else AlertSeverity.WARNING,
                    title=f"Health check failed: {check.name}",
                    message=f"{check.name} has failed {check.failures} times",
                    source=check.name
                )
            
            return result
            
        except Exception as e:
            check.failures += 1
            result = {
                "healthy": False,
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "check_name": check.name
            }
            check.last_result = result
            check.last_check = datetime.now(timezone.utc)
            return result
    
    def _create_alert(self, severity: AlertSeverity, title: str, message: str, source: str):
        """Create and dispatch alert"""
        alert = Alert(
            id=f"alert_{int(time.time())}_{hash(title) % 10000}",
            severity=severity,
            title=title,
            message=message,
            source=source,
            timestamp=datetime.now(timezone.utc)
        )
        
        self._alerts.append(alert)
        
        # Dispatch to handlers
        for handler in self._alert_handlers:
            try:
                handler(alert)
            except Exception:
                pass
    
    def run_all_checks(self) -> Dict[str, Dict]:
        """Run all health checks once"""
        results = {}
        for name, check in self.checks.items():
            results[name] = self._run_check(check)
        return results
    
    def start_monitoring(self):
        """Start background monitoring thread"""
        if self.running:
            return
        
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
    
    def stop_monitoring(self):
        """Stop monitoring"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
    
    def _monitor_loop(self):
        """Background monitoring loop"""
        while self.running:
            for check in self.checks.values():
                if check.last_check is None or \
                   (datetime.now(timezone.utc) - check.last_check).seconds >= check.interval_seconds:
                    self._run_check(check)
            
            time.sleep(1)
    
    def get_health_report(self) -> Dict:
        """Get complete health report"""
        checks_status = {}
        healthy_count = 0
        
        for name, check in self.checks.items():
            if check.last_result:
                checks_status[name] = check.last_result
                if check.last_result.get("healthy", False):
                    healthy_count += 1
        
        total = len(self.checks)
        
        return {
            "status": "healthy" if healthy_count == total else "degraded" if healthy_count > 0 else "unhealthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks_total": total,
            "checks_healthy": healthy_count,
            "checks": checks_status,
            "unacknowledged_alerts": len([a for a in self._alerts if not a.acknowledged and not a.resolved])
        }
    
    def get_alerts(self, severity: Optional[AlertSeverity] = None, unacknowledged_only: bool = False) -> List[Alert]:
        """Get alerts with optional filtering"""
        alerts = self._alerts
        
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        
        if unacknowledged_only:
            alerts = [a for a in alerts if not a.acknowledged and not a.resolved]
        
        return sorted(alerts, key=lambda a: a.timestamp, reverse=True)
    
    def acknowledge_alert(self, alert_id: str):
        """Acknowledge alert"""
        for alert in self._alerts:
            if alert.id == alert_id:
                alert.acknowledged = True
                return True
        return False
    
    def resolve_alert(self, alert_id: str):
        """Mark alert as resolved"""
        for alert in self._alerts:
            if alert.id == alert_id:
                alert.resolved = True
                return True
        return False


class AlertManager:
    """
    Alert dispatching to multiple channels
    """
    
    def __init__(self):
        self.channels: Dict[str, Callable] = {}
    
    def register_channel(self, name: str, handler: Callable[[Alert], None]):
        """Register alert channel"""
        self.channels[name] = handler
    
    def dispatch(self, alert: Alert):
        """Dispatch alert to all channels"""
        for name, handler in self.channels.items():
            try:
                handler(alert)
            except Exception as e:
                print(f"Alert channel {name} failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# BUILT-IN HEALTH CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

def create_database_health_check() -> HealthCheck:
    """Create database connectivity check"""
    def check():
        try:
            from sqlalchemy import text
            from ..db.database import get_engine
            engine = get_engine()
            with engine.connect() as conn:
                result = conn.execute(text("SELECT 1"))
                return {
                    "healthy": True,
                    "details": "Database connection OK"
                }
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e)
            }
    
    return HealthCheck(
        name="database",
        check_fn=check,
        interval_seconds=30
    )


def create_redis_health_check() -> HealthCheck:
    """Create Redis connectivity check"""
    def check():
        try:
            from .rate_limiter import get_rate_limiter
            limiter = get_rate_limiter()
            
            if not limiter.use_redis:
                return {
                    "healthy": True,
                    "details": "Redis not configured (using local cache)"
                }
            
            limiter.redis.ping()
            info = limiter.redis.info()
            
            return {
                "healthy": True,
                "details": f"Redis OK (version {info.get('redis_version', 'unknown')})"
            }
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e)
            }
    
    return HealthCheck(
        name="redis",
        check_fn=check,
        interval_seconds=30
    )


def create_wallet_health_check() -> HealthCheck:
    """Create Monero wallet RPC check"""
    def check():
        try:
            from ..wallet import MoneroWalletRPC
            wallet = MoneroWalletRPC.from_env()
            height = wallet.get_height()
            
            return {
                "healthy": True,
                "details": f"Wallet RPC OK (height {height})"
            }
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e)
            }
    
    return HealthCheck(
        name="wallet_rpc",
        check_fn=check,
        interval_seconds=60
    )


def create_system_health_check() -> HealthCheck:
    """Create system resource check"""
    def check():
        if not PSUTIL_AVAILABLE:
            return {
                "healthy": True,
                "details": "psutil not installed"
            }
        
        # Memory check
        memory = psutil.virtual_memory()
        memory_healthy = memory.percent < 90
        
        # Disk check
        disk = psutil.disk_usage('/')
        disk_healthy = disk.percent < 90
        
        # CPU check (average over 1 second)
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_healthy = cpu_percent < 95
        
        healthy = memory_healthy and disk_healthy and cpu_healthy
        
        return {
            "healthy": healthy,
            "details": {
                "memory_percent": memory.percent,
                "disk_percent": disk.percent,
                "cpu_percent": cpu_percent
            }
        }
    
    return HealthCheck(
        name="system_resources",
        check_fn=check,
        interval_seconds=60
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════

# Debounce: track last dispatch time per alert source
_last_dispatch: Dict[str, float] = {}
_DEBOUNCE_SECONDS = 300  # 5 minutes


def dispatch_alert_webhook(alert: Alert) -> None:
    """
    Dispatch alert to configured webhook (Telegram or Discord).
    Set ALERT_WEBHOOK_URL env var. Debounced to 1 per source per 5 min.
    """
    webhook_url = os.getenv("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return

    # Debounce
    now = time.time()
    key = f"{alert.source}:{alert.severity.value}"
    last = _last_dispatch.get(key, 0)
    if now - last < _DEBOUNCE_SECONDS:
        return
    _last_dispatch[key] = now

    severity_emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(alert.severity.value, "❓")

    try:
        import requests

        if "api.telegram.org" in webhook_url:
            # Telegram Bot API: expects /bot<token>/sendMessage?chat_id=<id>
            text = (
                f"{severity_emoji} *{alert.title}*\n"
                f"{alert.message}\n"
                f"Source: `{alert.source}` | {alert.timestamp.isoformat()}"
            )
            requests.post(webhook_url, json={"text": text, "parse_mode": "Markdown"}, timeout=10)
        else:
            # Discord webhook (or generic)
            embed = {
                "title": f"{severity_emoji} {alert.title}",
                "description": alert.message,
                "color": {"info": 3447003, "warning": 16776960, "critical": 15158332}.get(alert.severity.value, 0),
                "fields": [
                    {"name": "Source", "value": alert.source, "inline": True},
                    {"name": "Severity", "value": alert.severity.value.upper(), "inline": True},
                ],
                "timestamp": alert.timestamp.isoformat(),
            }
            requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception:
        logger.warning("Failed to dispatch alert webhook", exc_info=True)


def setup_default_monitoring(include_wallet=False) -> HealthMonitor:
    """Set up monitoring with default health checks"""
    monitor = get_monitor()

    monitor.register_check(create_database_health_check())
    monitor.register_check(create_redis_health_check())
    monitor.register_check(create_system_health_check())

    if include_wallet:
        monitor.register_check(create_wallet_health_check())

    # Register webhook alert dispatch if configured
    if os.getenv("ALERT_WEBHOOK_URL"):
        monitor.register_alert_handler(dispatch_alert_webhook)

    return monitor


# Global monitor
_monitor: Optional[HealthMonitor] = None


def get_monitor() -> HealthMonitor:
    """Get global health monitor"""
    global _monitor
    if _monitor is None:
        _monitor = HealthMonitor()
    return _monitor
