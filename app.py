import os
import ssl
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, bindparam
from config import Config
app = Flask(__name__)
app.config.from_object(Config)
# Cấu hình Database
# Lấy URL từ biến môi trường DATABASE_URL, nếu không có thì dùng SQLite local để test
db_url = os.environ.get('DATABASE_URL', 'sqlite:///local.db')
# Fix lỗi tương thích: SQLAlchemy yêu cầu 'postgresql://', nhưng một số dịch vụ trả về 'postgres://'
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
# Tự động chuyển đổi mysql:// thành mysql+pymysql:// để sử dụng driver pymysql
if db_url and db_url.startswith("mysql://"):
    db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)
    # Fix lỗi: pymysql không hỗ trợ tham số 'ssl-mode' từ chuỗi kết nối Aiven
    if "ssl-mode=REQUIRED" in db_url:
        db_url = db_url.replace("?ssl-mode=REQUIRED", "").replace("&ssl-mode=REQUIRED", "")
        # Thêm cấu hình SSL thông qua connect_args
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            "connect_args": {
                "ssl": {
                    "check_hostname": False,
                    "verify_mode": ssl.CERT_NONE
                }
            }
        }

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.environ.get('SECRET_KEY', 'kln_secret_key_change_me') # Key bảo mật cho session
db = SQLAlchemy(app)



# --- DECORATOR KIỂM TRA ĐĂNG NHẬP ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- DECORATOR KIỂM TRA QUYỀN (ROLE) ---
def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'role' not in session or session['role'] not in allowed_roles:
                return "Bạn không có quyền truy cập trang này! <a href='/logout'>Đăng xuất</a>", 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        try:
            # Query bảng user (giả định cột: username, password, role)
            query = text("SELECT username, password, role FROM users WHERE username = :username")
            user = db.session.execute(query, {'username': username}).fetchone()
            
            if user and user[1] == password: # Lưu ý: Nên mã hóa mật khẩu trong thực tế
                session['user'] = user[0]
                session['role'] = user[2]
                
                if user[2] == 'scanner':
                    return redirect(url_for('scan_page'))
                elif user[2] == 'printer':
                    return redirect(url_for('print_label_page'))
                else: # admin
                    return redirect(url_for('home'))
            else:
                return render_template('login.html', error="Sai tên đăng nhập hoặc mật khẩu")
        except Exception as e:
            return render_template('login.html', error=f"Lỗi kết nối: {str(e)}")
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def home():
    # Điều hướng thông minh nếu không phải admin
    # Bỏ redirect scanner để họ có thể thấy menu chọn chức năng (Scan hoặc In Tem Nhỏ)
    # if session.get('role') == 'scanner':
    #    return redirect(url_for('scan_page'))
    if session.get('role') == 'printer':
        return redirect(url_for('print_label_page'))
    # Render trang home.html
    return render_template('home.html')


@app.route('/scan')
@login_required
@role_required(['admin', 'scanner'])
def scan_page():
    job_types = []
    try:
        # Lấy danh sách jobno_type từ bảng scanfile, loại bỏ giá trị trùng và null
        result = db.session.execute(text("SELECT DISTINCT jobno_type FROM scanfile WHERE jobno_type IS NOT NULL"))
        job_types = [row[0] for row in result]
    except Exception as e:
        print(f"Lỗi khi lấy jobno_type: {e}")
        # Fallback: Dùng danh sách mặc định nếu bảng chưa có hoặc lỗi kết nối
        job_types = ['Normal', 'Urgent', 'Sample']

    # Lọc danh sách Pallet: Loại bỏ các số pallet mà pallet = jobno trong database
    available_pallets = []
    try:
        # Lấy job mặc định (đầu tiên) để lọc pallet khả dụng ban đầu
        default_job = job_types[0] if job_types else ''

        # Tìm các pallet đang được sử dụng bởi Job HIỆN TẠI
        current_query = text("SELECT DISTINCT pallet FROM scanfile WHERE jobno_type = :job_type AND pallet IS NOT NULL AND pallet != ''")
        current_res = db.session.execute(current_query, {'job_type': default_job})
        current_set = {str(row[0]) for row in current_res}
        
        # Tạo danh sách 1-25, trừ những số bị loại
        available_pallets = []
        for i in range(1, 26):
            s_i = str(i)
            status = " (Đang dùng)" if s_i in current_set else " (Trống)"
            available_pallets.append({'no': i, 'label': f"{i}{status}"})
    except Exception as e:
        print(f"Lỗi lọc pallet: {e}")
        available_pallets = [{'no': i, 'label': str(i)} for i in range(1, 26)]

    # Render trang scan.html cho máy quét
    return render_template('scan.html', job_types=job_types, available_pallets=available_pallets)

