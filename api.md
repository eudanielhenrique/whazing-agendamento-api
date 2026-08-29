# API de Agendamento de Mensagem — Whazing (wrapper não-oficial)

Uso: agendar envio de mensagem (texto, arquivo, ou mensagem com botões) pra um horário futuro no WhatsApp via Whazing. Não existe isso na API oficial do Whazing — essa API cobre esse buraco.

## Checklist rápido — o que mandar no POST

Sempre, nos 3 modos:
- **Header** `Authorization: Bearer <token>` — obrigatório
- `number` — obrigatório, só dígitos, DDI+DDD+número
- `sendAt` — obrigatório, ISO 8601 **com timezone**, no futuro
- `body` — obrigatório (texto da mensagem; nos botões é o texto principal)

Por modo:
| Modo | Content-Type | Campo extra obrigatório | Campos extra opcionais |
|---|---|---|---|
| Texto | `application/json` | — | — |
| Arquivo | `multipart/form-data` | `media` (o arquivo) | — |
| Botões | `application/json` | `buttons` (array) | `footerText` |

Nada mais é obrigatório. `externalKey` da API oficial de mensagens **não existe** aqui (a tabela `Schedules` não tem essa coluna) — se mandar, é ignorado.

## Quando usar

Use esse endpoint sempre que precisar **agendar uma mensagem pra enviar depois** em vez de mandar na hora — por exemplo:

- Follow-up com um cliente/lead depois de um tempo definido ("responde em 2 dias se não tiver resposta")
- Lembrete agendado (reunião, cobrança, renovação)
- Envio programado de proposta/material (arquivo) num horário específico
- Oferta com botão (link, resposta rápida, cupom) programada

Se a mensagem é pra ir **agora**, use a API normal de envio do Whazing (endpoint `/`), não essa.

## Endpoint

```
POST https://agendamento.boraautomatizar.com.br/agendar
Authorization: Bearer <token>
```

O `<token>` é o **mesmo token Bearer** já usado na API oficial de mensagens do Whazing daquele canal (gerado em Configurações → canal → API, aba API → Postman). Cada token já identifica o tenant e o canal (WhatsApp) — não precisa de nada extra.

## Corpo da requisição

### Texto simples — `Content-Type: application/json`

```json
{
  "number": "5527999594959",
  "body": "Texto da mensagem",
  "sendAt": "2026-09-01T10:00:00-03:00"
}
```

### Com arquivo — `Content-Type: multipart/form-data`

```
number: 5527999594959
body: Texto da mensagem
sendAt: 2026-09-01T10:00:00-03:00
media: (arquivo — imagem, áudio, vídeo ou PDF)
```

### Com botões — `Content-Type: application/json`

```json
{
  "number": "5527999594959",
  "body": "Texto principal da mensagem",
  "sendAt": "2026-09-01T10:00:00-03:00",
  "footerText": "Rodapé opcional",
  "buttons": [
    { "displayText": "Ver oferta", "type": "url", "url": "https://exemplo.com" },
    { "displayText": "Quero saber mais", "type": "reply" },
    { "displayText": "Copiar cupom", "type": "copy", "copyText": "PROMO10" }
  ]
}
```

Também dá pra combinar botões com imagem de cabeçalho: manda como `multipart/form-data` com `media` + `buttons` (como string JSON) + os demais campos. **Não testado em produção ainda** — os 3 modos abaixo foram validados separadamente, a combinação botão+arquivo é só teoricamente suportada pelo código (mesmo caminho de `save_media` + `templateComponents`, mas nunca disparada de verdade).

## Campos

| Campo | Obrigatório | Descrição |
|---|---|---|
| `number` | sim | DDI+DDD+número, só dígitos (ex: `5527999594959`) |
| `body` | sim, exceto se só botões sem texto principal | Texto da mensagem |
| `sendAt` | sim | Data/hora ISO 8601, **no futuro** (ex: `2026-09-01T10:00:00-03:00`). Se não tiver timezone, assume UTC — sempre mandar com offset explícito |
| `media` | não | Arquivo (multipart). Tipos aceitos: imagem (jpeg/png/webp/gif), áudio (mp3/ogg/wav), vídeo (mp4/3gpp), PDF |
| `buttons` | não | Array de botões (ver abaixo). Se usado, a mensagem vira interativa |
| `footerText` | não | Rodapé da mensagem com botões |

### Tipos de botão (`buttons[].type`)

| Tipo | Campo extra | Efeito |
|---|---|---|
| `url` | `url` | Botão abre um link |
| `reply` | — | Botão de resposta rápida (quick reply) |
| `copy` | `copyText` | Botão copia texto pro clipboard do contato |

Máximo de botões: seguir o limite do WhatsApp (normalmente 3).

## Resposta

**Sucesso — `201`**
```json
{ "id": 70, "status": "PENDENTE", "sendAt": "2026-09-01T10:00:00-03:00" }
```

Guarda o `id` se precisar consultar/cancelar depois (cancelamento ainda não existe nessa API — hoje só cria).

**Erro — `400`** (validação): campo obrigatório faltando, `number` fora do formato, `sendAt` no passado ou mal formatado, tipo de botão inválido, tipo de arquivo não suportado.
```json
{ "erro": "descrição do problema" }
```

**Erro — `401`**: token ausente, expirado ou inválido.
**Erro — `403`**: token revogado (desativado no Whazing).
**Erro — `500`**: erro interno — logar e não reprocessar automaticamente sem investigar.

## Comportamento

- A mensagem fica com `status=PENDENTE` até o horário `sendAt` chegar — um job do Whazing processa e envia automaticamente, sem precisar de nada nosso depois de criada.
- Se o `number` não existir como contato ainda, a API cria o contato automaticamente nesse tenant.
- Não há confirmação de entrega própria — pra saber se enviou, seria necessário consultar o Whazing (fora do escopo dessa API por enquanto).

## Validado em produção

Testado de ponta a ponta em 2026-08-29 (tenant 1, canal BoraAutomatizar) — os 3 modos abaixo foram criados via API e confirmados como `ENVIADA` no WhatsApp real:

| Modo | id | Resultado |
|---|---|---|
| Texto | 68 | ENVIADA |
| Arquivo (imagem) | 69 | ENVIADA |
| Botões (url + reply + copy) | 70 | ENVIADA |

Combinação botão+arquivo junto: não testada (ver nota acima).

## Exemplo cURL

```bash
curl -X POST https://agendamento.boraautomatizar.com.br/agendar \
  -H "Authorization: Bearer SEU_TOKEN_AQUI" \
  -H "Content-Type: application/json" \
  -d '{
    "number": "5527999594959",
    "body": "Oi! Passando pra saber se ficou alguma dúvida sobre a proposta.",
    "sendAt": "2026-09-02T09:00:00-03:00"
  }'
```

Com arquivo:

```bash
curl -X POST https://agendamento.boraautomatizar.com.br/agendar \
  -H "Authorization: Bearer SEU_TOKEN_AQUI" \
  -F "number=5527999594959" \
  -F "body=Segue a proposta em anexo" \
  -F "sendAt=2026-09-02T09:00:00-03:00" \
  -F "media=@/caminho/proposta.pdf;type=application/pdf"
```
