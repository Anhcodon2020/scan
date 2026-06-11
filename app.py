from flask import Flask, jsonify, render_template, request, redirect, url_for, session, Response
from sqlalchemy import text, func, case, extract
import os
import csv
import io
from datetime import datetime, timedelta, time
import pandas as pd
from dotenv import load_dotenv

# Nạp biến môi trường từ file .env
load_dotenv()

from config import Config
from extensions import db
from models.users import User
from models.scanfile import Scanfile
from models.masterdata import MasterData
from models.load import Load
from models.log import Log
from models.employees import Employee
from models.inbound import Inbound
from models.labor_assignment import LaborAssignment
from models.location import Location
from models.invetory_whs import InventoryWhs

app = Flask(__name__)
app.config.from_object(Config)

# Khởi tạo database
db.init_app(app)

# Cache cục bộ cho MasterData để giảm query DB khi scan liên tục
_refix_cache = {}

# --- Helper Functions: Xử lý Date/Time an toàn ---
def _parse_date(value):
    """Chuyển chuỗi thành date object, xử lý nhiều định dạng và chuỗi có giờ."""
    if not value:
        return None
    try:
        value_str = str(value).strip()
        if not value_str:
            return None
            
        # Nếu value đã là đối tượng date hoặc datetime (VD: từ pandas hoặc db)
        if isinstance(value, datetime):
            return value.date()
        if hasattr(value, 'date') and callable(value.date):
             return value.date()

        # Nếu chuỗi có cả giờ (vd: "2023-10-10 08:00:00" hoặc "2023-10-10T08:00:00"), cắt lấy phần ngày
        if ' ' in value_str:
            value_str = value_str.split(' ')[0]
        elif 'T' in value_str:
            value_str = value_str.split('T')[0]

        # Các định dạng ngày cần thử
        formats_to_try = [
            '%Y-%m-%d',  # 2023-10-25
            '%d/%m/%Y',  # 25/10/2023
            '%d-%m-%Y',  # 25-10-2023
            '%Y/%m/%d'   # 2023/10/25
        ]
        for fmt in formats_to_try:
            try:
                return datetime.strptime(value_str, fmt).date()
            except ValueError:
                continue
        
        return None
    except (ValueError, TypeError):
        return None

def _parse_time(value):
    """Chuyển chuỗi thành time object, xử lý nhiều định dạng, ISO, và chuỗi có ngày."""
    if not value:
        return None
    
    # Kiểm tra nếu value đã là đối tượng time hoặc datetime hợp lệ
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()

    try:
        value_str = str(value).strip()
        if not value_str:
            return None

        # Xử lý các định dạng ISO phức tạp, tách lấy phần giờ
        if 'T' in value_str:
            # Tách phần sau 'T', bỏ timezone info (Z, +07:00, etc.) nhưng giữ lại mili giây
            time_part = value_str.split('T', 1)[1]
            value_str = time_part.split('Z', 1)[0].split('+', 1)[0]
        elif ' ' in value_str and ':' in value_str:
            # Tìm phần tử có chứa ':' trong chuỗi "YYYY-MM-DD HH:MM:SS"
            value_str = next((part for part in value_str.split(' ') if ':' in part), value_str)

        # Các định dạng giờ cần thử, từ chi tiết đến tổng quát
        formats_to_try = ['%H:%M:%S.%f', '%H:%M:%S', '%H:%M', '%I:%M:%S %p', '%I:%M %p']
        for fmt in formats_to_try:
            try:
                return datetime.strptime(value_str, fmt).time()
            except ValueError:
                continue
        return None
    except (ValueError, TypeError, IndexError):
        return None
# -------------------------------------------------

@app.route('/')
def index():
    if 'user' not in session:
       return redirect(url_for('login'))

    labor_assignments = LaborAssignment.query.all()

    return render_template('home.html', labor_assignments=labor_assignments)

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

            # Tự động xóa log cũ hơn 30 ngày (giúp database không bị đầy qua các tháng)
            try:
                expiration_date = datetime.now() - timedelta(days=30)
                Log.query.filter(Log.created_at < expiration_date).delete()
                db.session.commit()
            except Exception:
                pass # Bỏ qua lỗi để không ảnh hưởng trải nghiệm đăng nhập

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
        job_types = [r[0] for r in db.session.query(Scanfile.jobno_type).filter(
            (Scanfile.pallet == '') | (Scanfile.pallet == None)
        ).distinct().all() if r[0]]
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
        
        # Thống kê số lượng SKU này trong pallet hiện tại
        sku_in_pallet = Scanfile.query.filter(
            Scanfile.jobno_type == job_type,
            Scanfile.sku == sku,
            Scanfile.pallet == pallet_no
        ).count()

        return jsonify({
            'success': True, 
            'message': 'Scan thành công', 
            'sku': sku, 
            'sku_scanned': sku_scanned, 
            'sku_total': sku_total,
            'sku_in_pallet': sku_in_pallet
        })

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

