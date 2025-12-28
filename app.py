import os
import ssl
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

app = Flask(__name__)

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
db = SQLAlchemy(app)

@app.route('/')
def home():
    # Render trang home.html
    return render_template('home.html')


@app.route('/scan')
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
        # Tìm các pallet cần loại bỏ (điều kiện: jobno_type có dữ liệu và pallet không null)
        exclude_query = text("SELECT DISTINCT pallet FROM scanfile WHERE jobno_type IS NOT NULL AND pallet IS NOT NULL")
        exclude_res = db.session.execute(exclude_query)
        excluded_set = {row[0] for row in exclude_res}
        
        # Tạo danh sách 1-25, trừ những số bị loại
        available_pallets = [i for i in range(1, 26) if str(i) not in excluded_set]
    except Exception as e:
        print(f"Lỗi lọc pallet: {e}")
        available_pallets = list(range(1, 26))

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

    if not barcode or len(barcode) < 14:
        return jsonify({'success': False, 'message': 'Mã vạch không hợp lệ (cần >= 14 ký tự)'})

    # Logic: Cắt từ vị trí số 9 (index 8), lấy 5 ký tự
    extracted_prefix = barcode[8:13]

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
            total_query = text("SELECT COUNT(sscc) FROM scanfile WHERE jobno_type = :job_type")
            total_res = db.session.execute(total_query, {'job_type': job_type}).fetchone()
            total_sscc = total_res[0] if total_res else 0

            scanned_query = text("SELECT COUNT(sscc) as qty FROM scanfile WHERE jobno_type = :job_type AND pallet !=''")
            scanned_res = db.session.execute(scanned_query, {'job_type': job_type}).fetchone()
            scanned_sscc = scanned_res[0] if scanned_res else 0

            remain_query = text("SELECT COUNT(sscc) FROM scanfile WHERE jobno_type = :job_type AND pallet IS NULL")
            remain_res = db.session.execute(remain_query, {'job_type': job_type}).fetchone()
            remain_sscc = remain_res[0] if remain_res else 0

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

    try:
        # Xóa pallet (set null) cho các item thuộc job, pallet và sku này
        query = text("UPDATE scanfile SET pallet = NULL, pallet_type = NULL WHERE jobno_type = :job_type AND pallet = :pallet AND sku = :sku")
        db.session.execute(query, {'job_type': job_type, 'pallet': pallet, 'sku': sku})
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_pallets', methods=['GET'])
def get_pallets():
    try:
        # Tìm các pallet đã được sử dụng (có jobno_type và pallet không rỗng)
        exclude_query = text("SELECT DISTINCT pallet FROM scanfile WHERE jobno_type IS NOT NULL AND pallet IS NOT NULL AND pallet != ''")
        exclude_res = db.session.execute(exclude_query)
        excluded_set = {str(row[0]) for row in exclude_res}
        
        # Tạo danh sách 1-25, trừ những số đã bị sử dụng
        available_pallets = [i for i in range(1, 26) if str(i) not in excluded_set]
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
        update_query = text("UPDATE scanfile SET pallet = :pallet, pallet_type = :pallet_type, time_scan = :time_scan WHERE id IN :ids")
        db.session.execute(update_query, {'pallet': pallet_no, 'pallet_type': pallet_type, 'time_scan': datetime.now(), 'ids': tuple(ids)})
        db.session.commit()

        return jsonify({'success': True, 'message': f'Đã cập nhật {len(ids)} thùng vào Pallet {pallet_no}'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)})

@app.route('/print-label')
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
                MAX(m.weight) as sku_weight
                            
            FROM scanfile s
            LEFT JOIN masterdata m ON s.sku = m.sku
            WHERE s.jobno_type = :job_type 
              AND s.pallet IS NOT NULL 
              AND s.pallet != ''
            GROUP BY s.pallet, s.pallet_type, s.sku
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
                'sku_weight': float(row[5]) if row[5] is not None else 0
                # Lấy pallet_type, nếu null (không tìm thấy trong masterdata) thì trả về '
                
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

if __name__ == '__main__':
    app.run(debug=True)
