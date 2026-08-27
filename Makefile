# patchbay — developer convenience targets
# Run `make` with no arguments to see this help.

# Auto-detect container runtime: podman beats docker when both are present.
# Override with: make COMPOSE="docker compose" poll
_RUNTIME := $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
COMPOSE  ?= $(shell \
  if command -v podman-compose >/dev/null 2>&1; then \
    echo podman-compose; \
  elif $(_RUNTIME) compose version >/dev/null 2>&1; then \
    echo "$(_RUNTIME) compose"; \
  elif command -v docker-compose >/dev/null 2>&1; then \
    echo docker-compose; \
  else \
    echo "docker compose"; \
  fi)

WEB_SVC     = patchbay
POLLER_SVC  = patchbay-poller
VENV        = .venv/bin

# ── colours ──────────────────────────────────────────────────────────────────
BOLD   := \033[1m
RESET  := \033[0m
CYAN   := \033[36m
GREEN  := \033[32m
YELLOW := \033[33m
BLUE   := \033[34m
DIM    := \033[2m

.DEFAULT_GOAL := help

.PHONY: help poll snapshot show logs restart pull shell \
        test lint fmt up down

# ── help ─────────────────────────────────────────────────────────────────────

help:
	@printf "\n"
	@printf "  $(BOLD)$(CYAN)patchbay$(RESET) — developer targets\n"
	@printf "  $(DIM)runtime: $(COMPOSE)$(RESET)\n"
	@printf "\n"
	@printf "  $(BOLD)$(GREEN)Container$(RESET)\n"
	@printf "  $(CYAN)  pull$(RESET)        Pull latest image and restart services\n"
	@printf "  $(CYAN)  up$(RESET)          Start services (detached)\n"
	@printf "  $(CYAN)  down$(RESET)        Stop and remove services\n"
	@printf "  $(CYAN)  restart$(RESET)     Restart all services\n"
	@printf "  $(CYAN)  logs$(RESET)        Follow combined log output\n"
	@printf "  $(CYAN)  shell$(RESET)       Open a shell in the web container\n"
	@printf "\n"
	@printf "  $(BOLD)$(YELLOW)Data$(RESET)\n"
	@printf "  $(YELLOW)  poll$(RESET)        Run one full collector poll cycle\n"
	@printf "  $(YELLOW)  snapshot$(RESET)    Write a self-contained HTML snapshot\n"
	@printf "  $(YELLOW)  show$(RESET)        Show devices, links, subnets, VLANs, endpoints\n"
	@printf "\n"
	@printf "  $(BOLD)$(BLUE)Development$(RESET)\n"
	@printf "  $(BLUE)  test$(RESET)        Run the test suite (hermetic, no network)\n"
	@printf "  $(BLUE)  lint$(RESET)        Ruff lint check\n"
	@printf "  $(BLUE)  fmt$(RESET)         Ruff format (in-place)\n"
	@printf "\n"
	@printf "  $(DIM)Override: COMPOSE, WEB_SVC, POLLER_SVC, VENV$(RESET)\n"
	@printf "\n"

# ── container ─────────────────────────────────────────────────────────────────

pull:
	$(COMPOSE) pull
	$(COMPOSE) up -d

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f

shell:
	$(COMPOSE) exec $(WEB_SVC) sh

# ── data ──────────────────────────────────────────────────────────────────────

poll:
	$(COMPOSE) exec $(WEB_SVC) patchbay poll

snapshot:
	$(COMPOSE) exec $(WEB_SVC) patchbay snapshot

show:
	@for item in devices links subnets vlans endpoints; do \
		printf "\n$(BOLD)$(CYAN)$$item$(RESET)\n"; \
		$(COMPOSE) exec $(WEB_SVC) patchbay show $$item; \
	done

# ── development ───────────────────────────────────────────────────────────────

test:
	$(VENV)/pytest

lint:
	$(VENV)/ruff check src tests

fmt:
	$(VENV)/ruff format src tests