@app.route('/api/get_job_details', methods=['POST'])
def get_job_details():
    if 'user' not in session: return jsonify({'success': False}), 401
    data = request.json
    job_type = data.get('job_type')
    
    if not job_type: return jsonify({'success': False, 'message': 'Thiếu Job Type'}), 400

    try:
        # Lấy tổng số lượng theo SKU
        total_query = db.session.query(
            Scanfile.sku,
            func.count(Scanfile.id)
        ).filter(
            Scanfile.jobno_type == job_type
        ).group_by(Scanfile.sku).all()
        
        # Lấy số lượng đã scan (có pallet)
        scanned_query = db.session.query(
            Scanfile.sku,
            func.count(Scanfile.id)
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).group_by(Scanfile.sku).all()
        
        scanned_map = {r[0]: r[1] for r in scanned_query}
        
        details = []
        for sku, total in total_query:
            scanned = scanned_map.get(sku, 0)
            details.append({
                'sku': sku,
                'total': total,
                'scanned': scanned,
                'remain': total - scanned
            })
            
        # Sắp xếp: Ưu tiên SKU còn hàng (remain > 0) lên đầu
        details.sort(key=lambda x: x['remain'], reverse=True)

        return jsonify({'success': True, 'details': details})
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
        
        # Tính thêm tổng số lượng của từng SKU trong toàn bộ Job
        final_skus = []
        for item in skus:
            total_job = db.session.query(func.count(Scanfile.id)).filter(
                Scanfile.jobno_type == job_type,
                Scanfile.sku == item['sku']
            ).scalar() or 0
            final_skus.append({'sku': item['sku'], 'qty': item['qty'], 'total_job': total_job})

        pallet_count = sum(item['qty'] for item in final_skus)
        
        return jsonify({'success': True, 'pallet_count': pallet_count, 'skus': final_skus, 'is_locked': is_locked})
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
            {'finish': None, 'jobscan': ''}, 
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
    
    # Lấy tham số lọc từ URL
    selected_job = request.args.get('job_no', '')

    # Lấy danh sách Job No để hiển thị trong Combobox
    job_list = [r[0] for r in db.session.query(Scanfile.jobno).distinct().order_by(Scanfile.jobno.desc()).all() if r[0]]

    # 1. Thống kê Pallet đã scan (Group by Job Type, Pallet Type)
    # Lấy danh sách các pallet unique (đã có pallet_no)
    query = db.session.query(
        Scanfile.jobno,
        Scanfile.jobno_type,
        Scanfile.pallet,
        Scanfile.pallet_type,
        func.count(Scanfile.id),
        func.max(Scanfile.confirm)
    ).filter(
        Scanfile.pallet != '',
        Scanfile.pallet != None
    )

    # Áp dụng bộ lọc nếu có
    if selected_job:
        query = query.filter(Scanfile.jobno == selected_job)

    pallets = query.group_by(
        Scanfile.jobno,
        Scanfile.jobno_type,
        Scanfile.pallet,
        Scanfile.pallet_type
    ).all()

    stats_data = {}
    grand_total = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0, 'total_box': 0, 'total_cbm': 0, 'scanned_cbm': 0, 'remain_cbm': 0, 'total_weight': 0, 'total_pallet_type_cbm': 0, 'userscan_names': []}

    for job_no, job_type, pallet_no, p_type, sscc_count, confirm in pallets:
        # Key: (Job No, Job Type) - Đồng bộ với các phần tính toán CBM và SKU bên dưới
        key = (job_no, job_type) 
        
        if key not in stats_data:
            stats_data[key] = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0, 'total_box': 0, 'confirm': 'N', 'total_cbm': 0, 'scanned_cbm': 0, 'remain_cbm': 0, 'weight': 0, 'pallet_type_cbm': 0}
        
        p_type_str = str(p_type) if p_type else ''
        
        # Nếu là loose hoặc loosecarton thì tính theo số lượng SSCC, ngược lại tính là 1 pallet
        increment = sscc_count if 'loose' in p_type_str.lower() else 1
        
        curr_weight = 0
        curr_pallet_cbm = 0

        if '1.2' in p_type_str:
            stats_data[key]['1.2'] += increment
            grand_total['1.2'] += increment
            curr_weight = increment * 15.5
            curr_pallet_cbm = increment * 0.1404
        elif '1.6' in p_type_str:
            stats_data[key]['1.6'] += increment
            grand_total['1.6'] += increment
            curr_weight = increment * 20.5
            curr_pallet_cbm = increment * 0.1872
        elif '1.9' in p_type_str:
            stats_data[key]['1.9'] += increment
            grand_total['1.9'] += increment
            curr_weight = increment * 22
            curr_pallet_cbm = increment * 0.2223
        else:
            stats_data[key]['loose'] += increment
            grand_total['loose'] += increment
            
        stats_data[key]['total'] += increment
        grand_total['total'] += increment
        stats_data[key]['total_box'] += sscc_count
        grand_total['total_box'] += sscc_count

        stats_data[key]['pallet_type_cbm'] = round(stats_data[key]['pallet_type_cbm'] + curr_pallet_cbm, 4)
        grand_total['total_pallet_type_cbm'] = round(grand_total['total_pallet_type_cbm'] + curr_pallet_cbm, 4)
        
        # Cộng dồn Weight pallet
        stats_data[key]['weight'] += curr_weight
        grand_total['total_weight'] += curr_weight

        if confirm == 'Y':
            stats_data[key]['confirm'] = 'Y'

    # 2. Thống kê hàng tồn (Chưa scan)
    remain_q = db.session.query(
        Scanfile.jobno,
        Scanfile.jobno_type,
        func.count(Scanfile.id)
    ).filter(
        (Scanfile.pallet == '') | (Scanfile.pallet == None)
    )

    if selected_job:
        remain_q = remain_q.filter(Scanfile.jobno == selected_job)

    remain_query = remain_q.group_by(Scanfile.jobno, Scanfile.jobno_type).all()

    remain_stats = {}
    grand_total_remain_sscc = 0
    for job_no, job_type, count in remain_query:
        key = (job_no, job_type)
        remain_stats[key] = count
        grand_total_remain_sscc += count
        
        # [FIX] Đảm bảo Job có hàng tồn nhưng chưa scan pallet nào vẫn hiện lên bảng
        if key not in stats_data:
            stats_data[key] = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0, 'total_box': 0, 'confirm': 'N', 'total_cbm': 0, 'scanned_cbm': 0, 'remain_cbm': 0, 'weight': 0, 'pallet_type_cbm': 0}

    # 3. Mapping SKU qua MasterData để tính CBM theo Job Type
    master_cbm_sq = db.session.query(
        MasterData.sku.label('sku'),
        func.max(MasterData.cbm).label('cbm')
    ).group_by(MasterData.sku).subquery()
    sku_cbm = func.coalesce(master_cbm_sq.c.cbm, 0)

    grand_total['total_cbm'] = 0
    grand_total['scanned_cbm'] = 0
    grand_total['remain_cbm'] = 0
    cbm_q = db.session.query(
        Scanfile.jobno,
        Scanfile.jobno_type,
        func.sum(sku_cbm),
        func.sum(case((Scanfile.pallet != '', sku_cbm), else_=0)),
        func.sum(case(((Scanfile.pallet == '') | (Scanfile.pallet == None), sku_cbm), else_=0))
    ).outerjoin(
        master_cbm_sq,
        Scanfile.sku == master_cbm_sq.c.sku
    )

    if selected_job:
        cbm_q = cbm_q.filter(Scanfile.jobno == selected_job)

    cbm_rows = cbm_q.group_by(Scanfile.jobno, Scanfile.jobno_type).all()

    for job_no, job_type, total_cbm, scanned_cbm, remain_cbm in cbm_rows:
        key = (job_no, job_type)
        if key not in stats_data:
            stats_data[key] = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0, 'total_box': 0, 'confirm': 'N', 'total_cbm': 0, 'scanned_cbm': 0, 'remain_cbm': 0, 'weight': 0, 'pallet_type_cbm': 0}

        total_cbm = round(float(total_cbm or 0), 3)
        scanned_cbm = round(float(scanned_cbm or 0), 3)
        remain_cbm = round(float(remain_cbm or 0), 3)

        stats_data[key]['total_cbm'] = total_cbm
        stats_data[key]['scanned_cbm'] = scanned_cbm
        stats_data[key]['remain_cbm'] = remain_cbm
        grand_total['total_cbm'] += total_cbm
        grand_total['scanned_cbm'] += scanned_cbm
        grand_total['remain_cbm'] += remain_cbm

    grand_total['total_cbm'] = round(grand_total['total_cbm'], 3)
    grand_total['scanned_cbm'] = round(grand_total['scanned_cbm'], 3)
    grand_total['remain_cbm'] = round(grand_total['remain_cbm'], 3)

    if selected_job:
        grand_total['userscan_names'] = [
            r[0] for r in db.session.query(Scanfile.userscan)
            .filter(
                Scanfile.jobno == selected_job,
                Scanfile.userscan.isnot(None),
                Scanfile.userscan != ''
            )
            .distinct()
            .order_by(Scanfile.userscan)
            .all()
            if r[0]
        ]

    return render_template('statistics.html', stats=stats_data, grand_total=grand_total, remain_stats=remain_stats, 
                           jobs=job_list, selected_job=selected_job)

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
    if session.get('role') != 'admin': return redirect(url_for('index'))
    return render_template('users.html')

