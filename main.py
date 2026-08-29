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
from dateutil import parser as dateparser
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

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

ALLOWED_MEDIA_CONTENT_TYPES = {
    'audio/mpeg', 'audio/mp3', 'audio/ogg', 'audio/wav', 'audio/x-wav',
    'image/jpeg', 'image/png', 'image/webp', 'image/gif',
    'video/mp4', 'video/3gpp',
    'application/pdf',
}


def get_db():
    return psycopg2.connect(**DB_CONFIG)


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
                    body, send_at, contact_id, user_id, tenant_id, whatsapp_id,
                    media_path, media_name,
                    message_type,
                    json.dumps(template_components) if template_components else None,
                    now, now,
                ),
            )
            schedule_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception('Erro ao criar agendamento')
        return jsonify(erro='Erro interno ao criar agendamento'), 500
    finally:
        conn.close()

    logging.info(f'Agendamento {schedule_id} criado — tenant={tenant_id} number={number} sendAt={send_at}')
    return jsonify(id=schedule_id, status='PENDENTE', sendAt=send_at.isoformat()), 201


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
