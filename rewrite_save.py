# -*- coding: utf-8 -*-
from pathlib import Path
path = Path("app.py")
text = path.read_text(encoding="utf-8")
needle_start = "@app.route('/api/labor_assignment/save', methods=['POST'])"
needle_end = "@app.route('/api/labor_assignment/delete', methods=['POST'])"
start = text.index(needle_start)
end = text.index(needle_end, start)
new_block = """@app.route('/api/labor_assignment/save', methods=['POST'])
def labor_assignment_save():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    ngaylv = _parse_date(data.get('ngaylv'))
    employee_ids_raw = data.get('employee_ids') or []
    task_id = (data.get('task_id') or '').strip()
    start_time_val = _parse_time(data.get('start_time'))
    end_time_val = _parse_time(data.get('end_time'))

    if not isinstance(employee_ids_raw, list):
        employee_ids_raw = [employee_ids_raw]

    normalized_employee_ids = []
    for raw_id in employee_ids_raw:
        raw_str = str(raw_id).strip()
        if not raw_str:
            continue
        try:
            normalized_employee_ids.append(int(raw_str))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': f"Invalid employee id: {raw_id}"}), 400

    seen = set()
    employee_ids = []
    for eid in normalized_employee_ids:
        if eid not in seen:
            seen.add(eid)
            employee_ids.append(eid)

    if not ngaylv or not employee_ids or not task_id or not start_time_val:
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400

    assignment_id = data.get('id')
    if assignment_id:
        try:
            assignment_id = int(assignment_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Invalid assignment ID'}), 400
        assignment = LaborAssignment.query.get(assignment_id)
        if not assignment:
            return jsonify({'success': False, 'message': 'Assignment not found'}), 404
    else:
        assignment = LaborAssignment(created_at=datetime.utcnow())
        db.session.add(assignment)

    assignment.ngaylv = ngaylv
    assignment.task_id = task_id
    assignment.start_time = start_time_val
    assignment.end_time = end_time_val
    assignment.carton = data.get('carton') or None
    assignment.cbm = data.get('cbm') or None
    assignment.inbound_vehicle_no = (data.get('inbound_vehicle_no') or '').strip() or None
    assignment.job_no = (data.get('job_no') or '').strip() or None
    assignment.loading_container_no = (data.get('loading_container_no') or '').strip() or None
    assignment.note = (data.get('note') or '').strip() or None
    assignment.inbound_id = (data.get('inbound_id') or '').strip() or None
    assignment.outbound_id = (data.get('outbound_id') or '').strip() or None

    try:
        cart_val = assignment.carton
        assignment.carton = int(cart_val) if cart_val not in (None, '') else None
    except (TypeError, ValueError):
        assignment.carton = None

    try:
        cbm_val = assignment.cbm
        assignment.cbm = float(cbm_val) if cbm_val not in (None, '') else None
    except (TypeError, ValueError):
        assignment.cbm = None

    assignment.task_employees.clear()
    for eid in employee_ids:
        assignment.task_employees.append(TaskEmployee(employee_id=eid))

    try:
        db.session.commit()
        return jsonify({'success': True, 'id': assignment.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

"""
text = text[:start] + new_block + text[end:]
path.write_text(text, encoding="utf-8")