@app.route('/api/users/list')
def get_users_list():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Không có quyền'}), 401
    try:
        users = User.query.all()
        data = [{'id': u.id, 'username': u.username, 'role': u.role} for u in users]
        return jsonify({'success': True, 'users': data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/users/save', methods=['POST'])
def save_user():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Không có quyền'}), 401
    
    data = request.json
    uid = data.get('id')
    username = data.get('username')
    password = data.get('password')
    role = data.get('role')
    
    if not username:
        return jsonify({'success': False, 'message': 'Tên đăng nhập là bắt buộc'}), 400
        
    try:
        if uid: # Chế độ Sửa
            user = User.query.get(uid)
            if not user: return jsonify({'success': False, 'message': 'Người dùng không tồn tại'}), 404
            user.username = username
            user.role = role
            if password: # Chỉ cập nhật mật khẩu nếu có nhập mới
                user.password = password
        else: # Chế độ Thêm mới
            if not password:
                return jsonify({'success': False, 'message': 'Mật khẩu là bắt buộc khi tạo tài khoản mới'}), 400
            new_user = User(username=username, password=password, role=role)
            db.session.add(new_user)
            
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/users/delete', methods=['POST'])
def delete_user():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Không có quyền'}), 401
    
    data = request.json
    uid = data.get('id')
    try:
        user = User.query.get(uid)
        if user:
            if user.username == session.get('user'):
                return jsonify({'success': False, 'message': 'Bạn không thể tự xóa chính mình'}), 400
            db.session.delete(user)
            db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/location')
def location_manage():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('location.html')

@app.route('/api/location/list')
def get_location_list():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        locations = Location.query.order_by(Location.loc_id.asc(), Location.id.asc()).all()
        data = [
            {
                'id': item.id,
                'loc_id': item.loc_id or '',
                'description': item.description or ''
            }
            for item in locations
        ]
        return jsonify({'success': True, 'locations': data, 'can_edit': session.get('role') == 'admin'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/location/save', methods=['POST'])
def save_location():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    data = request.json or {}
    location_id = data.get('id')
    loc_id = (data.get('loc_id') or '').strip()
    description = (data.get('description') or '').strip()

    if not loc_id:
        return jsonify({'success': False, 'message': 'Location ID is required'}), 400

    try:
        duplicate_query = Location.query.filter(Location.loc_id == loc_id)
        if location_id:
            duplicate_query = duplicate_query.filter(Location.id != location_id)
        if duplicate_query.first():
            return jsonify({'success': False, 'message': 'Location ID already exists'}), 400

        if location_id:
            item = Location.query.get(location_id)
            if not item:
                return jsonify({'success': False, 'message': 'Location not found'}), 404
            item.loc_id = loc_id
            item.description = description
        else:
            item = Location(loc_id=loc_id, description=description)
            db.session.add(item)

        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/location/delete', methods=['POST'])
def delete_location():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    data = request.json or {}
    location_id = data.get('id')

    try:
        item = Location.query.get(location_id)
        if item:
            db.session.delete(item)
            db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

def _can_edit_inventory_whs():
    return session.get('role') in ['admin', 'scanner']

@app.route('/invetory_whs')
def invetory_whs_manage():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('invetory_whs.html')

@app.route('/api/invetory_whs/list')
def get_invetory_whs_list():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        rows = (
            db.session.query(InventoryWhs, Location)
            .outerjoin(Location, InventoryWhs.loc_id == Location.loc_id)
            .order_by(InventoryWhs.id.desc())
            .all()
        )
        data = []
        for item, location in rows:
            data.append({
                'id': item.id,
                'loc_id': item.loc_id,
                'location_code': item.loc_id or '',
                'location_description': location.description if location else '',
                'sku': item.sku or '',
                'sub_loc': item.sub_loc if item.sub_loc is not None else 0,
                'qty': item.qty if item.qty is not None else 0
            })

        locations = Location.query.order_by(Location.loc_id.asc(), Location.id.asc()).all()
        location_options = [
            {
                'id': loc.id,
                'loc_id': loc.loc_id or '',
                'description': loc.description or ''
            }
            for loc in locations
        ]

        return jsonify({
            'success': True,
            'items': data,
            'locations': location_options,
            'can_edit': _can_edit_inventory_whs()
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/invetory_whs/save', methods=['POST'])
def save_invetory_whs():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if not _can_edit_inventory_whs():
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    data = request.json or {}
    item_id = data.get('id')
    loc_id = (data.get('loc_id') or '').strip()
    sku = (data.get('sku') or '').strip()
    sub_loc = data.get('sub_loc')
    qty = data.get('qty')

    if not loc_id:
        return jsonify({'success': False, 'message': 'Location is required'}), 400
    if not sku:
        return jsonify({'success': False, 'message': 'SKU is required'}), 400

    try:
        sub_loc = int(sub_loc or 0)
        qty = int(qty or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Sub location and quantity must be numbers'}), 400

    if not Location.query.filter_by(loc_id=loc_id).first():
        return jsonify({'success': False, 'message': 'Location not found'}), 404

    try:
        if item_id:
            item = InventoryWhs.query.get(item_id)
            if not item:
                return jsonify({'success': False, 'message': 'Inventory item not found'}), 404
        else:
            item = InventoryWhs()
            db.session.add(item)

        item.loc_id = loc_id
        item.sku = sku
        item.sub_loc = sub_loc
        item.qty = qty

        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/invetory_whs/delete', methods=['POST'])
def delete_invetory_whs():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if not _can_edit_inventory_whs():
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    data = request.json or {}
    item_id = data.get('id')

    try:
        item = InventoryWhs.query.get(item_id)
        if item:
            db.session.delete(item)
            db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/scan_sku_location')
def scan_sku_location():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('scan_sku_location.html')

@app.route('/api/scan_sku_location/options')
def get_scan_sku_location_options():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        locations = Location.query.order_by(Location.loc_id.asc(), Location.id.asc()).all()
        data = [
            {
                'id': loc.loc_id or '',
                'loc_id': loc.loc_id or '',
                'description': loc.description or ''
            }
            for loc in locations
        ]
        return jsonify({'success': True, 'locations': data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/scan_sku_location/update', methods=['POST'])
def update_scan_sku_location():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.json or {}
    loc_id = (data.get('loc_id') or '').strip()
    barcode = (data.get('barcode') or '').strip()
    sub_location = data.get('sub_location', data.get('sub_loc'))
    qty = data.get('qty', data.get('carton'))

    if not loc_id:
        return jsonify({'success': False, 'message': 'Location is required'}), 400
    if not barcode:
        return jsonify({'success': False, 'message': 'Barcode is required'}), 400
    if sub_location in (None, ''):
        return jsonify({'success': False, 'message': 'Sub location is required'}), 400

    try:
        sub_location = int(sub_location)
        qty = int(qty or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Sub location and quantity must be numbers'}), 400

    if sub_location < 1 or sub_location > 10:
        return jsonify({'success': False, 'message': 'Sub location must be between 1 and 10'}), 400
    if qty <= 0:
        return jsonify({'success': False, 'message': 'Quantity must be greater than 0'}), 400
    if len(barcode) < 6:
        return jsonify({'success': False, 'message': 'Barcode must have at least 6 characters'}), 400

    location = Location.query.filter_by(loc_id=loc_id).first()
    if not location:
        return jsonify({'success': False, 'message': 'Location not found'}), 404

    refix_val = barcode[-6:-1]
    sku = _refix_cache.get(refix_val)
    if not sku:
        master_item = MasterData.query.filter_by(refix=refix_val).first()
        if not master_item:
            return jsonify({'success': False, 'message': f'Refix {refix_val} not found in masterdata'}), 404
        sku = master_item.sku
        _refix_cache[refix_val] = sku

    try:
        item = InventoryWhs.query.filter_by(loc_id=loc_id, sku=sku, sub_loc=sub_location).first()
        if item:
            item.qty = (item.qty or 0) + qty
        else:
            item = InventoryWhs(loc_id=loc_id, sku=sku, sub_loc=sub_location, qty=qty)
            db.session.add(item)

        db.session.commit()
        updated_at = datetime.now()

        return jsonify({
            'success': True,
            'message': 'Updated inventory successfully',
            'id': item.id,
            'refix': refix_val,
            'sku': sku,
            'loc_id': location.loc_id,
            'location_description': location.description or '',
            'sub_loc': item.sub_loc,
            'qty_added': qty,
            'qty_total': item.qty,
            'carton_added': qty,
            'carton_total': item.qty,
            'time_update': updated_at.strftime('%H:%M:%S %d/%m/%Y')
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/findsku')
def findsku_page():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('findsku.html')

@app.route('/api/findsku/search', methods=['POST'])
def findsku_search():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.json or {}
    sku = (data.get('sku') or '').strip()
    if not sku:
        return jsonify({'success': False, 'message': 'SKU is required'}), 400

    try:
        rows = (
            db.session.query(
                InventoryWhs.loc_id,
                InventoryWhs.sku,
                InventoryWhs.sub_loc,
                func.sum(InventoryWhs.qty).label('qty'),
                Location.loc_id.label('location_code'),
                Location.description.label('location_description')
            )
            .outerjoin(Location, InventoryWhs.loc_id == Location.loc_id)
            .filter(InventoryWhs.sku.contains(sku, autoescape=True))
            .group_by(
                InventoryWhs.loc_id,
                InventoryWhs.sku,
                InventoryWhs.sub_loc,
                Location.loc_id,
                Location.description
            )
            .order_by(InventoryWhs.loc_id.asc(), InventoryWhs.sub_loc.asc())
            .all()
        )

        results = [
            {
                'loc_id': row.loc_id,
                'location_code': row.location_code or row.loc_id or '',
                'location_description': row.location_description or '',
                'sku': row.sku or '',
                'sub_loc': row.sub_loc if row.sub_loc is not None else 0,
                'qty': int(row.qty or 0),
                'pallet': row.sub_loc if row.sub_loc is not None else 0,
                'carton': int(row.qty or 0),
                'time_update': ''
            }
            for row in rows
        ]

        return jsonify({
            'success': True,
            'sku': sku,
            'results': results,
            'total_qty': sum(item['qty'] for item in results),
            'total_carton': sum(item['qty'] for item in results)
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

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
            func.max(Scanfile.jobscan).label('jobnoscan'),
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
            func.max(Scanfile.st_zip).label('st_zip'),
            func.max(Scanfile.finish).label('finish')
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
                    'jobnoscan': row.jobnoscan,
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
                    'has_small_label': is_small,
                    'is_completed': (row.finish == 'COMPLETED')
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
        # Chỉ lấy dữ liệu nếu Pallet đã hoàn thành (COMPLETED)
        rows = Scanfile.query.filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet == pallet_no,
            Scanfile.finish == 'COMPLETED'
        ).all()
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
            Scanfile.pallet != None,
            Scanfile.finish == 'COMPLETED'
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

        data_list = []
        for row in results:
            master_item = MasterData.query.filter_by(sku=row.sku).first()
            sku_weight = master_item.weight if master_item and master_item.weight else 0
            total_weight = sku_weight * row.sscc_count
            
            writer.writerow([row.pallet, job_type, row.pallet_type, row.sku, row.sscc_count, total_weight, row.tag_label, row.jobscan])
            
            data_list.append({
                'Pallet No': row.pallet,
                'Job Type': job_type,
                'Pallet Type': row.pallet_type,
                'SKU': row.sku,
                'Quantity': row.sscc_count,
                'Weight (Est)': total_weight,
                'Tag Label': row.tag_label,
                'Job Scan': row.jobscan
            })

        df = pd.DataFrame(data_list)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Pallet Data')
        
        output.seek(0)
        return Response(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment;filename=pallet_data_{job_type}.xlsx"})
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
        # Tìm container trong bảng outbound, sắp xếp theo ngày (dùng datercv hoặc datestuff nếu có)
        query = text("""
            SELECT DISTINCT container,
                COALESCE(datercv, datestuff) AS packing_date
            FROM outbound
            WHERE jobno = :jobno AND container IS NOT NULL AND container != ''
            ORDER BY packing_date ASC
        """)
        result = db.session.execute(query, {'jobno': jobno}).fetchall()
        
        containers = [{'container': r[0], 'packing_date': str(r[1]) if r[1] else ''} for r in result]
        return jsonify({'success': True, 'containers': containers})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# --- OUTBOUND EXPORT PAGE ---
@app.route('/outbound')
def outbound_page():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('outbound.html')

@app.route('/dashboarpallet')
@app.route('/dashboardpallet')
def dashboard_pallet_page():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('dashboarpallet.html')

@app.route('/api/outbound/jobnos')
def outbound_jobnos():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    try:
        query = text("""
            SELECT DISTINCT jobno
            FROM outbound
            WHERE (container IS NULL OR TRIM(container) = '')
            ORDER BY jobno
        """)
        rows = db.session.execute(query).fetchall()
        jobnos = [r[0] for r in rows if r[0]]
        return jsonify({'success': True, 'jobnos': jobnos})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/dashboard-pallet/jobnos')
def dashboard_pallet_jobnos():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    try:
        query = text("""
            SELECT DISTINCT jobno
            FROM outbound
            WHERE container IS NOT NULL AND TRIM(container) != ''
            ORDER BY jobno
        """)
        rows = db.session.execute(query).fetchall()
        jobnos = [r[0] for r in rows if r[0]]
        return jsonify({'success': True, 'jobnos': jobnos})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/dashboard-pallet/summary')
def dashboard_pallet_summary():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    jobno = request.args.get('jobno')
    if not jobno:
        return jsonify({'success': False, 'message': 'Thieu Job No'}), 400

    capacity_map = {'1.2': 3, '1m2': 3, '1.6': 4, '1m6': 4, '1.9': 4.75, '1m9': 4.75}
    try:
        query = text("""
            SELECT
                COALESCE(TRIM(kindpallet), '') AS kindpallet,
                COUNT(*) AS line_count,
                SUM(COALESCE(cbm, 0)) AS total_cbm
            FROM outbound
            WHERE jobno = :jobno
              AND container IS NOT NULL
              AND TRIM(container) != ''
            GROUP BY COALESCE(TRIM(kindpallet), '')
            ORDER BY COALESCE(TRIM(kindpallet), '')
        """)
        rows = db.session.execute(query, {'jobno': jobno}).fetchall()

        details = []
        grand_cbm = 0
        grand_pallet = 0
        unknown_cbm = 0

        for kindpallet, line_count, total_cbm in rows:
            kind = str(kindpallet or '').strip()
            cbm_value = round(float(total_cbm or 0), 3)
            divisor = capacity_map.get(kind)
            pallet_qty = int(-(-cbm_value // divisor)) if divisor and cbm_value > 0 else 0

            if not divisor:
                unknown_cbm = round(unknown_cbm + cbm_value, 3)
            grand_cbm = round(grand_cbm + cbm_value, 3)
            grand_pallet += pallet_qty

            details.append({
                'kindpallet': kind or 'Chua co loai',
                'line_count': int(line_count or 0),
                'total_cbm': cbm_value,
                'capacity_cbm': divisor or '',
                'pallet_qty': pallet_qty
            })

        return jsonify({
            'success': True,
            'jobno': jobno,
            'rows': details,
            'total_cbm': grand_cbm,
            'total_pallet': grand_pallet,
            'unknown_cbm': unknown_cbm
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/outbound/details')
def outbound_details():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    jobno = request.args.get('jobno')
    if not jobno:
        return jsonify({'success': False, 'message': 'Thiếu Job No'}), 400
    try:
        query = text("""
            SELECT *
            FROM outbound
            WHERE jobno = :jobno AND (container IS NULL OR TRIM(container) = '')
        """)
        rows = db.session.execute(query, {'jobno': jobno}).fetchall()

        def pick(mapping, candidates, default=''):
            for key in candidates:
                if key in mapping and mapping[key] is not None:
                    return mapping[key]
            return default

        data = []
        for row in rows:
            m = row._mapping
            data.append({
                'parentPO': pick(m, ['parentpo', 'parent_po', 'parentPO', 'ParentPO']),
                'childPO': pick(m, ['childpo', 'child_po', 'childPO', 'ChildPO']),
                'release_key': pick(m, ['release_key', 'relese_key', 'releasekey', 'ReleaseKey', 'Release_Key', 'rsl']),
                'sku': pick(m, ['sku', 'SKU']),
                'cbm': pick(m, ['cbm', 'CBM', 'cbm_pallet', 'cbmPallet'], default=''),
                'carton': pick(m, ['carton', 'Carton'], default='')
            })
        return jsonify({'success': True, 'rows': data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/outbound/export', methods=['GET'])
def outbound_export():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    jobno = request.args.get('jobno')
    if not jobno:
        return jsonify({'success': False, 'message': 'Thiếu Job No'}), 400
    try:
        query = text("""
            SELECT *
            FROM outbound
            WHERE jobno = :jobno AND (container IS NULL OR TRIM(container) = '')
        """)
        rows = db.session.execute(query, {'jobno': jobno}).fetchall()
        if not rows:
            return jsonify({'success': False, 'message': 'Không tìm thấy dữ liệu phù hợp'}), 404

        def pick(mapping, candidates, default=''):
            for key in candidates:
                if key in mapping and mapping[key] is not None:
                    return mapping[key]
            return default

        records = []
        for row in rows:
            m = row._mapping
            records.append({
                'ParentPO': pick(m, ['parentpo', 'parent_po', 'parentPO', 'ParentPO']),
                'ChildPO': pick(m, ['childpo', 'child_po', 'childPO', 'ChildPO']),
                'Release_Key': pick(m, ['release_key', 'relese_key', 'releasekey', 'ReleaseKey', 'Release_Key', 'rsl']),
                'SKU': pick(m, ['sku', 'SKU']),
                'CBM': pick(m, ['cbm', 'CBM', 'cbm_pallet', 'cbmPallet'], default=''),
                'Carton': pick(m, ['carton', 'Carton'], default='')
            })

        df = pd.DataFrame(records, columns=['ParentPO', 'ChildPO', 'Release_Key', 'SKU', 'CBM', 'Carton'])

        # Thêm dòng tổng
        def _safe_sum(key):
            total = 0
            for rec in records:
                val = rec.get(key, '')
                try:
                    total += float(val) if str(val).strip() != '' else 0
                except (TypeError, ValueError):
                    continue
            return total

        total_cbm = _safe_sum('CBM')
        total_carton = _safe_sum('Carton')
        df.loc[len(df)] = {
            'ParentPO': 'TOTAL',
            'ChildPO': '',
            'Release_Key': '',
            'SKU': '',
            'CBM': total_cbm,
            'Carton': total_carton
        }
        output = io.BytesIO()
        try:
            # Ưu tiên ghi Excel nếu có openpyxl
            import openpyxl  # noqa: F401
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Outbound')
            output.seek(0)
            filename = f"{jobno}.xlsx"
            return Response(
                output.getvalue(),
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition': f'attachment; filename={filename}'}
            )
        except ImportError:
            # Thử engine khác (xlsxwriter) nếu thiếu openpyxl
            try:
                import xlsxwriter  # noqa: F401
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, index=False, sheet_name='Outbound')
                output.seek(0)
                filename = f"{jobno}.xlsx"
                return Response(
                    output.getvalue(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename={filename}'}
                )
            except ImportError:
                return jsonify({
                    'success': False,
                    'message': 'Thiếu thư viện openpyxl hoặc xlsxwriter để xuất file Excel'
                }), 500
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

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
        if session.get('role') != 'admin':
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
                'userscan': '',
                'finish': None
            }, synchronize_session=False)
            count = len(ids)
        else:
            # Nếu không nhập số lượng -> Xóa HẾT SKU đó trong Pallet
            count = query.update({
                'pallet': '',
                'pallet_type': '',
                'userscan': '',
                'finish': None
            }, synchronize_session=False)
        
        db.session.commit()

        # Nếu Admin xóa hàng trong Pallet đã khóa -> Cập nhật lại số lượng trong bảng Load
        if is_locked and session.get('role') == 'admin':
            new_qty = Scanfile.query.filter(
                Scanfile.jobno_type == job_type,
                Scanfile.pallet == pallet
            ).count()
            
            if new_qty == 0:
                db.session.delete(is_locked) # Nếu xóa hết thì mở khóa luôn
            else:
                is_locked.quantity = new_qty # Cập nhật số lượng mới
            
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

@app.route('/api/get_pending_pallets', methods=['GET'])
def get_pending_pallets():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'success': False, 'message': 'Forbidden'}), 403

    try:
        # Lấy danh sách Pallet đã có hàng nhưng chưa hoàn thành (finish != 'COMPLETED')
        results = db.session.query(
            Scanfile.jobno_type,
            Scanfile.pallet,
            func.count(Scanfile.id).label('qty'),
            func.max(Scanfile.time_scan).label('last_scan')
        ).filter(
            Scanfile.pallet != '',
            Scanfile.pallet != None,
            (Scanfile.finish != 'COMPLETED') | (Scanfile.finish == None)
        ).group_by(
            Scanfile.jobno_type, 
            Scanfile.pallet
        ).order_by(Scanfile.jobno_type, Scanfile.pallet).all()

        data = [{
            'job_type': r.jobno_type,
            'pallet': r.pallet,
            'qty': r.qty,
            'last_scan': r.last_scan.strftime('%H:%M %d/%m') if r.last_scan else ''
        } for r in results]

        return jsonify({'success': True, 'pallets': data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/scan_dashboard_data')
def get_scan_dashboard_data():
    if 'user' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    try:
        # Query này nhóm theo jobno_type và tính toán tổng số, số lượng đã quét, và thời gian quét cuối cùng trong một lần.
        results = db.session.query(
            Scanfile.jobno_type,
            func.count(Scanfile.id).label('total'),
            func.sum(case(
                ((Scanfile.pallet != '') & (Scanfile.pallet.isnot(None)), 1),
                else_=0
            )).label('scanned'),
            func.max(case(
                ((Scanfile.pallet != '') & (Scanfile.pallet.isnot(None)), Scanfile.time_scan)
            )).label('last_scan')
        ).filter(
            Scanfile.jobno_type.isnot(None),
            Scanfile.jobno_type != ''
        ).group_by(Scanfile.jobno_type).order_by(Scanfile.jobno_type).all()
        
        # Lấy danh sách user đã scan cho từng Job Type
        active_users = db.session.query(
            Scanfile.jobno_type,
            Scanfile.userscan
        ).filter(
            Scanfile.jobno_type.isnot(None),
            Scanfile.jobno_type != '',
            Scanfile.pallet != '',
            Scanfile.pallet.isnot(None),
            Scanfile.userscan != '',
            Scanfile.userscan.isnot(None)
        ).distinct().all()

        users_map = {}
        for j_type, u_name in active_users:
            if j_type not in users_map:
                users_map[j_type] = []
            users_map[j_type].append(u_name)
        
        # Thống kê số lượng Pallet đã hoàn thành (COMPLETED)
        completed_pallets_query = db.session.query(
            Scanfile.jobno_type,
            func.count(func.distinct(Scanfile.pallet))
        ).filter(
            Scanfile.finish == 'COMPLETED',
            Scanfile.jobno_type.isnot(None),
            Scanfile.jobno_type != ''
        ).group_by(Scanfile.jobno_type).all()
        
        completed_map = {r[0]: r[1] for r in completed_pallets_query}
        
        # Tính tổng số lượng scan trong ngày hôm nay
        today = datetime.now().date()
        total_today = db.session.query(func.count(Scanfile.id)).filter(
            Scanfile.pallet != '',
            Scanfile.pallet != None,
            Scanfile.time_scan >= today
        ).scalar() or 0

        stats = []
        for job_type, total, scanned, last_scan in results:
            # Chuyển đổi scanned về int (vì sum có thể trả về Decimal)
            scanned_count = int(scanned) if scanned is not None else 0
            
            # Xử lý last_scan an toàn (tránh lỗi nếu là string hoặc None)
            last_scan_str = 'N/A'
            if last_scan:
                if hasattr(last_scan, 'strftime'):
                    last_scan_str = last_scan.strftime('%H:%M:%S %d/%m/%Y')
                else:
                    last_scan_str = str(last_scan)

            stats.append({
                'jobno_type': job_type,
                'total': total,
                'scanned': scanned_count,
                'remaining': total - scanned_count,
                'progress': (scanned_count / total * 100) if total > 0 else 0,
                'last_scan': last_scan_str,
                'users': users_map.get(job_type, []),
                'completed_pallets': completed_map.get(job_type, 0)
            })

        return jsonify({'success': True, 'stats': stats, 'total_today': total_today})
    except Exception as e:
        app.logger.error(f"Error in scan_dashboard_data: {e}", exc_info=True) # Log lỗi ra console server để debug
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/dashboard')
def dashboard():
    if 'user' not in session: return redirect(url_for('login'))
    # Cho phép Admin, Printer, Docs xem dashboard
    if session.get('role') not in ['admin', 'printer', 'Docs']:
         return redirect(url_for('index'))
    return render_template('dashboard.html')

@app.route('/scan_dashboard')
def scan_dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('scan_dashboard.html')

# --- QUẢN LÝ NHÂN VIÊN (EMPLOYEES) ---
@app.route('/employees')
def employees_manage():
    if 'user' not in session: return redirect(url_for('login'))
    # Chỉ cho phép Admin truy cập (hoặc tùy chỉnh role khác nếu cần)
    if session.get('role') != 'admin': return redirect(url_for('index'))
    return render_template('employees.html')

@app.route('/api/employees/list')
def get_employees_list():
    if 'user' not in session: return jsonify({'success': False}), 401
    try:
        employees = Employee.query.order_by(Employee.source.desc()).all()
        
        stats = {'KLN': 0, 'PST': 0, 'PNT': 0, 'TOTAL': 0}
        data = []
        for e in employees:
            data.append({
                'id': e.id,
                'hovaten': e.hovaten,
                'source': e.source,
                'active': e.active,
                'created_at': e.created_at.strftime('%H:%M %d/%m/%Y') if e.created_at else ''
            })
            
            if e.active == 1:
                stats['TOTAL'] += 1
                src = (e.source or '').upper()
                if 'KLN' in src: stats['KLN'] += 1
                elif 'PST' in src: stats['PST'] += 1
                elif 'PNT' in src: stats['PNT'] += 1

        return jsonify({'success': True, 'employees': data, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/employees/save', methods=['POST'])
def save_employee():
    if 'user' not in session: return jsonify({'success': False}), 401
    if session.get('role') != 'admin': return jsonify({'success': False, 'message': 'Forbidden'}), 403
    
    data = request.json
    emp_id = data.get('id')
    hovaten = data.get('hovaten')
    source = data.get('source')
    active = data.get('active')
    
    if not hovaten:
        return jsonify({'success': False, 'message': 'Họ và tên là bắt buộc'}), 400
        
    try:
        if emp_id: # Edit
            emp = Employee.query.get(emp_id)
            if not emp: return jsonify({'success': False, 'message': 'Nhân viên không tồn tại'}), 404
            
            emp.hovaten = hovaten
            emp.source = source
            emp.active = int(active) if active is not None else 1
        else: # Add new
            new_emp = Employee(
                hovaten=hovaten,
                source=source,
                active=int(active) if active is not None else 1
            )
            db.session.add(new_emp)
            
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/employees/delete', methods=['POST'])
def delete_employee():
    if 'user' not in session: return jsonify({'success': False}), 401
    if session.get('role') != 'admin': return jsonify({'success': False, 'message': 'Forbidden'}), 403
    
    data = request.json
    emp_id = data.get('id')
    
    try:
        emp = Employee.query.get(emp_id)
        if emp:
            db.session.delete(emp)
            db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

# --- QUẢN LÝ SỬA PALLET TYPE (ADMIN/SCANNER) ---
def can_edit_pallet_type():
    return session.get('role') in ['admin', 'scanner']

@app.route('/edit_pallet_type')
def edit_pallet_type():
    if 'user' not in session: return redirect(url_for('login'))
    if not can_edit_pallet_type(): return redirect(url_for('index'))
    
    # Lấy danh sách Job Type có trong hệ thống
    job_types = []
    try:
        job_types = [r[0] for r in db.session.query(Scanfile.jobno_type).distinct().all() if r[0]]
    except Exception:
        pass
        
    return render_template('edit_pallet_type.html', job_types=job_types)

@app.route('/api/admin/get_pallets_for_edit', methods=['POST'])
def get_pallets_for_edit():
    if 'user' not in session or not can_edit_pallet_type():
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    job_type = data.get('job_type')
    
    try:
        # Lấy danh sách pallet và type hiện tại
        results = db.session.query(
            Scanfile.pallet,
            func.max(Scanfile.pallet_type)
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).group_by(Scanfile.pallet).all()
        
        pallets = [{'no': r[0], 'type': r[1] if r[1] else ''} for r in results]
        
        # Sắp xếp pallet theo số (nếu là số)
        pallets.sort(key=lambda x: int(x['no']) if x['no'].isdigit() else x['no'])
        
        return jsonify({'success': True, 'pallets': pallets})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/update_pallet_type', methods=['POST'])
def update_pallet_type():
    if 'user' not in session or not can_edit_pallet_type():
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
        
    data = request.json
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')
    new_type = data.get('new_type')
    
    if not all([job_type, pallet_no, new_type]):
        return jsonify({'success': False, 'message': 'Thiếu thông tin'}), 400
        
    try:
        # Cập nhật trong bảng Scanfile
        Scanfile.query.filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet == pallet_no
        ).update({'pallet_type': new_type}, synchronize_session=False)
        
        # Cập nhật trong bảng Load (nếu đã khóa pallet)
        Load.query.filter(
            Load.jobno_type == job_type,
            Load.pallet_no == pallet_no
        ).update({'pallet_type': new_type}, synchronize_session=False)
        
        db.session.commit()
        
        # Ghi log
        db.session.add(Log(
            username=session.get('user'),
            action='EDIT_PALLET_TYPE',
            message=f"Đã sửa loại Pallet {pallet_no} (Job: {job_type}) thành {new_type}",
            is_read=False
        ))
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Cập nhật thành công'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/confirm_job_report', methods=['POST'])
def confirm_job_report():
    if 'user' not in session: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'success': False, 'message': 'Forbidden'}), 403
    
    data = request.json
    job_no = data.get('job_no')
    job_type = data.get('job_type')
    
    try:
        # Cập nhật cột confirm thành "Y" cho toàn bộ Job này
        Scanfile.query.filter(
            Scanfile.jobno == job_no,
            Scanfile.jobno_type == job_type
        ).update({'confirm': 'Y'}, synchronize_session=False)

        # Ghi log xác nhận
        db.session.add(Log(
            username=session.get('user'),
            action='CONFIRM_REPORT',
            message=f"Admin đã xác nhận báo cáo cho Job: {job_no} ({job_type})",
            is_read=False
        ))
        db.session.commit()
        return jsonify({'success': True, 'message': f'Đã xác nhận Job {job_no}'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


# --- QUẢN LÝ INBOUND (ADMIN ONLY) ---
@app.route('/inbound')
def inbound_page():
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('index'))
    return render_template('inbound.html')

@app.route('/api/inbound/list')
def get_inbound_list():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    try:
        inbounds = Inbound.query.order_by(Inbound.datercv.desc()).all()
        data = [{
            'id': i.id,
            'MANCC': i.MANCC,
            'po': i.po,
            'sku': i.sku,
            'carton': i.carton,
            'contxe': i.contxe,
            'datercv': i.datercv.strftime('%Y-%m-%d') if i.datercv else '',
            'cbm': i.cbm,
            'labour': i.labour,
            'PackinglistNo': i.PackinglistNo
        } for i in inbounds]
        return jsonify({'success': True, 'inbounds': data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/inbound/save', methods=['POST'])
def save_inbound():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    inbound_id = data.get('id')
    
    if not data.get('po') or not data.get('datercv'):
        return jsonify({'success': False, 'message': 'PO và Ngày nhận là bắt buộc'}), 400
        
    try:
        # datercv_str = data.get('datercv') # Đã thay thế bằng _parse_date bên dưới

        if inbound_id: # Edit
            inbound_item = Inbound.query.get(inbound_id)
            if not inbound_item: return jsonify({'success': False, 'message': 'Không tìm thấy mục Inbound'}), 404
        else: # Add new
            inbound_item = Inbound()
            db.session.add(inbound_item)

        inbound_item.MANCC = data.get('MANCC')
        inbound_item.po = data.get('po')
        inbound_item.sku = data.get('sku')
        inbound_item.carton = int(data['carton']) if data.get('carton') else None
        inbound_item.contxe = data.get('contxe')
        inbound_item.datercv = _parse_date(data.get('datercv'))
        inbound_item.cbm = float(data['cbm']) if data.get('cbm') else None
        inbound_item.labour = data.get('labour')
        inbound_item.PackinglistNo = data.get('PackinglistNo')
            
        db.session.commit()
        return jsonify({'success': True, 'message': 'Lưu thành công'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/inbound/delete', methods=['POST'])
def delete_inbound():
    if 'user' not in session or session.get('role') != 'admin': return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    data = request.json
    try:
        inbound_item = Inbound.query.get(data.get('id'))
        if inbound_item:
            db.session.delete(inbound_item)
            db.session.commit()
        return jsonify({'success': True, 'message': 'Xóa thành công'})
    except Exception as e:
        db.session.rollback()
        if 'foreign key constraint' in str(e).lower():
            return jsonify({'success': False, 'message': 'Không thể xóa. Dữ liệu Inbound này đang được sử dụng ở mục khác (ví dụ: Phân công lao động).'}), 500
        return jsonify({'success': False, 'message': str(e)}), 500



# --- QUẢN LÝ PHÂN CÔNG LAO ĐỘNG (ADMIN ONLY) ---
@app.route('/assignment')
def assignment_page():
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('index'))
    return render_template('assignment.html')

@app.route('/api/labor_assignment/list')
def get_labor_assignment_list():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
       # Query tất cả các phân công và join với dữ liệu liên quan để tối ưu
        assignments_query = db.session.query(LaborAssignment).options(
            db.joinedload(LaborAssignment.inbound),
            db.joinedload(LaborAssignment.employee)
        ).order_by(LaborAssignment.work_date.desc(), LaborAssignment.start_time.desc()).all()

        # Nhóm các phân công lại theo một "ca làm việc" logic
        assignments = []
        
        grouped_assignments = {}
        for assign in assignments_query:
            # Ưu tiên group theo PackinglistNo nếu có, nếu không thì theo inbound_id
            grouping_identifier = assign.inbound_id # Can be None
            if assign.inbound and assign.inbound.PackinglistNo and assign.inbound.PackinglistNo.strip():
                grouping_identifier = assign.inbound.PackinglistNo

            group_key = (assign.work_date, assign.start_time, assign.end_time, grouping_identifier)
            
            if group_key not in grouped_assignments:

                grouped_assignments[group_key] = {
                    'id': assign.id,  # Dùng ID của record đầu tiên làm đại diện cho nhóm
                    'packinglist_no': assign.packinglist_no, # Trả về Packing List No riêng của assignment
                    'work_date': assign.work_date.strftime('%Y-%m-%d') if assign.work_date else None,
                    'start_time': assign.start_time.strftime('%H:%M') if assign.start_time else None, 
                    'end_time': assign.end_time.strftime('%H:%M') if assign.end_time else None,
                    'inbound_id': grouping_identifier, # Can be None
                    'inbound': {
                        'po': assign.inbound.po,
                        'contxe': assign.inbound.contxe,
                        'carton': 0,
                        'cbm': 0.0,
                        'PackinglistNo': assign.inbound.PackinglistNo  if assign.inbound else None
                    } if assign.inbound else None,
                    'employees': []
                    ,'_processed_inbounds': set() # Dùng để tránh cộng lặp
                }

            # Logic cộng dồn Carton/CBM cho nhóm
            group_item = grouped_assignments[group_key]
            if assign.inbound and assign.inbound.id not in group_item['_processed_inbounds']:
                group_item['inbound']['carton'] += (assign.inbound.carton or 0)
                group_item['inbound']['cbm'] += (assign.inbound.cbm or 0.0)
                group_item['_processed_inbounds'].add(assign.inbound.id)

            if assign.employee:
                # Tránh thêm nhân viên trùng lặp vào cùng một nhóm
                if not any(e['id'] == assign.employee.id for e in group_item['employees']):
                    group_item['employees'].append({
                        'id': assign.employee.id,
                        'hovaten': assign.employee.hovaten
                    })
        assignments = list(grouped_assignments.values())

        # Tính toán CBM trung bình cho mỗi nhóm
        for assignment in assignments:
            packinglist_no = assignment.get('packinglist_no') #Can be None
            if packinglist_no:
                # Lấy tổng CBM từ bảng Inbound cho packinglist_no này
                total_cbm = db.session.query(func.sum(Inbound.cbm)).filter_by(PackinglistNo=packinglist_no).scalar() or 0.0
                assignment['inbound']['cbm'] = float(total_cbm)
                assignment['inbound']['cbm'] = float(total_cbm) #type:ignore







        return jsonify({'success': True, 'assignments': assignments})


    except Exception as e:
        app.logger.error(f"Error in get_labor_assignment_list: {e}", exc_info=True)
        app.logger.error(f"Error in get_labor_assignment_list: {e}", exc_info=True) #type:ignore
        return jsonify({'success': False, 'message': str(e)}), 500





@app.route('/api/labor_assignment/save', methods=['POST'])
def save_labor_assignment():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.json
    assignment_id = data.get('id') # ID đại diện của nhóm
    employee_ids = data.get('employee_ids', [])

    if not employee_ids:
        return jsonify({'success': False, 'message': 'Vui lòng chọn ít nhất một nhân viên.'}), 400

    try:
        work_date_raw = data.get('work_date')
        start_time_raw = data.get('start_time')
        end_time_raw = data.get('end_time')

        work_date = _parse_date(work_date_raw)
        start_time = _parse_time(start_time_raw)
        end_time = _parse_time(end_time_raw)

        # --- VALIDATION: Kiểm tra ngày giờ bắt buộc và hợp lệ ---
        if not work_date:
            return jsonify({'success': False, 'message': f"Ngày làm việc là bắt buộc và phải có định dạng hợp lệ (ví dụ: YYYY-MM-DD). Giá trị nhận được: '{work_date_raw}'"}), 400
        if not start_time:
            return jsonify({'success': False, 'message': f"Thời gian bắt đầu là bắt buộc và phải có định dạng hợp lệ (ví dụ: HH:MM). Giá trị nhận được: '{start_time_raw}'"}), 400
        if end_time_raw and not end_time:
            return jsonify({'success': False, 'message': f"Định dạng thời gian kết thúc không hợp lệ. Giá trị nhận được: '{end_time_raw}'"}), 400

        # Kiểm tra logic thời gian
        if start_time and end_time:
            if start_time == end_time:
                return jsonify({'success': False, 'message': 'Thời gian bắt đầu và kết thúc không được trùng nhau.'}), 400
            if start_time > end_time:
                return jsonify({'success': False, 'message': 'Thời gian kết thúc phải lớn hơn thời gian bắt đầu.'}), 400

        inbound_id = data.get('inbound_id')
        if str(inbound_id).strip() == '': inbound_id = None

        # Xử lý Packing List No và Inbound ID
        packinglist_no = data.get('packinglist_no')
        
        # Nếu có Packing List nhưng chưa có inbound_id, tìm ID từ bảng Inbound
        if packinglist_no and not inbound_id:
            inbound_obj = Inbound.query.filter_by(PackinglistNo=packinglist_no).first()
            if inbound_obj:
                inbound_id = inbound_obj.id
        
        # Ngược lại: Nếu có inbound_id nhưng chưa có tên Packing List, lấy từ DB
        if inbound_id and not packinglist_no:
            inbound_item = Inbound.query.get(inbound_id)
            if inbound_item and hasattr(inbound_item, 'PackinglistNo'):
                packinglist_no = inbound_item.PackinglistNo

        if assignment_id: # Chế độ sửa: Xóa nhóm cũ đi
            original_assignment = LaborAssignment.query.get(assignment_id)
            if original_assignment:
                LaborAssignment.query.filter_by(
                    work_date=original_assignment.work_date, start_time=original_assignment.start_time,
                    end_time=original_assignment.end_time, inbound_id=original_assignment.inbound_id
                ).delete(synchronize_session=False)

        # Thêm mới (hoặc thêm lại cho chế độ sửa)
        for emp_id in employee_ids:
            new_assignment = LaborAssignment(
                work_date=work_date, 
                start_time=start_time, 
                end_time=end_time, 
                inbound_id=inbound_id, 
                employee_id=emp_id
            )
            new_assignment.packinglist_no = packinglist_no
            db.session.add(new_assignment)
        
        db.session.commit()
        return jsonify({'success': True, 'message': 'Lưu thành công'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/labor_assignment/delete', methods=['POST'])
def delete_labor_assignment():
    if 'user' not in session or session.get('role') != 'admin': return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    data = request.json
    try:
        assign_to_delete = LaborAssignment.query.get(data.get('id'))
        if assign_to_delete:
            LaborAssignment.query.filter_by(work_date=assign_to_delete.work_date, start_time=assign_to_delete.start_time, end_time=assign_to_delete.end_time, inbound_id=assign_to_delete.inbound_id).delete(synchronize_session=False)
            db.session.commit()
        return jsonify({'success': True, 'message': 'Xóa thành công'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/labor_assignments')
def labor_assignments_page():
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('index'))
    
    # Tải danh sách phân công, eager load 'employee' để tránh N+1 query
    labor_assignments = LaborAssignment.query.options(
        db.joinedload(LaborAssignment.employee)
    ).order_by(LaborAssignment.work_date.desc(), LaborAssignment.id.desc()).all()

    # Lấy danh sách packinglist_no duy nhất từ các phân công
    packing_list_nos = {la.packinglist_no for la in labor_assignments if la.packinglist_no}

    if packing_list_nos:
        # Lấy tổng CBM cho mỗi packing list
        total_cbm_q = db.session.query(
            Inbound.PackinglistNo, func.sum(Inbound.cbm)
        ).filter(Inbound.PackinglistNo.in_(packing_list_nos)).group_by(Inbound.PackinglistNo).all()
        total_cbm_map = {pl[0]: float(pl[1] or 0) for pl in total_cbm_q}

        # Lấy số lượng assignment cho mỗi packing list
        assign_count_q = db.session.query(
            LaborAssignment.packinglist_no, func.count(LaborAssignment.id)
        ).filter(LaborAssignment.packinglist_no.in_(packing_list_nos)).group_by(LaborAssignment.packinglist_no).all()
        assign_count_map = {pl[0]: pl[1] for pl in assign_count_q}

        # Tính toán và gán CBM cho mỗi assignment (không lưu vào DB)
        for assignment in labor_assignments:
            if assignment.packinglist_no in total_cbm_map:
                total_cbm = total_cbm_map.get(assignment.packinglist_no, 0)
                count = assign_count_map.get(assignment.packinglist_no, 1)
                assignment.cbm = total_cbm / count if count > 0 else 0
            else:
                # Gán giá trị cbm từ DB nếu không có packinglist_no để hiển thị
                assignment.cbm = assignment.cbm or 0

    employees = Employee.query.filter_by(active=1).order_by(Employee.hovaten).all()
    packing_lists = [r[0] for r in db.session.query(Inbound.PackinglistNo).filter(Inbound.PackinglistNo != '', Inbound.PackinglistNo != None).group_by(Inbound.PackinglistNo).order_by(func.max(Inbound.datercv).desc()).all()]
    
    # Tính tổng CBM cho từng Packing List
    cbm_data = db.session.query(Inbound.PackinglistNo, func.sum(Inbound.cbm)).filter(Inbound.PackinglistNo != '', Inbound.PackinglistNo != None).group_by(Inbound.PackinglistNo).all()
    cbm_map = {item[0]: (item[1] or 0) for item in cbm_data}
    return render_template('labor_assignments.html', labor_assignments=labor_assignments, employees=employees, packing_lists=packing_lists, cbm_map=cbm_map)


@app.route('/api/create_labor_assignment', methods=['POST'])
def create_labor_assignment():
    # Extract data from the request
    data = request.get_json()

    # Process the data and create a new labor assignment in the database
    # ...

    # Return a success response
    return jsonify({'success': True, 'message': 'Labor assignment created successfully'})








if False and __name__ == '__main__':
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


if __name__ == '__main__':
    print("--- Starting app and checking database connection... ---")
    with app.app_context():
        try:
            db.session.execute(text('SELECT 1'))
            print("Database connection OK.")
        except Exception as e:
            print("Database connection failed.")
            print(f"Error detail: {e}")

    port_str = os.environ.get("PORT", "5000")
    try:
        port = int(port_str)
    except ValueError:
        port = 5000
        print(f"Invalid PORT, using default {port}")

    app.run(host='0.0.0.0', port=port)
