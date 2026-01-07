from flask import Flask, jsonify, render_template, request, redirect, url_for, session, Response
from sqlalchemy import text, func
import os
import csv
import io
from dotenv import load_dotenv
from datetime import datetime
import pandas as pd

# Nạp biến môi trường từ file .env
load_dotenv()

from config import Config
from extensions import db
from models.users import User
from models.scanfile import Scanfile
from models.masterdata import MasterData
from models.load import Load
from models.log import Log

app = Flask(__name__)
app.config.from_object(Config)

# Khởi tạo database
db.init_app(app)

# Cache cục bộ cho MasterData để giảm query DB khi scan liên tục
_refix_cache = {}

@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    # Lưu ý: Bạn cần có file templates/home.html
    return render_template('home.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Kiểm tra user trong database
        user = User.query.filter_by(username=username).first()
        
        if user and user.password == password:
            session['user'] = user.username
            session['role'] = user.role
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Sai tên đăng nhập hoặc mật khẩu")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/scan')
def scan():
    if 'user' not in session: return redirect(url_for('login'))
    
    # Lấy danh sách Job Type từ Database (để dropdown không bị trống)
    job_types = []
    try:
        job_types = [r[0] for r in db.session.query(Scanfile.jobno_type).distinct().all() if r[0]]
    except Exception:
        pass # Bỏ qua lỗi nếu bảng chưa có dữ liệu

    # Tạo danh sách Pallet 1-25, loại bỏ các pallet đã có jobscan (đã in)
    used_pallets = set()
    if job_types:
        try:
            rows = db.session.query(Scanfile.pallet).filter(
                Scanfile.jobno_type == job_types[0],
                Scanfile.jobscan != '',
                Scanfile.jobscan != None
            ).distinct().all()
            used_pallets = {r[0] for r in rows}
        except Exception:
            pass

    pallets = []
    for i in range(1, 26):
        if str(i) not in used_pallets:
            pallets.append({'no': i, 'label': f"{i}"})
    
    return render_template('scan.html', job_types=job_types, available_pallets=pallets)

@app.route('/api/get_pallets')
def get_pallets():
    job_type = request.args.get('job_type')
    
    used_pallets = set()
    if job_type:
        try:
            rows = db.session.query(Scanfile.pallet).filter(
                Scanfile.jobno_type == job_type,
                Scanfile.jobscan != '',
                Scanfile.jobscan != None
            ).distinct().all()
            used_pallets = {r[0] for r in rows}
        except Exception:
            pass

    pallets = []
    for i in range(1, 26):
        if str(i) not in used_pallets:
            pallets.append({'no': i, 'label': f"{i}"})
            
    return jsonify({'success': True, 'pallets': pallets})

@app.route('/api/scan', methods=['POST'])
def api_scan():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Phiên đăng nhập hết hạn'}), 401

    data = request.json
    barcode = data.get('barcode', '').strip()
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')
    pallet_type = data.get('pallet_type')

    if not barcode or not pallet_no:
        return jsonify({'success': False, 'message': 'Thiếu thông tin Barcode hoặc Pallet'}), 400

    # [TỐI ƯU] Kiểm tra khóa bằng bảng Load (nhanh hơn quét bảng Scanfile lớn)
    is_locked = Load.query.filter_by(jobno_type=job_type, pallet_no=pallet_no).first()
    
    if is_locked:
        return jsonify({'success': False, 'message': f'Pallet {pallet_no} đã bị khóa (Hoàn thành). Không thể thêm hàng.'}), 400

    # 1. Kiểm tra Barcode (Sử dụng Cache)
    # Logic: Lấy từ bên phải, bỏ qua 1 ký tự cuối (vị trí thứ 1 từ phải), lấy 5 ký tự trước đó
    refix_val = barcode[-6:-1]
    
    sku = _refix_cache.get(refix_val)
    if not sku:
        master_item = MasterData.query.filter_by(refix=refix_val).first()
        if not master_item:
            return jsonify({'success': False, 'message': f'Refix {refix_val} (từ barcode {barcode}) không tồn tại trong hệ thống'}), 404
        sku = master_item.sku
        _refix_cache[refix_val] = sku

    try:
        # 2. Cập nhật (Edit) record có sẵn trong Scanfile thay vì tạo mới
        # Tìm bản ghi khớp SKU, Job Type và chưa được scan (pallet là null hoặc rỗng)
        scan_entry = Scanfile.query.filter(
            Scanfile.sku == sku,
            Scanfile.jobno_type == job_type,
            (Scanfile.pallet == '') | (Scanfile.pallet == None)
        ).first()

        if not scan_entry:
            return jsonify({'success': False, 'message': 'Không tìm thấy dữ liệu chờ (pallet rỗng) cho SKU này'}), 404

        
        scan_entry.pallet = pallet_no
        scan_entry.pallet_type = pallet_type
        scan_entry.time_scan = datetime.now()
        scan_entry.userscan = session.get('user')

        db.session.commit()

        # Thống kê tiến độ của SKU này trong Job để hiển thị
        sku_total = Scanfile.query.filter_by(jobno_type=job_type, sku=sku).count()
        sku_scanned = Scanfile.query.filter(
            Scanfile.jobno_type == job_type,
            Scanfile.sku == sku,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).count()

        return jsonify({'success': True, 'message': 'Scan thành công', 'sku': sku, 'sku_scanned': sku_scanned, 'sku_total': sku_total})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Lỗi lưu database: {str(e)}'}), 500

@app.route('/api/get_remain_skus', methods=['POST'])
def get_remain_skus():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Phiên đăng nhập hết hạn'}), 401
    
    data = request.json
    job_type = data.get('job_type')

    if not job_type:
        return jsonify({'success': False, 'message': 'Thiếu thông tin Job Type'}), 400

    try:
        # Thống kê số lượng chưa scan (pallet rỗng) theo SKU
        results = db.session.query(
            Scanfile.sku, 
            func.count(Scanfile.id)
        ).filter(
            Scanfile.jobno_type == job_type,
            (Scanfile.pallet == '') | (Scanfile.pallet == None)
        ).group_by(Scanfile.sku).all()

        items = [{'sku': r[0], 'qty': r[1]} for r in results]
        
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/job_stats', methods=['POST'])
def job_stats_api():
    if 'user' not in session: return jsonify({'success': False}), 401
    
    data = request.json
    job_type = data.get('job_type')
    
    if not job_type: return jsonify({'success': False}), 400

    try:
        # Tổng số lượng trong Job
        total = Scanfile.query.filter_by(jobno_type=job_type).count()
        
        # Số lượng đã scan (có pallet)
        scanned = Scanfile.query.filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).count()
        
        return jsonify({
            'success': True,
            'total_sscc': total,
            'scanned_sscc': scanned,
            'remain_sscc': total - scanned
        })
    except Exception:
        return jsonify({'success': False}), 500

