# SoftExpert Portal Exporter — scraping exclusivo (Python 3.14)

FastAPI + PostgreSQL + Worker + APScheduler + Playwright/Chromium + armazenamento por bind mount. **Não contém Dataset, tokens de API SoftExpert nem callbacks.** A API pública do SoftExpert permite navegação via navegador autenticado; os relatórios podem vir de um host diferente.

> **Status:** pacote completo para desenvolvimento e homologação, **não validado ponta a ponta no portal autenticado do cliente**. Seletores de abas/portais, acesso ao Jasper na porta 8082, carregamento histórico e autenticação são dependentes do site. Não afirmar que a carga histórica cobriu 100% sem comparar contagens por aba e registros.

## Regras de negócio

- **`test`:** amostra de até 3 portais/5 PDFs e profundidade 3 por padrão; não aplica filtro de datas.
- **`historical`:** percorre tudo o que a conta autorizada vê (inclusive portais filhos e "carregar mais") e baixa relatórios **com data até `cutoff_date`, inclusive**. A data de corte é a data de aprovação do código, **informada explicitamente** na API ou em `HISTORICAL_CUTOFF_DATE`.
- **`incremental`:** pesquisa o portal mas só baixa documentos com data na janela **[window_start, window_end)**, incluído o início e excluído o final. Manualmente, sem janela explícita, considera os seis meses de calendário anteriores à data atual. O Scheduler configura janelas fixas, conforme abaixo.
- **Sem duplicação:** cada URL normalizada vira uma chave SHA-256 única no PostgreSQL; antes de baixar, confere se essa chave já foi exportada com sucesso **e se o arquivo ainda existe**. Mesma URL, arquivo alterado: não detecta mudança automaticamente; é preciso reexportação intencional com `force: true`.
- **Data desconhecida:** Jobs históricos/incrementais **não exportam** PDFs sem data legível: contam como falhas para revisão, evitando misturar períodos silenciosamente. O teste inicial ainda baixa amostras sem data.
- **Recuperação:** o Worker tenta novamente Jobs interrompidos; reexecução histórica pula PDFs já salvos. O crawler recomeça a navegação desde a raiz, mas não faz download repetido dos sucessos.

## Agenda semestral

`SCHEDULER_CRON=0 2 1 1,7 *` e `SCHEDULER_TIMEZONE=America/Sao_Paulo`.

| Executa às 02:00 | Período de origem, fim exclusivo |
|---|---|
| 01/01/2027 | 01/07/2026 a 01/01/2027 |
| 01/07/2027 | 01/01/2027 a 01/07/2027 |

O Scheduler cria Job `incremental` com janela explícita no PostgreSQL. A mesma janela só é criada uma vez (`schedule_key` único). Scheduler em memória não recupera automaticamente a execução perdida com o container desligado; nesse caso **dispare um Job manual** com a janela perdida. **Não há trigger automático em data de aprovação**: a carga histórica é iniciada manualmente depois de aprovada.

## Instalação WSL/Docker (banco novo)

Use uma pasta NOVA e NÃO reutilize volume PostgreSQL de projetos anteriores. As migrations deste projeto são novas e incompatíveis com o esquema legado:

```bash
unzip softexpert-portal-exporter-clean.zip -d ~/projects
cd ~/projects/softexpert-portal-exporter-clean
cp .env.example .env
mkdir -p runtime/share
nano .env
```

No `.env`, forneça `PORTAL_ENTRY_URL` completa do Checklist - Metrô, `PORTAL_USERNAME` e `PORTAL_PASSWORD` (conta técnica), `HISTORICAL_CUTOFF_DATE=AAAA-MM-DD` quando aprovar. **Troque a senha de usuário que apareceu em HTML enviado na conversa.** Nunca versionar o `.env`.

```bash
docker compose up --build -d
docker compose ps -a
curl http://127.0.0.1:8080/health/live
curl http://127.0.0.1:8080/health/ready
```

Porta local `127.0.0.1:8080` (evita conflito com Nginx antigo na 80). O `migrate` deve terminar com `Exited (0)`.

## 1. TESTE CONTROLADO

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
 -H 'X-API-Key: change-me' -H 'Content-Type: application/json' \
 -d '{"requested_by":"homologacao","mode":"test","max_portals":3,"max_reports":5,"max_depth":3}'
