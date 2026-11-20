from flask import Blueprint, request, jsonify
from app.pipeline import calculate
from app.numeric import serial, number as parse_number
from app.errors import DomainError
from app.store import get_run
from app.reblend import build_plan
from app import reblend_store as stock

bp = Blueprint('m7', __name__)


@bp.route('/api/m7', methods=['GET', 'POST'])
def module_7():
    payload = request.get_json(silent=True) or {}
    result = calculate(payload.get('csv'), stop=7)
    return jsonify(serial(result['stages']['7']))


def _plan_for(run_id):
    run = get_run(run_id)
    acceptance = run['stages']['6']['rows']
    materials = stock.load_materials()
    orders = stock.load_orders()
    plan = build_plan(acceptance, materials, orders, run_id)
    plan['confirm_key'] = f'{run_id}:{plan["plan_hash"]}'
    plan['run_id'] = run_id
    return plan, orders


@bp.route('/api/reblend/plan', methods=['POST'])
def reblend_plan():
    body = request.get_json(silent=True) or {}
    run_id = body.get('run_id')
    if not isinstance(run_id, str) or not run_id:
        raise DomainError('REQUEST', '缺少run_id')
    plan, _ = _plan_for(run_id)
    return jsonify(serial(plan))


@bp.route('/api/reblend/confirm', methods=['POST'])
def reblend_confirm():
    body = request.get_json(silent=True) or {}
    run_id = body.get('run_id')
    client_key = body.get('confirm_key')
    if not isinstance(run_id, str) or not run_id:
        raise DomainError('REQUEST', '缺少run_id')
    if client_key is not None and not isinstance(client_key, str):
        raise DomainError('REQUEST', 'confirm_key必须为字符串')
    # 幂等优先：同一确认键的重试直接回放原单据，绝不二次扣料
    if client_key:
        try:
            existing = stock.get_confirmation(client_key)
            if existing['run_id'] == run_id:
                after = stock.inventory_view()
                return jsonify(serial({**existing, 'idempotent': True,
                                       'plan': _plan_for(run_id)[0], 'inventory': after}))
        except DomainError as error:
            if error.code != 'NOT_FOUND':
                raise
    # 新确认：每次都用最新库存重算方案，库存被他人占用时 plan_hash 必然变化
    plan, _ = _plan_for(run_id)
    if client_key and client_key != plan['confirm_key']:
        raise DomainError('VERSION', '补料库存或方案已变化，请刷新后重新确认', 409)
    confirmation = stock.confirm_plan(
        plan['confirm_key'], run_id, plan['plan_hash'],
        plan['allocations'], plan['rows'])
    after = stock.inventory_view()
    return jsonify(serial({**confirmation, 'plan': plan, 'inventory': after}))


@bp.route('/api/reblend/inventory', methods=['GET'])
def reblend_inventory():
    return jsonify(serial(stock.inventory_view()))


@bp.route('/api/reblend/confirmations', methods=['GET'])
def reblend_confirmations():
    return jsonify(serial(stock.list_confirmations()))


@bp.route('/api/reblend/confirmations/<confirm_key>', methods=['GET'])
def reblend_confirmation_detail(confirm_key):
    return jsonify(serial(stock.get_confirmation(confirm_key)))


@bp.route('/api/reblend/replenish', methods=['POST'])
def reblend_replenish():
    body = request.get_json(silent=True) or {}
    try:
        change = parse_number(body.get('change_kg'))
    except DomainError:
        raise DomainError('NUMBER', '变动量必须为数值')
    note = body.get('note') or '手工调整'
    if not isinstance(note, str):
        raise DomainError('REQUEST', '备注必须为文本')
    material_id = body.get('material_id')
    if not isinstance(material_id, str) or not material_id:
        raise DomainError('REQUEST', '缺少material_id')
    return jsonify(serial(stock.replenish(material_id, change, note[:100])))


@bp.route('/api/reblend/reset', methods=['POST'])
def reblend_reset():
    body = request.get_json(silent=True) or {}
    csv_text = body.get('inventory_csv')
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise DomainError('REQUEST', '缺少inventory_csv')
    order_text = body.get('orders_csv')
    if order_text is not None and (not isinstance(order_text, str) or not order_text.strip()):
        order_text = None
    return jsonify(serial(stock.reset_inventory(csv_text, order_text)))
