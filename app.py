from flask import Flask, jsonify, render_template, request, redirect, url_for, session
from sqlalchemy import text, func
import os
from dotenv import load_dotenv
from datetime import datetime

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

    # [MỚI] Kiểm tra xem pallet đã bị khóa chưa (dựa vào finish='COMPLETED')
    is_locked = Scanfile.query.filter(
        Scanfile.jobno_type == job_type,
        Scanfile.pallet == pallet_no,
        Scanfile.finish == 'COMPLETED'
    ).first()
    
    if is_locked:
        return jsonify({'success': False, 'message': f'Pallet {pallet_no} đã bị khóa (Hoàn thành). Không thể thêm hàng.'}), 400

    # 1. Kiểm tra Barcode có tồn tại trong MasterData không
    # Logic: Lấy từ bên phải, bỏ qua 1 ký tự cuối (vị trí thứ 1 từ phải), lấy 5 ký tự trước đó
    refix_val = barcode[-6:-1]
    print(f"DEBUG: Barcode='{barcode}' -> Refix='{refix_val}'")
    master_item = MasterData.query.filter_by(refix=refix_val).first()
    
    if not master_item:
        return jsonify({'success': False, 'message': f'Refix {refix_val} (từ barcode {barcode}) không tồn tại trong hệ thống'}), 404

    try:
        # 2. Cập nhật (Edit) record có sẵn trong Scanfile thay vì tạo mới
        # Tìm bản ghi khớp SKU, Job Type và chưa được scan (pallet là null hoặc rỗng)
        scan_entry = Scanfile.query.filter(
            Scanfile.sku == master_item.sku,
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

        return jsonify({'success': True, 'message': 'Scan thành công', 'sku': master_item.sku})

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

@app.route('/stats')
def stats():
    if 'user' not in session: return redirect(url_for('login'))
    # Dữ liệu mẫu để tránh lỗi template khi chưa có logic thống kê
    dummy_total = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0}
    return render_template('statistics.html', stats={}, grand_total=dummy_total, remain_stats={})

@app.route('/print-label')
def print_label():
    if 'user' not in session: return redirect(url_for('login'))
    
    # Lấy danh sách Job Type
    job_types = []
    try:
        job_types = [r[0] for r in db.session.query(Scanfile.jobno_type).distinct().all() if r[0]]
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
        # Group by pallet and SKU to get counts
        results = db.session.query(
            Scanfile.pallet,
            Scanfile.pallet_type,
            Scanfile.sku,
            func.count(Scanfile.id).label('sscc_count'),
            func.max(Scanfile.tag_label).label('tag_label'), # Get tag_label if any
            func.max(Scanfile.jobscan).label('jobscan') # Get jobscan if any
        ).filter(
            Scanfile.jobno_type == job_type,
            Scanfile.pallet != '',
            Scanfile.pallet != None
        ).group_by(
            Scanfile.pallet,
            Scanfile.pallet_type,
            Scanfile.sku
        ).all()
        
        items = []
        for row in results:
            # Also get weight from masterdata
            master_item = MasterData.query.filter_by(sku=row.sku).first()
            sku_weight = master_item.weight if master_item and master_item.weight else 0

            items.append({
                'pallet_no': row.pallet,
                'pallet_type': row.pallet_type,
                'sku': row.sku,
                'qty': row.sscc_count, # This is now the count of cartons
                'tag_label': row.tag_label,
                'jobscan': row.jobscan,
                'sku_weight': sku_weight # Add weight here
            })
            
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
                'sscc': r.sscc,
                'barcode': r.barcode,
                'sku': r.sku,
                'qty': r.qty,
                'master_delivery': r.master_delivery,
                'ship_to': r.ship_to,
                'master_add1': r.master_add1,
                'master_add2': r.master_add2,
                'master_add3': r.master_add3,
                'master_add4': r.master_add4,
                'tag_label': r.tag_label,
                'master_st_company': r.master_st_company,
                'st_zip': r.st_zip,
                'master_ctl': r.master_ctl
            })
        return jsonify({'success': True, 'items': items})
    except Exception as e:
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
