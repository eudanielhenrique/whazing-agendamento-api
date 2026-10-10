# whazing-agendamento-api/main.py
#
# Wrapper que expõe agendamento de mensagem (texto, arquivo, botão interativo) via HTTP,
# escrevendo direto na tabela Schedules que o job VerifySchedules do Whazing já processa.
# Reaproveita o mesmo token Bearer (JWT) que a API oficial de mensagens do Whazing usa.

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from functools import wraps

import jwt
import psycopg2
import psycopg2.extras
import requests
from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

RECURRENCE_INTERVALS = {
    'diario': relativedelta(days=1),
    'semanal': relativedelta(weeks=1),
    'quinzenal': relativedelta(days=15),
    'mensal': relativedelta(months=1),
    'bimestral': relativedelta(months=2),
    'trimestral': relativedelta(months=3),
    'semestral': relativedelta(months=6),
    'anual': relativedelta(years=1),
}

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

JWT_SECRET = os.environ['JWT_SECRET']

DB_CONFIG = dict(
    host=os.environ.get('DB_HOST', 'postgres'),
    port=os.environ.get('DB_PORT', '5432'),
    user=os.environ.get('DB_USER', 'whazing'),
    password=os.environ['DB_PASSWORD'],
    dbname=os.environ.get('DB_NAME', 'postgres'),
)

PUBLIC_DIR = os.environ.get('PUBLIC_DIR', '/app/public')

# Proxy transparente pra Datasigh — não guarda nem valida credencial própria, só repassa o
# header Authorization que o chamador mandar. Existe só pra filtrar diaSemana antes do dado
# chegar no agente (ex: Dr. Vinícius NUNCA atende terça/quinta/sexta, mas a Datasigh às vezes
# devolve vaga nesses dias mesmo assim — ver ticket 16031, 2026-10-09: o agente ofereceu uma
# vaga de quinta, o paciente mandou documento, e só 2h depois o agente percebeu que não podia
# ter oferecido aquele dia. Filtrar aqui, no dado, é mais seguro que confiar que o prompt do
# agente vai lembrar da regra toda vez que listar uma resposta de /horarios).
DATASIGH_HORARIOS_URL = 'https://ws.datasigh.com.br/api/integracao/v1/horarios'

ALLOWED_MEDIA_CONTENT_TYPES = {
    'audio/mpeg', 'audio/mp3', 'audio/ogg', 'audio/wav', 'audio/x-wav',
    'image/jpeg', 'image/png', 'image/webp', 'image/gif',
    'video/mp4', 'video/3gpp',
    'application/pdf',
}


def get_db():
    return psycopg2.connect(**DB_CONFIG)


@app.route('/datasigh/horarios', methods=['GET'])
def datasigh_horarios():
    auth = request.headers.get('Authorization', '')
    if not auth:
        return jsonify(erro='Header Authorization ausente'), 401

    params = request.args.to_dict()
    # excludeMap: JSON tipo {"<idProfissional>": ["Terça-feira", "Quinta-feira", ...]} — nunca
    # vai pra Datasigh, só decide o que esse wrapper tira do resultado antes de devolver.
    exclude_map_raw = params.pop('excludeMap', None)
    exclude_days = set()
    if exclude_map_raw:
        try:
            exclude_map = json.loads(exclude_map_raw)
            exclude_days = set(exclude_map.get(str(params.get('idProfissional', '')), []))
        except (ValueError, AttributeError, TypeError):
            logging.warning('excludeMap inválido recebido: %r', exclude_map_raw)

    try:
        upstream = requests.get(
            DATASIGH_HORARIOS_URL,
            params=params,
            headers={'Authorization': auth},
            timeout=15,
        )
    except requests.RequestException:
        logging.exception('Erro ao chamar Datasigh /horarios')
        return jsonify(erro='Erro ao consultar Datasigh'), 502

    content_type = upstream.headers.get('Content-Type', 'application/json')
    if upstream.status_code != 200:
        return upstream.content, upstream.status_code, {'Content-Type': content_type}

    try:
        data = upstream.json()
    except ValueError:
        return upstream.content, upstream.status_code, {'Content-Type': content_type}

    if exclude_days and isinstance(data, list):
        data = [item for item in data if item.get('diaSemana') not in exclude_days]

    return jsonify(data)


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify(erro='Token ausente'), 401

        token = auth[len('Bearer '):].strip()
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            return jsonify(erro='Token expirado'), 401
        except jwt.InvalidTokenError:
            return jsonify(erro='Token inválido'), 401

        tenant_id = payload.get('tenantId')
        session_id = payload.get('sessionId')
        if not tenant_id or not session_id:
            return jsonify(erro='Token sem tenantId/sessionId'), 401

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT "isActive" FROM "ApiConfigs" WHERE token = %s AND "tenantId" = %s',
                    (token, tenant_id),
                )
                row = cur.fetchone()
        finally:
            conn.close()

        if row is None or row[0] is not True:
            return jsonify(erro='Token revogado ou não encontrado'), 403

        request.tenant_id = tenant_id
        request.whatsapp_id = session_id
        request.user_id = payload.get('userId') or None
        return f(*args, **kwargs)

    return wrapper


