# Planejamento — API de Agendamento de Mensagem (Whazing)

## Contexto

O Whazing tem uma API externa documentada (`integracoes/api/`) pra envio de mensagem (texto, arquivo, sticker, localização, botões, listas), mas **não tem nenhum parâmetro de agendamento** — só envio imediato. Isso foi levantado como feature request oficial: [issue #479](https://github.com/cleitonme/Whazing-SaaS/issues/479).

Internamente, o Whazing já tem o mecanismo completo de agendamento — só não é exposto via API:

- Tabela `Schedules` no Postgres (`body`, `sendAt`, `contactId`, `userId`, `tenantId`, `whatsappId`, `status`, `mediaPath`, `mediaName`, `isTemplate`, etc.)
- Job `VerifySchedules` no backend (`/app/dist/jobs/VerifySchedules.js`) varre `status='PENDENTE' AND sendAt <= now()`, dispara a mensagem e marca `status='ENVIADA'` + `sentAt`

Isso foi **validado manualmente** em produção (tenant 1, canal BoraAutomatizar, `whatsappId=53`): inserido `Schedule id=65` via SQL direto, com `sendAt = now() + 10min`, e o job processou e enviou a mensagem "Ola" pro WhatsApp real automaticamente. Confirma que o mecanismo funciona ponta a ponta sem precisar mexer no backend.

## Objetivo

Construir uma **API wrapper** própria, hospedada na mesma VPS (`100.92.173.21`), que expõe agendamento de mensagem via HTTP — preenchendo o buraco que o Whazing ainda não cobre, sem esperar o time do Whazing implementar.

## Consumidor

Um agente rodando no Orca vai usar essa API pra agendar mensagens (e follow-ups que o próprio agente cadastra) pra clientes reais. Isso significa: texto, arquivo **e botão interativo** desde o v1 (proposta, boleto, print, oferta com botão, são follow-up comum de agente comercial) — não dá pra empurrar isso pra v2.

## Escopo v1 (texto + arquivo + botões)

Endpoint único, autenticado, aceitando tanto JSON (texto puro) quanto `multipart/form-data` (com arquivo):

```json
{
  "number": "5527999594959",
  "body": "Texto da mensagem",
  "sendAt": "2026-09-01T10:00:00-03:00",
  "externalKey": "ID_UNICO_SISTEMA"
}
```

```
multipart/form-data:
  media: (arquivo)
  body: "Texto da mensagem"
  number: "5527999594959"
  sendAt: "2026-09-01T10:00:00-03:00"
  externalKey: "ID_UNICO_SISTEMA"
```

E internamente:

1. Resolve `tenantId` a partir do token Bearer (um token por canal/tenant, igual ao padrão da API oficial do Whazing)
2. Resolve `whatsappId` a partir do canal vinculado ao token
3. Resolve (ou cria, se não existir) `contactId` a partir de `number` + `tenantId`
4. Valida `sendAt` no futuro
5. Se tiver arquivo: salva no volume/pasta correta do backend (`/app/public` ou `/app/private`, a confirmar) com o path/naming exato que o job de disparo espera, preenche `mediaPath`/`mediaName`/`messageType` de acordo
6. Insere em `Schedules` com `status='PENDENTE'`, `Tunel=false`, `isTemplate=false`, `openTicket=false`
7. Retorna o `id` do agendamento criado

**Formato de mídia — confirmado via teste empírico** (agendamentos reais `id=66` e `id=67`, tenant 1, feitos pela UI do Whazing):

```
mediaPath = "{tenantId}/schedule/{AAAAMM}"                              -- só o diretório, sem nome de arquivo
mediaName = "{nome-base}_{DDMMAAAAHHmmss}{ms}.{ext}"                    -- nome do arquivo, com sufixo de timestamp
```

Exemplos reais:
- Áudio: `mediaPath="1/schedule/202608"`, `mediaName="audio_1787998519985_29082026071550282.mp3"`
- Imagem: `mediaPath="1/schedule/202608"`, `mediaName="source (3)_29082026071705427.png"`

Arquivo físico fica em `/app/public/{tenantId}/schedule/{AAAAMM}/{mediaName}` — pasta pública do backend, sem nenhum hash/token de segurança no path (só o nome do arquivo mesmo, então nomes previsíveis idealmente incluem algo não-adivinhável se o conteúdo for sensível).

`messageType` continua `'texto'` mesmo com mídia anexada — só vira outro valor (ex: `'botoes'`) pra mensagens interativas via `templateComponents`, fora do escopo desse wrapper.

Wrapper vai: gerar `mediaName` seguindo esse padrão (nome original + timestamp), criar a pasta `/app/public/{tenantId}/schedule/{AAAAMM}/` se não existir, salvar o arquivo recebido lá, e gravar `mediaPath`/`mediaName` na linha do `Schedule`.

**Formato de botão interativo — confirmado via teste empírico** (agendamento real `id=67`, tenant 1, feito via "API Plus"):

```json
{
  "text": "Texto principal da mensagem",
  "footerText": "Rodapé opcional",
  "choices": [
    { "displayText": "Link", "type": "url", "id": "opt_...", "url": "https://..." },
    { "displayText": "Ver oferta aqui", "type": "reply", "id": "opt_..." },
    { "displayText": "Copiar cupom", "type": "copy", "id": "opt_...", "copyText": "br10" }
  ]
}
```

Isso vai no campo `templateComponents` (serializado como texto/JSON), com `messageType='botoes'`, `isTemplate=false`, `body` = mesmo texto de `templateComponents.text`. Suporta os 3 tipos de botão vistos: `url` (abre link), `reply` (resposta rápida), `copy` (copia texto pro clipboard) — `id` de cada choice pode ser gerado como `opt_{timestamp_ms}`. Pode combinar com imagem de cabeçalho usando o mesmo mecanismo de `mediaPath`/`mediaName` (não usa `templateHeaderMediaPath`, que ficou vazio no teste real).

Endpoint do wrapper pra isso aceita um campo `buttons` (array com `displayText`/`type`/`url`|`copyText`) + `footerText` opcional, e monta o `templateComponents` internamente.

Fora de escopo no v1:
- Template de WhatsApp oficial agendado (`isTemplate=true`) — diferente de botão via Baileys/API Plus, que já está no escopo
- Lista interativa (`type: list`), CTA e outros tipos de `/apioficial` além dos 3 tipos de botão confirmados
- Cancelamento/edição de agendamento (poderia ser um `DELETE`/`PATCH` num v2)
- Agendamento recorrente / sequência (`Tunel`/`TunelAgendamentos`)

## Arquitetura

Mesmo padrão já usado pro `transcreve-api` nessa VPS:

- Serviço novo no `docker-compose.yaml` de `/home/deploy/whazing/`, na rede `whazing-net`
- Sem porta pública direta — roteado via Traefik com subdomínio próprio (ex: `agendamento.boraautomatizar.com.br`) + TLS automático
- Conecta no Postgres pela rede interna Docker (`postgres:5432`, não precisa expor via Tailscale pra isso — é tudo interno)
- Stack sugerida: Node (mesma linguagem do backend, facilita eventual migração da lógica pra dentro do Whazing depois) ou Python/Flask (mais rápido de prototipar, mesmo padrão do transcreve-api)

## Autenticação — confirmado

`ApiConfigs.token` é o **mesmo JWT (HS256)** que a API oficial do Whazing já usa (gerado em Configurações → canal → API). Payload:

```json
{ "tenantId": 1, "profile": "admin", "sessionId": 53, "iat": ..., "exp": ... }
```

`sessionId` = `Whatsapps.id` = exatamente o `whatsappId` esperado em `Schedules`.

Fluxo de auth do wrapper:
1. Recebe `Authorization: Bearer <token>` (mesmo token que o cliente já usa na API oficial de mensagens)
2. Valida assinatura com `JWT_SECRET` (do `.env` do backend — mesmo secret, não gera nada novo)
3. Extrai `tenantId` e `sessionId` (=`whatsappId`) do payload
4. Confirma na tabela `ApiConfigs` que existe um registro com esse `token` e `isActive=true` (permite revogar sem invalidar o JWT em si)

Zero necessidade de gerar token novo ou expor endpoint de login — reaproveita 100% o que já existe.

## Plano de execução

1. ~~Teste empírico de mídia~~ — feito, formato confirmado (ver acima)
2. Levantar exatamente como `ApiConfigs` mapeia token → tenant/canal (schema + exemplo real)
3. Escrever o serviço wrapper (v1, texto + arquivo) — Flask, seguindo padrão do `transcreve-api`
4. Testar de ponta a ponta num canal de teste (mesmo fluxo já validado manualmente: tenant 1, BoraAutomatizar) — texto e arquivo
5. Documentar o endpoint num `.md` (mesmo estilo de `integracoes/api/endpoints/mensagens.md`) pra ficar junto da doc oficial, deixando claro que é uma extensão não-oficial
6. Subir no VPS, mesmo processo do `transcreve-api` (compose + Traefik)

## Riscos / pontos em aberto

- **Validações que a API oficial faz e a nossa não replica** — código do backend é ofuscado, não dá pra confirmar todas as regras (permissão, limite de plano, etc.) que o fluxo normal de agendamento aplica. Mitigação: escopo v1 mínimo (só texto), testado em canal de baixo risco antes de uso real.
- **IP do `whazing-backend` no Docker (`172.18.0.3`) não é fixo** — se o container for recriado, muda. Não afeta esse wrapper diretamente (ele fala com o Postgres, não com o backend), mas é um ponto de atenção geral da infra.
- **Path de mídia sem hash de segurança** — `mediaPath`/`mediaName` ficam num diretório público previsível (`/app/public/{tenantId}/schedule/{AAAAMM}/`). Arquivo sensível anexado por engano fica acessível via URL direta pra quem souber/adivinhar o nome. Mitigação: o wrapper deve gerar nomes com componente aleatório, não só o nome original do arquivo.
