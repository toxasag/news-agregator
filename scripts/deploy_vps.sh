#!/usr/bin/env bash
#
# VPS deployment for SvodkaPress.
#
# Brings the production stack up on a fresh or existing host, in an order chosen so
# a misconfigured deployment fails *before* it can half-replace a running one:
#
#   1. Prerequisites — Docker and Compose, with versions.
#   2. Configuration — .env present, no placeholder secrets, adequate key lengths.
#   3. Compose validation — both files parse and merge.
#   4. Build and start.
#   5. Alembic migrations, inside the app container.
#   6. Health gates — /health then /health/ready.
#   7. Worker states — the three ARQ services are running.
#
# Steps 1-3 touch nothing. A deployment that would fail on a missing SECRET_KEY
# fails while the previous version is still serving, which is the entire reason the
# validation runs before the build rather than after it.
#
# Usage:
#   ./scripts/deploy_vps.sh                 # full deployment
#   ./scripts/deploy_vps.sh --check-only    # validate and exit, change nothing
#   ./scripts/deploy_vps.sh --skip-build    # restart without rebuilding images
#
set -Eeuo pipefail

# `pipefail` plus `-e` is what makes a failing step inside a pipe abort the script
# instead of being masked by a successful `tee` or `grep`.

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

readonly COMPOSE_BASE="${PROJECT_DIR}/docker-compose.yml"
readonly COMPOSE_PROD="${PROJECT_DIR}/docker-compose.prod.yml"
readonly ENV_FILE="${PROJECT_DIR}/.env"

# Bound to loopback in the prod compose, so the probe targets the same interface a
# host reverse proxy would.
readonly HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
readonly READY_URL="${READY_URL:-http://127.0.0.1:8000/health/ready}"

# The app waits on Postgres and runs migrations, so first-boot readiness is not
# instant. 90s at 3s intervals is generous enough for a cold 4 GB VPS.
readonly HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
readonly HEALTH_INTERVAL="${HEALTH_INTERVAL:-3}"

readonly MIN_SECRET_LENGTH=32

# Values published in .env.example. Shipping any of them to production means the
# deployment is running with a secret an attacker can read from the repository.
readonly PLACEHOLDERS=(
  "insecure-development-secret-key-change-me"
  "change_me_generate_a_64_byte_url_safe_secret"
  "change_me_postgres_password"
  "svodkapress"
)

CHECK_ONLY=0
SKIP_BUILD=0

log()  { printf '\033[0;36m[deploy]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m  ok\033[0m    %s\n' "$*"; }
warn() { printf '\033[0;33m  warn\033[0m  %s\n' "$*" >&2; }
fail() { printf '\033[0;31m  fail\033[0m  %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: scripts/deploy_vps.sh [--check-only] [--skip-build] [--help]

  --check-only  Run every validation and exit without touching the stack.
  --skip-build  Restart with the existing images instead of rebuilding.
USAGE
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check-only) CHECK_ONLY=1 ;;
      --skip-build) SKIP_BUILD=1 ;;
      -h|--help)    usage; exit 0 ;;
      *)            fail "unknown argument: $1 (try --help)" ;;
    esac
    shift
  done
}

# --------------------------------------------------------------- prerequisites

