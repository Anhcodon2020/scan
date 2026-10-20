import os
from datetime import timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "kln_scan_secret_key_2024")
    
    uri = os.getenv("DATABASE_URL", "sqlite:///site.db")
    if not os.getenv("DATABASE_URL") and os.getenv("DB_HOST"):
        uri = URL.create(
            "mysql+pymysql",
            username=os.getenv("DB_USER") or None,
            password=os.getenv("DB_PASSWORD") or None,
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", "3306")),
            database=os.getenv("DB_NAME", "wms_db"),
        ).render_as_string(hide_password=False)

    ssl_required = os.getenv("DB_SSL", "").lower() in {"1", "true", "yes", "required"}
    if uri and ("ssl-mode" in uri or "ssl_mode" in uri):
        parsed = urlparse(uri)
        qs = parse_qs(parsed.query)
        ssl_required = ssl_required or any(
            value.lower() in {"required", "verify-ca", "verify-identity"}
            for key in ("ssl-mode", "ssl_mode")
            for value in qs.get(key, [])
        )

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

    # Chỉ cấu hình SSL khi database MySQL được yêu cầu rõ ràng.
    if uri and uri.startswith("mysql+pymysql://"):
        SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_pre_ping": True,
            "pool_recycle": 300,
        }
        if ssl_required:
            SQLALCHEMY_ENGINE_OPTIONS["connect_args"] = {
                "ssl": {
                    "cafile": os.path.join(os.path.abspath(os.path.dirname(__file__)), "ca.pem"),
                }
            }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_pre_ping": True,
            "pool_recycle": 300,
        }
