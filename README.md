# SoftExpert Portal Exporter — Falcão Bauer

> **Operação / manutenção — dev-09 | 05/11/2026**  
> Extração de relatórios PDF do **SoftExpert SE Suite** mediante navegação autenticada (Playwright), com persistência de execução no PostgreSQL e destino final em compartilhamento SMB/CIFS do cliente.
>
> **Estado conhecido:** imagem `dev-09` implantada na EC2; API e Worker iniciaram e `/health/live` respondeu `200`. **Não há evidência neste handover de VPN/SMB homologados, carga histórica concluída ou scheduler ativo.** Validar no ambiente antes de marcar como concluído.

## Índice

1. [Acessos e infraestrutura](#1-acessos-e-infraestrutura)
2. [Arquitetura e regras funcionais](#2-arquitetura-e-regras-funcionais)
3. [Entrar na EC2 e verificar serviços](#3-entrar-na-ec2-e-verificar-serviços)
4. [VPN Site-to-Site e responsabilidades](#4-vpn-site-to-site-e-responsabilidades)
5. [Montar o compartilhamento SMB](#5-montar-o-compartilhamento-smb)
6. [Executar extrações](#6-executar-extrações)
7. [Ativar o scheduler](#7-ativar-o-scheduler)
8. [Publicar uma nova imagem no ECR](#8-publicar-uma-nova-imagem-no-ecr)
9. [Banco, auditoria e logs](#9-banco-auditoria-e-logs)
10. [Segurança, recuperação e problemas frequentes](#10-segurança-recuperação-e-problemas-frequentes)
11. [Pendências e contatos](#11-pendências-e-contatos)

## 1. Acessos e infraestrutura

| Item | Valor / ação |
|---|---|
| GitLab | **PREENCHER URL DO REPOSITÓRIO:** `________________________________` |
| Portal SE Suite | https://bpm.falcaobauer.com.br/softexpert/workspace?page=dashboard,8aa18285841a50b0018443f5d6794fe2,8aa18285841a50b0018443f5d67a4fe3 |
| Login do portal | Solicitar à equipe responsável / cofre de senhas. **Não gravar credenciais no Git.** |
| Instância EC2 | [`i-0cd84d2d882f6a667` — abrir console](https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#InstanceDetails:instanceId=i-0cd84d2d882f6a667) |
| Nome EC2 | `softexpert-portal-exporter-dev` |
| Região AWS / conta | `us-east-1` / `936506603712` |
| VPC | `OnDemandDev-vpc` — `vpc-0c7f4257d8d0c2f13` |
| Sub-rede EC2 | `subnet-0222770d28f5abe9e` (private1, us-east-1a) |
| IP privado da EC2 | `10.144.128.40` (confirmar se houver substituição de instância) |
| IP público EC2 | Sem IPv4 público na evidência de 05/11/2026 |
| Tipo EC2 | `t3.medium` na evidência de 05/11/2026 |
| Pasta no host | `/opt/softexpert-portal-exporter-dev` |
| ECR | `936506603712.dkr.ecr.us-east-1.amazonaws.com/softexpert-portal-exporter-dev` |
| Imagem em uso | `:dev-09` (confirmar após alterações) |
| RDS PostgreSQL | `softexpert-portal-exporter-dev.cjy24npbryhy.us-east-1.rds.amazonaws.com:5432/exporter` |
| Destino SMB do cliente | `\\BAUER069\projeto_metro` |
| IP SMB informado | `192.168.7.169` — **confirmar que corresponde ao BAUER069** |
| Montagem no Linux/container | `/mnt/client-share` |
| Acesso à API | `http://127.0.0.1:8080` **apenas no host**, via Nginx em loopback |

**Contato para alinhamentos de rede/compartilhamento:** `matheus.cobra@falcaobauer.com.br` e `alexandre.lima@vexia.com.br`. Confirmar responsável por cada etapa, sem assumir atribuições específicas.

**Atenção ao ambiente:** o nome AWS inclui `-dev`, mas o `.env` observado usa `APP_ENV=production`. Isso **não altera automaticamente a identidade do ambiente**; confirme com a equipe antes de usar dados reais e antes de mudanças.

### Credenciais e configuração

As credenciais da API, portal, banco e SMB **não devem ser versionadas** nem compartilhadas em README, tickets ou e-mails. No host, `.env` contém as configurações de runtime; a senha SMB deve ficar em arquivo root-only (veja §5). Recomenda-se rotacionar credenciais que tenham sido exibidas em chats ou terminais compartilhados. Não copiar `.env` para pastas públicas.

**Ponto de homologação:** nos registros anteriores, o valor `PORTAL_ENTRY_URL` na EC2 tinha identificadores diferentes do link principal fornecido pelo cliente. Comparar os dois no navegador autenticado antes da carga histórica; não trocar a URL sem validar o dashboard correto.

## 2. Arquitetura e regras funcionais

```text
SoftExpert SE Suite (HTTPS) -- Playwright --> Worker (EC2 / Docker)
                                                   |
                      API FastAPI <--> PostgreSQL RDS (Jobs / deduplicação / auditoria)
                                                   |
                                   /tmp/softexpert-exporter (tmpfs)
                                                   |
                                          SMB/CIFS via VPN Site-to-Site
                                                   |
                                     \\BAUER069\projeto_metro
```

- **`api`**: criação e consulta de Jobs; `/health/live` e `/health/ready`.
- **`worker`**: autenticação no portal, navegação pelas abas e subportais, obtenção/validação dos PDFs, gravação na rede, auditoria e relatórios.
- **`migrate`**: aplica migrations Alembic, mas **verificar reconciliação do histórico antes de executar novamente** (ver §9).
- **`scheduler`**: cria Jobs semestrais. **Deve permanecer desativado até o aceite do cliente.**
- **`nginx`**: proxy na porta local `127.0.0.1:8080`.

### Modos de extração

| Modo | Regra | Grava documentos? |
|---|---|---|
| `test` + `dry_run=true` | Amostragem configurável; valida conteúdo e registra auditoria; não persiste PDF nem relatório de execução no compartilhamento | Não |
| `test` + `dry_run=false` | Amostragem com limite (`max_reports`, `max_portals`, `max_depth`) | Sim, exige SMB pronto |
| `historical` | PDFs com data **até** `cutoff_date`, inclusive. Sem data válida = falha a revisar | Sim, exige SMB pronto |
| `incremental` | PDF cuja data pertence à janela `[window_start, window_end)` | Sim, exige SMB pronto |

A verificação de duplicidade usa uma chave derivada da **URL normalizada** e a presença do arquivo já exportado. `force=true` força novo processamento e deve ser usado somente com aprovação. A mesma URL alterada não é detectada automaticamente como nova versão.

A data de corte histórica é **data de aprovação definida pelo negócio**, não uma data presumida. O incremental sem intervalo explícito considera os seis meses anteriores à data atual. O scheduler usa semestres fechados (abaixo).

### Organização dos PDFs

O diretório final é composto pelo caminho do portal, aba, unidade e nome do documento. Exemplo ilustrativo:

```text
/mnt/client-share/
  Checklist - Metrô/
    FIE/
      Estação Penha/
        214540-<hash>.pdf
  Relatorios-de-Execucao/
    AAAA/MM/
      Relatorio-*.pdf
      Documentos-*.csv
      Navegacao-*.csv
```

O nome exato pode variar conforme o portal e o registro. `downloaded`, `skipped`, `failed` e `error_summary` descrevem o Job; não considere `succeeded` prova de cobertura integral sem confrontar o inventário com o portal.

## 3. Entrar na EC2 e verificar serviços

1. Abra [EC2 — instância do projeto](https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#InstanceDetails:instanceId=i-0cd84d2d882f6a667).
2. No console **EC2 > Instances**, selecione `i-0cd84d2d882f6a667`.
3. Clique em **Connect > Session Manager > Connect** (requer permissão IAM e instância gerenciada pelo SSM).
4. No shell remoto, execute:

```bash
sudo -i
cd /opt/softexpert-portal-exporter-dev
pwd

docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker compose -f docker-compose.prod.yml ps -a
curl -sS http://127.0.0.1:8080/health/live
curl -sS http://127.0.0.1:8080/health/ready

docker compose -f docker-compose.prod.yml logs --tail=80 api worker
```

**Leitura dos health checks:** `live` verifica que a API responde; `ready` também verifica banco e compartilhamento CIFS. Antes do SMB, um `503` em `ready` pode ser esperado; depois da homologação deve ser investigado.

Nunca faça `docker compose down -v` no ambiente sem plano de recuperação. Não recrie a EC2 nem o RDS para atualizar a imagem.

## 4. VPN Site-to-Site e responsabilidades

A VPC já tinha um **Virtual Private Gateway (VGW) associado a outro cliente**. Para a Falcão Bauer, a arquitetura escolhida foi **novo Customer Gateway + nova conexão Site-to-Site no VGW existente**, sem remover os túneis antigos. A configuração da nova VPN foi enviada ao cliente, mas **não há evidência confirmando túneis UP** neste handover.

**Fluxo a permitir:**

| Origem | Destino | Porta | Finalidade |
|---|---|---|---|
| EC2 `10.144.128.40/32` | SMB `192.168.7.169/32` | TCP/445 | Acesso de saída ao compartilhamento |
| WatchGuard / Firebox | 2 endpoints públicos da nova VPN AWS | IKE/IPsec (UDP 500/4500 conforme NAT-T) | Estabelecer túneis |
| Rede do cliente | `10.144.128.40/32` via BOVPN | Respostas | Rota de retorno para a EC2 |

`192.168.7.169/32` foi configurado como **Static IP prefix** da VPN AWS. A equipe deve validar **não sobreposição** com os dois CIDRs da VPC e com as rotas da VPN do outro cliente. O caminho na AWS até o SMB depende da **tabela de rotas associada à sub-rede da EC2**, não necessariamente da tabela principal da VPC.

### Responsabilidades

**Equipe AWS:** confirmar IDs de CGW/VPN/VGW e estados de Tunnel 1/2; conferir a tabela de rotas da sub-rede `subnet-0222770d28f5abe9e`; adicionar, se faltar e sem conflito, rota `192.168.7.169/32 -> VGW`; verificar saída pelo Security Group da EC2; não modificar rotas do outro cliente sem análise.

**Equipe Falcão/Vexia:** configurar WatchGuard em **VPN > BOVPN Virtual Interfaces** conforme arquivo de configuração compartilhado por canal seguro, com **duas PSKs distintas**; incluir rota para `10.144.128.40/32` via nova VPN; autorizar TCP 445 no Firebox e firewall do servidor; criar conta técnica SMB com acesso ao compartilhamento; indicar domínio e política de senha; testar túneis e resposta do host.

**Links oficiais:** [AWS — rotas estáticas de VPN](https://docs.aws.amazon.com/vpn/latest/s2svpn/vpn-edit-static-routes.html) e [WatchGuard — BOVPN Virtual Interface com AWS](https://www.watchguard.com/help/docs/help-center/en-US/content/en-us/Fireware/bovpn/manual/bovpn_vif_static_routing_aws.html).

Testar **na EC2** após os túneis ficarem operacionais:

```bash
nc -zv -w 5 192.168.7.169 445
```

`Succeeded` comprova conectividade TCP, **não** autenticação/permite escrita SMB.

## 5. Montar o compartilhamento SMB

**Pré-requisito:** TI confirmar que `\\BAUER069\projeto_metro` corresponde a `//192.168.7.169/projeto_metro`, e disponibilizar **conta técnica**, senha por canal seguro, domínio (se aplicável) e permissão de escrita/criação de pastas.

```bash
sudo -i
dnf install -y cifs-utils nmap-ncat
mkdir -p /mnt/client-share
nc -zv -w 5 192.168.7.169 445
```

Criar arquivo de credenciais fora do Git, com modo `600`:

```bash
install -m 600 /dev/null /root/.smb-falcao
nano /root/.smb-falcao
```

Conteúdo **apenas no servidor, com valores reais fornecidos pela TI**:

```ini
username=USUARIO_TECNICO
password=SENHA_RECEBIDA_POR_CANAL_SEGURO
domain=DOMINIO_SE_APLICAVEL
```

Montagem inicial manual (validar política SMB 3.x com o cliente):

```bash
mount -t cifs //192.168.7.169/projeto_metro /mnt/client-share \
  -o credentials=/root/.smb-falcao,vers=3.0,iocharset=utf8,uid=10001,gid=10001,file_mode=0660,dir_mode=0770,nosuid,nodev

findmnt -T /mnt/client-share -o TARGET,SOURCE,FSTYPE
df -h /mnt/client-share
```

**Muito importante:** os containers da imagem executam como usuário `appuser` (`uid=10001`). O teste com `sudo touch` no host sozinho não comprova que o Worker conseguirá escrever. Depois que a montagem for validada:

1. Pedir ao cliente para criar `softexpert-exporter-share.ready` **na raiz da pasta remota**.
2. Recriar o Worker após o mount (bind mount existente no Compose):

```bash
cd /opt/softexpert-portal-exporter-dev
docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate worker
docker compose -f docker-compose.prod.yml exec worker sh -c \
  'test -f /mnt/client-share/softexpert-exporter-share.ready && touch /mnt/client-share/.worker-write-probe && rm /mnt/client-share/.worker-write-probe'
curl -sS http://127.0.0.1:8080/health/ready
```

Se o `ready` continuar `503`, recriar também a API depois de a montagem estar ativa e rever permissões ou bind mount. **Não desabilitar `NETWORK_SHARE_REQUIRE_CIFS` nem `NETWORK_SHARE_REQUIRE_SENTINEL`.** O compartilhamento só será considerado pronto quando for CIFS, tiver sentinel e aceitar escrita do usuário da aplicação.

Após homologação, configurar montagem **persistente com systemd/fstab**, sem armazenar senha na linha de comando. Exemplo a ser adaptado/validado:

```fstab
//192.168.7.169/projeto_metro /mnt/client-share cifs credentials=/root/.smb-falcao,vers=3.0,uid=10001,gid=10001,file_mode=0660,dir_mode=0770,_netdev,nofail,nosuid,nodev 0 0
```

Aplicar `systemctl daemon-reload && mount -a`; validar `findmnt -T /mnt/client-share -t cifs` **após reiniciar a instância**. Se a VPN cair, a aplicação deverá recusar novas gravações; não presumir disponibilidade só porque o diretório existe.

## 6. Executar extrações

No host, dentro de `/opt/softexpert-portal-exporter-dev`:

```bash
API_KEY=$(sed -n 's/^APP_API_KEY=//p' .env | tail -1)
```

**Teste de carga sem gravação (pode ser executado antes do SMB, sujeito a memória/rede):**

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"requested_by":"teste-validacao","mode":"test","dry_run":true,"max_reports":100,"max_portals":0,"max_depth":12}'
```

**Teste de apenas um PDF REAL na pasta SMB (SOMENTE depois de `ready` saudável):**

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"requested_by":"homologacao-smb","mode":"test","dry_run":false,"max_reports":1,"max_portals":3,"max_depth":3}'
```

O modo teste **seleciona o primeiro relatório elegível encontrado**; não escolhe um ID de documento específico. Capture o `job_id` retornado e confirme com a TI do cliente a presença do PDF no caminho remoto.

**Carga histórica (SOMENTE após aprovação formal da data, VPN/SMB e espaço remoto):**

```bash
# Substituir pela data REAL de aprovação do código (formato ISO)
CUT_OFF_DATE='AAAA-MM-DD'
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d "{\"requested_by\":\"carga-historica-aprovada\",\"mode\":\"historical\",\"cutoff_date\":\"$CUT_OFF_DATE\"}"
```

**Incremental manual de uma janela específica:**

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/sync \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"requested_by":"reprocessamento-manual","mode":"incremental","window_start":"2026-01-01","window_end":"2026-07-01"}'
```

As datas do exemplo incremental servem **somente como ilustração**. A janela inclui o início e exclui o fim. Antes de executar, verificar se o período não já está coberto pelo histórico/última carga. **Não disparar várias cargas históricas simultaneamente.**

**Consultar Job e logs:**

```bash
JOB_ID='SUBSTITUIR_JOB_ID'
curl -sS -H "X-API-Key: $API_KEY" "http://127.0.0.1:8080/api/v1/sync/$JOB_ID"
docker compose -f docker-compose.prod.yml logs -f worker
find /mnt/client-share/Relatorios-de-Execucao -type f \( -name '*.pdf' -o -name '*.csv' \) 2>/dev/null | tail -30
```

`downloaded` = PDFs gravados nesta execução; `skipped` = arquivos já registrados e encontrados; `failed` = falhas (incluindo datas inválidas); `dry_run` usa eventos `validated` na auditoria. A API pode indicar execução parcial; investigar `error_summary` e CSV de navegação. Não aplicar `force=true` de forma generalizada sem impacto aprovado.

## 7. Ativar o scheduler

**Pré-condições:** carga histórica validada, período incremental de continuidade acertado com o cliente, montagem SMB persistente testada após reboot, alertas/monitoramento definidos e nenhuma divergência de schema.

O `.env` deve conter:

```dotenv
SCHEDULER_ENABLED=true
SCHEDULER_CRON=0 2 1 1,7 *
SCHEDULER_TIMEZONE=America/Sao_Paulo
```

O cron significa **02:00 de 1º de janeiro e 1º de julho**, extraindo o semestre anterior. Não faz backfill automático se o serviço esteve indisponível durante o disparo.

```bash
cd /opt/softexpert-portal-exporter-dev
# Editar .env em segurança e confirmar SCHEDULER_ENABLED=true
# IMPORTANTE: conferir status das migrations antes de iniciar serviços dependentes

docker compose -f docker-compose.prod.yml up -d --no-deps scheduler
docker compose -f docker-compose.prod.yml logs --tail=60 scheduler
```

Se aparecer `scheduler_disabled`, validar o `.env` e recriar o serviço com `--force-recreate`. Manter somente **uma instância do scheduler** para evitar concorrência desnecessária.

## 8. Publicar uma nova imagem no ECR

**Na máquina de desenvolvimento / WSL (com AWS CLI, Docker e permissões):**

```bash
aws sso login --profile SE-OnDemand-Admin-Full
export AWS_PROFILE=SE-OnDemand-Admin-Full
export AWS_DEFAULT_REGION=us-east-1
aws sts get-caller-identity

ECR='936506603712.dkr.ecr.us-east-1.amazonaws.com/softexpert-portal-exporter-dev'
NEXT_TAG='dev-10'  # AJUSTAR para a versão aprovada
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin "${ECR%%/*}"
docker build --platform linux/amd64 -t "$ECR:$NEXT_TAG" .
docker push "$ECR:$NEXT_TAG"
aws ecr describe-images --region us-east-1 \
  --repository-name softexpert-portal-exporter-dev --image-ids imageTag="$NEXT_TAG"
```

O ECR está configurado como **IMMUTABLE** no Terraform; use tag nova a cada entrega. Nunca incluir `.env`, estados Terraform ou credenciais na imagem/repositório.

**Na EC2:**

```bash
sudo -i
cd /opt/softexpert-portal-exporter-dev
cp -a .env ".env.backup.$(date +%Y%m%d-%H%M%S)"
cp -a docker-compose.prod.yml "docker-compose.prod.yml.backup.$(date +%Y%m%d-%H%M%S)"

ECR='936506603712.dkr.ecr.us-east-1.amazonaws.com/softexpert-portal-exporter-dev'
NEXT_TAG='dev-10'  # AJUSTAR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin "${ECR%%/*}"
docker pull "$ECR:$NEXT_TAG"

# Atualizar APP_IMAGE=...:dev-10 no .env, sem remover demais variáveis.
# Atualizar docker-compose.prod.yml no host caso haja mudanças de compose.
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml config --images
```

**Antes da troca:** conferir execução atual, backup/snapshot do RDS e `alembic current` versus `alembic heads`. A `dev-09` recebeu alterações manuais de DDL no banco; ver §9.

```bash
# Quando não houver Job em andamento e o schema estiver consistente:
docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate api worker
curl -sS http://127.0.0.1:8080/health/live
docker compose -f docker-compose.prod.yml ps -a
```

Não reiniciar Nginx se a alteração for apenas na imagem da API/Worker, a menos que seja necessário. Se precisar voltar, restaurar `APP_IMAGE` para tag anterior e recriar os serviços, sem deletar volume ou RDS. **Rollback de código não desfaz migration:** planejar compatibilidade.

## 9. Banco, auditoria e logs

Persistência: PostgreSQL RDS 17, modelos `sync_jobs`, `extractions` e `job_audit_events`, além da tabela `alembic_version`. A auditoria completa pode ser consultada no banco; os CSVs de navegação e documentos são publicados junto ao relatório PDF da execução **quando há compartilhamento disponível e não é dry_run**.

**Verificar versões de migration antes de novas atualizações:**

```bash
docker compose -f docker-compose.prod.yml exec worker alembic current
docker compose -f docker-compose.prod.yml exec worker alembic heads
```

**Ponto de atenção dev-09:** após o pull, foram adicionadas manualmente em RDS as colunas `report_path`, `report_generated_at`, `report_error` e a tabela `job_audit_events` para alinhar com a imagem. A migration `20261029_0002` existe no repositório-base, mas não há comprovação de que `alembic_version` tenha sido marcada nessa revisão. **Não execute migrations às cegas**: o histórico precisa ser reconciliado e auditado antes de um próximo `alembic upgrade head`.

Logs correntes:

```bash
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs --tail=100 api
docker compose -f docker-compose.prod.yml logs --tail=100 scheduler
```

O Terraform define um CloudWatch Log Group, mas **o Docker Compose mostrado não configura driver CloudWatch**. Portanto os logs de aplicação devem ser consultados pelo Docker até a integração de coleta ser efetivamente configurada. Não confundir a existência do Log Group com ingestão de logs ativa.

## 10. Segurança, recuperação e problemas frequentes

- EC2 em sub-rede privada, sem IP público na evidência de 05/11, e **IMDSv2 requerido**. Isso não comprova sozinho segurança integral: Security Groups, NACL, IAM, VPN e DNS ainda precisam de revisão.
- API publicada localmente em `127.0.0.1:8080`. Não liberar essa porta na internet sem projeto de autenticação/TLS/restrição de origem.
- SMB: exigir montagem real `cifs`, arquivo sentinel e escrita com UID do container; não usar diretório local como substituto silencioso.
- Worker `tmpfs` em `/tmp/softexpert-exporter` com limite definido no Compose **somente se o arquivo atualizado estiver instalado na EC2**; confirmar com `docker compose config | grep -A5 tmpfs` e `docker exec <worker> df -h /tmp/softexpert-exporter`.
- A limitação de `tmpfs` não cobre todos os usos de disco (logs Docker, imagens, relatórios antigos, `teste-crawler`). Monitorar `df -h /`, `docker system df`, `du -sh /opt/softexpert-portal-exporter-dev/teste-crawler` **antes** de qualquer limpeza.
- Não usar `docker system prune --volumes` sem plano de mudança.
- Em falha de VPN/SMB: interromper novas cargas reais, testar TCP 445, `findmnt`, credenciais e permissões; preservar Job e auditoria; reexecutar com controle de deduplicação.
- Em `503` de `/health/ready`: verificar RDS e CIFS; `200` de `/health/live` **não** significa que a gravação está pronta.
- Em `403` ao enviar imagem ao ECR: confirmar login SSO no perfil correto na máquina local e permissões da conta `936506603712`.
- Em atraso de agendamento por parada de container: rodar **Job incremental manual** com janela aprovada; scheduler não recupera disparos perdidos automaticamente.

## 11. Pendências e contatos

Marque `- [x]` somente depois de verificar no console, na EC2 ou com evidência do cliente.

### Infraestrutura e segurança

- [x] Imagem `dev-09` baixada do ECR e API/Worker iniciados na EC2 (evidência 05/11/2026).
- [x] `/health/live` respondeu `200`.
- [ ] Confirmar os **dois CIDRs IPv4 da VPC**, a rota da sub-rede da EC2 e não sobreposição com a VPN antiga.
- [ ] Identificar IDs da nova VPN/Customer Gateway e verificar estados dos 2 túneis com o WatchGuard.
- [ ] Validar regras SG/NACL, IPsec/IKE, rota de ida `192.168.7.169/32` e retorno `10.144.128.40/32`.
- [ ] Rotacionar credenciais potencialmente expostas e confirmar guarda em cofre.
- [ ] Verificar status de `alembic_version` versus `20261029_0002`.

### SMB e homologação

- [ ] Confirmar `BAUER069 = 192.168.7.169`, caminho `\\BAUER069\projeto_metro`, domínio e conta técnica.
- [ ] Montar SMB no host e testar TCP 445, leitura e escrita com a conta do container (`uid 10001`).
- [ ] Criar/verificar sentinel remoto e `/health/ready=200`.
- [ ] Configurar montagem persistente e testar após reinício controlado.
- [ ] Confirmar `tmpfs` no Worker e espaço disponível no disco local e remoto.
- [ ] Rodar e homologar 1 PDF real com o cliente.
- [ ] Rodar teste de 100 PDFs com `dry_run=true` e avaliar falhas/cobertura das abas.
- [ ] Definir data de corte do legado, aprovar a carga histórica e conferir inventário/CSV.
- [ ] Definir janela de continuidade e ativar scheduler com aceite formal.
- [ ] Definir responsável por alertas, reinícios, mudanças e auditoria de Jobs.

**Contatos (confirmar atribuições):**

- `matheus.cobra@falcaobauer.com.br` — contato do domínio Falcão Bauer; tratar WatchGuard, SMB e aceite da gravação.
- `alexandre.lima@vexia.com.br` — contato do domínio Vexia; tratar alinhamento de infraestrutura e permissões conforme responsabilidade acordada.

> **Nota de passagem:** esta documentação reflete o código anexado (`dev-09`) e os resultados enviados até 05/11/2026; não equivale a homologação presencial na AWS ou na rede do cliente. Atualize o checklist após cada evidência.