compose() {
  # `docker compose` (v2 plugin) is preferred; the standalone `docker-compose`
  # binary is still what many older VPS images carry.
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

check_prerequisites() {
  log "Checking prerequisites"

  command -v docker >/dev/null 2>&1 \
    || fail "docker is not installed or not on PATH"
  ok "docker $(docker --version | awk '{print $3}' | tr -d ',')"

  # A daemon that is installed but not running is the most common first-deploy
  # failure, and its error otherwise surfaces much later as a confusing build
  # error.
  docker info >/dev/null 2>&1 \
    || fail "the docker daemon is not reachable; start it and check permissions"
  ok "docker daemon reachable"

  if docker compose version >/dev/null 2>&1; then
    ok "compose plugin $(docker compose version --short 2>/dev/null || echo v2)"
  elif command -v docker-compose >/dev/null 2>&1; then
    warn "using the legacy docker-compose binary; the v2 plugin is recommended"
  else
    fail "neither 'docker compose' nor 'docker-compose' is available"
  fi

  command -v curl >/dev/null 2>&1 \
    || fail "curl is required for the health checks"
  ok "curl present"
}

# ---------------------------------------------------------------- environment

env_value() {
  # Reads one key from .env without sourcing the file: `source` would execute
  # whatever it contains, and a password holding a backtick or `$(...)` would run
  # as a shell command with this script's privileges.
  local key="$1"
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "${ENV_FILE}" \
    | tail -n 1 \
    | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

require_env() {
  local key="$1" value
  value="$(env_value "${key}")"
  if [[ -z "${value}" ]]; then
    fail "${key} is empty in .env"
  fi
  printf '%s' "${value}"
}

check_not_placeholder() {
  local key="$1" value="$2" placeholder
  for placeholder in "${PLACEHOLDERS[@]}"; do
    if [[ "${value}" == "${placeholder}" ]]; then
      fail "${key} is still the .env.example placeholder"
    fi
  done
}

check_environment() {
  log "Validating configuration"

  [[ -f "${ENV_FILE}" ]] \
    || fail ".env not found at ${ENV_FILE} (copy .env.example and fill it in)"
  ok ".env present"

  # World-readable secrets on a shared host are a finding in their own right.
  local mode
  mode="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || stat -f '%Lp' "${ENV_FILE}")"
  if [[ "${mode}" != "600" && "${mode}" != "400" ]]; then
    warn ".env is mode ${mode}; 600 is recommended (chmod 600 .env)"
  else
    ok ".env permissions ${mode}"
  fi

  local app_env
  app_env="$(env_value APP_ENV)"
  if [[ "${app_env}" != "production" ]]; then
    warn "APP_ENV is '${app_env:-unset}', not 'production'"
  else
    ok "APP_ENV=production"
  fi

  local secret
  secret="$(require_env SECRET_KEY)"
  check_not_placeholder SECRET_KEY "${secret}"
  if (( ${#secret} < MIN_SECRET_LENGTH )); then
    fail "SECRET_KEY is ${#secret} characters; at least ${MIN_SECRET_LENGTH} required"
  fi
  ok "SECRET_KEY length ${#secret}"

  local pg_password
  pg_password="$(require_env POSTGRES_PASSWORD)"
  check_not_placeholder POSTGRES_PASSWORD "${pg_password}"
  ok "POSTGRES_PASSWORD set"

  # The dashboard cannot authenticate anybody without these, and the app's own
  # production validator refuses to boot — better to say so here than to have the
  # container exit in a restart loop.
  local admin_hash
  admin_hash="$(require_env ADMIN_PASSWORD_HASH)"
  if [[ "${admin_hash}" != \$2* ]]; then
    fail "ADMIN_PASSWORD_HASH is not a bcrypt hash; generate it with
       python -m app.core.security hash '<password>'"
  fi
  ok "ADMIN_PASSWORD_HASH looks like bcrypt"

  local fernet
  fernet="$(require_env FERNET_KEY)"
  if (( ${#fernet} != 44 )); then
    fail "FERNET_KEY is ${#fernet} characters; a Fernet key is 44 (python -m app.core.security genkey)"
  fi
  ok "FERNET_KEY length ${#fernet}"

  # gunicorn resolves every `forwarded_allow_ips` entry with
  # `ipaddress.ip_address`, which rejects CIDR notation. uvicorn's own middleware
  # accepts it, so a value like `172.16.0.0/12` looks correct, passes a local
  # uvicorn test, and then crash-loops the container under gunicorn with only
  # "does not appear to be an IPv4 or IPv6 address" in the logs. Caught here,
  # before the running deployment is replaced.
  local proxies
  proxies="$(env_value TRUSTED_PROXY_HOSTS)"
  if [[ "${proxies}" == *"/"* ]]; then
    fail "TRUSTED_PROXY_HOSTS contains CIDR notation (${proxies}), which gunicorn
       rejects. List individual addresses, e.g. the docker bridge gateway
       $(docker network inspect svodkapress_net \
         --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null \
         || echo 172.18.0.1)"
  fi
  if [[ "${proxies}" == "*" ]]; then
    fail "TRUSTED_PROXY_HOSTS='*' trusts forged X-Forwarded-* headers from any client"
  fi
  ok "TRUSTED_PROXY_HOSTS=${proxies:-unset}"

  # Binding 0.0.0.0 is legitimate when the reverse proxy is on another host, but
  # only if the port is actually filtered. Docker publishes ports with its own
  # DNAT rules in the nat table, which are evaluated BEFORE ufw's INPUT rules —
  # so `ufw deny 8000` looks correct and the port stays wide open to the internet.
  # DOCKER-USER is the chain Docker consults, and the only one worth checking.
  if [[ "$(env_value APP_BIND_ADDRESS)" == "0.0.0.0" ]]; then
    local port
    port="$(env_value APP_HOST_PORT)"
    port="${port:-8000}"
    if iptables -L DOCKER-USER -n 2>/dev/null | grep -q "dpt:${port}.*DROP\|DROP.*dpt:${port}"; then
      ok "port ${port} is bound to 0.0.0.0 and filtered in DOCKER-USER"
    else
      warn "APP_BIND_ADDRESS=0.0.0.0 publishes port ${port} to every interface."
      warn "A ufw rule does NOT cover it: Docker's DNAT runs before ufw's INPUT."
      warn "Restrict it in the chain Docker honours:"
      warn "  iptables -I DOCKER-USER 1 -p tcp --dport ${port} -s <proxy-ip> -j RETURN"
      warn "  iptables -I DOCKER-USER 2 -p tcp --dport ${port} -j DROP"
    fi
  fi

  # The host allowlist is checked three ways, because two of the three failure
  # modes leave the control switched off while still looking configured:
  #   unset      -> TrustedHostMiddleware is never registered
  #   `*` entry  -> Starlette sets allow_any and stops checking
  #   no loopback-> the container healthcheck gets a 400 and the container is
  #                 restarted despite serving correctly
  # The app refuses to boot on all three; failing here reports it before the
  # running deployment is replaced by one that cannot start.
  local hosts
  hosts="$(env_value ALLOWED_HOSTS)"
  if [[ -z "${hosts}" ]]; then
    fail "ALLOWED_HOSTS is empty, so the Host header would not be validated.
       List the public hostname plus 127.0.0.1 and localhost"
  fi
  # Matched as a comma-delimited element so a hostname legitimately containing an
  # asterisk-free `*` substring cannot trip this, and `*.example.com` still does.
  if [[ ",${hosts//[[:space:]]/}," == *",*,"* ]]; then
    fail "ALLOWED_HOSTS contains '*', which disables Host validation entirely.
       List each hostname explicitly"
  fi
  if [[ "${hosts}" != *"127.0.0.1"* ]]; then
    fail "ALLOWED_HOSTS omits 127.0.0.1, so the container healthcheck and this
       script's own probe would be rejected with 400"
  fi
  ok "ALLOWED_HOSTS=${hosts}"

  # AI_EMBEDDING_DIMENSIONS must equal the pgvector column width, and the app
  # refuses to boot when it does not. Caught here because the failure mode is
  # brutal: the containers are replaced, every one crash-loops on a config
  # validation error, and the site is down until the value is corrected.
  local dims
  dims="$(env_value AI_EMBEDDING_DIMENSIONS)"
  if [[ -n "${dims}" && "${dims}" != "384" ]]; then
    fail "AI_EMBEDDING_DIMENSIONS=${dims} but the schema stores vector(384).
       Embeddings are generated locally by fastembed, which emits 384 dimensions.
       Changing the width needs a migration plus an index rebuild."
  fi
  ok "AI_EMBEDDING_DIMENSIONS=${dims:-unset}"

  # Advisory rather than fatal: a deployment can legitimately start with some
  # integrations switched off.
  local key
  for key in OPENROUTER_API_KEY WP_BASE_URL WP_APP_PASSWORD; do
    if [[ -z "$(env_value "${key}")" ]]; then
      warn "${key} is empty; the matching pipeline stage will be disabled"
    fi
  done
}

# ------------------------------------------------------------------- compose

check_compose_files() {
  log "Validating compose files"

  [[ -f "${COMPOSE_BASE}" ]] || fail "missing ${COMPOSE_BASE}"
  [[ -f "${COMPOSE_PROD}" ]] || fail "missing ${COMPOSE_PROD}"

  # `config -q` parses and merges both files and resolves every variable, so a
  # typo or an unset interpolation is caught before anything is built.
  compose -f "${COMPOSE_BASE}" -f "${COMPOSE_PROD}" config -q \
    || fail "the merged compose configuration is invalid"
  ok "docker-compose.yml + docker-compose.prod.yml merge cleanly"

  # The override file is developer-only: it publishes Postgres and Redis to the
  # host, which must never happen on a public VPS. Passing -f explicitly is what
  # excludes it, so this is a reminder rather than a check on the file itself.
  if [[ -f "${PROJECT_DIR}/docker-compose.override.yml" ]]; then
    ok "override file present but excluded (both -f flags are passed explicitly)"
  fi
}

# --------------------------------------------------------------------- deploy

start_stack() {
  log "Starting the stack"
  local args=(-f "${COMPOSE_BASE}" -f "${COMPOSE_PROD}" up -d)
  if (( SKIP_BUILD == 0 )); then
    args+=(--build)
  else
    warn "skipping the image build (--skip-build)"
  fi
  compose "${args[@]}" || fail "compose up failed"
  ok "containers started"
}

run_migrations() {
  log "Applying database migrations"
  # Run inside the app container so the migration uses the same image, driver and
  # network as the application. Running Alembic from the host would need the DSN
  # to resolve `postgres` from outside the compose network, which it cannot.
  compose -f "${COMPOSE_BASE}" -f "${COMPOSE_PROD}" exec -T app \
    alembic upgrade head \
    || fail "alembic upgrade head failed; the stack is up but the schema is stale"
  ok "schema at head"
}

probe() {
  local url="$1" want="$2" attempt body
  for (( attempt = 1; attempt <= HEALTH_RETRIES; attempt++ )); do
    if body="$(curl -fsS --max-time 10 "${url}" 2>/dev/null)"; then
      if [[ "${body}" == *"\"${want}\""* ]]; then
        printf '%s' "${body}"
        return 0
      fi
    fi
    sleep "${HEALTH_INTERVAL}"
  done
  return 1
}

check_health() {
  log "Waiting for the application to become healthy"

  # Liveness first: it only requires PostgreSQL, so it distinguishes "the app is
  # up but Redis is down" from "the app never started".
  probe "${HEALTH_URL}" "ok" >/dev/null \
    || fail "liveness probe never passed; inspect: compose logs app"
  ok "GET /health -> ok"

  if probe "${READY_URL}" "ready" >/dev/null; then
    ok "GET /health/ready -> ready"
  else
    # Readiness additionally requires Redis. The deployment is not failed on this:
    # the site serves, but the queue does not, and that distinction is worth
    # reporting rather than hiding behind a non-zero exit.
    warn "readiness probe did not pass: PostgreSQL is up but Redis is not."
    warn "The dashboard works; ingestion and publishing will not run until Redis is up."
  fi
}

check_workers() {
  log "Checking the ARQ workers"

  # The workers are compose services, so their state is inspectable. Started as
  # `docker exec -d` processes instead they would be invisible here, carry no
  # restart policy, and disappear silently on the next app restart, which stops
  # the pipeline without stopping the site.
  local service missing=0
  for service in worker-ingestion worker-content worker-publisher; do
    local state
    state="$(compose -f "${COMPOSE_BASE}" -f "${COMPOSE_PROD}" ps \
      --format '{{.State}}' "${service}" 2>/dev/null | head -n 1)"
    if [[ "${state}" == "running" ]]; then
      ok "${service} running"
    else
      warn "${service} is '${state:-absent}'; check: compose logs ${service}"
      missing=1
    fi
  done

  if (( missing == 1 )); then
    warn "the site serves, but part of the pipeline is not processing"
  fi
}

show_summary() {
  log "Deployment summary"
  compose -f "${COMPOSE_BASE}" -f "${COMPOSE_PROD}" ps
  cat <<SUMMARY

  Dashboard   http://127.0.0.1:8000/  (front this with nginx/Caddy for TLS)
  Liveness    ${HEALTH_URL}
  Readiness   ${READY_URL}

  The three ARQ workers run as compose services and restart with the stack.
  Follow one with:
    docker compose -f docker-compose.yml -f docker-compose.prod.yml \\
      logs -f worker-ingestion

SUMMARY
}

main() {
  parse_args "$@"
  log "SvodkaPress deployment — ${PROJECT_DIR}"

  check_prerequisites
  check_environment
  check_compose_files

  if (( CHECK_ONLY == 1 )); then
    log "All checks passed (--check-only: nothing was changed)"
    exit 0
  fi

  start_stack
  run_migrations
  check_health
  check_workers
  show_summary
  log "Done"
}

main "$@"