```

## 2. CARGA HISTÓRICA — UMA VEZ DEPOIS DA APROVAÇÃO

Exemplo fictício com código aprovado em 22/10/2026 (ajuste a data real):

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
 -H 'X-API-Key: change-me' -H 'Content-Type: application/json' \
 -d '{"requested_by":"carga-legada","mode":"historical","cutoff_date":"2026-10-22"}'
```

Sem a data de corte, a API retorna 422 (a menos que `HISTORICAL_CUTOFF_DATE` esteja definida).

## 3. ATUALIZAÇÃO MANUAL — ÚLTIMOS SEIS MESES

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
 -H 'X-API-Key: change-me' -H 'Content-Type: application/json' \
 -d '{"requested_by":"atualizacao-manual","mode":"incremental"}'
```

**Reprocessar um semestre exato**, por exemplo janeiro a junho:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
 -H 'X-API-Key: change-me' -H 'Content-Type: application/json' \
 -d '{"requested_by":"reprocessar-semestre","mode":"incremental","window_start":"2026-01-01","window_end":"2026-07-01"}'
```

## 4. ATUALIZAÇÃO AUTOMÁTICA

Deixe `SCHEDULER_ENABLED=false` no WSL durante testes. Em produção, configure `SCHEDULER_ENABLED=true` e mantenha `0 2 1 1,7 *` (02:00 de 01/01 e 01/07 em São Paulo). Recrie o serviço após mudar `.env`:

```bash
docker compose up -d --force-recreate scheduler
```

## Acompanhar qualquer Job

Anote o `job_id` retornado e faça:

```bash
curl -H 'X-API-Key: change-me' http://127.0.0.1:8080/api/v1/sync/SEU_JOB_ID
docker compose logs -f worker
docker compose logs --tail=50 scheduler
docker compose logs --tail=50 migrate
find runtime/share -type f -iname '*.pdf' | head -30

docker compose exec db psql -U exporter -d exporter -c \
 "SELECT id,mode,status,downloaded,skipped,failed,error_summary FROM sync_jobs ORDER BY created_at DESC LIMIT 10;"
```

`downloaded` = PDFs gravados nesta execução; `skipped` = já extraídos; `failed` = download, navegação ou data impossível de validar. Há também detalhes individuais em `extractions`. **Cargas interrompidas/incompletas devem ser conferidas, não tomadas como sucesso.**

## Segurança e produção

- Para o navegador, instale Chromium (Dockerfile já instala). Não automatize MFA sem aprovação. A conta de scraping precisa visualizar todos os portais.
- O host de relatórios em **HTTP:8082** precisa ser acessível pela EC2; valide política de segurança e possível autenticação adicional. A VPN tratada anteriormente é somente para a pasta SMB, não para o SoftExpert público.
- Em produção monte SMB/CIFS **no host EC2**, confirme `findmnt -T /mnt/client-share -t cifs` e depois bind mount `/mnt/client-share:/mnt/client-share` **somente no Worker**. Ative `NETWORK_SHARE_REQUIRE_SENTINEL=true` e crie o sentinel exclusivamente no servidor SMB. O sentinel **não prova** montagem remota sozinho; valide o host antes de iniciar Docker e a cada reinício.
- Credenciais de portal no `.env` apenas para desenvolvimento; em produção busque-as em armazenamento seguro e injete-as antes de executar Docker. Evite colocá-las em Terraform State, imagens ou repositório.
- O Nginx local só publica em loopback. Em produção não exponha a API sem TLS e controle de origem/autenticação.
- A extração de abas/portais precisa ser homologada: seletores, paginação, data correta e PDF de cada variante. Sem acesso autenticado à Falcão, não é possível garantir cobertura integral apenas pelos prints fornecidos.

## Estrutura

`src/softexpert_exporter/` possui API (`interfaces/api.py`), Worker (`worker.py`), agendador (`scheduler.py`), aplicação de exportação (`application/portal.py`), crawler Playwright (`infrastructure/portal_scraper.py`), banco PostgreSQL (`infrastructure/db.py`), armazenamento em disco/SMB (`infrastructure/storage.py`) e regras de janela (`date_windows.py`).

O Terraform em `infra/terraform/` é base de EC2/RDS/ECR e **não configura VPN Site-to-Site automaticamente**, pois depende do gateway e da política de rede do cliente.
