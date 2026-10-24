# GitHub Backup System — Design

- **Data:** 2026-10-23
- **Status:** aprovado (abordagem A)
- **Diagrama:** [`.archify/architecture-github-bkp-20261023-205216/github-bkp.html`](../../../.archify/architecture-github-bkp-20261023-205216/github-bkp.html)

## 1. Objetivo

Serviço self-hosted, em um container Docker, que:

1. Mantém cópia local (mirror git) de repositórios GitHub escolhidos pelo usuário.
2. Atualiza essas cópias uma vez por dia.
3. Avisa por email quando um repositório deixa de existir no GitHub (e quando volta).

### Fora de escopo

- Backup de issues, PRs, releases, wiki ou outros metadados (só git).
- Autenticação na Web UI (acesso restrito por rede: LAN/VPN).
- Restauração automática para o GitHub (restauração é manual, ver §9).
- Múltiplos usuários, múltiplos tokens, outros provedores (GitLab etc.).

## 2. Decisões

| Tema | Decisão |
|---|---|
| Conteúdo salvo | `git clone --mirror` (todas branches, tags, histórico) |
| Acesso | Repos públicos e privados via um GitHub PAT read-only |
| Configuração de repos | Web UI simples |
| Force-push / branch apagada | Preservar refs perdidas em `refs/backup/<data>/...` |
| Email | SMTP genérico configurado por env |
| Stack | Python 3.12, FastAPI, Jinja2 + HTMX, APScheduler, SQLite |
| Proteção da UI | Sem auth; expor só em LAN/VPN |
| Deploy | Um container, `docker compose`, volume `/data` |

## 3. Arquitetura

Um processo Python dentro de um container:

```
Você ──HTTP──▶ Web UI ──CRUD──▶ SQLite (/data/state.db)
                 │ "sync agora"
                 ▼
             Scheduler ──cron──▶ Backup Job ──▶ GitHub (REST API + git HTTPS)
                                   │  │
                                   │  └──▶ Git mirrors (/data/repos/<owner>/<repo>.git)
                                   └──▶ Notifier ──SMTP──▶ servidor de email
```

### Unidades

Cada módulo tem uma responsabilidade e interface pequena; pode ser testado isolado.

| Módulo | Responsabilidade | Depende de |
|---|---|---|
| `config.py` | Lê e valida env vars (pydantic-settings). Falha no startup se algo obrigatório faltar. | — |
| `db.py` | Schema SQLite, migrações simples, funções de acesso (`list_repos`, `add_repo`, `record_run`, ...). | `config` |
| `github_client.py` | `check_repo(full_name) -> RepoCheck` (`EXISTS`, `NOT_FOUND`, `ERROR`). Usa httpx com o PAT. | `config` |
| `mirror.py` | Operações git: `ensure_mirror`, `snapshot_vanishing_refs`, `update_mirror`, `repo_size`. Chama `git` via subprocess. | `config` |
| `state.py` | Lógica pura de transição de status (ver §5). Sem I/O. | — |
| `job.py` | Orquestra um ciclo: para cada repo → check → mirror → state → eventos. | todos acima, `notifier` |
| `notifier.py` | Monta e envia emails (templates Jinja2) via `smtplib`. | `config` |
| `scheduler.py` | APScheduler: job diário + job semanal de resumo; expõe `trigger_now()`. | `job`, `notifier` |
| `web.py` | Rotas FastAPI + templates HTMX. | `db`, `scheduler` |
| `main.py` | Monta tudo, inicia uvicorn + scheduler. | todos |

## 4. Modelo de dados (SQLite)

```sql
CREATE TABLE repos (
  id              INTEGER PRIMARY KEY,
  full_name       TEXT NOT NULL UNIQUE,      -- "owner/repo", sempre minúsculo
  status          TEXT NOT NULL DEFAULT 'pending',
                  -- pending | active | suspect | missing | error
  consecutive_404 INTEGER NOT NULL DEFAULT 0,
  consecutive_err INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,                      -- ISO-8601 UTC
  last_checked_at TEXT,
  last_error      TEXT,
  size_bytes      INTEGER,
  enabled         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL
);

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  trigger     TEXT NOT NULL,                 -- schedule | manual
  ok_count    INTEGER NOT NULL DEFAULT 0,
  fail_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE events (
  id         INTEGER PRIMARY KEY,
  repo_id    INTEGER REFERENCES repos(id),
  run_id     INTEGER REFERENCES runs(id),
  kind       TEXT NOT NULL,
             -- synced | refs_preserved | not_found | missing | restored | error | backup_failing
  detail     TEXT,
  created_at TEXT NOT NULL,
  emailed    INTEGER NOT NULL DEFAULT 0
);
```

Remover um repo na UI faz `enabled = 0` (soft delete). O mirror em disco **nunca** é apagado pelo sistema.

## 5. Ciclo diário e máquina de estados