@app.route('/api/get_history', methods=['POST'])
def get_history():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Phiên đăng nhập hết hạn'}), 401
    
    data = request.json
    job_type = data.get('job_type')

    if not job_type:
        return jsonify({'success': False, 'message': 'Thiếu thông tin Job Type'}), 400

    try:
        results = db.session.query(
            Scanfile.pallet,
            Scanfile.sku,
            func.count(Scanfile.sscc)
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).group_by(
            Scanfile.pallet,
            Scanfile.sku
        ).all()

        history = [{'pallet': r[0], 'sku': r[1], 'qty': r[2]} for r in results]
        
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/pallet_details', methods=['POST'])
def pallet_details():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Phiên đăng nhập hết hạn'}), 401
    
    data = request.json
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')

    if not job_type or not pallet_no:
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400

    try:
        # Kiểm tra trạng thái khóa của Pallet (dựa vào tag_label='COMPLETED')
        is_locked = Scanfile.query.filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet == pallet_no,
            Scanfile.finish == 'COMPLETED'
        ).first() is not None

        # Lấy danh sách SKU và số lượng trong Pallet cụ thể
        results = db.session.query(
            Scanfile.sku,
            func.count(Scanfile.id)
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet == pallet_no
        ).group_by(Scanfile.sku).all()

        skus = [{'sku': r[0], 'qty': r[1]} for r in results]
        pallet_count = sum(item['qty'] for item in skus)
        
        return jsonify({'success': True, 'pallet_count': pallet_count, 'skus': skus, 'is_locked': is_locked})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/sku_details', methods=['POST'])