@app.route('/api/job_stats', methods=['POST'])
def job_stats():
    data = request.get_json()
    job_type = data.get('job_type', '')
    
    try:
        total_query = text("SELECT COUNT(id) FROM scanfile WHERE jobno_type = :job_type")
        total_res = db.session.execute(total_query, {'job_type': job_type}).fetchone()
        total_sscc = total_res[0] if total_res else 0

        scanned_query = text("SELECT COUNT(id) FROM scanfile WHERE jobno_type = :job_type AND pallet !=''")
        scanned_res = db.session.execute(scanned_query, {'job_type': job_type}).fetchone()
        scanned_sscc = scanned_res[0] if scanned_res else 0

        remain_query = text("SELECT COUNT(id) FROM scanfile WHERE jobno_type = :job_type AND pallet=''")
        remain_res = db.session.execute(remain_query, {'job_type': job_type}).fetchone()
        remain_sscc = remain_res[0] if remain_res else 0
        
        return jsonify({'success': True, 'total_sscc': total_sscc, 'scanned_sscc': scanned_sscc, 'remain_sscc': remain_sscc})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/scan', methods=['POST'])
def process_scan():
    data = request.get_json()
    barcode = data.get('barcode', '')
    job_type = data.get('job_type', '')
    pallet_no = data.get('pallet_no', '')
    pallet_type = data.get('pallet_type', '')

    if not barcode or len(barcode) < 10:
        return jsonify({'success': False, 'message': 'Mã vạch không hợp lệ (cần >= 10 ký tự)'})

    # Logic: Cắt bên phải vị trí số 1 lấy 5 ký tự
    extracted_prefix = barcode[-6:-1]

    try:
        # 1. Tìm SKU trong masterdata dựa trên prefix (refix)
        # Giả sử tên cột trong DB là 'prefix'. Nếu tên là 'refix', hãy đổi lại ở đây.
        master_query = text("SELECT sku FROM masterdata WHERE refix = :refix")
        master_res = db.session.execute(master_query, {'refix': extracted_prefix}).fetchone()

        if not master_res:
            return jsonify({'success': False, 'message': f'Lỗi: Prefix {extracted_prefix} không có trong Masterdata'})
        
        sku = master_res[0]

        # 2. Kiểm tra trong scanfile: khớp SKU, Job Type và chưa có Pallet (NULL hoặc rỗng)
        scan_query = text("""
            SELECT id FROM scanfile 
            WHERE sku = :sku 
            AND jobno_type = :job_type 
            AND (pallet IS NULL OR pallet = '') 
            LIMIT 1
        """)
        scan_res = db.session.execute(scan_query, {'sku': sku, 'job_type': job_type}).fetchone()

        if scan_res:
            # 3. Tìm thấy -> Cập nhật Pallet (Ghi nhận quét thành công)
            row_id = scan_res[0]
            update_query = text("UPDATE scanfile SET pallet = :pallet, pallet_type = :pallet_type WHERE id = :id")
            db.session.execute(update_query, {'pallet': pallet_no, 'pallet_type': pallet_type, 'id': row_id})
            db.session.commit()

            # Đếm lại tổng số lượng trên Pallet này để hiển thị
            count_query = text("SELECT COUNT(id) FROM scanfile WHERE jobno_type = :job_type AND pallet = :pallet")
            count_res = db.session.execute(count_query, {'job_type': job_type, 'pallet': pallet_no}).fetchone()
            pallet_count = count_res[0] if count_res else 0
            
            # Lấy thống kê Job để cập nhật giao diện
            stats_query = text("""
                SELECT 
                    COUNT(id) as total,
                    COUNT(CASE WHEN pallet IS NOT NULL AND pallet != '' THEN 1 END) as scanned
                FROM scanfile 
                WHERE jobno_type = :job_type
            """)
            stats_res = db.session.execute(stats_query, {'job_type': job_type}).fetchone()
            total_sscc = stats_res[0] if stats_res else 0
            scanned_sscc = stats_res[1] if stats_res else 0
            remain_sscc = total_sscc - scanned_sscc

            return jsonify({'success': True, 'sku': sku, 'message': 'OK', 'pallet_count': pallet_count, 'total_sscc': total_sscc, 'scanned_sscc': scanned_sscc, 'remain_sscc': remain_sscc})
        else:
            return jsonify({'success': False, 'message': f'Lỗi: Không tìm thấy dữ liệu chờ cho SKU {sku} (Job: {job_type})'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Lỗi Server: {str(e)}'})

@app.route('/api/get_history', methods=['POST'])
def get_history():
    data = request.get_json()
    job_type = data.get('job_type', '')
    
    try:
        # Lấy danh sách SKU đã có pallet thuộc job_type, group by Pallet, SKU và count SSCC
        query = text("SELECT pallet, sku, COUNT(sscc) as qty FROM scanfile WHERE jobno_type = :job_type AND (pallet IS NOT NULL AND pallet != '') GROUP BY pallet, sku ORDER BY pallet DESC, sku ASC")
        result = db.session.execute(query, {'job_type': job_type})
        
        history = []
        for row in result:
            history.append({
                'pallet': row[0],
                'sku': row[1],
                'qty': row[2]
            })
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/delete_scan', methods=['POST'])
def delete_scan():
    data = request.get_json()
    job_type = data.get('job_type', '')
    pallet = data.get('pallet', '')
    sku = data.get('sku', '')
    quantity = data.get('quantity')

    try:
        # Nếu có số lượng cụ thể
        if quantity is not None and str(quantity).strip() != '':
            qty = int(quantity)
            if qty > 0:
                # Lấy danh sách ID cần xóa (giới hạn theo số lượng)
                select_query = text("SELECT id FROM scanfile WHERE jobno_type = :job_type AND pallet = :pallet AND sku = :sku LIMIT :limit")
                ids_res = db.session.execute(select_query, {'job_type': job_type, 'pallet': pallet, 'sku': sku, 'limit': qty})
                ids = [row[0] for row in ids_res]
                
                if not ids:
                    return jsonify({'success': False, 'message': 'Không tìm thấy dữ liệu để xóa'})

                # Xóa (update về null) các ID này
                update_query = text("UPDATE scanfile SET pallet = '', pallet_type = '' WHERE id IN :ids")
                update_query = update_query.bindparams(bindparam('ids', expanding=True))
                db.session.execute(update_query, {'ids': ids})
                db.session.commit()
                return jsonify({'success': True, 'message': f'Đã xóa {len(ids)} thùng.'})
        
        # Mặc định: Xóa hết nếu không nhập số lượng
        query = text("UPDATE scanfile SET pallet = NULL, pallet_type = NULL WHERE jobno_type = :job_type AND pallet = :pallet AND sku = :sku")
        db.session.execute(query, {'job_type': job_type, 'pallet': pallet, 'sku': sku})
        db.session.commit()
        return jsonify({'success': True, 'message': 'Đã xóa tất cả thùng của SKU này.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/check_barcode', methods=['POST'])
def check_barcode():
    data = request.get_json()
    barcode = data.get('barcode', '')
    job_type = data.get('job_type', '')

    if not barcode or len(barcode) < 10:
        return jsonify({'success': False, 'message': 'Mã vạch không hợp lệ'})

    # Logic: Cắt bên phải vị trí số 1 lấy 5 ký tự (giống process_scan)
    extracted_prefix = barcode[-6:-1]

    try:
        master_query = text("SELECT sku FROM masterdata WHERE refix = :refix")
        master_res = db.session.execute(master_query, {'refix': extracted_prefix}).fetchone()

        if not master_res:
            return jsonify({'success': False, 'message': f'Prefix {extracted_prefix} không có trong Masterdata'})
        
        sku = master_res[0]

        # Đếm số lượng khả dụng (chưa có pallet) của SKU này trong Job
        count_query = text("SELECT COUNT(id) FROM scanfile WHERE sku = :sku AND jobno_type = :job_type AND (pallet IS NULL OR pallet = '')")
        count_res = db.session.execute(count_query, {'sku': sku, 'job_type': job_type}).fetchone()
        count = count_res[0] if count_res else 0

        return jsonify({'success': True, 'sku': sku, 'count': count})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/bulk_update', methods=['POST'])
def bulk_update():
    data = request.get_json()
    sku = data.get('sku', '')
    job_type = data.get('job_type', '')
    pallet_no = data.get('pallet_no', '')
    pallet_type = data.get('pallet_type', '')
    quantity = data.get('quantity')

    try:
        count = 0
        # Nếu có số lượng cụ thể
        if quantity:
            qty = int(quantity)
            # Lấy danh sách ID cần update (giới hạn theo số lượng)
            select_query = text("SELECT id FROM scanfile WHERE sku = :sku AND jobno_type = :job_type AND (pallet IS NULL OR pallet = '') LIMIT :limit")
            ids_res = db.session.execute(select_query, {'sku': sku, 'job_type': job_type, 'limit': qty})
            ids = [row[0] for row in ids_res]
            
            if not ids:
                 return jsonify({'success': False, 'message': 'Không còn hàng khả dụng để cập nhật'})

            # Update các ID đã chọn
            update_query = text("UPDATE scanfile SET pallet = :pallet, pallet_type = :pallet_type WHERE id IN :ids")
            update_query = update_query.bindparams(bindparam('ids', expanding=True))
            db.session.execute(update_query, {'pallet': pallet_no, 'pallet_type': pallet_type, 'ids': ids})
            count = len(ids)
        else:
            # Cập nhật tất cả (Logic cũ)
            update_query = text("UPDATE scanfile SET pallet = :pallet, pallet_type = :pallet_type WHERE sku = :sku AND jobno_type = :job_type AND (pallet IS NULL OR pallet = '')")
            result = db.session.execute(update_query, {'pallet': pallet_no, 'pallet_type': pallet_type, 'sku': sku, 'job_type': job_type})
            count = result.rowcount

        db.session.commit()
        return jsonify({'success': True, 'message': f'Đã cập nhật {count} thùng SKU {sku} vào Pallet {pallet_no}.', 'count': count, 'sku': sku})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_pallets', methods=['GET'])
def get_pallets():
    job_type = request.args.get('job_type', '')
    try:
        # Tìm các pallet đang được sử dụng bởi Job HIỆN TẠI
        current_query = text("SELECT DISTINCT pallet FROM scanfile WHERE jobno_type = :job_type AND pallet IS NOT NULL AND pallet != ''")
        current_res = db.session.execute(current_query, {'job_type': job_type})
        current_set = {str(row[0]) for row in current_res}
        
        # Tạo danh sách 1-25, trừ những số đã bị sử dụng
        available_pallets = []
        for i in range(1, 26):
            s_i = str(i)
            status = " (Đang dùng)" if s_i in current_set else " (Trống)"
            available_pallets.append({'no': i, 'label': f"{i}{status}"})
        return jsonify({'success': True, 'pallets': available_pallets})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/pallet_details', methods=['POST'])
def pallet_details():
    data = request.get_json()
    job_type = data.get('job_type', '')
    pallet_no = data.get('pallet_no', '')

    try:
        # Đếm tổng số lượng trên Pallet này
        count_query = text("SELECT COUNT(id) FROM scanfile WHERE jobno_type = :job_type AND pallet = :pallet")
        count_res = db.session.execute(count_query, {'job_type': job_type, 'pallet': pallet_no}).fetchone()
        pallet_count = count_res[0] if count_res else 0

        # Lấy danh sách SKU trên Pallet này
        sku_query = text("SELECT sku, COUNT(sscc) as qty FROM scanfile WHERE jobno_type = :job_type AND pallet = :pallet GROUP BY sku")
        sku_res = db.session.execute(sku_query, {'job_type': job_type, 'pallet': pallet_no})
        
        skus = [{'sku': row[0], 'qty': row[1]} for row in sku_res]

        return jsonify({'success': True, 'pallet_count': pallet_count, 'skus': skus})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/sku_details', methods=['POST'])
def sku_details():
    data = request.get_json()
    job_type = data.get('job_type', '')
    sku = data.get('sku', '')

    try:
        # Lấy danh sách các pallet chứa SKU này trong job hiện tại
        query = text("SELECT pallet, COUNT(sscc) as qty FROM scanfile WHERE jobno_type = :job_type AND sku = :sku AND (pallet IS NOT NULL AND pallet != '') GROUP BY pallet ORDER BY pallet")
        result = db.session.execute(query, {'job_type': job_type, 'sku': sku})
        
        details = [{'pallet': row[0], 'qty': row[1]} for row in result]
        return jsonify({'success': True, 'sku': sku, 'details': details})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/add-product')
@login_required
@role_required(['admin', 'printer'])
def manual_label_page():
    # Lấy danh sách jobno_type
    job_types = []
    try:
        result = db.session.execute(text("SELECT DISTINCT jobno_type FROM scanfile WHERE jobno_type IS NOT NULL"))
        job_types = [row[0] for row in result]
    except:
        job_types = ['Normal', 'Urgent', 'Sample']

    # Lấy danh sách Pallet khả dụng
    available_pallets = []
    try:
        exclude_query = text("SELECT DISTINCT pallet FROM scanfile WHERE jobno_type IS NOT NULL AND pallet IS NOT NULL AND pallet != ''")
        exclude_res = db.session.execute(exclude_query)
        excluded_set = {str(row[0]) for row in exclude_res}
        available_pallets = [i for i in range(1, 26) if str(i) not in excluded_set]
    except:
        available_pallets = list(range(1, 26))

    return render_template('manual_label.html', job_types=job_types, available_pallets=available_pallets)

@app.route('/api/get_job_skus', methods=['POST'])
def get_job_skus():
    data = request.get_json()
    job_type = data.get('job_type', '')
    try:
        # Lấy danh sách SKU có item chưa gán pallet (khả dụng) trong Job
        query = text("SELECT DISTINCT sku FROM scanfile WHERE jobno_type = :job_type AND (pallet IS NULL OR pallet = '')")
        result = db.session.execute(query, {'job_type': job_type})
        skus = [row[0] for row in result]
        return jsonify({'success': True, 'skus': skus})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_sku_availability', methods=['POST'])
def get_sku_availability():
    data = request.get_json()
    job_type = data.get('job_type', '')
    sku = data.get('sku', '')
    try:
        query = text("SELECT COUNT(id) FROM scanfile WHERE jobno_type = :job_type AND sku = :sku AND (pallet IS NULL OR pallet = '')")
        res = db.session.execute(query, {'job_type': job_type, 'sku': sku}).fetchone()
        count = res[0] if res else 0
        return jsonify({'success': True, 'count': count})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_remain_skus', methods=['POST'])
def get_remain_skus():
    data = request.get_json()
    job_type = data.get('job_type', '')
    try:
        # Lấy danh sách SKU và số lượng chưa scan (pallet null hoặc rỗng)
        query = text("SELECT sku, COUNT(id) as qty FROM scanfile WHERE jobno_type = :job_type AND (pallet IS NULL OR pallet = '') GROUP BY sku ORDER BY sku")
        result = db.session.execute(query, {'job_type': job_type})
        
        items = [{'sku': row[0], 'qty': row[1]} for row in result]
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/manual_update', methods=['POST'])
def manual_update():
    data = request.get_json()
    job_type = data.get('job_type')
    sku = data.get('sku')
    pallet_type = data.get('pallet_type')
    pallet_no = data.get('pallet_no')
    quantity = int(data.get('quantity', 0))

    if quantity <= 0:
        return jsonify({'success': False, 'message': 'Số lượng phải lớn hơn 0'})

    try:
        # Lấy danh sách ID cần update (giới hạn theo số lượng yêu cầu)
        select_ids_query = text("""
            SELECT id FROM scanfile 
            WHERE jobno_type = :job_type AND sku = :sku AND (pallet IS NULL OR pallet = '') 
            LIMIT :limit
        """)
        ids_res = db.session.execute(select_ids_query, {'job_type': job_type, 'sku': sku, 'limit': quantity})
        ids = [row[0] for row in ids_res]

        if not ids or len(ids) < quantity:
             return jsonify({'success': False, 'message': f'Không đủ số lượng khả dụng (Tìm thấy {len(ids)})'})

        # Cập nhật pallet, pallet_type và thời gian
        # Sử dụng bindparam với expanding=True để xử lý danh sách ID an toàn cho mệnh đề IN
        update_query = text("UPDATE scanfile SET pallet = :pallet, pallet_type = :pallet_type, time_scan = :time_scan WHERE id IN :ids")
        update_query = update_query.bindparams(bindparam('ids', expanding=True))
        db.session.execute(update_query, {'pallet': pallet_no, 'pallet_type': pallet_type, 'time_scan': datetime.now(), 'ids': list(ids)})
        db.session.commit()

        return jsonify({'success': True, 'message': f'Đã cập nhật {len(ids)} thùng vào Pallet {pallet_no}'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/print-label')
@login_required
@role_required(['admin', 'printer'])
def print_label_page():
    # Lấy danh sách jobno_type để hiển thị dropdown
    job_types = []
    try:
        result = db.session.execute(text("SELECT DISTINCT jobno_type FROM scanfile WHERE jobno_type IS NOT NULL"))
        job_types = [row[0] for row in result]
    except:
        job_types = ['Normal', 'Urgent', 'Sample']
    
    return render_template('print_label.html', job_types=job_types)

@app.route('/api/get_print_data', methods=['POST'])
def get_print_data():
    data = request.get_json()
    job_type = data.get('job_type', '')

    try:
        # Query lấy dữ liệu: Group theo Pallet và SKU để tính tổng số lượng
        # Giả định bảng masterdata có cột 'weight' (số kg/thùng)
        # Giả định bảng scanfile có cột 'tag_label' (ghi chú tem)
        query = text("""
            SELECT 
                s.pallet, 
                s.pallet_type, 
                s.sku, 
                COUNT(s.id) as qty, 
                MAX(s.tag_label) as tag_label,
                MAX(m.weight) as sku_weight,
                s.jobscan
                            
            FROM scanfile s
            LEFT JOIN masterdata m ON s.sku = m.sku
            WHERE s.jobno_type = :job_type 
              AND s.pallet IS NOT NULL 
              AND s.pallet != ''
            GROUP BY s.pallet, s.pallet_type, s.sku,s.jobscan
            ORDER BY s.pallet, s.sku
        """)
        
        result = db.session.execute(query, {'job_type': job_type})
        
        items = []
        for row in result:
            items.append({
                'pallet_no': row[0],
                'pallet_type': row[1],
                'sku': row[2],
                'qty': row[3],
                # Lấy tag_label, nếu null thì trả về chuỗi rỗng
                'tag_label': 'Tem nhỏ' if row[4] else '',
                # Lấy weight, nếu null (không tìm thấy trong masterdata) thì trả về 0
                'sku_weight': float(row[5]) if row[5] is not None else 0,
                # Lấy pallet_type, nếu null (không tìm thấy trong masterdata) thì trả về '
                'jobscan':row[6]
                
            })

        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_sscc_data', methods=['POST'])
def get_sscc_data():
    data = request.get_json()
    job_type = data.get('job_type', '')
    pallet_no = data.get('pallet_no', '')

    try:
        # Lấy tất cả thông tin từ bảng scanfile cho job và pallet này
        query = text("SELECT * FROM scanfile WHERE jobno_type = :job_type AND pallet = :pallet_no")
        result = db.session.execute(query, {'job_type': job_type, 'pallet_no': pallet_no})
        
        items = []
        keys = result.keys() # Lấy danh sách tên cột
        
        for row in result:
            item = {}
            for key, val in zip(keys, row):
                # Xử lý định dạng ngày tháng nếu có để tránh lỗi JSON
                if isinstance(val, datetime):
                    item[key] = val.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    item[key] = val
            items.append(item)

        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/update_jobscan', methods=['POST'])
def update_jobscan():
    data = request.get_json()
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')
    jobscan = data.get('jobscan')

    try:
        # Cập nhật jobscan cho các record thuộc job_type và pallet này mà chưa có jobscan (NULL hoặc rỗng)
        query = text("UPDATE scanfile SET jobscan = :jobscan WHERE jobno_type = :job_type AND pallet = :pallet_no AND (jobscan IS NULL OR jobscan = '')")
        db.session.execute(query, {'jobscan': jobscan, 'job_type': job_type, 'pallet_no': pallet_no})
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/users')
@login_required
@role_required(['admin'])
def users_page():
    return render_template('users.html')

@app.route('/api/users/list', methods=['GET'])
@login_required
@role_required(['admin'])
def get_users():
    try:
        query = text("SELECT id, username, role FROM users ORDER BY id")
        result = db.session.execute(query)
        users = [{'id': row[0], 'username': row[1], 'role': row[2]} for row in result]
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/users/save', methods=['POST'])
@login_required
@role_required(['admin'])
def save_user():
    data = request.get_json()
    user_id = data.get('id')
    username = data.get('username')
    password = data.get('password')
    role = data.get('role')

    if not username or not role:
        return jsonify({'success': False, 'message': 'Thiếu thông tin bắt buộc'})

    try:
        if user_id: # Update
            if password:
                query = text("UPDATE users SET username = :u, password = :p, role = :r WHERE id = :id")
                db.session.execute(query, {'u': username, 'p': password, 'r': role, 'id': user_id})
            else:
                query = text("UPDATE users SET username = :u, role = :r WHERE id = :id")
                db.session.execute(query, {'u': username, 'r': role, 'id': user_id})
        else: # Insert
            if not password:
                return jsonify({'success': False, 'message': 'Mật khẩu là bắt buộc khi tạo mới'})
            # Check exist
            check = db.session.execute(text("SELECT id FROM users WHERE username = :u"), {'u': username}).fetchone()
            if check:
                return jsonify({'success': False, 'message': 'Tên đăng nhập đã tồn tại'})
            
            query = text("INSERT INTO users (username, password, role) VALUES (:u, :p, :r)")
            db.session.execute(query, {'u': username, 'p': password, 'r': role})
        
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/users/delete', methods=['POST'])
@login_required
@role_required(['admin'])
def delete_user():
    data = request.get_json()
    user_id = data.get('id')
    try:
        db.session.execute(text("DELETE FROM users WHERE id = :id"), {'id': user_id})
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

# --- API LOGGING & NOTIFICATION ---

@app.route('/api/finish_pallet', methods=['POST'])
@login_required
def finish_pallet():
    data = request.get_json()
    job_type = data.get('job_type')
    pallet_no = data.get('pallet_no')
    pallet_type = data.get('pallet_type')
    user = session.get('user', 'Unknown')

    try:
        # Đếm số lượng để ghi vào log
        count_query = text("SELECT COUNT(id) FROM scanfile WHERE jobno_type = :job_type AND pallet = :pallet")
        count_res = db.session.execute(count_query, {'job_type': job_type, 'pallet': pallet_no}).fetchone()
        qty = count_res[0] if count_res else 0

        message = f"Pallet {pallet_no} ({pallet_type}) - Job {job_type} đã hoàn thành. SL: {qty} thùng."
        
        # Ghi vào bảng logs
        log_query = text("INSERT INTO logs (username, action, message, created_at, is_read) VALUES (:u, 'FINISH_PALLET', :m, :t, 0)")
        db.session.execute(log_query, {'u': user, 'm': message, 't': datetime.now()})
        db.session.commit()

        return jsonify({'success': True, 'message': 'Đã gửi thông báo cho bộ phận in!'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_logs', methods=['GET'])
def get_logs():
    try:
        # Lấy 10 log mới nhất
        query = text("SELECT id, username, action, message, created_at, is_read FROM logs ORDER BY id DESC LIMIT 10")
        result = db.session.execute(query)
        logs = [{'id': r[0], 'username': r[1], 'action': r[2], 'message': r[3], 'created_at': str(r[4]), 'is_read': bool(r[5])} for r in result]
        return jsonify({'success': True, 'logs': logs})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/mark_read', methods=['POST'])
def mark_read():
    data = request.get_json()
    log_id = data.get('id')
    try:
        db.session.execute(text("UPDATE logs SET is_read = 1 WHERE id = :id"), {'id': log_id})
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/print-small-label')
@login_required
@role_required(['admin', 'printer', 'scanner'])
def print_small_label_page():
    # Lấy danh sách jobno_type
    job_types = []
    try:
        result = db.session.execute(text("SELECT DISTINCT jobno_type FROM scanfile WHERE jobno_type IS NOT NULL"))
        job_types = [row[0] for row in result]
    except:
        job_types = ['Normal', 'Urgent', 'Sample']
    return render_template('print_small_label.html', job_types=job_types)

@app.route('/api/get_small_label_data', methods=['POST'])
def get_small_label_data():
    data = request.get_json()
    job_type = data.get('job_type', '')
    try:
        # Lấy danh sách các item có tag_label, group theo Pallet, SKU và Tag
        query = text("""
            SELECT 
                s.pallet, 
                s.sku, 
                s.tag_label,
                COUNT(s.id) as qty,
                s.jobscan
            FROM scanfile s
            WHERE s.jobno_type = :job_type 
              AND s.tag_label IS NOT NULL 
              AND s.tag_label != ''
            GROUP BY s.pallet, s.sku, s.tag_label, s.jobscan
            ORDER BY s.pallet, s.sku
        """)
        result = db.session.execute(query, {'job_type': job_type})
        items = [{'pallet': row[0], 'sku': row[1], 'tag_label': row[2], 'qty': row[3], 'jobscan': row[4]} for row in result]
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/stats')
@login_required
def stats_page():
    try:
        # Query: Đếm số lượng pallet duy nhất (DISTINCT pallet) theo từng loại và job
        # Chỉ lấy những dòng đã có pallet (không null, không rỗng)
        query = text("""
            SELECT jobno,jobno_type, pallet_type, COUNT(DISTINCT pallet), COUNT(id)
            FROM scanfile
            WHERE pallet IS NOT NULL AND pallet != ''
            GROUP BY jobno, pallet_type,jobno_type
            ORDER BY jobno
        """)
        result = db.session.execute(query)
        
        stats = {}
        for row in result:
            job_no = row[0]
            job_type = row[1]
            p_type = row[2]
            pallet_count = row[3]
            sscc_count = row[4]
            
            if not job_no: continue
            
            # Sử dụng khóa là tuple (Job No, Job Type) để nhóm
            key = (job_no, job_type)
            
            if key not in stats:
                stats[key] = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0}
            
            # Nếu là loose (Loose Carton) thì đếm số SSCC (thùng), ngược lại đếm số Pallet
            if p_type == 'loose' or p_type == 'loosecarton':
                count = sscc_count
            else:
                count = pallet_count

            if p_type in stats[key]:
                stats[key][p_type] += count
                stats[key]['total'] += count

        # Tính tổng cộng (Grand Total) cho hàng đầu trang
        grand_total = {'1.2': 0, '1.6': 0, '1.9': 0, 'loose': 0, 'total': 0}
        for s in stats.values():
            grand_total['1.2'] += s['1.2']
            grand_total['1.6'] += s['1.6']
            grand_total['1.9'] += s['1.9']
            grand_total['loose'] += s['loose']
            grand_total['total'] += s['total']

        # Thống kê hàng chưa scan (Tồn) theo jobno_type
        remain_query = text("""
            SELECT jobno, jobno_type, COUNT(id)
            FROM scanfile
            WHERE pallet IS NULL OR pallet = ''
            GROUP BY jobno, jobno_type
            ORDER BY jobno
        """)
        remain_result = db.session.execute(remain_query)
        # Lưu key là tuple (jobno, jobno_type)
        remain_stats = {(row[0], row[1]): row[2] for row in remain_result}

        return render_template('statistics.html', stats=stats, remain_stats=remain_stats, grand_total=grand_total)
    except Exception as e:
        return f"Lỗi: {str(e)}"

if __name__ == '__main__':
    app.run(debug=True)