Para cada repo com `enabled = 1`, em sequência (um repo por vez — evita rate limit e uso de disco/CPU concorrente):

1. `github_client.check_repo(full_name)`:
   - HTTP 200 → `EXISTS`
   - HTTP 404 → `NOT_FOUND` (GitHub também retorna 404 quando o token perde acesso a repo privado; tratado igual — o usuário verá o email e pode investigar)
   - Outros (timeout, 5xx, 401, 403 de rate limit) → `ERROR`
2. Se `EXISTS`:
   1. `ensure_mirror` — se o diretório não existe, `git clone --mirror`.
   2. `snapshot_vanishing_refs` — ver §6.
   3. `update_mirror` — `git remote update --prune`.
   4. Se algum passo git falhar → tratar como `ERROR`.
3. `state.next(current, result)` calcula novo status e eventos.
4. Grava status, contadores e eventos; envia emails pendentes.

### Transições (`state.py`)

| Status atual | Resultado | Novo status | Evento / email |
|---|---|---|---|
| qualquer exceto `missing` | `EXISTS` + git ok | `active` | `synced` (sem email) |
| `missing` | `EXISTS` + git ok | `active` | `restored` → **email "voltou"** |
| `pending`/`active`/`error` | `NOT_FOUND` (1º) | `suspect` | `not_found` (sem email) |
| `suspect` | `NOT_FOUND` (2º seguido) | `missing` | `missing` → **email "sumiu"** |
| `missing` | `NOT_FOUND` | `missing` | nenhum (não repete email) |
| qualquer exceto `missing`/`suspect` | `ERROR` | `error` | `error`; se `consecutive_err` chega a 3 → `backup_failing` → **email "falha de backup"** (uma vez por sequência) |
| `suspect` | `ERROR` | `suspect` | `error` (não zera nem avança `consecutive_404`) |
| `missing` | `ERROR` | `missing` | `error` (sem email) |

Regras:
- `EXISTS` zera `consecutive_404` e `consecutive_err`.
- `NOT_FOUND` zera `consecutive_err`.
- "2 ciclos seguidos" = 2 resultados `NOT_FOUND` sem `EXISTS` entre eles; um `ERROR` no meio não reinicia a contagem.
- Um repo recém-adicionado que retorna `NOT_FOUND` já no primeiro check entra em `suspect`; a UI mostra aviso ("não encontrado — confira o nome ou o token").

### "Sync agora"

Botão na UI chama `scheduler.trigger_now()`, que enfileira um ciclo `manual`. Se um ciclo já está rodando, o pedido é ignorado e a UI mostra "já em execução" (lock em memória; há um único processo).

## 6. Proteção de histórico

O refspec padrão de `--mirror` (`+refs/*:refs/*`) faria `--prune` apagar `refs/backup/*`, porque elas não existem no remoto. Por isso `ensure_mirror`, logo após o `git clone --mirror`, troca o refspec por:

```
remote.origin.fetch = +refs/heads/*:refs/heads/*
remote.origin.fetch = +refs/tags/*:refs/tags/*
```

(sem `refs/pull/*`). Com isso `--prune` só mexe em `refs/heads` e `refs/tags`. O clone e essa configuração são feitos em `<nome>.git.tmp` e só então movidos (`os.replace`) para o caminho final, para que um mirror meio configurado nunca pareça pronto.

Atualização em duas fases (`snapshot_vanishing_refs` + `update_mirror`):

1. **Staging:** `git fetch --prune origin +refs/heads/*:refs/incoming/heads/* +refs/tags/*:refs/incoming/tags/*` — baixa todos os objetos novos sem tocar nas refs reais.
2. **Comparação:** para cada ref local em `refs/heads/*` e `refs/tags/*`:
   - sem correspondente em `refs/incoming/*` → **apagada no remoto**;
   - correspondente existe e `git merge-base --is-ancestor <local> <incoming>` falha → **force-push**.
3. **Preservação:** para cada ref nesses casos, `git update-ref refs/backup/<YYYY-MM-DD>/<ref-original> <sha-local>`; registra evento `refs_preserved` com a lista. Se já existe uma ref com o mesmo nome no mesmo dia, adiciona sufixo `-2`, `-3`...
4. **Aplicação:** `git fetch --prune --no-tags . +refs/incoming/heads/*:refs/heads/* +refs/incoming/tags/*:refs/tags/*` — aplica localmente exatamente o que foi comparado, sem ir à rede (um segundo fetch do `origin` poderia trazer um force-push ocorrido depois da comparação, sem backup).
5. **Limpeza:** `git update-ref -d` em todas as `refs/incoming/*`.

Refs em `refs/backup/` nunca são removidas automaticamente.

## 7. Emails

Configuração SMTP via env. Todos os emails são texto simples + HTML mínimo, assunto prefixado com `[github-bkp]`.