def sku_details():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Phiên đăng nhập hết hạn'}), 401
    
    data = request.json
    job_type = data.get('job_type')
    sku = data.get('sku')

    if not job_type or not sku:
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400

    try:
        # Tìm xem SKU này đang nằm ở những Pallet nào
        results = db.session.query(
            Scanfile.pallet,
            func.count(Scanfile.id)
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.sku == sku,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).group_by(Scanfile.pallet).all()

        details = [{'pallet': r[0], 'qty': r[1]} for r in results]
        
        return jsonify({'success': True, 'details': details})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/finish_pallet', methods=['POST'])
def finish_pallet():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Phiên đăng nhập hết hạn'}), 401
    
    data = request.json
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')

    if not job_type or not pallet_no:
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400

    try:
        # Kiểm tra pallet có hàng không (không khóa pallet trống)
        count = Scanfile.query.filter_by(jobno_type=job_type, pallet=pallet_no).count()
        if count == 0:
            return jsonify({'success': False, 'message': 'Pallet trống, không thể khóa'}), 400

        # Kiểm tra xem Pallet này đã có trong bảng Load chưa (tránh trùng lặp)
        if Load.query.filter_by(jobno_type=job_type, pallet_no=pallet_no).first():
             return jsonify({'success': False, 'message': 'Pallet này đã được báo xong trước đó.'}), 400

        # Cập nhật finish thành 'COMPLETED' cho toàn bộ item trong pallet này
        Scanfile.query.filter_by(jobno_type=job_type, pallet=pallet_no).update(
            {'finish': 'COMPLETED'}, 
            synchronize_session=False
        )
        
        # Lưu thông tin vào bảng Load để nhân viên Printer biết
        new_load = Load(
            jobno_type=job_type,
            pallet_no=pallet_no,
            pallet_type=data.get('pallet_type', ''),
            quantity=count,
            created_by=session.get('user'),
            status='PENDING' # Trạng thái mặc định là Chờ in
        )
        db.session.add(new_load)
        
        # Tạo Log thông báo cho Printer
        new_log = Log(
            username=session.get('user'),
            action='FINISH_PALLET',
            message=f"Đã báo xong Pallet {pallet_no} ({count} thùng)",
            is_read=False
        )
        db.session.add(new_log)

        db.session.commit()
        
        return jsonify({'success': True, 'message': f'Đã khóa Pallet {pallet_no} và gửi yêu cầu in tem.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/unlock_pallet', methods=['POST'])
def unlock_pallet():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Phiên đăng nhập hết hạn'}), 401
    
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Chỉ Admin mới có quyền mở khóa Pallet.'}), 403

    data = request.json
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')

    if not job_type or not pallet_no:
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400

    try:
        # 1. Xóa khỏi bảng Load (bảng quản lý trạng thái khóa/in tem)
        Load.query.filter_by(jobno_type=job_type, pallet_no=pallet_no).delete()

        # 2. Cập nhật trạng thái finish trong Scanfile về NULL
        Scanfile.query.filter_by(jobno_type=job_type, pallet=pallet_no).update(
            {'finish': None}, 
            synchronize_session=False
        )
        
        # 3. Ghi log
        db.session.add(Log(
            username=session.get('user'),
            action='UNLOCK_PALLET',
            message=f"Đã mở khóa Pallet {pallet_no}",
            is_read=False
        ))

        db.session.commit()
        return jsonify({'success': True, 'message': f'Đã mở khóa Pallet {pallet_no} thành công.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/stats')
def stats():
    if 'user' not in session: return redirect(url_for('login'))
    
    # 1. Thống kê Pallet đã scan (Group by Job Type, Pallet Type)
    # Lấy danh sách các pallet unique (đã có pallet_no)
    pallets = db.session.query(
        Scanfile.jobno,
        Scanfile.jobno_type,
        Scanfile.pallet,
        Scanfile.pallet_type,
        func.count(Scanfile.id)
    ).filter(
        Scanfile.pallet != '',
        Scanfile.pallet != None
    ).group_by(
        Scanfile.jobno,
        Scanfile.jobno_type,
        Scanfile.pallet,
        Scanfile.pallet_type
    ).all()

    stats_data = {}
    grand_total = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0, 'total_box': 0}

    for job_no, job_type, pallet_no, p_type, sscc_count in pallets:
        # Key: (Job No, Job Type)
        key = (job_no, job_type) 
        
        if key not in stats_data:
            stats_data[key] = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0, 'total_box': 0}
        
        p_type_str = str(p_type) if p_type else ''
        
        # Nếu là loose hoặc loosecarton thì tính theo số lượng SSCC, ngược lại tính là 1 pallet
        increment = sscc_count if 'loose' in p_type_str.lower() else 1
        
        if '1.2' in p_type_str:
            stats_data[key]['1.2'] += increment
            grand_total['1.2'] += increment
        elif '1.6' in p_type_str:
            stats_data[key]['1.6'] += increment
            grand_total['1.6'] += increment
        elif '1.9' in p_type_str:
            stats_data[key]['1.9'] += increment
            grand_total['1.9'] += increment
        else:
            stats_data[key]['loose'] += increment
            grand_total['loose'] += increment
            
        stats_data[key]['total'] += increment
        grand_total['total'] += increment
        stats_data[key]['total_box'] += sscc_count
        grand_total['total_box'] += sscc_count

    # 2. Thống kê hàng tồn (Chưa scan)
    remain_query = db.session.query(
        Scanfile.jobno,
        Scanfile.jobno_type,
        func.count(Scanfile.id)
    ).filter(
        (Scanfile.pallet == '') | (Scanfile.pallet == None)
    ).group_by(Scanfile.jobno, Scanfile.jobno_type).all()

    remain_stats = {}
    for job_no, job_type, count in remain_query:
        key = (job_no, job_type)
        remain_stats[key] = count

    return render_template('statistics.html', stats=stats_data, grand_total=grand_total, remain_stats=remain_stats)