def resolve_contact(cur, tenant_id, number):
    cur.execute(
        'SELECT id FROM "Contacts" WHERE "tenantId" = %s AND number = %s LIMIT 1',
        (tenant_id, number),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    now = datetime.now(timezone.utc)
    cur.execute(
        '''INSERT INTO "Contacts" (name, number, "tenantId", "isGroup", "createdAt", "updatedAt")
           VALUES (%s, %s, %s, false, %s, %s) RETURNING id''',
        (number, number, tenant_id, now, now),
    )
    return cur.fetchone()[0]


def build_media_name(original_filename):
    stem, ext = os.path.splitext(secure_filename(original_filename))
    now = datetime.now()
    suffix = now.strftime('%d%m%Y%H%M%S') + f'{now.microsecond // 1000:03d}'
    return f'{stem}_{suffix}{ext}'


def save_media(file_storage, tenant_id):
    now = datetime.now()
    media_path = f'{tenant_id}/schedule/{now.strftime("%Y%m")}'
    media_name = build_media_name(file_storage.filename)

    dest_dir = os.path.join(PUBLIC_DIR, str(tenant_id), 'schedule', now.strftime('%Y%m'))
    os.makedirs(dest_dir, exist_ok=True)

    dest_path = os.path.join(dest_dir, media_name)
    file_storage.save(dest_path)

    return media_path, media_name


def build_template_components(buttons, footer_text, body_text):
    choices = []
    for i, btn in enumerate(buttons):
        btn_type = btn.get('type')
        if btn_type not in ('url', 'reply', 'copy'):
            raise ValueError(f'Tipo de botão inválido: {btn_type!r} (use url, reply ou copy)')
        choice = {
            'displayText': btn['displayText'],
            'type': btn_type,
            'id': f'opt_{int(time.time() * 1000)}_{i}',
        }
        if btn_type == 'url':
            choice['url'] = btn['url']
        elif btn_type == 'copy':
            choice['copyText'] = btn['copyText']
        choices.append(choice)

    components = {'text': body_text, 'choices': choices}
    if footer_text:
        components['footerText'] = footer_text
    return components


@app.route('/health', methods=['GET'])
def health():
    return 'ok', 200


@app.route('/agendar', methods=['POST'])
@require_auth
def agendar():
    tenant_id = request.tenant_id
    whatsapp_id = request.whatsapp_id
    user_id = request.user_id

    is_multipart = request.content_type and 'multipart/form-data' in request.content_type
    data = request.form if is_multipart else (request.get_json(silent=True) or {})

    number = (data.get('number') or '').strip()
    body = data.get('body') or ''
    send_at_raw = data.get('sendAt')
    buttons_raw = data.get('buttons')
    footer_text = data.get('footerText')
    recurrence_raw = data.get('recurrence')

    if not number:
        return jsonify(erro='Campo "number" é obrigatório'), 400
    if not re.fullmatch(r'\d{10,15}', number):
        return jsonify(erro='Campo "number" deve ser DDI+DDD+número, só dígitos'), 400
    if not body and not buttons_raw:
        return jsonify(erro='Campo "body" é obrigatório'), 400
    if not send_at_raw:
        return jsonify(erro='Campo "sendAt" é obrigatório (ISO 8601)'), 400

    try:
        send_at = dateparser.isoparse(send_at_raw)
    except (ValueError, OverflowError):
        return jsonify(erro='Campo "sendAt" inválido, use ISO 8601'), 400

    if send_at.tzinfo is None:
        send_at = send_at.replace(tzinfo=timezone.utc)
    if send_at <= datetime.now(timezone.utc):
        return jsonify(erro='"sendAt" precisa ser no futuro'), 400

    send_at_list = [send_at]
    if recurrence_raw:
        try:
            recurrence = json.loads(recurrence_raw) if isinstance(recurrence_raw, str) else recurrence_raw
            interval_name = recurrence['interval']
            repetitions = int(recurrence.get('repetitions', 1))
        except (ValueError, KeyError, TypeError):
            return jsonify(erro='Campo "recurrence" inválido — use {"interval": ..., "repetitions": N}'), 400

        delta = RECURRENCE_INTERVALS.get(interval_name)
        if delta is None:
            return jsonify(erro=f'"recurrence.interval" inválido: {interval_name!r} '
                                 f'(use um de {sorted(RECURRENCE_INTERVALS)})'), 400
        if repetitions < 1:
            return jsonify(erro='"recurrence.repetitions" precisa ser >= 1'), 400

        send_at_list = [send_at + i * delta for i in range(repetitions)]

    message_type = 'texto'
    template_components = None
    if buttons_raw:
        try:
            buttons = json.loads(buttons_raw) if isinstance(buttons_raw, str) else buttons_raw
            template_components = build_template_components(buttons, footer_text, body)
        except (ValueError, KeyError, TypeError) as e:
            return jsonify(erro=f'Campo "buttons" inválido: {e}'), 400
        message_type = 'botoes'

    media_path = media_name = None
    if is_multipart and 'media' in request.files and request.files['media'].filename:
        media_file = request.files['media']
        if media_file.content_type not in ALLOWED_MEDIA_CONTENT_TYPES:
            return jsonify(erro=f'Tipo de arquivo não suportado: {media_file.content_type}'), 400
        media_path, media_name = save_media(media_file, tenant_id)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            contact_id = resolve_contact(cur, tenant_id, number)

            created = []
            for occurrence_send_at in send_at_list:
                now = datetime.now(timezone.utc)
                cur.execute(
                    '''INSERT INTO "Schedules" (
                           body, "sendAt", "contactId", "userId", "tenantId", "whatsappId",
                           status, "mediaPath", "mediaName", "Tunel", "isTemplate", "openTicket",
                           "messageType", "templateComponents", "createdAt", "updatedAt"
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s,
                           'PENDENTE', %s, %s, false, false, false,
                           %s, %s, %s, %s
                       ) RETURNING id''',
                    (
                        body, occurrence_send_at, contact_id, user_id, tenant_id, whatsapp_id,
                        media_path, media_name,
                        message_type,
                        json.dumps(template_components) if template_components else None,
                        now, now,
                    ),
                )
                created.append({'id': cur.fetchone()[0], 'sendAt': occurrence_send_at.isoformat()})
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception('Erro ao criar agendamento')
        return jsonify(erro='Erro interno ao criar agendamento'), 500
    finally:
        conn.close()

    ids = [c['id'] for c in created]
    logging.info(f'Agendamento(s) {ids} criado(s) — tenant={tenant_id} number={number}')

    if len(created) == 1:
        return jsonify(id=created[0]['id'], status='PENDENTE', sendAt=created[0]['sendAt']), 201
    return jsonify(schedules=created, status='PENDENTE', count=len(created)), 201


@app.route('/nota', methods=['POST'])
@require_auth
def criar_nota():
    tenant_id = request.tenant_id
    user_id = request.user_id

    data = request.get_json(silent=True) or {}
    ticket_id = data.get('ticketId')
    note = (data.get('note') or '').strip()

    if not isinstance(ticket_id, int):
        return jsonify(erro='Campo "ticketId" é obrigatório (inteiro)'), 400
    if not note:
        return jsonify(erro='Campo "note" é obrigatório'), 400

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT "contactId" FROM "Tickets" WHERE id = %s AND "tenantId" = %s',
                (ticket_id, tenant_id),
            )
            row = cur.fetchone()
            if row is None:
                return jsonify(erro='Ticket não encontrado'), 404
            contact_id = row[0]

            now = datetime.now(timezone.utc)
            cur.execute(
                '''INSERT INTO "TicketNotes" (
                       note, "userId", "contactId", "ticketId", "tenantId", "createdAt", "updatedAt"
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                (note, user_id, contact_id, ticket_id, tenant_id, now, now),
            )
            note_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception('Erro ao criar nota interna')
        return jsonify(erro='Erro interno ao criar nota'), 500
    finally:
        conn.close()

    logging.info(f'Nota {note_id} criada — tenant={tenant_id} ticket={ticket_id}')
    return jsonify(id=note_id, ticketId=ticket_id, createdAt=now.isoformat()), 201


@app.route('/agendar/<int:schedule_id>', methods=['DELETE'])
@require_auth
def cancelar(schedule_id):
    tenant_id = request.tenant_id

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT status FROM "Schedules" WHERE id = %s AND "tenantId" = %s',
                (schedule_id, tenant_id),
            )
            row = cur.fetchone()
            if row is None:
                return jsonify(erro='Agendamento não encontrado'), 404
            if row[0] != 'PENDENTE':
                return jsonify(erro=f'Agendamento já está "{row[0]}", não é possível cancelar'), 409

            cur.execute('DELETE FROM "Schedules" WHERE id = %s AND "tenantId" = %s', (schedule_id, tenant_id))
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception('Erro ao cancelar agendamento')
        return jsonify(erro='Erro interno ao cancelar agendamento'), 500
    finally:
        conn.close()

    logging.info(f'Agendamento {schedule_id} cancelado — tenant={tenant_id}')
    return '', 204


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
