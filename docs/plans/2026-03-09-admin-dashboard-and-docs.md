# Admin Dashboard & API Docs

## Goal
1. Lightweight admin dashboard for monitoring agents, balances, transactions
2. Branded API documentation page for agent developers

---

## Phase 1: Admin Dashboard

Minimal internal tool. No auth framework — protect with `ADMIN_API_KEY` header.

### Stack
- **FastAPI + Jinja2 templates** (no separate frontend, no JS framework)
- Server-side rendered HTML, Tailwind CSS via CDN
- Lives in `api/admin_ui/` — same deploy, no extra service

### Pages

| Page | Data Source | Purpose |
|------|-----------|---------|
| `/admin/` | `get_stats()` | Overview: total agents, by tier, active 24h, total volume |
| `/admin/agents` | `Agent` table | List agents, search, filter by tier/status |
| `/admin/agents/{id}` | Agent + reputation + balances | Agent detail: balance, transactions, reputation |
| `/admin/transactions` | `hub_routes` table | Recent payments, filter by status/agent |
| `/admin/balances` | `agent_balances` table | All balances, sort by amount |
| `/admin/deposits` | `pending_deposits` table | Pending deposits, confirmation status |

### Auth
- Cookie-based session after login with `ADMIN_API_KEY`
- Middleware checks cookie on all `/admin/*` routes
- Auto-logout after 8 hours

### Tasks
1. Create `api/admin_ui/` directory with templates
2. Add Jinja2 dependency, configure template directory
3. Create base template (layout, nav, Tailwind)
4. Implement overview page with stats
5. Implement agents list with search/filter
6. Implement agent detail page
7. Implement transactions list with filters
8. Implement balances page
9. Add login page and cookie auth middleware
10. Add to `api/main_v2.py` router includes

---

## Phase 2: API Docs Page

FastAPI Swagger UI is functional but unbranded. Replace with customized version.

### Approach
- Custom Swagger UI via `FastAPI(docs_url=None)` + manual mount
- Or: **Redoc** (cleaner for public docs) at `/docs`
- Keep Swagger at `/docs/playground` for interactive testing

### Tasks
1. Add custom Redoc page at `/docs` with branding (logo, colors, description)
2. Move Swagger UI to `/docs/playground`
3. Enrich OpenAPI schema:
   - Group endpoints by tag (Registration, Payments, Balance, Discovery, Admin)
   - Add request/response examples to every endpoint
   - Add authentication description (Bearer token flow)
   - Add error response schemas (401, 403, 404, 429)
4. Add "Getting Started" section in OpenAPI description:
   - Register agent → get API key
   - Check balance → deposit XMR
   - Send payment
   - MCP server setup instructions
5. Add rate limit info to endpoint descriptions

---

## Estimated Scope
- Phase 1: ~6 files, ~800 lines (templates + routes)
- Phase 2: ~2 files, ~200 lines (custom docs + schema enrichment)
- No new dependencies except `jinja2` (already a FastAPI extra)
- No separate deploy — everything in the existing API service

## Priority
Phase 2 (docs) first — helps agent developers onboard. Phase 1 (dashboard) second.