@app.route('/print-label')
def print_label():
    if 'user' not in session: return redirect(url_for('login'))
    
    # Lấy danh sách Job Type
    job_types = []
    try:
        job_types = [r[0] for r in db.session.query(Scanfile.jobno_type).filter(
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).distinct().all() if r[0]]
    except Exception:
        pass

    return render_template('print_label.html', job_types=job_types)

@app.route('/users')
def users_manage():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('users.html')

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('change_password.html')

@app.route('/notifications')
def notifications():
    if 'user' not in session: return redirect(url_for('login'))
    page = request.args.get('page', 1, type=int)
    per_page = 20 # Số lượng log trên mỗi trang
    pagination = Log.query.order_by(Log.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('notifications.html', pagination=pagination)

@app.route('/healthz')
def health_check():
    try:
        # API để Render kiểm tra sức khỏe định kỳ
        db.session.execute(text('SELECT 1'))
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'database': 'disconnected', 'error': str(e)}), 500

@app.route('/api/get_print_data', methods=['POST'])
def get_print_data():
    if 'user' not in session: return jsonify({'success': False}), 401
    data = request.json
    job_type = data.get('job_type')
    
    try:
        # Lấy chi tiết từng SKU trong mỗi pallet
        sku_details_query = db.session.query(
            Scanfile.pallet,
            Scanfile.sku,
            func.count(Scanfile.id).label('sscc_count'),
            func.max(Scanfile.tag_label).label('tag_label'),
            func.max(Scanfile.pallet_type).label('pallet_type'),
            func.max(Scanfile.jobscan).label('jobscan'),
            func.max(Scanfile.ship_to).label('ship_to'),
            func.max(Scanfile.master_add1).label('master_add1'),
            func.max(Scanfile.master_add2).label('master_add2'),
            func.max(Scanfile.master_add3).label('master_add3'),
            func.max(Scanfile.master_add4).label('master_add4'),
            func.max(Scanfile.master_delivery).label('master_delivery'),
            func.max(Scanfile.master_ctl).label('master_ctl'),
            func.max(Scanfile.st_zip).label('st_zip')
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).group_by(
            Scanfile.pallet,
            Scanfile.sku
        ).order_by(Scanfile.pallet, Scanfile.sku).all()

        # Nhóm các SKU lại theo từng pallet
        pallets_data = {}
        for row in sku_details_query:
            # Tách riêng Tem Nhỏ và Tem Thường trong cùng 1 pallet
            is_small = (row.tag_label == 'Y')
            key = (row.pallet, is_small)

            if key not in pallets_data:
                pallets_data[key] = {
                    'pallet_no': row.pallet,
                    'pallet_type': row.pallet_type,
                    'jobscan': row.jobscan,
                    'ship_to': row.ship_to,
                    'master_add1': row.master_add1,
                    'master_add2': row.master_add2,
                    'master_add3': row.master_add3,
                    'master_add4': row.master_add4,
                    'master_delivery': row.master_delivery,
                    'master_ctl': row.master_ctl,
                    'st_zip': row.st_zip,
                    'skus': [],
                    'qty': 0, # Tổng số thùng của nhóm này
                    'has_small_label': is_small
                }
            
            master_item = MasterData.query.filter_by(sku=row.sku).first()
            sku_weight = master_item.weight if master_item and master_item.weight else 0

            pallets_data[key]['skus'].append({
                'sku': row.sku,
                'qty': row.sscc_count,
                'tag_label': row.tag_label,
                'sku_weight': sku_weight
            })
            # Cập nhật tổng số lượng
            pallets_data[key]['qty'] += row.sscc_count
            
        items = list(pallets_data.values())
            
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/update_jobscan', methods=['POST'])
def update_jobscan():
    if 'user' not in session: return jsonify({'success': False}), 401
    data = request.json
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')
    jobscan = data.get('jobscan')
    
    try:
        Scanfile.query.filter_by(jobno_type=job_type, pallet=pallet_no).update(
            {'jobscan': jobscan}, synchronize_session=False
        )
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/get_sscc_data', methods=['POST'])
def get_sscc_data():
    if 'user' not in session: return jsonify({'success': False}), 401
    data = request.json
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')
    
    try:
        rows = Scanfile.query.filter_by(jobno_type=job_type, pallet=pallet_no).all()
        items = []
        for r in rows:
            items.append({
                'id': r.id,
                'pallet': r.pallet,
                'sscc': getattr(r, 'sscc', ''),
                'barcode': getattr(r, 'barcode', ''),
                'sku': r.sku,
                'qty': r.qty,
                'tag_label': getattr(r, 'tag_label', ''),
                'master_delivery': getattr(r, 'master_delivery', ''),
                'ship_to': getattr(r, 'ship_to', ''),
                'master_add1': getattr(r, 'master_add1', ''),
                'master_add2': getattr(r, 'master_add2', ''),
                'master_add3': getattr(r, 'master_add3', ''),
                'master_add4': getattr(r, 'master_add4', ''),
                'st_zip': getattr(r, 'st_zip', ''),
                'master_ctl': getattr(r, 'master_ctl', '')
            })
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/export_pallet_data')
def export_pallet_data():
    if 'user' not in session: return redirect(url_for('login'))
    job_type = request.args.get('job_type')
    
    try:
        # Truy vấn dữ liệu tổng hợp theo Pallet
        results = db.session.query(
            Scanfile.pallet,
            Scanfile.pallet_type,
            Scanfile.sku,
            func.count(Scanfile.id).label('sscc_count'),
            func.max(Scanfile.tag_label).label('tag_label'),
            func.max(Scanfile.jobscan).label('jobscan')
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).group_by(
            Scanfile.pallet,
            Scanfile.pallet_type,
            Scanfile.sku
        ).all()

        # Tạo file CSV trong bộ nhớ
        output = io.StringIO()
        writer = csv.writer(output)
        # Thêm BOM để Excel hiển thị đúng tiếng Việt/UTF-8
        output.write('\ufeff') 
        
        writer.writerow(['Pallet No', 'Job Type', 'Pallet Type', 'SKU', 'Quantity', 'Weight (Est)', 'Tag Label', 'Job Scan'])

        for row in results:
            master_item = MasterData.query.filter_by(sku=row.sku).first()
            sku_weight = master_item.weight if master_item and master_item.weight else 0
            total_weight = sku_weight * row.sscc_count
            
            writer.writerow([row.pallet, job_type, row.pallet_type, row.sku, row.sscc_count, total_weight, row.tag_label, row.jobscan])
            
        output.seek(0)
        return Response(output, mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename=pallet_data_{job_type}.csv"})
    except Exception as e:
        return f"Lỗi: {str(e)}", 500

@app.route('/importshipment')
def import_shipment_page():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('importshipment.html')

@app.route('/api/outbound/search', methods=['GET'])
def search_outbound():
    jobno = request.args.get('jobno')
    if not jobno: return jsonify({'success': False, 'message': 'Thiếu Job No'})
    
    try:
        # Tìm container trong bảng outbound, sắp xếp theo ngày đóng hàng
        # Giả định cột ngày là packing_date hoặc created_at
        query = text("""
            SELECT DISTINCT container, packing_date 
            FROM outbound 
            WHERE jobno = :jobno AND container IS NOT NULL AND container != ''
            ORDER BY packing_date ASC
        """)
        result = db.session.execute(query, {'jobno': jobno}).fetchall()
        
        containers = [{'container': r[0], 'packing_date': str(r[1]) if r[1] else ''} for r in result]
        return jsonify({'success': True, 'containers': containers})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/import_shipment', methods=['POST'])
def import_shipment_api():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    shipment_val = request.form.get('shipment')
    file = request.files.get('file')
    
    if not file: return jsonify({'success': False, 'message': 'Chưa chọn file'})
    
    try:
        # Đọc file Excel
        df = pd.read_excel(file)
        # Chuẩn hóa tên cột (xóa khoảng trắng thừa)
        df.columns = [str(c).strip() for c in df.columns]
        
        count = 0
        for index, row in df.iterrows():
            jobno = str(row.get('JOB NO', '')).strip()
            if not jobno or jobno.lower() == 'nan': continue
            
            pallet_no = str(row.get('Pallet number', '')).strip()
            
            # Tìm thông tin từ bảng outbound (cont, seal, loosecarton)
            # Dựa vào jobno và palletnumber
            outbound_q = text("""
                SELECT container, seal, loosecarton 
                FROM outbound 
                WHERE jobno = :jobno AND palletnumber = :pallet_no
                LIMIT 1
            """)
            out_res = db.session.execute(outbound_q, {'jobno': jobno, 'pallet_no': pallet_no}).fetchone()
            
            cont = out_res[0] if out_res else None
            seal = out_res[1] if out_res else None
            loose = out_res[2] if out_res else None
            
            # Insert vào bảng importshipment
            insert_q = text("""
                INSERT INTO importshipment (
                    jobno, relese_key, ponumber, sku, finaldc, hubdc, systempallet, 
                    measurement, weight, cbm_pallet, carton, palletnumber, 
                    cont, seal, shipmentorder, loosecarton, created_at, created_by
                ) VALUES (
                    :jobno, :relese_key, :ponumber, :sku, :finaldc, :hubdc, :systempallet,
                    :measurement, :weight, :cbm_pallet, :carton, :palletnumber,
                    :cont, :seal, :shipmentorder, :loosecarton, NOW(), :user
                )
            """)
            
            db.session.execute(insert_q, {
                'jobno': jobno,
                'relese_key': row.get('Release Key'),
                'ponumber': row.get('PO-NUMBER'),
                'sku': row.get('SKU'),
                'finaldc': row.get('ToFinalDC_ID'),
                'hubdc': row.get('ToHubDC_ID'),
                'systempallet': row.get('System Pallet number'),
                'measurement': row.get('MEASUREMENT'),
                'weight': row.get('G.WEIGHT'),
                'cbm_pallet': row.get('cbm'),
                'carton': row.get('ctn'),
                'palletnumber': pallet_no,
                'cont': cont,
                'seal': seal,
                'shipmentorder': shipment_val,
                'loosecarton': loose,
                'user': session.get('user')
            })
            count += 1
            
        db.session.commit()
        return jsonify({'success': True, 'message': f'Đã import thành công {count} dòng.'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Lỗi: {str(e)}'})

@app.route('/api/delete_scan', methods=['POST'])
def delete_scan():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    job_type = data.get('job_type')
    pallet = data.get('pallet')
    sku = data.get('sku')
    quantity = data.get('quantity')
    
    if not job_type or not pallet or not sku:
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400
    
    # Kiểm tra xem pallet có bị khóa không
    is_locked = Load.query.filter_by(jobno_type=job_type, pallet_no=pallet).first()
    if is_locked:
        return jsonify({'success': False, 'message': 'Pallet đã bị khóa, không thể xóa hàng.'}), 400

    try:
        # Query tìm các item đã scan vào pallet này
        query = Scanfile.query.filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet == pallet,
            Scanfile.sku == sku
        )
        
        count = 0
        # Nếu có nhập số lượng cụ thể
        if quantity and str(quantity).strip().isdigit() and int(quantity) > 0:
            qty_limit = int(quantity)
            # Lấy danh sách ID cần reset (ưu tiên xóa những cái mới scan nhất - LIFO)
            items_to_reset = query.order_by(Scanfile.time_scan.desc()).limit(qty_limit).all()
            
            if not items_to_reset:
                return jsonify({'success': False, 'message': 'Không tìm thấy dữ liệu để xóa'}), 404
                
            ids = [item.id for item in items_to_reset]
            
            # Reset trạng thái về NULL (trở thành hàng chờ scan)
            Scanfile.query.filter(Scanfile.id.in_(ids)).update({
                'pallet': '',
                'pallet_type': '',
                'userscan': ''
               
            }, synchronize_session=False)
            count = len(ids)
        else:
            # Nếu không nhập số lượng -> Xóa HẾT SKU đó trong Pallet
            count = query.update({
                'pallet': '',
                'pallet_type': '',
                'userscan': ''
                
            }, synchronize_session=False)
        
        db.session.commit()
        return jsonify({'success': True, 'message': f'Đã xóa {count} thùng SKU {sku} khỏi Pallet {pallet}.'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/delete_sku', methods=['POST'])
def delete_sku():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    sku = data.get('sku')
    job_type = data.get('job_type')
    
    if not sku or not job_type:
        return jsonify({'success': False, 'message': 'Thiếu SKU hoặc Job Type'}), 400
    
    try:
        count = Scanfile.query.filter(
            Scanfile.sku == sku,
            Scanfile.jobno_type == job_type,
            (Scanfile.pallet == None) | (Scanfile.pallet == ''),
            (Scanfile.pallet_type == None) | (Scanfile.pallet_type == ''),
            (Scanfile.finish == None) | (Scanfile.finish == '')
        ).delete(synchronize_session=False)
        
        db.session.commit()
        return jsonify({'success': True, 'message': f'Đã xóa {count} dòng SKU {sku}.'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Lỗi: {str(e)}'}), 500

@app.route('/api/check_barcode', methods=['POST'])
def check_barcode():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    barcode = data.get('barcode', '').strip()
    job_type = data.get('job_type')
    
    if not barcode or not job_type:
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400

    # 1. Xác định SKU từ Barcode (Logic giống api_scan)
    refix_val = barcode[-6:-1]
    sku = _refix_cache.get(refix_val)
    if not sku:
        master_item = MasterData.query.filter_by(refix=refix_val).first()
        if not master_item:
            return jsonify({'success': False, 'message': f'Refix {refix_val} không tồn tại'}), 404
        sku = master_item.sku
        _refix_cache[refix_val] = sku

    # 2. Đếm số lượng item của SKU này đang chờ (chưa có pallet) trong Job Type này
    count = Scanfile.query.filter(
        Scanfile.sku == sku,
        Scanfile.jobno_type == job_type,
        (Scanfile.pallet == '') | (Scanfile.pallet == None)
    ).count()

    # Lấy thông tin trọng lượng
    # Nếu master_item chưa có (do lấy SKU từ cache), cần query lại
    master_item = MasterData.query.filter_by(sku=sku).first()
    weight = master_item.weight if master_item and master_item.weight else 0

    return jsonify({'success': True, 'sku': sku, 'count': count, 'weight': weight})

@app.route('/api/bulk_update', methods=['POST'])
def bulk_update():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    sku = data.get('sku')
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')
    pallet_type = data.get('pallet_type')
    quantity = data.get('quantity')

    if not all([sku, job_type, pallet_no, quantity]):
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400

    # Kiểm tra khóa Pallet
    is_locked = Load.query.filter_by(jobno_type=job_type, pallet_no=pallet_no).first()
    if is_locked:
        return jsonify({'success': False, 'message': f'Pallet {pallet_no} đã bị khóa. Không thể thêm hàng.'}), 400

    try:
        qty = int(quantity)
        if qty <= 0: return jsonify({'success': False, 'message': 'Số lượng phải lớn hơn 0'}), 400

        # Lấy danh sách ID cần update (Lấy những dòng chưa có pallet)
        items_to_update = db.session.query(Scanfile.id).filter(
            Scanfile.sku == sku,
            Scanfile.jobno_type == job_type,
            (Scanfile.pallet == '') | (Scanfile.pallet == None)
        ).limit(qty).all()

        ids = [item.id for item in items_to_update]
        
        if not ids:
            return jsonify({'success': False, 'message': 'Không còn hàng chờ cho SKU này'}), 400

        Scanfile.query.filter(Scanfile.id.in_(ids)).update({
            'pallet': pallet_no,
            'pallet_type': pallet_type,
            'userscan': session.get('user'),
            'time_scan': datetime.now()
        }, synchronize_session=False)

        db.session.commit()
        return jsonify({'success': True, 'message': f'Đã cập nhật {len(ids)} thùng vào Pallet {pallet_no}', 'sku': sku})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

if __name__ == '__main__':
    # --- KIỂM TRA KẾT NỐI KHI KHỞI ĐỘNG ---
    print("--- Đang khởi động ứng dụng và kiểm tra kết nối Database... ---")
    with app.app_context():
        try:
            db.session.execute(text('SELECT 1'))
            print("✅ KẾT NỐI DATABASE THÀNH CÔNG!")
        except Exception as e:
            print("❌ KẾT NỐI DATABASE THẤT BẠI!")
            print(f"Chi tiết lỗi: {e}")
            # Có thể thêm exit(1) nếu muốn dừng app khi lỗi DB
    # --------------------------------------

    # Xử lý Port
    port_str = os.environ.get("PORT", "5000")
    try:
        port = int(port_str)
    except ValueError:
        port = 5000
        print(f"Cảnh báo: PORT không hợp lệ, chuyển về mặc định {port}")
    
    app.run(host='0.0.0.0', port=port)

@app.route('/api/get_logs')
def get_logs():
    if 'user' not in session: return jsonify({'success': False}), 401
    try:
        logs = Log.query.order_by(Log.created_at.desc()).limit(20).all()
        log_list = [{'id': l.id, 'username': l.username, 'message': l.message, 'created_at': l.created_at.strftime('%H:%M:%S %d/%m'), 'is_read': l.is_read} for l in logs]
        return jsonify({'success': True, 'logs': log_list})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/mark_read', methods=['POST'])
def mark_read():
    if 'user' not in session: return jsonify({'success': False}), 401
    data = request.json
    try:
        log = Log.query.get(data.get('id'))
        if log:
            log.is_read = True
            db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/mark_all_read', methods=['POST'])
def mark_all_read():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    try:
        # Cập nhật tất cả các log chưa đọc thành đã đọc
        Log.query.filter_by(is_read=False).update({'is_read': True})
        db.session.commit()
        return jsonify({'success': True, 'message': 'Tất cả đã được đánh dấu đã đọc.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/unread_count')
def unread_count():
    if 'user' not in session: return jsonify({'success': False, 'count': 0}), 401
    try:
        count = Log.query.filter_by(is_read=False).count()
        return jsonify({'success': True, 'count': count})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e), 'count': 0}), 500
