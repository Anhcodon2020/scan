import os
from datetime import timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "kln_scan_secret_key_2024")
    
    uri = os.getenv("DATABASE_URL", "sqlite:///site.db")
    # Fix lỗi phương ngữ cho PostgreSQL (nếu Aiven trả về postgres:// thay vì postgresql://)
    if uri and uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    # Fix lỗi phương ngữ cho MySQL (sử dụng pymysql driver)
    if uri and uri.startswith("mysql://"):
        uri = uri.replace("mysql://", "mysql+pymysql://", 1)
    
    # Loại bỏ tham số ssl-mode/ssl_mode khỏi URL vì PyMySQL không hỗ trợ qua query string
    # (Chúng ta sẽ cấu hình SSL qua connect_args bên dưới)
    if uri and ("ssl-mode" in uri or "ssl_mode" in uri):
        parsed = urlparse(uri)
        qs = parse_qs(parsed.query)
        qs.pop('ssl-mode', None)
        qs.pop('ssl_mode', None)
        new_query = urlencode(qs, doseq=True)
        parsed = parsed._replace(query=new_query)
        uri = urlunparse(parsed)
    
    SQLALCHEMY_DATABASE_URI = uri
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)

    # Cấu hình SSL và Pool cho Aiven Database
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {
            "ssl": {
                # PyMySQL dùng 'cafile' thay vì 'ca'. Mặc định create_default_context đã bật check_hostname.
                "cafile": os.path.join(os.path.abspath(os.path.dirname(__file__)), "ca.pem"),
            }
        }
    }