| Email | Quando | Conteúdo |
|---|---|---|
| Repo sumiu | transição para `missing` | nome, data do último sync ok, caminho do mirror local, comando para restaurar |
| Repo voltou | transição `missing → active` | nome, quanto tempo ficou fora |
| Falha de backup | `consecutive_err` atinge 3 | nome, último erro, data do último sucesso |
| Resumo semanal | domingo, horário configurável | tabela: repo, status, último sync, tamanho; eventos da semana |

Vários eventos do mesmo ciclo são agrupados em **um** email no fim do ciclo (exceto resumo semanal, que é separado). Falha ao enviar email: loga, mantém `emailed = 0` e tenta de novo no próximo ciclo.

## 8. Web UI

Sem auth; porta exposta só em LAN/VPN (documentado no README e no `docker-compose.yml`, bind padrão `127.0.0.1:8080` — o usuário muda para o IP da LAN/VPN conscientemente).

Páginas (server-side + HTMX):

- **/** — lista de repos: nome, status (badge colorido), último sync, tamanho, último erro. Formulário "adicionar repo" (aceita `owner/repo` ou URL `https://github.com/owner/repo(.git)`). Botão remover por repo (com confirmação). Botão "sync agora".
- **/runs** — últimos 50 ciclos com contagens; expandir mostra eventos.
- **/health** — JSON `{ "ok": true, "last_run": ... }` para healthcheck do Docker.

Validação ao adicionar: normaliza para `owner/repo` minúsculo, rejeita formato inválido e duplicado. Não chama o GitHub na hora (o próximo ciclo ou "sync agora" verifica).

## 9. Configuração e deploy

### Env vars

| Var | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `GITHUB_TOKEN` | sim | — | PAT fine-grained com `Contents: read` + `Metadata: read` |
| `SMTP_HOST` | sim | — | |
| `SMTP_PORT` | não | `587` | |
| `SMTP_USER` / `SMTP_PASSWORD` | sim | — | |
| `SMTP_STARTTLS` | não | `true` | `false` + porta 465 usa SSL direto |
| `MAIL_FROM` / `MAIL_TO` | sim | — | `MAIL_TO` aceita lista separada por vírgula |
| `SYNC_CRON` | não | `0 3 * * *` | expressão cron |
| `SUMMARY_CRON` | não | `0 9 * * 0` | resumo semanal |
| `TZ` | não | `UTC` | |
| `DATA_DIR` | não | `/data` | |

Token nunca aparece em logs nem é gravado na URL do remote no disco: git usa `GIT_ASKPASS`/header `Authorization` via `-c http.extraHeader` em cada comando.

### Container

- Base `python:3.12-slim` + `git`.
- Usuário não-root.
- `VOLUME /data`.
- `HEALTHCHECK` em `/health`.
- `docker-compose.yml` com `env_file: .env`, volume nomeado ou bind mount, `restart: unless-stopped`.
- `.env.example` no repo; `.env` no `.gitignore`.

### Restauração (manual, documentada no README)

```bash
git clone /data/repos/owner/repo.git repo          # cópia de trabalho
# ou republicar:
cd /data/repos/owner/repo.git && git push --mirror https://github.com/owner/novo-repo.git
```

## 10. Erros e logs

- Log estruturado (JSON) em stdout → `docker logs`.
- Cada repo é processado em `try/except`: falha de um não interrompe o ciclo.
- Timeouts: API 30 s; comandos git 30 min por repo.
- Falta de espaço em disco aparece como erro git → conta como `ERROR` e eventualmente gera email "falha de backup".
- Startup falha rápido se env obrigatória faltar ou `git` não estiver no PATH.

## 11. Testes

- **Unit `state.py`:** tabela de transições completa de §5 (todas as linhas + regras de contadores).
- **Unit `github_client.py`:** respostas mockadas (`respx`) para 200, 404, 401, 403, 500, timeout.
- **Integração `mirror.py`:** repos git locais em `tmp_path` como "origin" (`file://`); cobre clone, update, branch apagada, force-push preservando ref em `refs/backup/`, e `--prune` não apagando `refs/backup/`.
- **Integração `job.py`:** GitHub client fake + repos locais + SMTP fake (`aiosmtpd`); verifica fluxo "sumiu após 2 ciclos", "voltou", "falha 3 dias", agrupamento de emails.
- **Web:** `TestClient` do FastAPI para adicionar/remover/validar repos e "sync agora".
- **Smoke Docker:** `docker compose up` + `curl /health` (manual / CI opcional).

Ferramentas: `pytest`, `ruff`.

## 12. Estrutura de diretórios

```
github-bkp-system/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── db.py
│   ├── github_client.py
│   ├── mirror.py
│   ├── state.py
│   ├── job.py
│   ├── notifier.py
│   ├── scheduler.py
│   ├── web.py
│   └── templates/
├── tests/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── README.md
```
