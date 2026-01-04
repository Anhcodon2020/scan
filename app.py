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

    # Tạo danh sách Pallet cố định từ 1 đến 25
    pallets = [{'no': i, 'label': f"{i}"} for i in range(1, 26)]
    
    return render_template('scan.html', job_types=job_types, available_pallets=pallets)

@app.route('/api/get_pallets')
def get_pallets():
    # API trả về danh sách pallet 1-25 cho frontend (scan.html gọi hàm này)
    pallets = [{'no': i, 'label': f"{i}"} for i in range(1, 26)]
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

@app.route('/stats')
def stats():
    if 'user' not in session: return redirect(url_for('login'))
    # Dữ liệu mẫu để tránh lỗi template khi chưa có logic thống kê
    dummy_total = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0}
    return render_template('statistics.html', stats={}, grand_total=dummy_total, remain_stats={})

@app.route('/print-label')
def print_label():
    if 'user' not in session: return redirect(url_for('login'))
    return "<h3>Chức năng In Tem Pallet đang phát triển</h3><a href='/'>Quay lại</a>"

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
